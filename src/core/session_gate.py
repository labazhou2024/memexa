"""
Session Gate — Programmatic enforcement of workflow mandatory requirements.

v6.0 (CC-Native):
  Simplified from 3-layer to 2-layer architecture.
  Pipeline state tracking removed (replaced by CC Task system).
  Kimi review removed (replaced by code-reviewer agent).
  Post-edit tracker removed (replaced by CC PostToolUse hook).
  Big-loop enforcement removed (CLAUDE.md declarative rule).

Enforces:
  - Phase C: local review on staged .py files (zero-cost AST checks)
  - Phase D.7a: pytest verification before commit
  - Phase D.8: memory integrity + harness freshness check
  - Release-gate trigger: auto-queue after N commits
  - Session-end audit: uncommitted changes + persistence

Called by:
  1. PreToolUse hook on `git commit` → commit-gate (blocks on failures)
  2. Stop hook → session-end (audits persistence, outputs warnings)
  3. CLI: python -m src.core.session_gate [commit-gate|session-end|check]

Exit codes:
  0 = PASS
  1 = FAIL (commit blocked / issues found)
"""


import json
import logging
import os
import sys
from pathlib import Path

# Script-mode safety: when invoked as `python memexa/core/session_gate.py ...`
# (e.g. by git pre-commit hook), the `memexa` package is not on sys.path → all
# inner `from src.core.X import Y` calls fail silently (caught by fail-soft
# except blocks) → trace_sink writes go to a void → Layer A skip-trace
# observability appears to work but never lands events. LIVE-fire diagnosis
# 2026-04-25 commit cbf34f6: gate_skipped events were 0 despite [GATE COVERAGE]
# 5/5 skipped printed. NOT cargo-cult — this file is dual-invocation by design.
_pkg_root = Path(__file__).resolve().parents[2]
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from src.core._path_resolver import memory_dir

logger = logging.getLogger(__name__)

def _find_workspace() -> Path:
    """Resolve workspace root robustly (Windows CJK path safe)."""
    marker = Path(".claude") / "config" / "settings.json"
    try:
        cwd = Path(os.getcwd())
        if (cwd / marker).exists():
            return cwd
    except OSError:
        pass
    try:
        candidate = Path(__file__).parent.parent.parent.parent
        if (candidate / marker).exists():
            return candidate
    except OSError:
        pass
    return Path(os.getcwd())


_WORKSPACE = _find_workspace()
_MEMEXA_ROOT = _WORKSPACE / "memexa"

# [Env var override for test isolation] Respect MEMEXA_DATA_DIR / MEMEXA_HARNESS_FILE
# Matches _hook_utils.py behavior. Production behavior unchanged when env unset.
_env_data = os.environ.get("MEMEXA_DATA_DIR")
if _env_data and Path(_env_data).is_dir():
    _DATA = Path(_env_data)
else:
    _DATA = _MEMEXA_ROOT / "memexa" / "data"

_env_harness = os.environ.get("MEMEXA_HARNESS_FILE")
if _env_harness:
    _HARNESS = Path(_env_harness)
else:
    _HARNESS = _WORKSPACE / ".claude" / "config" / "harness_state.json"
_MEMORY_DIR = memory_dir()
_MEMORY_INDEX = _MEMORY_DIR / "MEMORY.md"

# Thresholds
RELEASE_GATE_COMMIT_THRESHOLD = 5


# ================================================================
# Memory & Harness checks
# ================================================================

def check_memory_integrity() -> Tuple[bool, List[str]]:
    """Check MEMORY.md index references match actual files."""
    issues = []
    if not _MEMORY_INDEX.exists():
        return False, ["MEMORY.md not found"]

    index_text = _MEMORY_INDEX.read_text(encoding="utf-8")
    referenced = set(re.findall(r'\(([a-zA-Z0-9_]+\.md)\)', index_text))

    for ref in referenced:
        if not (_MEMORY_DIR / ref).exists():
            issues.append(f"INDEX->MISSING: {ref} in MEMORY.md but file absent")

    if _MEMORY_DIR.exists():
        actual = {f.name for f in _MEMORY_DIR.glob("*.md") if f.name != "MEMORY.md"}
        for orphan in actual - referenced:
            issues.append(f"ORPHAN: {orphan} not indexed in MEMORY.md")

    return len(issues) == 0, issues


def check_harness_freshness() -> Tuple[bool, List[str]]:
    """Check harness_state.json is up to date."""
    issues = []
    if not _HARNESS.exists():
        return False, ["harness_state.json not found"]
    try:
        harness = json.loads(_HARNESS.read_text(encoding="utf-8"))
    except Exception as e:
        return False, [f"harness parse error: {e}"]

    # allow-silent: fail-soft observability path
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(_MEMEXA_ROOT), timeout=5,
        )
        current = result.stdout.strip()
        recorded = harness.get("git_repos", {}).get("memexa", {}).get("last_commit", "")
        if current and recorded and current != recorded:
            issues.append(f"HARNESS_STALE: HEAD={current} harness={recorded}")
    # allow-silent: observability fail-soft
    except Exception:
        pass

    return len(issues) == 0, issues


# ================================================================
# pytest verification
# ================================================================

def _write_test_result(passed: bool, source: str, summary: str = "") -> None:
    """[Feedback loop Gate 3] Write last_session_test_result.json so
    hook_session_end._credit_helpful_patterns can read tests_passed.

    Before this was wired, helpful_count could never be credited (昨天诊断:
    0/52 因为这个文件从未被写入). Called on every pytest run in commit gate.
    """
    # allow-silent: fail-soft observability path
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        result_file = _DATA / "last_session_test_result.json"
        result_file.write_text(json.dumps({
            "tests_passed": passed,
            "source": source,
            "summary": summary[:200],
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }, ensure_ascii=False), encoding="utf-8")
    # allow-silent: observability fail-soft
    except Exception:
        pass  # Non-blocking — don't let feedback loop break commit


def _compute_pytest_targets(staged_files: Optional[List[str]] = None) -> List[str]:
    """v2.0 §3.1 fix (autopilot_v20_spec_fixes TU-2): targeted pytest set.

    Returns list of pytest paths derived from staged .py files. Heuristic:
      - tests/<f>.py → include verbatim
      - memexa/core/<X>.py → glob tests/test_<X>*.py (stem glob-escaped per
        security-iter1-1: prevents path-named like `[evil].py` matching all)
      - any other .py → ignored (no test mapping)
    Always appends tests/test_integration_autopilot_enforcement.py LAST.
    Returns ALPHABETIZED unique list with no duplicates.

    Empty/no-mapping input → returns [integration suite] only (caller decides
    whether to fall back to legacy full-suite via `had_mapped_targets`-equiv
    check using == 1 + integration-only test).
    """
    import glob as _glob
    targets: set[str] = set()
    if staged_files is None:
        try:
            # diff-filter=d: exclude DELETED files (logic-iter1-3 fix)
            r = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=d", "HEAD", "--cached"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, cwd=str(_MEMEXA_ROOT),
            )
            staged_files = [
                f.strip() for f in r.stdout.splitlines()
                if f.strip().endswith(".py")
            ] if r.returncode == 0 else []
        except Exception:
            staged_files = []
    for f in staged_files or []:
        if not f.endswith(".py"):
            continue
        if f.startswith("tests/"):
            if (_MEMEXA_ROOT / f).exists():
                targets.add(f)
            continue
        # Map memexa/core/X.py → tests/test_X*.py via glob.
        # security-iter1-1 fix: escape stem to prevent metachar injection
        # (e.g. `[evil].py` would otherwise match dozens of unrelated tests).
        stem = Path(f).stem
        if not stem:
            continue
        escaped_stem = _glob.escape(stem)
        for hit in (_MEMEXA_ROOT / "tests").glob(f"test_{escaped_stem}*.py"):
            rel = hit.relative_to(_MEMEXA_ROOT).as_posix()
            targets.add(rel)
    integration = "tests/test_integration_autopilot_enforcement.py"
    if (_MEMEXA_ROOT / integration).exists():
        targets.add(integration)
    return sorted(targets)


def _run_pytest_quick() -> Tuple[bool, str]:
    """Run targeted pytest quick check. Returns (passed, summary).

    v2.0 §3.1 spec-conformant (autopilot_v20_spec_fixes TU-2):
      - Targets: changed-file related tests + always-include integration suite.
      - Drop -x: collect ALL targeted failures (not stop-on-first).
      - Empty target set → fall back to legacy full-suite (preserves CI mode).

    Memory safety: --tb=line --no-header; capture last 5 lines.

    Failure policy:
      - Infrastructure failure (pytest not found, timeout) -> NON-BLOCKING
      - Test failures detected -> BLOCKING
    Side effect: writes last_session_test_result.json for helpful_count Gate 3.

    Override: MEMEXA_PYTEST_FULL_SUITE=1 forces legacy full-suite mode
    (e.g. for CI weekly run).
    """
    if os.environ.get("MEMEXA_PYTEST_FULL_SUITE", "").strip() == "1":
        targets = ["tests/"]
        mode_label = "full"
    else:
        targets = _compute_pytest_targets()
        mode_label = "targeted"
        # logic-iter1-1 fix: only fall back to full-suite if NO staged .py
        # files mapped to any test. Integration suite (always added) does
        # NOT count as a "real" target. Test: if list contains only the
        # integration suite, fall back; otherwise stay targeted.
        integration_only = (
            len(targets) == 0
            or (len(targets) == 1
                and targets[0] == "tests/test_integration_autopilot_enforcement.py")
        )
        if integration_only:
            targets = ["tests/"]
            mode_label = "full_fallback_no_staged"

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *targets, "-q", "--tb=line", "--no-header"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(_MEMEXA_ROOT),
        )
        # Only take last 5 lines to prevent context explosion
        lines = result.stdout.strip().splitlines()
        summary = lines[-1] if lines else ""
        # Truncate: never return more than 500 chars from pytest
        summary = summary[:500]

        # Emit observability trace (best-effort, non-blocking)
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("pytest_targeted_baseline", {
                "mode": mode_label,
                "target_count": len(targets),
                "summary": summary,
                "exit_code": result.returncode,
            })
        except Exception:
            pass

        if result.returncode == 0:
            _write_test_result(True, "commit_gate", summary)
            return True, f"pytest({mode_label}, {len(targets)} targets): {summary} -> PASSED"

        _write_test_result(False, "commit_gate", summary)
        return False, f"pytest({mode_label}, {len(targets)} targets): {summary} -> BLOCKED (test failures detected)"

    except subprocess.TimeoutExpired:
        # Infra failure: don't write tests_passed result (ambiguous state)
        return True, f"pytest timeout (infra non-blocking, mode={mode_label})"
    except FileNotFoundError:
        return True, "pytest not found (infra non-blocking)"
    except Exception as e:
        return True, f"pytest infra error (non-blocking): {e}"


# ================================================================
# Release-gate & Strategic-advisor auto-trigger
# ================================================================

def _check_release_gate_trigger() -> Optional[str]:
    """Check if enough commits since last release-gate to trigger one."""
    try:
        harness = json.loads(_HARNESS.read_text(encoding="utf-8"))
        last_release = harness.get("virtual_company", {}).get("last_release_gate_commit", "")

        if not last_release:
            result = subprocess.run(
                ["git", "rev-list", "--count", "HEAD~20..HEAD"],
                capture_output=True, text=True, cwd=str(_MEMEXA_ROOT), timeout=5,
            )
            commits_since = int(result.stdout.strip()) if result.returncode == 0 else 0
        else:
            result = subprocess.run(
                ["git", "rev-list", "--count", f"{last_release}..HEAD"],
                capture_output=True, text=True, cwd=str(_MEMEXA_ROOT), timeout=5,
            )
            commits_since = int(result.stdout.strip()) if result.returncode == 0 else 0

        if commits_since >= RELEASE_GATE_COMMIT_THRESHOLD:
            specs = [
                {
                    "agent": "release-gate",
                    "model": "sonnet",
                    "prompt": f"Release gate: {commits_since} commits since last gate. "
                              "Full verification: build + test + harness sync + CHANGELOG.",
                    "trigger": "auto_session_gate",
                },
                {
                    "agent": "strategic-advisor",
                    "model": "opus",
                    "prompt": f"Post-release strategic review: {commits_since} commits completed. "
                              "Read harness_state + memory + events. Output ROI-sorted recommendations.",
                    "trigger": "auto_session_gate",
                },
            ]

            specs_file = _DATA / "pending_agent_specs.json"
            _DATA.mkdir(parents=True, exist_ok=True)
            existing = []
            if specs_file.exists():
                try:
                    existing = json.loads(specs_file.read_text(encoding="utf-8"))
                except Exception:
                    existing = []

            existing_agents = {s.get("agent") for s in existing}
            new_specs = [s for s in specs if s["agent"] not in existing_agents]
            if new_specs:
                existing.extend(new_specs)
                specs_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                agents = [s["agent"] for s in new_specs]
                return f"{commits_since} commits -> queued {', '.join(agents)}"

        return None
    except Exception:
        return None


# ================================================================
# Main gate functions
# ================================================================

_BENCH_PIPELINE_GLOBS = (
    "memexa/core/hindsight",
    "memexa/core/memory_",
    "memexa/core/graph_",
    "memexa/core/dual_llm",
    "memexa/core/entity_",
    "memexa/core/predicate_",
    "memexa/vendor/",
    "memexa/core/bench_runner",
    "memexa/core/bench_dashboard",
    ".claude/data/audit_corpus.jsonl",
)


def _staged_touches_pipeline(staged_files: List[str]) -> bool:
    """Skip predicate (D4): True if any staged file matches memory_pipeline globs.

    Path normalisation: convert backslash → forward, strip ONLY leading "./"
    relative prefix (NOT leading "." which would mangle ".claude/" paths).
    Plan_v1 RP-candidate: cited as latent bug in TU-7 cross-check.
    """
    if not staged_files:
        return False
    for f in staged_files:
        norm = f.replace("\\", "/")
        if norm.startswith("./"):
            norm = norm[2:]
        for needle in _BENCH_PIPELINE_GLOBS:
            if needle in norm:
                return True
    return False


def _bench_gate_block_mode(env: Dict[str, str]) -> bool:
    """2x2 matrix for MEMEXA_BENCH_BLOCK env (HARD RULE feedback_priority_inverted_fallback_2x2_matrix.md):
       (unset / set_empty) x (canonical / no_canonical).
       Empty/unset → False (warn-mode). Set non-empty truthy → True (block).
    """
    raw = env.get("MEMEXA_BENCH_BLOCK")
    if raw is None:
        return False  # unset: warn-mode default per F-5
    raw = raw.strip()
    if raw == "":
        return False  # set_empty treated same as unset
    return raw.lower() in ("1", "true", "yes", "on", "block")


def _bench_gate(
    staged_files: List[str],
    autopilot: bool,
    env: Dict[str, str],
) -> Tuple[str, Optional[str]]:
    """U6 TU-2 bench_gate — 6th commit gate.

    Returns (decision, reason):
      decision in {pass, block, warn_only, skip, fail_open}.

    Skip predicate runs FIRST before any daemon I/O.
    Recursion guard: MEMEXA_BENCH_GATE_INVOKED env semaphore.
    Empty staged → skip with reason 'no_files_staged'.
    No memory_pipeline match → skip with reason 'no_memory_pipeline_staged'.
    MEMEXA_BENCH_SKIP=1 → skip with reason 'env_skip_authorized'.
    MEMEXA_BENCH_BYPASS_TOKEN valid → skip with reason 'hmac_bypass'.
    Otherwise: invoke bench_runner; warn-mode default (exit 0 + stderr); block-mode if env set.
    """
    # 0: recursion guard (per R-E)
    if env.get("MEMEXA_BENCH_GATE_INVOKED") == "1":
        return ("skip", "recursion_guard")
    # 1: empty staged → skip (no daemon contact; per AC-6)
    if not staged_files:
        return ("skip", "no_files_staged")
    # 2: skip predicate before any I/O (per AC-7 + synthesis #7)
    if not _staged_touches_pipeline(staged_files):
        return ("skip", "no_memory_pipeline_staged")
    # 3: explicit skip env (security-iter1-2 fix: emit observable trace)
    if env.get("MEMEXA_BENCH_SKIP", "").strip() == "1":
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("bench_gate_env_skip", {"channel": "MEMEXA_BENCH_SKIP"})
        except Exception:
            pass
        return ("skip", "env_skip_authorized")
    # 4: HMAC bypass token (CEO-signed)
    bypass_token = env.get("MEMEXA_BENCH_BYPASS_TOKEN", "").strip()
    if bypass_token:
        try:
            from src.core._hook_utils import _verify_bench_bypass_token
            if _verify_bench_bypass_token(bypass_token):
                try:
                    from src.core.trace_sink import write_trace_event
                    write_trace_event("bench_bypass_authorized", {"channel": "MEMEXA_BENCH_BYPASS_TOKEN"})
                except Exception:
                    pass
                return ("skip", "hmac_bypass_authorized")
        except Exception:
            pass  # fail-soft: invalid token treated as no-bypass
    # 5: invoke bench_runner. security-iter1-1 fix: outer try/finally
    # MUST cover ALL exit paths (including import error) to prevent env leak.
    env["MEMEXA_BENCH_GATE_INVOKED"] = "1"
    try:
        try:
            from src.core.bench_runner import run_benchmark, append_history
        except Exception as e:
            return ("fail_open", f"import_error:{type(e).__name__}")
        threshold = 0.35
        try:
            t_env = env.get("MEMEXA_BENCH_THRESHOLD", "").strip()
            if t_env:
                threshold = float(t_env)
        except Exception:
            pass
        result = run_benchmark(
            corpus_path=str(_WORKSPACE / ".claude" / "data" / "audit_corpus.jsonl"),
            mode="fast",
            timeout_s=15,
            max_queries=10,
            no_daemon_start=False,
        )
        try:
            append_history(result)
        except Exception:
            pass  # fail-soft on history append
        recall = result.recall_at_10_real_only
        if recall < threshold:
            if _bench_gate_block_mode(env):
                return ("block", f"recall@10_real={recall:.3f}<threshold={threshold:.2f}")
            return ("warn_only", f"recall@10_real={recall:.3f}<threshold={threshold:.2f}")
        return ("pass", f"recall@10_real={recall:.3f}>=threshold={threshold:.2f}")
    finally:
        env.pop("MEMEXA_BENCH_GATE_INVOKED", None)


def run_commit_gate(phase: Optional[int] = None) -> int:
    """Commit gate: local review + pytest + persistence checks.

    Exit 0 = allow commit, Exit 1 = block commit.

    U8 plan_v2 TU-4: when `phase` is provided, rule-7/rule-8 scope ACs and
    TUs to that phase only. Final monolithic commit (phase=None) refuses if
    any phase-sentinel is present (cross-phase audit guard per logic-iter1-3).

    Check order:
      1. Local reviewer — fast, no API
      2. pytest — medium, local only
      3. Persistence checks — warnings only
      4. Release-gate trigger — advisory

    [Item #2] Emits gate_decision event on completion.
    """
    import os
    os.environ["MEMEXA_COMMIT_GATE_RUNNING"] = "1"
    # U8 TU-4: propagate phase via env var so _rule7/_rule8 can read it
    # without needing to thread param through every callsite (keeps the
    # diff localised; existing rule-7/rule-8 call signatures unchanged).
    if phase is not None:
        os.environ["MEMEXA_COMMIT_GATE_PHASE"] = str(phase)
    else:
        os.environ.pop("MEMEXA_COMMIT_GATE_PHASE", None)

    print("[SESSION GATE v6.0] Commit gate running...")
    _failed_checks: List[str] = []
    _passed_checks: List[str] = []

    # 2026-04-24 plan_v3 TU-5: observability summary — track each gate's
    # outcome so we can emit a single-line [GATE STATUS] at the tail.
    gate_status: Dict[str, str] = {
        "local": "skip", "pytest": "skip", "depth": "skip",
        "ac_audit": "skip", "plan_retro": "skip", "autopilot": "off",
        "bench": "skip",
        "mode_b": "skip",  # 2026-04-26 U3 plan_v4 TU-4: rule-9 Mode-B HMAC governance
    }
    # AC-A2: track skip reasons parallel to gate_status for [GATE COVERAGE] line
    _skip_reasons: Dict[str, str] = {}

    # 2026-04-24 plan_v3 TU-1: autopilot detection. When True, gates
    # switch from fail-open to fail-CLOSED on missing evidence.
    try:
        from src.core._autopilot_flag import autopilot_active
        _autopilot = autopilot_active()
    except Exception:
        _autopilot = False
    gate_status["autopilot"] = "on" if _autopilot else "off"

    # Pre-resolve spec_file path for REVIEW GATE and STEP GATE (LOW-8 fix)
    spec_file = _DATA / "task_spec.json"

    # Get staged files (exclude deleted files — they can't be reviewed)
    all_staged = []
    py_files = []
    # allow-silent: fail-soft observability path
    try:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--diff-filter=d", "--name-only"],
            capture_output=True, text=True, cwd=str(_MEMEXA_ROOT), timeout=10,
        )
        all_staged = [f for f in staged.stdout.strip().splitlines() if f.strip()]
        py_files = [f for f in all_staged if f.endswith(".py")]
    # allow-silent: observability fail-soft
    except Exception:
        pass

    blocked = False

    # 2026-04-24 plan_v3 TU-1: MANDATORY pre-block when autopilot is
    # active, .py files are staged, but no task_binding exists.
    # Without this, Rule 7/8/plan_retro_gate all silently skip on
    # active_tid=None (demonstrated in today's audit: 3 commits zero
    # rule7/rule8 events). Non-autopilot path is preserved below.
    #
    # 2026-04-25 plan_v1 TU-2: routed through _resolve_active_tid which
    # adds Tier-2 autopilot_flag fallback so env-loss subprocesses
    # (git pre-commit) still resolve a tid.
    if _autopilot and py_files:
        _tid_check = _resolve_active_tid(_autopilot)
        if not _tid_check:
            print(
                "[AUTOPILOT BLOCK] autopilot active but no task_binding found. "
                "Run: python -m src.core.task_binding bind <task_id>  "
                "(see .claude/harness/tasks/ for active task ids). "
                "Or clear the flag to opt out: python -m src.core._autopilot_flag clear"
            )
            # Short-circuit: do not run other gates; this is a pre-flight failure.
            gate_status["local"] = "blocked_upstream"
            gate_status["pytest"] = "blocked_upstream"
            gate_status["depth"] = "blocked_upstream"
            gate_status["ac_audit"] = "blocked_upstream"
            gate_status["plan_retro"] = "blocked_upstream"
            gate_status["mode_b"] = "blocked_upstream"  # U3 TU-4
            _print_gate_status(gate_status, _skip_reasons)
            return 1

    # --- Local review (zero-cost AST checks) ---
    if py_files:
        try:
            files_json = json.dumps(py_files)
            lr_code = (
                "import json, sys, os\n"
                "files = json.loads(os.environ['_MEMEXA_REVIEW_FILES'])\n"
                "from src.core.local_reviewer import review_files\n"
                "r = review_files(files)\n"
                "print(r.summary)\n"
                "for f in r.findings:\n"
                "    print(f'  [{f.severity.upper()}] {f.file}:{f.line} {f.message}')\n"
                "sys.exit(1 if any(f.severity in ('critical','high') for f in r.findings) else 0)\n"
            )
            # 2026-04-26 U3 plan_v4 TU-8 (S-3 + T-3): scrub MEMEXA_CEO_*
            # prefix from local_reviewer subprocess env. local_reviewer
            # executes attacker-controllable staged .py files; HMAC key /
            # approvals dir env must NOT leak to that subprocess. Prefix
            # match (not single tuple) so future MEMEXA_CEO_* additions
            # auto-scrubbed.
            _scrubbed_env = {k: v for k, v in os.environ.items()
                             if not k.startswith("MEMEXA_CEO_")}
            env = {**_scrubbed_env, "_MEMEXA_REVIEW_FILES": files_json}
            lr_result = subprocess.run(
                [sys.executable, "-c", lr_code],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, cwd=str(_MEMEXA_ROOT), env=env,
            )
            # Cap reviewer output to prevent OOM in long sessions (max 1500 chars)
            _lr_out = lr_result.stdout.strip()
            # ASCII-safe print: parent stdio may be GBK on Windows; UTF-8
            # subprocess output containing CJK or U+FFFD will crash print().
            try:
                print(f"[LOCAL] {_lr_out[:1500]}")
            except UnicodeEncodeError:
                print(f"[LOCAL] {_lr_out[:1500].encode('ascii', 'replace').decode('ascii')}")
            if lr_result.returncode != 0:
                print("[LOCAL] BLOCKED: critical/high findings")
                blocked = True
                gate_status["local"] = "block"
            else:
                gate_status["local"] = "ok"
        except Exception as e:
            # v2.0 §9.1 fix (autopilot_v20_spec_fixes TU-1): local gate spec
            # says "never skip — always cheap". Default = block on infra
            # exception. Authorized fail_open requires explicit env (mirrors
            # pytest gate pattern from §9.1).
            _safe_msg = repr(e).encode("ascii", "replace").decode("ascii")
            _local_fo_reason = os.environ.get(
                "MEMEXA_LOCAL_GATE_FAIL_OPEN_REASON", ""
            ).strip()
            if len(_local_fo_reason) >= 80:
                print(
                    f"[LOCAL] AUTHORIZED fail_open via "
                    f"MEMEXA_LOCAL_GATE_FAIL_OPEN_REASON "
                    f"({len(_local_fo_reason)} chars): {_local_fo_reason[:200]}"
                )
                print(f"[LOCAL] Underlying infra exception: {_safe_msg[:300]}")
                gate_status["local"] = "fail_open"
                _skip_reasons["local"] = "authorized_fail_open"
                # Custom observability trace event (matches pytest pattern
                # at line ~689). Spec §9.1 names this `local_fail_open_authorized`.
                try:
                    from src.core.trace_sink import write_trace_event
                    write_trace_event("local_fail_open_authorized", {
                        "reason": _local_fo_reason[:300],
                        "exception_type": type(e).__name__,
                        "active_tid": _resolve_active_tid(_autopilot) or "",
                        "commit_sha": _get_head_sha_short() or "",
                    })
                except Exception:
                    pass
                _emit_skip_trace("local", "authorized_fail_open",
                                 _resolve_active_tid(_autopilot),
                                 _autopilot, _get_head_sha_short())
            else:
                print(
                    f"[LOCAL] BLOCKED: subprocess infra exception "
                    f"(set MEMEXA_LOCAL_GATE_FAIL_OPEN_REASON env with >=80 "
                    f"char justification to override): {_safe_msg[:300]}"
                )
                blocked = True
                gate_status["local"] = "block"
                _skip_reasons["local"] = f"infra_exception_blocked_{type(e).__name__}"
    else:
        print("[LOCAL] No .py files staged")
        gate_status["local"] = "skip"
        _skip_reasons["local"] = "no_py_files_staged"
        _emit_skip_trace("local", "no_py_files_staged",
                         None, _autopilot, _get_head_sha_short())

    # --- pytest quick check ---
    if py_files:
        test_ok, test_msg = _run_pytest_quick()
        print(f"[PYTEST] {test_msg}")
        if not test_ok:
            # 2026-04-26 U2 plan_v2 §9.1: explicit fail_open authorization
            # via MEMEXA_PYTEST_FAIL_OPEN_REASON env (≥80-char justification)
            # per autopilot v2 spec. Without env, pytest is fail_closed.
            _fail_open_reason = os.environ.get(
                "MEMEXA_PYTEST_FAIL_OPEN_REASON", ""
            ).strip()
            # B-3 (2026-05-04): reason MUST contain `regression_test_added=<path>`
            # AND that path must exist (be a real test file). Pre-fix: any 80+
            # char text passed → "草草标记返回" pattern (LIVE D-1: 30% of recent
            # 20 commits used fail_open authorized with "pre-existing" reason).
            # Forward-only: applies to commits ≥ this commit; legacy not retroactive.
            _b3_ok = False
            _b3_err = ""
            if len(_fail_open_reason) >= 80:
                import re as _b3_re
                _m = _b3_re.search(
                    r"regression_test_added\s*=\s*([^\s,;]+)",
                    _fail_open_reason,
                )
                if not _m:
                    _b3_err = "missing regression_test_added=<path> field (B-3)"
                else:
                    _added_path = _m.group(1).strip()
                    from pathlib import Path as _B3P
                    _ap = _B3P(_added_path)
                    if not _ap.is_absolute():
                        # try workspace + memexa roots
                        from src.core.task_dir_layout import _workspace_root as _wsr
                        for base in (_wsr(), _wsr() / "memexa"):
                            if (base / _ap).exists():
                                _ap = base / _ap
                                break
                    if not _ap.exists():
                        _b3_err = f"regression_test_added path not found: {_added_path}"
                    elif not str(_ap).endswith(".py"):
                        _b3_err = f"regression_test_added must be .py: {_added_path}"
                    else:
                        _b3_ok = True
            if _b3_ok:
                print(
                    f"[PYTEST] AUTHORIZED fail_open via "
                    f"MEMEXA_PYTEST_FAIL_OPEN_REASON "
                    f"({len(_fail_open_reason)} chars + B-3 regression_test_added verified): "
                    f"{_fail_open_reason[:200]}"
                )
                gate_status["pytest"] = "fail_open"
                _skip_reasons["pytest"] = "authorized_fail_open_b3"
                try:
                    from src.core.trace_sink import write_trace_event
                    write_trace_event("pytest_fail_open_authorized", {
                        "reason": _fail_open_reason[:500],
                        "head": _get_head_sha_short(),
                        "b3_regression_test_verified": True,
                    })
                except Exception:
                    pass  # allow-silent
            else:
                if _b3_err:
                    print(f"[PYTEST] fail_open REJECTED: {_b3_err} (set "
                          f"regression_test_added=<existing.py> in reason)")
                    try:
                        from src.core.trace_sink import write_trace_event
                        write_trace_event("fail_open_reason_rejected", {
                            "reason_len": len(_fail_open_reason),
                            "b3_error": _b3_err,
                            "head": _get_head_sha_short(),
                        })
                    except Exception:
                        pass
                blocked = True
                gate_status["pytest"] = "block"
        elif "timeout" in test_msg.lower() or "non-blocking" in test_msg.lower():
            gate_status["pytest"] = "fail_open"
        else:
            gate_status["pytest"] = "ok"
    else:
        gate_status["pytest"] = "skip"
        _skip_reasons["pytest"] = "no_py_files_staged"
        _emit_skip_trace("pytest", "no_py_files_staged",
                         None, _autopilot, _get_head_sha_short())

    # --- D.8: Persistence check (warnings only, non-blocking) ---
    mem_ok, mem_issues = check_memory_integrity()
    harness_ok, harness_issues = check_harness_freshness()
    for issue in mem_issues:
        print(f"[MEMORY] {issue}")
    for issue in harness_issues:
        print(f"[HARNESS] {issue}")

    # --- Release-gate + strategic-advisor trigger ---
    trigger_msg = _check_release_gate_trigger()
    if trigger_msg:
        print(f"[RELEASE] {trigger_msg}")

    # --- REVIEW GATE: complex tasks must pass review before commit ---
    try:
        if spec_file.exists():
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
            if spec.get("complexity") == "complex" and spec.get("status") == "in_progress":
                criteria = {c["id"]: c.get("verified", False)
                            for c in spec.get("acceptance_criteria", [])}
                if "review_approved" in criteria and not criteria["review_approved"]:
                    print("[REVIEW GATE] BLOCKED: complex task requires review_approved before commit")
                    print("[REVIEW GATE] Run Stage 4 review agents, then verify_criteria('review_approved')")
                    blocked = True
    except Exception as e:
        print(f"[REVIEW GATE] Check error (non-blocking): {e}")

    # --- STEP GATE: check required steps not skipped ---
    try:
        if spec_file.exists():
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
            tracker = spec.get("step_tracker")
            if tracker and spec.get("complexity") in ("complex", "medium"):
                required = set(tracker.get("required_for_completion", []))
                completed = set(tracker.get("completed_steps", []))
                # Only check review/security/regression steps (not early steps like scope_validate)
                review_steps = {"review", "technical_review", "narrative_review",
                                "consistency_check", "security", "regression",
                                "cross_review", "review_findings"}
                missing_review = (required & review_steps) - completed
                if missing_review:
                    print(f"[STEP GATE] WARNING: {len(missing_review)} review/security steps not done: "
                          f"{', '.join(sorted(missing_review))}")
                    if spec.get("complexity") == "complex":
                        print("[STEP GATE] BLOCKED: complex tasks must complete review steps before commit")
                        blocked = True
    except Exception as e:
        print(f"[STEP GATE] Check error (non-blocking): {e}")

    # --- PLANNING-INFRA (2026-04-21) rules 7 + 8 ---
    # Both only fire when there's an active task binding; legacy commits
    # (no task_id) fail-open as per ANCHOR-5.
    #
    # 2026-04-25 plan_v1 TU-2: _resolve_active_tid adds autopilot_flag
    # fallback so direct-shell commits (which lose MEMEXA_ACTIVE_TASK_ID
    # in the .git/hooks/pre-commit subprocess) still resolve a tid when
    # autopilot=on. LIVE evidence pre-fix: 12/17 gate_skipped had
    # reason=no_task_binding. Post-fix: those become enforced.
    _sha_pre = _get_head_sha_short()
    active_tid = _resolve_active_tid(_autopilot, _sha_pre)

    if not active_tid:
        # No task binding: depth, ac_audit, plan_retro, mode_b all skip
        _sha = _sha_pre
        for _gate in ("depth", "ac_audit", "plan_retro", "mode_b"):
            gate_status[_gate] = "skip"
            _skip_reasons[_gate] = "no_task_binding"
            _emit_skip_trace(_gate, "no_task_binding", None, _autopilot, _sha)

    if active_tid:
        try:
            # Estimate code_lines_added from staged diff
            diff_stat = subprocess.run(
                ["git", "diff", "--cached", "--numstat"],
                capture_output=True, text=True, cwd=str(_MEMEXA_ROOT), timeout=10,
            )
            code_lines = 0
            for line in (diff_stat.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    try:
                        code_lines += int(parts[0])
                    except ValueError:
                        pass
            # Rule-7 depth gate
            r7_allow, r7_reason = _rule7_depth_gate(active_tid, code_lines)
            print(f"[DEPTH GATE] {r7_reason}")
            if not r7_allow:
                blocked = True
                gate_status["depth"] = "block"
            elif "fail_open" in r7_reason or "fail-open" in r7_reason:
                gate_status["depth"] = "fail_open"
            else:
                gate_status["depth"] = "ok"
            # Rule-8 ac_audit
            r8_allow, r8_reason = _rule8_ac_audit(active_tid)
            print(f"[AC AUDIT] {r8_reason}")
            if not r8_allow:
                blocked = True
                gate_status["ac_audit"] = "block"
            elif "fail_open" in r8_reason or "fail-open" in r8_reason:
                gate_status["ac_audit"] = "fail_open"
            else:
                gate_status["ac_audit"] = "ok"
        except Exception as e:
            print(f"[PLANNING-INFRA] Check error (non-blocking): {e}")
            gate_status["depth"] = "fail_open"
            gate_status["ac_audit"] = "fail_open"

        # --- PLAN RETRO GATE (TU-1, 2026-04-22) ---
        # Enforces that Stage 4 reviewer findings with root_cause=plan_gap
        # have been annotated into plan_v<N>.md before commit lands. This
        # closes the feedback loop — otherwise plan-gap findings get
        # code-patched but plan templates stay stale.
        # Operator override: MEMEXA_SKIP_PLAN_RETRO=1 (logged).
        try:
            from src.core.plan_retro_gate import check_gate as _prg_check, \
                env_skip_flag as _prg_skip
            # AC-A4 part-2: multi-path override resolution.
            # Read order: (1) ~/.claude_gates_override file, (2) MEMEXA_GATES_OVERRIDE env,
            # (3) legacy MEMEXA_GATES_BOOTSTRAP=1.
            # OWASP LLM01: all accept paths emit observable trace events.
            _override_consumed = False
            _sha_now = _get_head_sha_short()

            # Path 1: file-based override (out-of-band, CEO-owned)
            _override_file = Path.home() / ".claude_gates_override"
            if _override_file.exists() and not _override_consumed:
                try:
                    _file_token = _override_file.read_text(encoding="utf-8").strip()
                    from src.core._gates_skip_budget import verify_override_token
                    _tok_ok, _tok_reason = verify_override_token(_file_token)
                    if _tok_ok:
                        _emit_trace("override_consumed", {
                            "source": "file",
                            "gate": "plan_retro",
                            "active_tid": active_tid or "",
                            "commit_sha": _sha_now or "",
                        })
                        print("[PLAN RETRO GATE] skipped via ~/.claude_gates_override token")
                        gate_status["plan_retro"] = "skip"
                        _skip_reasons["plan_retro"] = "override_token_consumed"
                        _emit_skip_trace("plan_retro", "override_token_consumed",
                                         active_tid, _autopilot, _sha_now)
                        _override_consumed = True
                    else:
                        _emit_trace("override_invalid", {
                            "source": "file",
                            "reason": _tok_reason,
                            "gate": "plan_retro",
                        })
                        print(f"[PLAN RETRO GATE] override file invalid: {_tok_reason}")
                except (ImportError, OSError, RuntimeError):
                    pass  # fail-soft: invalid file falls through to next path

            # Path 2: env var override (secondary/backward-compat)
            if not _override_consumed:
                _env_token = os.environ.get("MEMEXA_GATES_OVERRIDE", "").strip()
                if _env_token:
                    try:
                        from src.core._gates_skip_budget import verify_override_token
                        _tok_ok, _tok_reason = verify_override_token(_env_token)
                        if _tok_ok:
                            _emit_trace("override_consumed", {
                                "source": "env",
                                "gate": "plan_retro",
                                "active_tid": active_tid or "",
                                "commit_sha": _sha_now or "",
                            })
                            # Also emit fallback_to_env audit warning
                            _emit_trace("fallback_to_env", {
                                "name": "MEMEXA_GATES_OVERRIDE",
                                "gate": "plan_retro",
                            })
                            print("[PLAN RETRO GATE] skipped via MEMEXA_GATES_OVERRIDE env token")
                            gate_status["plan_retro"] = "skip"
                            _skip_reasons["plan_retro"] = "override_token_consumed"
                            _emit_skip_trace("plan_retro", "override_token_consumed",
                                             active_tid, _autopilot, _sha_now)
                            _override_consumed = True
                        else:
                            _emit_trace("override_invalid", {
                                "source": "env",
                                "reason": _tok_reason,
                                "gate": "plan_retro",
                            })
                    except (ImportError, OSError, RuntimeError):
                        pass  # fail-soft: fall through to legacy path

            # Path 3: legacy MEMEXA_GATES_BOOTSTRAP=1
            # TU-6 (plan_v3): bootstrap counter. MEMEXA_GATES_BOOTSTRAP=1
            # skips plan_retro enforcement for the current commit BUT
            # increments a counter and emits L2 action_item at N>=1
            # (OWASP LLM01 — cannot silently ride override channel).
            bootstrap = os.environ.get("MEMEXA_GATES_BOOTSTRAP", "") == "1"
            if bootstrap and not _override_consumed:
                _record_bootstrap_bypass()
                print("[PLAN RETRO GATE] skipped via MEMEXA_GATES_BOOTSTRAP=1 "
                      "(CEO L2 action_item emitted)")
                gate_status["plan_retro"] = "skip"
                _skip_reasons["plan_retro"] = "bootstrap_bypass"
                _emit_skip_trace("plan_retro", "bootstrap_bypass",
                                 active_tid, _autopilot, _sha_now)
                _override_consumed = True
            elif _prg_skip() and not _override_consumed:
                print("[PLAN RETRO GATE] skipped via MEMEXA_SKIP_PLAN_RETRO=1")
                gate_status["plan_retro"] = "skip"
                _skip_reasons["plan_retro"] = "MEMEXA_SKIP_PLAN_RETRO"
                _emit_skip_trace("plan_retro", "MEMEXA_SKIP_PLAN_RETRO",
                                 active_tid, _autopilot, _sha_now)
            elif not _override_consumed:
                _prg_allow, _prg_reason = _prg_check(active_tid)
                if _prg_allow:
                    if _prg_reason:
                        print(f"[PLAN RETRO GATE] {_prg_reason[:200]}")
                    gate_status["plan_retro"] = "ok"
                else:
                    print(f"[PLAN RETRO GATE] BLOCKED: {_prg_reason[:400]}")
                    blocked = True
                    gate_status["plan_retro"] = "block"
        except Exception as e:
            print(f"[PLAN RETRO GATE] Check error (non-blocking): {e}")
            gate_status["plan_retro"] = "fail_open"

        # TU-5 (plan_v3): stage4_enforcement predicate check
        # Blocks commit when autopilot flag + complexity=complex + no
        # review_findings/*.json exists in task dir. This stops me from
        # skipping Stage 4 reviewers under the guise of "tests passed".
        try:
            from src.core.gates.stage4_enforcement import check as _s4_check
            _s4_allow, _s4_reason = _s4_check(active_tid)
            if _s4_allow:
                if _s4_reason:
                    print(f"[STAGE 4 ENFORCEMENT] {_s4_reason[:200]}")
            else:
                print(f"[STAGE 4 ENFORCEMENT] BLOCKED: {_s4_reason[:400]}")
                blocked = True
        except Exception as e:
            print(f"[STAGE 4 ENFORCEMENT] Check error (non-blocking): {e}")

        # U6 TU-2: bench_gate — continuous benchmark gate (PR-time).
        # Skip predicate runs FIRST before any daemon I/O.
        # Recursion guard via MEMEXA_BENCH_GATE_INVOKED env semaphore.
        try:
            _b_decision, _b_reason = _bench_gate(all_staged, _autopilot, os.environ)
            gate_status["bench"] = _b_decision
            if _b_reason:
                _skip_reasons["bench"] = _b_reason
            if _b_decision == "block":
                print(f"[BENCH GATE] BLOCKED: {_b_reason[:400] if _b_reason else 'threshold trip'}")
                blocked = True
            elif _b_decision == "warn_only":
                print(f"[BENCH GATE] WARN: {_b_reason[:300] if _b_reason else 'below threshold'}")
            elif _b_decision == "skip":
                if _b_reason:
                    print(f"[BENCH GATE] SKIP: {_b_reason[:200]}")
        except Exception as e:
            print(f"[BENCH GATE] Check error (non-blocking): {e}")
            gate_status["bench"] = "fail_open"
            _skip_reasons["bench"] = "check_exception"

        # 2026-04-26 U3 plan_v4 TU-3: rule-9 Mode-B HMAC self-review
        # governance (OWASP LLM01). Reads <task_dir>/review_findings/*.json,
        # corroborates "verified_by=sub_agent" claims via traces, falls
        # through to ceo_approve.verify_approval when main_session_fallback.
        # Orthogonal to stage4_enforcement / bench: rule-9 emits its own
        # gate_status independently (architect B1 policy).
        try:
            _r9_allow, _r9_reason = _check_mode_b_governance(active_tid, _autopilot)
            if not _r9_allow:
                print(f"[MODE-B GATE] BLOCKED: {_r9_reason[:300]}")
                blocked = True
                gate_status["mode_b"] = "block"
                _skip_reasons["mode_b"] = _r9_reason
            else:
                if _r9_reason in ("no_findings_skip",):
                    gate_status["mode_b"] = "skip"
                    _skip_reasons["mode_b"] = _r9_reason
                    # v2.0 fix (autopilot_v20_spec_fixes TU-3): arity must
                    # match _emit_skip_trace(gate, reason, active_tid,
                    # autopilot, commit_sha). Was 2-arg call → TypeError.
                    _emit_skip_trace("mode_b", _r9_reason,
                                     active_tid, _autopilot,
                                     _get_head_sha_short())
                elif "fail_open" in _r9_reason:
                    gate_status["mode_b"] = "fail_open"
                    _skip_reasons["mode_b"] = _r9_reason
                else:
                    gate_status["mode_b"] = "ok"
                    print(f"[MODE-B GATE] {_r9_reason}")
        except Exception as e:
            print(f"[MODE-B GATE] Check error (non-blocking): {e}")
            gate_status["mode_b"] = "fail_open"
            _skip_reasons["mode_b"] = f"infra_error_{type(e).__name__}"

    # L-5 (Phase 3, 2026-05-04): live_findings gate. If ac_verifier earlier
    # reported any LIVE-finding (Stage 6 → Stage 2 closed loop), commit MUST
    # block until those findings are cleared (re-verified or explicitly
    # withdrawn). This is the structural mechanism that makes "submit
    # /memexa-pilot, walk away" actually safe — if E2E shows red, Stage 5
    # refuses to commit.
    try:
        if active_tid:
            from src.core.failure_cluster_detector import list_live_findings
            _live = list_live_findings(active_tid)
            if _live:
                ac_ids = [f.get("ac_id", "?") for f in _live[:5]]
                print(f"[LIVE FINDINGS GATE] BLOCKED: {len(_live)} unresolved "
                      f"AC red flag(s): {ac_ids} — fix code or withdraw via "
                      f"clear_live_finding() before commit")
                blocked = True
                gate_status["live_findings"] = "block"
                _skip_reasons["live_findings"] = f"{len(_live)} unresolved"
                try:
                    from src.core.trace_sink import write_trace_event
                    write_trace_event("live_findings_gate_block", {
                        "task_id": active_tid,
                        "count": len(_live),
                        "ac_ids": ac_ids,
                    })
                except Exception:
                    pass
            else:
                gate_status["live_findings"] = "ok"
    except (ImportError, AttributeError):
        pass  # detector module loading; non-blocking
    except Exception as e:
        print(f"[LIVE FINDINGS GATE] check error (non-blocking): {e}")
        gate_status["live_findings"] = "fail_open"

    # [Item #2] Emit gate_decision event
    # allow-silent: fail-soft observability path
    try:
        from src.core._hook_utils import log_gate_decision
        log_gate_decision(
            gate="session_gate",
            rule="commit_gate",
            decision="block" if blocked else "allow",
            target=",".join(all_staged[:3]) if all_staged else "",
            reason=f"py_files={len(py_files)} staged={len(all_staged)}",
            extra={"blocked": blocked, "staged_count": len(all_staged)},
        )
    # allow-silent: observability fail-soft
    except Exception:
        pass

    # TU-5 observability: emit [GATE STATUS] summary regardless of block/allow.
    _print_gate_status(gate_status, _skip_reasons)

    if blocked:
        print("[SESSION GATE v6.0] COMMIT BLOCKED")
        return 1

    print("[SESSION GATE v6.0] COMMIT ALLOWED")
    return 0


def _record_bootstrap_bypass() -> None:
    """TU-6 (plan_v3): count MEMEXA_GATES_BOOTSTRAP=1 uses + emit L2
    action_item at N>=1 (OWASP LLM01). Fail-soft.
    """
    count = -1
    # LOG-5 fix: capture count via mutator closure, not separate re-read,
    # to eliminate increment/observe race between concurrent commit gates.
    try:
        from src.core._atomic_state import atomic_update_json
        counter_path = _WORKSPACE / ".claude" / "harness" / "bootstrap_invocations.json"
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        if not counter_path.exists():
            counter_path.write_text(json.dumps({"count": 0}), encoding="utf-8")

        captured = {"c": -1}
        def _mut(d):
            new_count = int(d.get("count", 0)) + 1
            d["count"] = new_count
            d["last_invocation"] = time.time()
            captured["c"] = new_count
            return d
        atomic_update_json(counter_path, _mut)
        count = captured["c"]
    except Exception:
        # allow-silent: counter failure should never block commit
        count = -1

    # Emit L2 action_item + trace event at N>=1 (every use)
    try:
        from src.core._atomic_state import atomic_update_json
        hs_path = _WORKSPACE / ".claude" / "config" / "harness_state.json"
        msg = (f"[BootstrapBypass] MEMEXA_GATES_BOOTSTRAP=1 used in "
               f"commit (count={count}) — CEO review recommended")
        def _mut_hs(d):
            items = d.setdefault("action_items_for_user", [])
            if msg not in items:
                items.append(msg)
                d["action_items_for_user"] = items[-10:]
            return d
        if hs_path.exists():
            atomic_update_json(hs_path, _mut_hs)
    # allow-silent: observability fail-soft
    except Exception:
        # allow-silent: action_item emission is observability only
        pass

    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(
            "bootstrap_bypass_repeated" if count >= 2 else "bootstrap_bypass",
            {"count": count},
        )
    except Exception:
        pass  # allow-silent


def _print_gate_status(
    status: Dict[str, str],
    skip_reasons: Optional[Dict[str, str]] = None,
) -> None:
    """TU-5 observability summary. One-line rollup of every gate's outcome.

    Format:  [GATE STATUS] local=ok pytest=ok depth=skip ac_audit=skip
             plan_retro=skip autopilot=off
             [GATE COVERAGE] N/9 enforced, M/9 skipped (reasons: gate=reason, ...)

    Values are one of: ok | skip | block | fail_open | blocked_upstream |
    on | off (last two for autopilot field).

    The 9 gates counted are: local, pytest, depth, ac_audit, plan_retro,
    and the 4 implicit check gates (review, step, stage4, memory). In
    practice this function counts from the keys present in status, excluding
    the special 'autopilot' meta-field. Total denominator = len(status) - 1.
    """
    parts = [f"{k}={v}" for k, v in status.items()]
    print(f"[GATE STATUS] {' '.join(parts)}")

    # [GATE COVERAGE] line (AC-A2)
    skip_reasons = skip_reasons or {}
    # Count enforcement gates (ok + block) vs skipped gates (skip + fail_open + blocked_upstream)
    # Exclude 'autopilot' which is a meta-field, not a gate
    gate_keys = [k for k in status if k != "autopilot"]
    n_enforced = sum(1 for k in gate_keys if status[k] in ("ok", "block"))
    n_skipped = sum(1 for k in gate_keys if status[k] in ("skip", "fail_open", "blocked_upstream"))
    total = len(gate_keys)

    skipped_gates = [k for k in gate_keys if status[k] in ("skip", "fail_open", "blocked_upstream")]
    reason_parts = []
    for g in skipped_gates:
        r = skip_reasons.get(g, status[g])
        reason_parts.append(f"{g}={r}")
    reasons_str = ", ".join(reason_parts) if reason_parts else "none"
    coverage_line = (f"[GATE COVERAGE] {n_enforced}/{total} enforced, "
                     f"{n_skipped}/{total} skipped (reasons: {reasons_str})")
    print(coverage_line)
    # Phase B TU-B2 (2026-05-04): write to data/.last_gate_coverage so
    # commit-msg hook can read + inject into commit message body.
    # Best-effort: never raise. Fail-soft.
    try:
        import os as _os
        from pathlib import Path as _Path
        gate_path = _Path(__file__).resolve().parents[2] / "data" / ".last_gate_coverage"
        gate_path.parent.mkdir(parents=True, exist_ok=True)
        gate_path.write_text(coverage_line + "\n", encoding="utf-8")
    except Exception:
        pass


def _autopilot_active_at_gate() -> bool:
    """Check autopilot flag — use from within rule functions without
    importing at module top-level (avoid cyclic import risk if
    _autopilot_flag ever grows session_gate dependencies)."""
    try:
        from src.core._autopilot_flag import autopilot_active
        return autopilot_active()
    except Exception:
        return False


def _current_phase_filter() -> Optional[int]:
    """Read current phase scope from env (set by run_commit_gate).
    Returns None if no phase scoping (monolithic commit). Sanitized via
    _sanitize_env_phase_value (security-iter2-4)."""
    raw = os.environ.get("MEMEXA_COMMIT_GATE_PHASE", "")
    cleaned = _sanitize_env_phase_value(raw)
    if cleaned is None:
        return None
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def _filter_acs_by_phase(spec, phase: int) -> Tuple[List, List[str]]:
    """Return (filtered_acs, orphan_ac_ids).

    AC `phase` lookup priority: spec.acceptance_criteria[*].phase attr if
    present, else parse from AC verify_cmd / signal_hint hints (best-effort).
    Orphan = AC has no phase attribute at all.
    """
    filtered = []
    orphans = []
    for ac in spec.acceptance_criteria:
        ac_phase = getattr(ac, "phase", None)
        if ac_phase is None:
            # Try dict-style for non-dataclass ACs
            try:
                ac_phase = ac.__dict__.get("phase")
            except AttributeError:
                ac_phase = None
        if ac_phase is None:
            orphans.append(ac.id)
            continue
        try:
            if int(str(ac_phase)) == phase:
                filtered.append(ac)
        except (ValueError, TypeError):
            orphans.append(ac.id)
    return filtered, orphans


def _rule7_depth_gate(task_id: str, code_lines: int) -> Tuple[bool, str]:
    """Depth double-gate: AC density + line ratio. BOTH below → block.
    Individual failures → soft_warn event only.

    U8 TU-4: when MEMEXA_COMMIT_GATE_PHASE env is set, scope ACs to phase.
    """
    # 2026-04-24 plan_v3 TU-4: fail-CLOSED when autopilot is active
    # AND data missing. Prior fail-open let today's 3 complex commits
    # silently bypass gates. Non-autopilot preserves fail-open.
    _auto = _autopilot_active_at_gate()
    try:
        from src.core.plan_spec import (
            get_latest, probe_evidence_store, ac_density,
        )
    except Exception as e:
        _emit_trace("rule7_fail_open", {"task_id": task_id,
                    "reason": "plan_spec_unavailable",
                    "error_type": type(e).__name__})
        if _auto:
            _emit_trace("rule7_block", {"task_id": task_id,
                        "reason": "autopilot active; plan_spec unavailable"})
            return (False, f"rule-7 BLOCK (autopilot): plan_spec unavailable ({type(e).__name__})")
        return (True, f"plan_spec unavailable, fail-open: {type(e).__name__}")
    try:
        spec = get_latest(task_id)
    except FileNotFoundError:
        _emit_trace("rule7_fail_open", {"task_id": task_id,
                    "reason": "no_plan"})
        if _auto:
            _emit_trace("rule7_block", {"task_id": task_id, "reason": "autopilot active; no_plan"})
            return (False, f"rule-7 BLOCK (autopilot): no plan_v*.md under task dir {task_id}")
        return (True, "no_plan_fail_open")
    # ANCHOR-5 probe
    health = probe_evidence_store(task_id)
    if not health.healthy:
        _emit_trace_unhealthy(task_id, "rule7_depth", health.reason)
        _emit_trace("rule7_fail_open", {"task_id": task_id,
                    "reason": f"evidence_unhealthy_{health.reason}"})
        if _auto:
            _emit_trace("rule7_block", {"task_id": task_id,
                        "reason": f"autopilot active; evidence unhealthy: {health.reason}"})
            return (False,
                    f"rule-7 BLOCK (autopilot): evidence store {health.reason}. "
                    f"Run `python -m src.core.ac_verifier run {task_id}` first")
        return (True, f"data_source_{health.reason}_fail_open")
    # ANCHOR-9 zero-AC guard
    if len(spec.acceptance_criteria) < 1:
        return (False, "rule-7 block: no_acs_defined (ANCHOR-9)")
    # U8 TU-4: phase-scoped AC filter
    phase_scope = _current_phase_filter()
    acs_to_check = spec.acceptance_criteria
    if phase_scope is not None:
        acs_to_check, orphans = _filter_acs_by_phase(spec, phase_scope)
        if orphans:
            try:
                from src.core.trace_sink import write_trace_event
                write_trace_event("orphan_ac_blocked", {
                    "task_id": task_id, "phase": phase_scope,
                    "n_orphans": len(orphans),
                    "orphan_ids": orphans[:10],
                })
            except Exception:
                pass
            write_phase_sentinel(task_id, phase_scope, orphans)
            return (False, f"rule-7 block: orphan ACs (no phase tag): {orphans[:5]}; sentinel written")
        if not acs_to_check:
            return (False, f"rule-7 block: phase_not_found (no ACs scoped to phase={phase_scope})")
    # Missing verify_cmd check (scoped)
    missing_vc = [ac.id for ac in acs_to_check if not ac.verify_cmd]
    if missing_vc:
        return (False, f"rule-7 block: ACs without verify_cmd: {missing_vc[:5]}")
    # Double gate
    if code_lines <= 0:
        return (True, "rule-7 ok: no_code_lines_added")
    # Phase-scoped density: count phase-N ACs / code_lines (×100 per ac_density)
    if phase_scope is not None:
        density = (len(acs_to_check) / max(1, code_lines)) * 100.0
    else:
        density = ac_density(spec, code_lines)
    ratio = spec.line_count / max(1, code_lines)
    if density < 1.0 and ratio < 0.3:
        return (False, f"rule-7 block: density={density:.2f} ratio={ratio:.2f} (both below)")
    # Single fail → soft warn
    if density < 1.0:
        _emit_trace("plan_depth_soft_warn", {"task_id": task_id, "metric": "density", "value": density})
    if ratio < 0.3:
        _emit_trace("plan_depth_soft_warn", {"task_id": task_id, "metric": "ratio", "value": ratio})
    return (True, f"rule-7 ok: density={density:.2f} ratio={ratio:.2f}")


def _rule8_ac_audit(task_id: str) -> Tuple[bool, str]:
    """AC audit: every AC must have evidence.jsonl entry with exit_code=0.

    2026-04-24 plan_v3 TU-4: fail-CLOSED when autopilot active AND data
    missing. Non-autopilot path preserves original fail-open.
    """
    _auto = _autopilot_active_at_gate()
    try:
        from src.core.plan_spec import (
            get_latest, load_evidence, probe_evidence_store,
        )
    except Exception as e:
        _emit_trace("rule8_fail_open", {"task_id": task_id,
                    "reason": "plan_spec_unavailable",
                    "error_type": type(e).__name__})
        if _auto:
            _emit_trace("rule8_block", {"task_id": task_id,
                        "reason": "autopilot active; plan_spec unavailable"})
            return (False, f"rule-8 BLOCK (autopilot): plan_spec unavailable ({type(e).__name__})")
        return (True, f"plan_spec unavailable, fail-open: {type(e).__name__}")
    try:
        spec = get_latest(task_id)
    except FileNotFoundError:
        _emit_trace("rule8_fail_open", {"task_id": task_id,
                    "reason": "no_plan"})
        if _auto:
            _emit_trace("rule8_block", {"task_id": task_id, "reason": "autopilot active; no_plan"})
            return (False, f"rule-8 BLOCK (autopilot): no plan under {task_id}")
        return (True, "no_plan_fail_open")
    health = probe_evidence_store(task_id)
    if not health.healthy:
        _emit_trace_unhealthy(task_id, "rule8_ac_audit", health.reason)
        _emit_trace("rule8_fail_open", {"task_id": task_id,
                    "reason": f"evidence_unhealthy_{health.reason}"})
        if _auto:
            _emit_trace("rule8_block", {"task_id": task_id,
                        "reason": f"autopilot active; evidence unhealthy: {health.reason}"})
            return (False,
                    f"rule-8 BLOCK (autopilot active): evidence.jsonl {health.reason}. "
                    f"Run `python -m src.core.ac_verifier run {task_id}` then retry commit")
        return (True, f"data_source_{health.reason}_fail_open")
    # Zero-AC guard
    if len(spec.acceptance_criteria) < 1:
        from src.core.task_dir_layout import task_dir as _task_dir_fn
        td = _task_dir_fn(task_id)
        try:
            plan_n = len(sorted(td.glob("plan_v*.md"))) if td and td.exists() else 0
        except Exception:
            plan_n = -1
        # LOG-R1-3 fix (2026-04-23 Stage 4): include both legacy "no_acs_defined"
        # token AND new "plan_parse_fail_zero_acs" label so log-parsers grepping
        # for either one still match. Rule-7 + task_complete_gate still emit
        # "no_acs_defined"; backward-compat matters.
        return (False, f"rule-8 block: plan_parse_fail_zero_acs / no_acs_defined "
                       f"(plan_v*.md found={plan_n}; need ## Acceptance Criteria section with verify_cmd per AC)")
    evidence = load_evidence(task_id)
    # U8 TU-4: phase-scoped audit
    phase_scope = _current_phase_filter()
    acs_to_audit = spec.acceptance_criteria
    if phase_scope is not None:
        acs_to_audit, orphans = _filter_acs_by_phase(spec, phase_scope)
        if orphans:
            try:
                from src.core.trace_sink import write_trace_event
                write_trace_event("orphan_ac_blocked", {
                    "task_id": task_id, "phase": phase_scope,
                    "n_orphans": len(orphans),
                })
            except Exception:
                pass
            write_phase_sentinel(task_id, phase_scope, orphans)
            return (False, f"rule-8 block: orphan ACs: {orphans[:5]}; sentinel written")
        if not acs_to_audit:
            return (False, f"rule-8 block: phase_not_found (phase={phase_scope})")
    else:
        # Final monolithic commit: refuse if any phase sentinel pending
        if has_phase_sentinel(task_id):
            return (False, "rule-8 block: phase_audit_pending sentinel exists; resolve per-phase first or use clear-phase-sentinel CLI")
    missing = [ac.id for ac in acs_to_audit if ac.id not in evidence]
    if missing:
        return (False, f"rule-8 block: evidence missing for {missing[:5]}")
    failed = [aid for aid, ev in evidence.items()
              if ev.exit_code != 0 and aid in {ac.id for ac in acs_to_audit}]
    if failed:
        return (False, f"rule-8 block: verify failed for {failed[:5]}")
    # Phase-scoped success: emit phase_commit_done event
    if phase_scope is not None:
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("phase_commit_done", {
                "task_id": task_id, "phase_id": phase_scope,
                "n_acs_verified": len(acs_to_audit),
                "commit_sha": _get_head_sha_short(),
            })
            # Auto-delete sentinel if zero orphan ACs (logic-iter1-3 fix)
            sp = _phase_sentinel_path(task_id)
            if sp is not None and sp.exists():
                # Verify sentinel is not for THIS phase by reading it
                try:
                    sentinel_data = json.loads(sp.read_text(encoding="utf-8"))
                    if int(sentinel_data.get("phase_id", -1)) == phase_scope:
                        sp.unlink()
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
        except Exception:
            pass
        return (True, f"rule-8 ok (phase={phase_scope}): {len(acs_to_audit)} AC(s) verified")
    return (True, f"rule-8 ok: {len(evidence)} AC(s) verified")


# ============================================================================
# 2026-04-26 U3 plan_v4 TU-2: rule-9 Mode-B HMAC self-review governance
# ============================================================================
# Closes OWASP LLM01 self-attestation hole: when Stage 4 sub_agent reviewers
# stall and main_session falls back, rule-9 requires CEO HMAC approval to
# proceed. Decomposed into 3 sub-helpers per T-5 (architect):
#   _iter_findings — yields (path, parsed_dict) for review_findings/*.json
#   _corroborated_sub_agent — bounded scan of traces.jsonl for
#       agent_spawned_for_task event with matching task_id+role within 24h.
#       T-4: 10MB cap, reverse-chronological, 200ms timeout, early-return.
#   _classify_finding — applies S-1 cross-reference downgrade
#       (sub_agent without corroboration → main_session_fallback).
#   _check_mode_b_governance — main entry; 8-state decision tree (state 9
#       removed in v4 per O-2; orthogonal-emission handled at caller site).

def _iter_findings(task_id: str):
    """Yield (path, parsed_dict_or_list) tuples for each parseable JSON
    file in <task_dir>/review_findings/. Logs unparseable but doesn't raise.
    """
    try:
        from src.core.task_dir_layout import task_dir as _td_fn
        td = _td_fn(task_id)
    except Exception:
        return
    if td is None or not td.exists():
        return
    findings_dir = td / "review_findings"
    if not findings_dir.exists() or not findings_dir.is_dir():
        return
    for p in sorted(findings_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # allow-silent: unparseable findings file
        yield (p, data)


_TRACES_MAX_BYTES = 10 * 1024 * 1024   # 10 MB cap (T-4)
_TRACES_WINDOW_SEC = 86400              # 24h window (T-4)
_TRACES_TIMEOUT_MS = 200                # wall-clock timeout (T-4)


def _corroborated_sub_agent(task_id: str, reviewer_role: str) -> bool:
    """T-4 bounded scan: True iff at least one agent_spawned_for_task event
    in last 10MB of <task_dir>/traces.jsonl OR <workspace>/.claude/data/
    traces.jsonl matches (task_id, reviewer_role) within last 24h.

    Bounded: reads at most _TRACES_MAX_BYTES from file tail; iterates
    line-by-line forward (best-effort); early-return on first match;
    wall-clock timeout _TRACES_TIMEOUT_MS as defense-in-depth.

    S-1 defense — without this corroboration, "verified_by=sub_agent"
    self-claim is treated as main_session_fallback (rule-9 engages).
    """
    import time as _time
    start = _time.monotonic()
    cutoff_ts = _time.time() - _TRACES_WINDOW_SEC

    candidate_paths = []
    try:
        from src.core.task_dir_layout import task_dir as _td_fn
        td = _td_fn(task_id)
        if td and td.exists():
            candidate_paths.append(td / "traces.jsonl")
    except Exception:
        pass
    candidate_paths.append(_WORKSPACE / ".claude" / "data" / "traces.jsonl")

    for p in candidate_paths:
        if (_time.monotonic() - start) * 1000 > _TRACES_TIMEOUT_MS:
            return False  # bail; defensive timeout
        if not p.exists():
            continue
        try:
            size = p.stat().st_size
            with open(p, "rb") as f:
                if size > _TRACES_MAX_BYTES:
                    f.seek(size - _TRACES_MAX_BYTES)
                    f.readline()  # advance past partial line
                for line in f:
                    if (_time.monotonic() - start) * 1000 > _TRACES_TIMEOUT_MS:
                        return False
                    try:
                        s = line.decode("utf-8", errors="replace").strip()
                        if not s:
                            continue
                        rec = json.loads(s)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if rec.get("event") != "agent_spawned_for_task":
                        continue
                    payload = rec.get("payload") or rec
                    if payload.get("task_id") != task_id:
                        continue
                    role = payload.get("role") or payload.get("agent_type") or ""
                    if reviewer_role not in str(role):
                        continue
                    ts = rec.get("ts") or payload.get("ts") or 0
                    try:
                        if float(ts) >= cutoff_ts:
                            return True
                    except (TypeError, ValueError):
                        # ts may be ISO string; conservative accept
                        return True
        except OSError:
            continue
    return False


def _classify_finding(finding_dict, task_id: str):
    """Returns (verified_by_resolved, reviewer_role).

    Applies S-1 cross-reference: sub_agent claim without corroboration →
    main_session_fallback. Unknown values pass through verbatim.
    """
    if not isinstance(finding_dict, dict):
        return ("unknown", "")
    role = (
        finding_dict.get("reviewer_role")
        or finding_dict.get("role")
        or finding_dict.get("agent_type")
        or ""
    )
    claimed = finding_dict.get("verified_by")
    if claimed is None:
        return ("__missing__", role)
    if claimed == "sub_agent":
        if _corroborated_sub_agent(task_id, str(role)):
            return ("sub_agent", role)
        return ("main_session_fallback", role)  # S-1 downgrade
    return (str(claimed), role)


def _detect_commit_author(active_tid: Optional[str]) -> Tuple[str, str]:
    """U3 plan_v3 TU-1: 3-tier independent corroboration of commit_author.

    Returns (label, reason) where label ∈ {"main_session","sub_agent",
    "ambiguous"} and reason is human-readable.

    Tier 1 (signal-strong): scan most-recent 30s of agent_spawned_for_task
        events in <task_dir>/traces.jsonl AND <workspace>/.claude/data/
        traces.jsonl. UNION events; take MAX timestamp; task_dir wins on tie.
        Bounded 1MB tail.
    Tier 2 (medium, SEC-2 hardened): subprocess git config user.email with
        cwd=_MEMEXA_ROOT + minimal env (PATH only) to prevent PATH-hijack.
        Match against MEMEXA_CEO_EMAIL env override or hardcoded CEO email.
    Tier 3 (signal-weak): MEMEXA_ACTIVE_SUB_AGENT_ID env var presence.

    Collapse rules:
        (tier1=no_recent_spawn) AND (tier2=ceo_email) AND (tier3=no_env)
            → ("main_session", "all_3_tier_main_session")
        (tier1=sub_agent_active) AND (tier3=sub_agent_env_set)
            → ("sub_agent", "tier1_3_sub_agent")
        otherwise → ("ambiguous", f"{tier1}|{tier2}|{tier3}")

    Defensive: ANY exception → return ("ambiguous", "infra_error_<TYPE>")
    rather than raise (caller simplification).
    """
    try:
        import time as _time
        cutoff_ts = _time.time() - 30  # 30s window for "recent"

        # Tier 1: scan dual-candidate traces.jsonl with 1MB bounded tail
        tier1 = "no_recent_spawn"
        candidates = []
        try:
            from src.core.task_dir_layout import task_dir as _td_fn
            if active_tid:
                td = _td_fn(active_tid)
                if td and td.exists():
                    candidates.append(td / "traces.jsonl")
        except Exception:
            pass
        candidates.append(_WORKSPACE / ".claude" / "data" / "traces.jsonl")

        max_spawn_ts = 0.0
        for p in candidates:
            if not p.exists():
                continue
            try:
                size = p.stat().st_size
                with open(p, "rb") as f:
                    if size > 1024 * 1024:  # 1MB tail
                        f.seek(size - 1024 * 1024)
                        f.readline()  # skip partial
                    for line in f:
                        try:
                            s = line.decode("utf-8", errors="replace").strip()
                            if not s:
                                continue
                            rec = json.loads(s)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if rec.get("event") != "agent_spawned_for_task":
                            continue
                        # logic-iter1-LOW2 fix: use explicit None check to allow ts=0
                        ts = rec.get("ts")
                        if ts is None:
                            ts = (rec.get("payload") or {}).get("ts")
                        if ts is None:
                            ts = 0
                        try:
                            ts_f = float(ts)
                        except (TypeError, ValueError):
                            continue
                        if ts_f > max_spawn_ts:
                            max_spawn_ts = ts_f
            except OSError:
                continue
        if max_spawn_ts >= cutoff_ts:
            tier1 = "sub_agent_active"

        # Tier 2 (SEC-2 hardened): git config user.email with explicit cwd + minimal env
        tier2 = "no_git_email"
        try:
            r = subprocess.run(
                ["git", "config", "--get", "user.email"],
                cwd=str(_MEMEXA_ROOT),
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                },
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0:
                got_email = (r.stdout or "").strip()
                ceo_email = os.environ.get("MEMEXA_CEO_EMAIL", "")
                if got_email == ceo_email:
                    tier2 = "ceo_email"
                elif got_email:
                    tier2 = "non_ceo_email"
        except (OSError, subprocess.SubprocessError, ValueError):
            pass  # tier2 stays "no_git_email"

        # Tier 3: MEMEXA_ACTIVE_SUB_AGENT_ID env var
        tier3 = "no_env" if not os.environ.get("MEMEXA_ACTIVE_SUB_AGENT_ID", "") else "sub_agent_env_set"

        # Collapse
        if tier1 == "no_recent_spawn" and tier2 == "ceo_email" and tier3 == "no_env":
            return ("main_session", "all_3_tier_main_session")
        if tier1 == "sub_agent_active" and tier3 == "sub_agent_env_set":
            return ("sub_agent", "tier1_3_sub_agent")
        return ("ambiguous", f"{tier1}|{tier2}|{tier3}")
    except Exception as exc:
        return ("ambiguous", f"infra_error_{type(exc).__name__}")


def _emit_commit_author_trace(event_name: str, tier_reason: str,
                              n_main_session: int, autopilot: bool) -> None:
    """U3 plan_v3 TU-2: redacted trace for commit_author 3-tier events.

    Events: main_session_detection_ambiguous,
            commit_author_finding_mismatch,
            suspicious_pure_sub_agent_with_main_session_signals.
    """
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event_name, {
            "tier_reason": str(tier_reason)[:200],
            "n_main_session": int(n_main_session),
            "autopilot": bool(autopilot),
        })
    except Exception:
        pass


def _check_mode_b_governance(active_tid: Optional[str],
                             autopilot: bool) -> Tuple[bool, str]:
    """Rule-9 main entry. Returns (allow, reason).

    8-state decision tree (state 9 removed in v4 per O-2 — caller-side
    orthogonal-emission handles "stage4_already_blocked"):

      1/2: no findings dir or empty → (True, "no_findings_skip")
      3:   all sub_agent + corroborated → (True, "sub_agent_skip")
      4:   any verified_by missing → (True, "legacy_no_verified_by")
      5:   any main_session_fallback + key missing:
           autopilot=True  → (False, "key_missing_block")
           autopilot=False → (True, "key_missing_fail_open")
      6:   any main_session_fallback + key + verify_approval ok →
           (True, "main_session_with_approval")
      7:   any main_session_fallback + key + verify_approval fail:
           "key_rotation_detected" → ALWAYS (False, "key_rotation_required_reapproval")
           else autopilot=True  → (False, "main_session_no_approval")
                autopilot=False → (True, "main_session_no_approval_fail_open")
      8:   verified_by has unknown value → (True, "unknown_verified_by")

    Trace event "mode_b_governance_check" emitted with redacted payload
    {decision, has_approval, autopilot, n_findings, n_main_session,
     n_corroborated_sub_agent} — NO key_fingerprint, NO HMAC bytes (S-4).
    """
    if not active_tid:
        return (True, "no_findings_skip")

    findings = list(_iter_findings(active_tid))
    if not findings:
        _emit_mode_b_trace("no_findings_skip", False, autopilot, 0, 0, 0)
        return (True, "no_findings_skip")

    # Classify each finding
    n_main_session = 0
    n_sub_agent = 0
    has_legacy_missing = False
    has_unknown = False
    for _path, data in findings:
        # Support top-level dict OR list-of-dicts
        items = data if isinstance(data, list) else [data]
        for item in items:
            resolved, _role = _classify_finding(item, active_tid)
            if resolved == "__missing__":
                has_legacy_missing = True
            elif resolved == "main_session_fallback":
                n_main_session += 1
            elif resolved == "sub_agent":
                n_sub_agent += 1
            else:
                has_unknown = True

    # State 4: any legacy schema → fail_open
    if has_legacy_missing:
        _emit_mode_b_trace("legacy_no_verified_by", False, autopilot,
                           len(findings), n_main_session, n_sub_agent)
        return (True, "legacy_no_verified_by")

    # ============================================================
    # U3 plan_v3 TU-2: 3-tier commit_author detection (LOGIC-2 fix call site)
    # CALL GRAPH NOTES: called from commit_gate() → _check_mode_b_governance()
    # only; no re-entrant callers. Inserted AFTER state-4 legacy return AND
    # BEFORE state-5/6/7 n_main_session>0 branch so the helper is reachable
    # for BOTH n_main_session==0 (forged-sub_agent attack) AND >0 paths.
    # ============================================================
    detected, det_reason = _detect_commit_author(active_tid)

    # Branch matrix per plan_v3 §3 TU-2 (8 cells):
    # n_main_session == 0 + detected == "main_session" → forged-sub_agent attack
    if n_main_session == 0 and detected == "main_session":
        _emit_commit_author_trace(
            "suspicious_pure_sub_agent_with_main_session_signals",
            det_reason, n_main_session, autopilot)
        if autopilot:
            return (False, "suspicious_sub_agent_block")
        return (True, "suspicious_sub_agent_fail_open")

    # n_main_session > 0 paths handled by 3-tier matrix
    if n_main_session > 0:
        # detected == "sub_agent" + finding claims main_session_fallback → mismatch
        if detected == "sub_agent":
            _emit_commit_author_trace(
                "commit_author_finding_mismatch",
                det_reason, n_main_session, autopilot)
            if autopilot:
                return (False, "author_mismatch_block")
            return (True, "author_mismatch_block_fail_open")

        # detected == "ambiguous" → BLOCK (autopilot=True) or fail_open IMMEDIATE
        if detected == "ambiguous":
            _emit_commit_author_trace(
                "main_session_detection_ambiguous",
                det_reason, n_main_session, autopilot)
            if autopilot:
                return (False, "ambiguous_commit_author_block")
            # LOGIC-1 fix: immediate return, do NOT fall through to state 5/6/7
            return (True, "ambiguous_fail_open")

        # detected == "main_session" → fall through to state 5/6/7 (verify_approval)

    # State 5/6/7: main_session_fallback path (only reached if detected="main_session")
    if n_main_session > 0:
        try:
            from src.core import ceo_approve as _ca
            ok, reason = _ca.verify_approval(active_tid)
        except Exception as exc:
            ok = False
            reason = f"infra_error_{type(exc).__name__}"

        if ok:
            _emit_mode_b_trace("main_session_with_approval", True, autopilot,
                               len(findings), n_main_session, n_sub_agent)
            return (True, "main_session_with_approval")

        # State 7 sub-branches mapping
        reason_lc = str(reason).lower()
        if "key_rotation_detected" in reason_lc or "fingerprint" in reason_lc:
            _emit_mode_b_trace("main_session_fingerprint_rotated", False,
                               autopilot, len(findings), n_main_session,
                               n_sub_agent)
            return (False, "key_rotation_required_reapproval")  # S-5 always blocks

        # State 5: key missing
        key_missing = (
            "env not set" in reason_lc
            or "too short" in reason_lc
            or "key not set" in reason_lc
        )
        if key_missing:
            if autopilot:
                _emit_mode_b_trace("key_missing_block", False, autopilot,
                                   len(findings), n_main_session, n_sub_agent)
                return (False, "key_missing_block")
            _emit_mode_b_trace("key_missing_fail_open", False, autopilot,
                               len(findings), n_main_session, n_sub_agent)
            return (True, "key_missing_fail_open")

        # State 7 default: no approval / tampered / unreadable
        if "tampered" in reason_lc or "signature mismatch" in reason_lc:
            decision = "main_session_tampered"
        elif "unreadable" in reason_lc:
            decision = "main_session_record_unreadable"
        else:
            decision = "main_session_no_approval"

        if autopilot:
            _emit_mode_b_trace(decision, False, autopilot, len(findings),
                               n_main_session, n_sub_agent)
            return (False, decision)
        _emit_mode_b_trace(decision + "_fail_open", False, autopilot,
                           len(findings), n_main_session, n_sub_agent)
        return (True, decision + "_fail_open")

    # State 8: unknown verified_by value
    if has_unknown and n_sub_agent == 0:
        _emit_mode_b_trace("unknown_verified_by", False, autopilot,
                           len(findings), 0, 0)
        return (True, "unknown_verified_by")

    # State 3: all sub_agent (with corroboration applied)
    _emit_mode_b_trace("sub_agent_skip", False, autopilot, len(findings),
                       0, n_sub_agent)
    return (True, "sub_agent_skip")


def _emit_mode_b_trace(decision: str, has_approval: bool, autopilot: bool,
                       n_findings: int, n_main_session: int,
                       n_corroborated_sub_agent: int) -> None:
    """S-4 redacted payload: NO key_fingerprint, NO reason text, NO HMAC."""
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("mode_b_governance_check", {
            "decision": decision,
            "has_approval": bool(has_approval),
            "autopilot": bool(autopilot),
            "n_findings": int(n_findings),
            "n_main_session": int(n_main_session),
            "n_corroborated_sub_agent": int(n_corroborated_sub_agent),
        })
    except Exception:
        pass  # allow-silent observability


def _get_head_sha_short() -> Optional[str]:
    """Return short (12-char) HEAD commit SHA. Fail-soft: returns None on any error.

    Defensive: accepts both bytes (real subprocess) and str (test mocks
    that swap subprocess.run with a fake whose .stdout is already str).
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(_MEMEXA_ROOT),
            timeout=5,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(out, bytes):
        try:
            out = out.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(out, str):
        return None
    return out.strip() or None


def _resolve_active_tid(
    autopilot: bool,
    commit_sha: Optional[str] = None,
) -> Optional[str]:
    """TU-2 (plan_v1, 2026-04-25) — single helper for active_tid resolution.

    Tier 1 (env channel): ``task_binding.get_active_task_id()`` — primary,
        fast hot path.
    Tier 2 (autopilot channel): ``_autopilot_flag.flag_info().task_id`` —
        durable, subprocess-safe; fires ONLY when ``autopilot=True``.

    Returns None if both empty. Emits ``active_tid_recovered`` trace event
    when Tier 2 succeeds (observability for the env-loss fallback path).

    Defense-in-depth: rejects 'unknown_session' sentinel even though TU-1
    set_flag now refuses to write it — guards against stale flag files
    written by pre-TU-1 callers.
    """
    # Tier 1: env channel
    try:
        from src.core.task_binding import get_active_task_id
        tid = get_active_task_id()
        if tid:
            return tid
    except Exception:  # pragma: no cover — defensive
        pass

    # Tier 2: autopilot flag fallback (only when autopilot=on)
    if autopilot:
        try:
            from src.core._autopilot_flag import flag_info
            info = flag_info() or {}
            tid = info.get("task_id")
            if tid and tid != "unknown_session":
                _emit_recovered_trace(tid, commit_sha)
                return tid
        except Exception:  # pragma: no cover
            pass

    return None


def _emit_recovered_trace(
    active_tid: str,
    commit_sha: Optional[str],
) -> None:
    """Emit ``active_tid_recovered`` event when Tier-2 fallback fires."""
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("active_tid_recovered", {
            "source": "autopilot_flag",
            "active_tid": active_tid,
            "commit_sha": commit_sha or "",
        })
    except Exception:  # pragma: no cover
        pass


def _emit_skip_trace(
    gate: str,
    reason: str,
    active_tid: Optional[str],
    autopilot: bool,
    commit_sha: Optional[str],
) -> None:
    """Emit a gate_skipped trace event. Sanitizes reason per S-S5.

    Fail-soft: only catches (ImportError, OSError) — never breaks commit gate.
    """
    try:
        clean_reason = re.sub(r"[\x00-\x1f\x7f]", "", reason)[:200]
        from src.core.trace_sink import write_trace_event
        write_trace_event("gate_skipped", {
            "gate": gate,
            "reason": clean_reason,
            "active_tid": active_tid or "",
            "autopilot": autopilot,
            "commit_sha": commit_sha or "",
        })
    except (ImportError, OSError):
        pass  # fail-soft: trace failure must never break commit gate


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def _emit_trace_unhealthy(task_id: str, gate: str, reason: str) -> None:
    _emit_trace("gate_data_source_unhealthy",
                {"task_id": task_id, "gate": gate, "reason": reason})


def _check_strategic_output() -> Optional[str]:
    """Check if strategic output was produced after significant work."""
    try:
        harness = json.loads(_HARNESS.read_text(encoding="utf-8"))
        # Count actual commits today via git log (total_commits_today field was never populated)
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--since=midnight", "--format=%h"],
                capture_output=True, text=True, cwd=str(_MEMEXA_ROOT), timeout=5,
            )
            commits_today = len(result.stdout.strip().splitlines()) if result.returncode == 0 else 0
        except Exception:
            commits_today = 0
        if commits_today < 3:
            return None

        approvals_file = _DATA / "pending_approvals.json"
        if approvals_file.exists():
            import os as _os
            mtime = _os.path.getmtime(str(approvals_file))
            harness_mtime = _os.path.getmtime(str(_HARNESS))
            if mtime >= harness_mtime - 300:
                return None

        action_items = harness.get("action_items_for_user", [])
        if action_items:
            return None

        return (
            f"STRATEGIC_OUTPUT_MISSING: {commits_today} commits this session but no "
            f"strategic planning output."
        )
    except Exception:
        return None


def run_session_end_check() -> int:
    """Session end audit: strategic output + memory + harness + uncommitted."""
    print("[SESSION END v6.0] Persistence audit...")

    issues = []

    # Strategic output check
    strategic_issue = _check_strategic_output()
    if strategic_issue:
        issues.append(strategic_issue)

    mem_ok, mem_issues = check_memory_integrity()
    issues.extend(mem_issues)

    harness_ok, harness_issues = check_harness_freshness()
    issues.extend(harness_issues)

    # Uncommitted changes
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(_MEMEXA_ROOT), timeout=10,
        )
        uncommitted = [l for l in result.stdout.strip().splitlines() if l.strip()]
        if uncommitted:
            py_count = sum(1 for l in uncommitted if ".py" in l)
            other_count = len(uncommitted) - py_count
            issues.append(f"UNCOMMITTED: {py_count} Python + {other_count} other files")
    # allow-silent: observability fail-soft
    except Exception:
        pass

    # Release-gate check
    trigger_msg = _check_release_gate_trigger()
    if trigger_msg:
        issues.append(f"RELEASE_PENDING: {trigger_msg}")

    if issues:
        print("[SESSION END v6.0] ISSUES FOUND:")
        for i in issues:
            print(f"  - {i}")
        return 1

    print("[SESSION END v6.0] All checks PASS")
    return 0


_ANSI_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_env_phase_value(raw: Optional[str]) -> Optional[str]:
    """Strip control chars + truncate to 20 chars (security-iter2-4 LOW fix).

    Returns None for None or empty-after-strip; caller treats None as "unset".
    """
    if raw is None:
        return None
    cleaned = _ANSI_CTRL_RE.sub("", raw).strip()
    if not cleaned:
        return None
    return cleaned[:20]


def _resolve_phase(
    cli_phase: Optional[int] = None,
    env_phase: Optional[str] = None,
    template_phase: Optional[int] = None,
) -> Tuple[Optional[int], str, Dict[str, Optional[str]]]:
    """U8 plan_v2 TU-3: 4-tier phase resolution.

    Order: tier-1 CLI flag > tier-2 MEMEXA_AUTOPILOT_PHASE env >
    tier-3 plan_template `current_phase` > None (caller decides).

    Per HARD RULE feedback_value_resolution_chain_explicit + feedback_priority_
    inverted_fallback_2x2_matrix. When 2+ tiers set with different values,
    tier-1 wins AND emits `phase_value_disagreement` trace event.

    Returns (resolved_phase, source, diag_payload) tuple.
    `source` ∈ {"cli", "env", "template", "none"}.
    """
    # security-iter2-4: sanitize raw env value before any logic
    env_clean = _sanitize_env_phase_value(env_phase)
    env_int: Optional[int] = None
    if env_clean is not None:
        try:
            env_int = int(env_clean)
        except (ValueError, TypeError):
            env_int = None
    diag: Dict[str, Optional[str]] = {
        "cli": str(cli_phase) if cli_phase is not None else None,
        "env": env_clean,
        "template": str(template_phase) if template_phase is not None else None,
    }
    # Detect disagreement: collect all non-None resolved values
    candidates = []
    if cli_phase is not None:
        candidates.append(("cli", cli_phase))
    if env_int is not None:
        candidates.append(("env", env_int))
    if template_phase is not None:
        candidates.append(("template", template_phase))
    distinct_values = {v for _, v in candidates}
    if len(distinct_values) >= 2:
        diag["disagreement"] = "yes"
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("phase_value_disagreement", {
                "cli": diag["cli"],
                "env": diag["env"],
                "template": diag["template"],
                "chosen": str(cli_phase) if cli_phase is not None
                          else str(env_int) if env_int is not None
                          else str(template_phase),
            })
        except Exception:  # fail-soft on trace_sink import failure
            pass
    # Resolution order
    if cli_phase is not None:
        return (cli_phase, "cli", diag)
    if env_int is not None:
        return (env_int, "env", diag)
    if template_phase is not None:
        return (template_phase, "template", diag)
    return (None, "none", diag)


def _phase_sentinel_path(task_id: str) -> Optional[Path]:
    """Resolve sentinel path with traversal guard (security-iter2-1 HIGH fix).

    Calls plan_versioning._validate_task_id which enforces _VALID_TASK_ID_PATTERN
    + resolve().relative_to(tasks_root) defense. Returns None on rejection.
    """
    try:
        from src.core import plan_versioning
        td = plan_versioning._validate_task_id(task_id)
    except (ValueError, OSError, ImportError):
        return None
    return td / "_phase_audit_pending.json"


def write_phase_sentinel(task_id: str, phase_id: int, orphan_acs: List[str]) -> bool:
    """Write _phase_audit_pending.json sentinel + emit trace."""
    p = _phase_sentinel_path(task_id)
    if p is None:
        return False
    payload = {
        "phase_id": phase_id,
        "orphan_acs": orphan_acs,
        "ts": time.time(),
    }
    try:
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError:
        return False
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("phase_audit_pending", {
            "task_id": task_id, "phase_id": phase_id,
            "n_orphan_acs": len(orphan_acs),
        })
    except Exception:
        pass
    return True


def has_phase_sentinel(task_id: str) -> bool:
    """Return True if sentinel exists (final monolithic commit must refuse)."""
    p = _phase_sentinel_path(task_id)
    return p is not None and p.exists()


def clear_phase_sentinel(task_id: str, reason: str) -> int:
    """Manual-override clear-phase-sentinel CLI handler.

    Requires reason >= 40 chars (audit trail). Emits phase_sentinel_cleared trace.
    Returns 0 on success, non-zero on rejection.
    """
    if len(reason) < 40:
        print(f"[clear-phase-sentinel] reason too short ({len(reason)} chars; need >=40)",
              file=sys.stderr)
        return 64
    p = _phase_sentinel_path(task_id)
    if p is None:
        print(f"[clear-phase-sentinel] task_id rejected (traversal guard): {task_id!r}",
              file=sys.stderr)
        return 65
    if not p.exists():
        print(f"[clear-phase-sentinel] no sentinel at {p}")
        return 0  # idempotent
    try:
        p.unlink()
    except OSError as e:
        print(f"[clear-phase-sentinel] unlink failed: {e}", file=sys.stderr)
        return 3
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("phase_sentinel_cleared", {
            "task_id": task_id,
            "reason": reason[:200],
        })
    except Exception:
        pass
    print(f"[clear-phase-sentinel] cleared {p}")
    return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session Gate v6.0")
    parser.add_argument("action", choices=[
        "check", "commit-gate", "session-end",
        "phase-commit", "clear-phase-sentinel",
    ])
    parser.add_argument("--phase", type=int, default=None,
                        help="phase id for phase-commit")
    parser.add_argument("--task-id", default=None,
                        help="explicit task_id (for clear-phase-sentinel)")
    parser.add_argument("--reason", default="",
                        help="reason text (clear-phase-sentinel)")
    parser.add_argument("positional", nargs="?", default=None,
                        help="positional arg: phase_id for phase-commit, task_id for clear-phase-sentinel")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.action == "check":
        mem_ok, mi = check_memory_integrity()
        har_ok, hi = check_harness_freshness()
        for i in mi + hi:
            print(f"  {i}")
        all_ok = mem_ok and har_ok
        print("PASS" if all_ok else "ISSUES FOUND")
        sys.exit(0 if all_ok else 1)
    elif args.action == "commit-gate":
        sys.exit(run_commit_gate())
    elif args.action == "session-end":
        sys.exit(run_session_end_check())
    elif args.action == "phase-commit":
        # phase from --phase flag OR positional fallback
        phase = args.phase
        if phase is None and args.positional is not None:
            try:
                phase = int(args.positional)
            except ValueError:
                print(f"[phase-commit] invalid phase: {args.positional!r}",
                      file=sys.stderr)
                sys.exit(64)
        env_phase = os.environ.get("MEMEXA_AUTOPILOT_PHASE")
        resolved, source, diag = _resolve_phase(phase, env_phase, None)
        if resolved is None:
            print("[phase-commit] no phase resolved (CLI/env/template all empty)",
                  file=sys.stderr)
            sys.exit(64)
        print(f"[phase-commit] phase={resolved} source={source} diag={diag}")
        sys.exit(run_commit_gate(phase=resolved))
    elif args.action == "clear-phase-sentinel":
        tid = args.task_id or args.positional
        if not tid:
            print("[clear-phase-sentinel] --task-id (or positional) required",
                  file=sys.stderr)
            sys.exit(64)
        sys.exit(clear_phase_sentinel(tid, args.reason or ""))


if __name__ == "__main__":
    main()
