"""
PreToolUse Gate — Layer 1 enforcement for Write/Edit operations.

Claude Code Hook Protocol:
  - Input: stdin JSON with {tool_name, tool_input, session_id, ...}
  - Output: stdout JSON with {hookSpecificOutput: {permissionDecision, ...}}
  - permissionDecision: "allow" | "deny" | "ask"

Checks (v6.0 — CC-Native simplified):
  1. Root directory file control (§3.5): blocks *_REPORT.md, *_PLAN.md, *_INDEX.* in workspace root
  2. L3 protected paths (§二c): asks confirmation for polymarket, CLAUDE.md, settings.json
  3. ast.parse validation: for .py Write operations, validates syntax before write

Called by: PreToolUse hook in settings.json, matcher "Write|Edit"
"""


import ast
import json
import os
import re
import sys
from pathlib import Path


def _find_workspace() -> Path:
    """Resolve workspace root robustly (Windows CJK path safe).

    Path(__file__).resolve() garbles CJK chars on Windows subprocess pipes.
    os.getcwd() is reliable because Claude Code sets cwd to workspace root.
    """
    marker = Path(".claude") / "config" / "settings.json"
    # Strategy 1: cwd (hooks run from workspace root)
    try:
        cwd = Path(os.getcwd())
        if (cwd / marker).exists():
            return cwd
    except OSError:
        pass
    # Strategy 2: __file__ parent chain (fixed depth: core/memexa/memexa/workspace)
    try:
        candidate = Path(__file__).parent.parent.parent.parent
        if (candidate / marker).exists():
            return candidate
    except OSError:
        pass
    # Strategy 3: walk parent dirs of __file__ (handles variable depth / CJK paths)
    try:
        p = Path(__file__).resolve()
        for _ in range(8):  # max 8 levels up
            p = p.parent
            if (p / marker).exists():
                return p
    except OSError:
        pass
    return Path(os.getcwd())


_WORKSPACE = _find_workspace()

# ================================================================
# Hook response helpers
# ================================================================

def _respond(decision: str, reason: str = "", context: str = "", rule: str = "default", target: str = ""):
    """Output hook decision and exit.

    [V-4/Item #2] Also emit gate_decision event with dedup rate-limiting.
    """
    # Emit gate event (non-blocking, rate-limited for allows)
    try:
        # Lazy import to avoid circular dependency
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from src.core._hook_utils import log_gate_decision  # noqa: E402
        log_gate_decision(
            gate="pretool_gate",
            rule=rule,
            decision=decision,
            target=target,
            reason=reason,
        )
    except Exception:
        pass
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = reason
    if context:
        output["hookSpecificOutput"]["additionalContext"] = context
    print(json.dumps(output))
    sys.exit(0)


# ================================================================
# Rule 1: Root directory file control (CLAUDE.md §3.5)
# ================================================================

ROOT_BLOCKED_PATTERNS = [
    re.compile(r".*_REPORT\.md$", re.IGNORECASE),
    re.compile(r".*_PLAN\.md$", re.IGNORECASE),
    re.compile(r".*_INDEX\..+$", re.IGNORECASE),
    re.compile(r".*_ANALYSIS\.md$", re.IGNORECASE),
    re.compile(r"REPORT_.*\.md$", re.IGNORECASE),
    re.compile(r"PLAN_.*\.md$", re.IGNORECASE),
    re.compile(r"INDEX_.*\..+$", re.IGNORECASE),
]

ROOT_ALLOWED = {
    "CLAUDE.md", "MEMORY.md", "TASKS.md", "README.md",
    "日程表.html", "schedule_data.json",
}


_WS_DIR_NAME = "claude workspace"  # ASCII, encoding-safe


def _get_relative(file_path: str) -> str:
    """Get workspace-relative path, encoding-safe.

    Strategy 1: Find ASCII workspace dir name in path (encoding-safe on Windows CJK).
    Strategy 2: Fall back to _WORKSPACE string prefix match (for tests with fake workspace).
    Strategy 3: Bare filename (already relative).
    """
    normalized = file_path.replace("\\", "/")
    # Strategy 1: ASCII anchor (production - avoids CJK encoding mismatch)
    marker = _WS_DIR_NAME + "/"
    idx = normalized.find(marker)
    if idx >= 0:
        return normalized[idx + len(marker):]
    # Strategy 2: _WORKSPACE prefix match (tests with tmp dirs)
    ws_str = str(_WORKSPACE).replace("\\", "/")
    if not ws_str.endswith("/"):
        ws_str += "/"
    if normalized.startswith(ws_str):
        return normalized[len(ws_str):]
    # Strategy 3: bare filename
    if "/" not in file_path and "\\" not in file_path:
        return file_path
    # Fallback: return the full normalized path rather than empty string.
    # An empty string causes check_root_directory and check_l3_protection
    # to silently skip all protections.
    return normalized


# ================================================================
# Rule 11 helper: tool-triggered memory prime (inline, fail-soft)
# Used by both Rule 10 (deny path) and Rule 11 (allow path).
# ================================================================

# (regex, keywords list, memory file to mention)
_PRIME_TRIGGERS = [
    (re.compile(r"Start-Process.*\.exe", re.IGNORECASE),
     ["probe before gui", "service", "port"],
     "feedback_probe_before_gui_recommend.md"),
    (re.compile(r"\bdocker\s+desktop\s+(start|restart|engine\s+use)\b", re.IGNORECASE),
     ["docker desktop", "wsl poll", "engine"],
     "feedback_wsl_poll_zombie_trap.md"),
    (re.compile(r"\bdocker\s+(pull|build)\s+", re.IGNORECASE),
     ["install precheck", "version check"],
     "feedback_install_precheck.md"),
    (re.compile(r"\bwinget\s+install\b", re.IGNORECASE),
     ["install precheck"],
     "feedback_install_precheck.md"),
    (re.compile(r"\bwsl(\.exe)?\s+(-d|--distribution|--list)\b", re.IGNORECASE),
     ["wsl poll", "zombie"],
     "feedback_wsl_poll_zombie_trap.md"),
    (re.compile(r"\bpip\s+install\b", re.IGNORECASE),
     ["install precheck"],
     "feedback_install_precheck.md"),
]

_PRIME_DEADLINE_MS = 500


def _inline_prime(cmd: str, preferred_memory: str = "") -> str:
    """Look up memory patterns for `cmd`. Fail-soft on any exception or timeout.
    Returns "" if nothing applies or prime exceeds deadline."""
    triggered_kws: list = []
    mem_files: list = []
    for pat, kws, mem in _PRIME_TRIGGERS:
        if pat.search(cmd):
            triggered_kws.extend(kws)
            if mem and mem not in mem_files:
                mem_files.append(mem)
    if preferred_memory and preferred_memory not in mem_files:
        mem_files.insert(0, preferred_memory)
    if not triggered_kws and not mem_files:
        return ""

    import time as _t
    t0 = _t.monotonic()
    hits_text = ""
    try:
        from src.core.pattern_extractor import prime as _prime
        if triggered_kws:
            results = _prime(keywords=triggered_kws, limit=3, write_usage_count=False)
            if (_t.monotonic() - t0) * 1000 > _PRIME_DEADLINE_MS:
                results = []  # exceeded deadline → soft-drop
            if results:
                frags = []
                for r in results:
                    # recommendation ~120 chars; fact ~200 chars
                    frags.append(f"- {getattr(r, 'recommendation', '')[:120]}")
                hits_text = "; ".join(frags)[:500]
    except Exception:
        hits_text = ""  # fail-soft

    parts = []
    if mem_files:
        parts.append("see memory: " + ", ".join(mem_files))
    if hits_text:
        parts.append(hits_text)
    return " | ".join(parts)[:800]


def check_root_directory(file_path: str) -> tuple:
    """Returns (allowed: bool, reason: str)"""
    relative = _get_relative(file_path)
    if not relative:
        return True, ""
    # Check if file is in root (no directory separator)
    if "/" in relative:
        return True, ""
    filename = relative
    if filename in ROOT_ALLOWED:
        return True, ""
    for pattern in ROOT_BLOCKED_PATTERNS:
        if pattern.match(filename):
            return False, (
                f"BLOCKED: '{filename}' violates root directory rules (CLAUDE.md §3.5). "
                f"Use archive/reports/ or memexa/ subdirectories instead."
            )
    return True, ""


# ================================================================
# Rule 2: L3 protected paths (CLAUDE.md §二c)
# ================================================================

L3_PROTECTED_PATHS = [
    ("CLAUDE.md", "行为规范文件 — 需要 CEO 确认"),
    ("polymarket-agent/", "交易系统 — 需要 CEO 确认"),
    ("schedule_data.json", "日程数据 — 需要 CEO 确认"),
    ("日程表.html", "日程应用 — 需要 CEO 确认"),
    (".claude/config/settings.json", "Hook/权限配置 — 需要 CEO 确认"),
    (".claude/config/settings.local.json", "本地配置 — 需要 CEO 确认"),
]


def check_l3_protection(file_path: str) -> tuple:
    """Returns (allowed: bool, reason: str)"""
    relative = _get_relative(file_path)
    if not relative:
        return True, ""
    relative = relative.replace("\\", "/")
    for pattern, desc in L3_PROTECTED_PATHS:
        if relative == pattern or relative.startswith(pattern):
            return False, f"L3 PROTECTED: {desc} (path: {relative})"
    return True, ""


# ================================================================
# Rule 3: ast.parse for Python files (Write only)
# ================================================================

def check_python_syntax(content: str, file_path: str) -> tuple:
    """Returns (valid: bool, reason: str)"""
    if not file_path.endswith(".py"):
        return True, ""
    try:
        ast.parse(content)
        return True, ""
    except SyntaxError as e:
        return False, (
            f"SYNTAX ERROR in {Path(file_path).name} line {e.lineno}: {e.msg}. "
            f"Fix the syntax before writing."
        )


# ================================================================
# Main
# ================================================================

_SUBAGENT_SPAWN_PATTERNS = [
    re.compile(r"\bclaude\s+-p\b"),
    re.compile(r"\bAgent\s*\("),
    re.compile(r"python\s+-m\s+memexa\.[a-z_]*executor", re.IGNORECASE),
    re.compile(r"\bspawn_(?:agent|subagent)\b"),
]


def _resolve_active_tid_for_budget() -> "str | None":
    """Resolve active task_id for budget gate. Tier-1 env / Tier-2 flag / Tier-3 None.

    Per HARD RULE feedback_value_resolution_chain_explicit: enumerate sources.
    Tier-1: MEMEXA_ACTIVE_TASK_ID env var (set by stripped-env subprocess paths)
    Tier-2: <workspace>/.claude/harness/autopilot_active.json (flag file)
    Tier-3: None (no active task; allow all)
    """
    tid = os.environ.get("MEMEXA_ACTIVE_TASK_ID")
    if tid:
        return tid.strip() or None
    try:
        ws = _find_workspace()
        flag = ws / ".claude" / "harness" / "autopilot_active.json"
        if flag.exists():
            data = json.loads(flag.read_text(encoding="utf-8"))
            tid_val = data.get("task_id")
            if isinstance(tid_val, str) and tid_val:
                return tid_val
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _check_cost_budget_block(cmd: str) -> "str | None":
    """Rule 12 helper. Returns deny-reason string if blocked, else None.

    Logic:
      1. Check if cmd matches any subagent-spawn pattern. If not → None (allow).
      2. Resolve active_tid (Tier-1/2/3). If None → None (allow; no context).
      3. Check <task_dir>/cost_budget_blocked flag file. If absent → None.
      4. Otherwise return human-readable reason.
    """
    if not cmd or not isinstance(cmd, str):
        return None
    if not any(pat.search(cmd) for pat in _SUBAGENT_SPAWN_PATTERNS):
        return None  # not a subagent-spawn pattern; never block
    tid = _resolve_active_tid_for_budget()
    if not tid:
        return None  # no active task; budget gate doesn't apply
    try:
        ws = _find_workspace()
        flag = ws / ".claude" / "harness" / "tasks" / tid / "cost_budget_blocked"
    except OSError:
        return None
    if not flag.exists():
        return None
    # Read flag content for the timestamp (best-effort)
    ts = ""
    try:
        first_line = flag.read_text(encoding="utf-8").splitlines()[:1]
        if first_line:
            ts = first_line[0]
    except OSError:
        pass
    return (
        f"COST BUDGET EXCEEDED for task {tid} (flag set at {ts or 'unknown'}). "
        "Subagent-spawn-pattern command denied. Increase task_spec.cost_budget_usd or "
        "complete the task before spawning new sub-agents. "
        "Run: python -m src.core.cost_meter summary "
        f"{tid}"
    )


# Rule 17 (TU-7 backfill 2026-05-02): Sonnet quarantine cap + HMAC guard.
# Backfill writes Sonnet-extracted facts to a daily-capped quarantine when
# Mac Qwen3 + DeepSeek both fail. To prevent runaway cost / abuse:
#   - if data/sonnet_quarantine_blocked.flag exists → BLOCK any sonnet-bumping
#     subprocess (typically Bash invoking backfill_*_driver with --quarantine)
#   - flag is written by src.core.sonnet_quarantine_counter when the daily
#     cap (default 50) is crossed OR HMAC verify fails on the counter file
# Pattern matches: "sonnet_quarantine", "--use-sonnet-quarantine",
# "increment_sonnet_quarantine", "backfill_*_driver --use-sonnet"
import re as _re_r17
from src.core._path_resolver import memory_dir
_SONNET_QUARANTINE_PATTERNS = (
    _re_r17.compile(r"sonnet[_-]?quarantine", _re_r17.IGNORECASE),
    _re_r17.compile(r"--use[_-]?sonnet", _re_r17.IGNORECASE),
    _re_r17.compile(r"increment.*sonnet", _re_r17.IGNORECASE),
)


def _check_sonnet_quarantine_block(cmd: str) -> "str | None":
    """Rule 17 helper. Returns deny-reason if blocked, else None.

    Logic:
      1. Check if cmd matches any sonnet-quarantine pattern. If not → None.
      2. Locate workspace; check data/sonnet_quarantine_blocked.flag.
      3. If flag absent → None (allow).
      4. Otherwise read reason + count from flag and return deny msg.
    """
    if not cmd or not isinstance(cmd, str):
        return None
    if not any(pat.search(cmd) for pat in _SONNET_QUARANTINE_PATTERNS):
        return None
    try:
        ws = _find_workspace()
        flag = ws / "memexa" / "data" / "sonnet_quarantine_blocked.flag"
    except OSError:
        return None
    if not flag.exists():
        return None
    reason = "unknown"
    count = -1
    try:
        d = json.loads(flag.read_text(encoding="utf-8"))
        reason = d.get("reason", "unknown")
        count = int(d.get("count", -1))
    except (OSError, json.JSONDecodeError):
        pass
    return (
        f"Rule 17 sonnet_quarantine_blocked (reason={reason}, count={count}). "
        "Daily Sonnet quarantine cap reached or HMAC verify fail. "
        "CEO must review + run "
        "`python -m src.core.sonnet_quarantine_counter clear`."
    )


# Rule 18 (2026-05-05): memory_runaway_guard.
# Reference incident: 2026-05-02 20:30:01 Win Resource-Exhaustion 2004
# (python.exe PID 33592 used 89.6 GB virtual memory; pagefile auto-grew
# 16GB → 92GB, non-reversible without reboot+size-cap). Root cause was
# stub-as-dependency-injection in extract_chat_triples — pipeline batched
# 56k WeChat msgs into memory waiting for results that never came.
# Pattern: deny Bash spawns of historically-OOM pipelines when
# data/memory_runaway_blocked.flag exists (written by memory_guardrail
# daemon when current process VMS crosses threshold_block_gb, default 40).
_MEMORY_RUNAWAY_PATTERNS = (
    _re_r17.compile(r"backfill[_-]\w+[_-]driver", _re_r17.IGNORECASE),
    _re_r17.compile(r"\bbatch[_-]?extract\b", _re_r17.IGNORECASE),
    _re_r17.compile(r"\bextract[_-]chat[_-]\w+", _re_r17.IGNORECASE),
    _re_r17.compile(r"mlx_lm\.server", _re_r17.IGNORECASE),
    _re_r17.compile(r"\bpytest\b.*\s-n\s+\d", _re_r17.IGNORECASE),
)


def _check_memory_runaway_block(cmd: str) -> "str | None":
    """Rule 18 helper. Returns deny-reason string if blocked, else None.

    Logic:
      1. Cmd must match a known-OOM-prone pipeline pattern. If not → None.
      2. Read data/memory_runaway_blocked.flag. If absent → None (allow).
      3. Otherwise return human-readable deny msg with witness data.
    """
    if not cmd or not isinstance(cmd, str):
        return None
    if not any(pat.search(cmd) for pat in _MEMORY_RUNAWAY_PATTERNS):
        return None
    try:
        ws = _find_workspace()
        flag = ws / "memexa" / "data" / "memory_runaway_blocked.flag"
    except OSError:
        return None
    if not flag.exists():
        return None
    vms_gb = -1.0
    state = "unknown"
    ts = ""
    try:
        d = json.loads(flag.read_text(encoding="utf-8"))
        vms_gb = float(d.get("vms_gb", -1))
        state = str(d.get("state", "unknown"))
        ts = str(d.get("ts", ""))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return (
        f"Rule 18 memory_runaway_blocked (state={state}, vms_gb={vms_gb:.2f}, "
        f"tripped_at={ts}). A high-memory pipeline command was denied because "
        "an earlier process crossed the commit-charge threshold. Inspect "
        "memexa/data/memory_guardrail_dumps/ for thread stacks. After root-causing, "
        "run `python -m src.core.memory_guardrail clear` to release the gate."
    )


def _rule14_owasp_env_inline(tool_name: str, tool_input: dict):
    """Rule 14 (TU-5 2026-04-24): block MEMEXA_GATES_OVERRIDE inline assignments per OWASP LLM01.

    Returns (decision, reason) tuple to deny, or None to allow (rule did not match).
    Token-position-aware: only rejects where env var is the SUBJECT of an assignment,
    not where it appears as an argument to grep/findstr/python -c '...'/etc.
    """
    import shlex
    payload = tool_input.get("command", "")
    if not payload or not isinstance(payload, str):
        return None
    try:
        tokens = shlex.split(payload, posix=True)
    except ValueError:
        # Malformed shell — let the shell itself reject; do not block here
        return None
    if not tokens:
        return None
    # Pattern 1: tokens[0] starts with MEMEXA_GATES_OVERRIDE= (POSIX inline)
    if tokens[0].startswith("MEMEXA_GATES_OVERRIDE="):
        return ("deny",
                "Rule 14 OWASP LLM01: MEMEXA_GATES_OVERRIDE must be set via dedicated CEO CLI "
                "(`python -m src.cli.gates_override mint`), not LLM-authored inline shell. "
                "See memory/feedback_override_channel_owasp.md HARD RULE.")
    # Pattern 2: ['export', 'MEMEXA_GATES_OVERRIDE=...'] (POSIX export)
    if len(tokens) >= 2 and tokens[0] == "export" and tokens[1].startswith("MEMEXA_GATES_OVERRIDE="):
        return ("deny",
                "Rule 14 OWASP LLM01: `export MEMEXA_GATES_OVERRIDE=` rejected; use CEO CLI.")
    # Pattern 3: ['set', 'MEMEXA_GATES_OVERRIDE=...'] (Windows set, case-insensitive)
    if len(tokens) >= 2 and tokens[0].lower() == "set" and tokens[1].startswith("MEMEXA_GATES_OVERRIDE="):
        return ("deny",
                "Rule 14 OWASP LLM01: `set MEMEXA_GATES_OVERRIDE=` rejected; use CEO CLI.")
    # Pattern 4: ['setx', 'MEMEXA_GATES_OVERRIDE', value] (Windows setx persistent)
    if len(tokens) >= 3 and tokens[0].lower() == "setx" and tokens[1] == "MEMEXA_GATES_OVERRIDE":
        return ("deny",
                "Rule 14 OWASP LLM01: `setx MEMEXA_GATES_OVERRIDE` rejected; use CEO CLI.")
    # All other forms (grep/findstr/git log/python literal etc.) → allow
    return None


# ---------------------------------------------------------------
# Rule 15 (U20 2026-04-28): plan-driven filesystem traversal guard.
# Mechanises HARD RULE feedback_plan_driven_filesystem_traversal_guard.
#
# Allowed write zones (defense-in-depth):
#   - workspace root tree
#   - user memory dir (~/.claude/projects/<workspace-tag>/memory)
#   - system temp dir (pytest tmp_path, etc.)
# Anything else → deny.
#
# `..` traversal is normalised by Path.resolve(); a write that resolves
# outside the allowed zones is rejected even if the literal input contains
# no `..` segments.
# ---------------------------------------------------------------
_TRAVERSAL_ALLOWED_PARENTS_CACHE: "list[Path] | None" = None


def _traversal_allowed_parents() -> "list[Path]":
    """Resolve allowed write parent zones once per process."""
    global _TRAVERSAL_ALLOWED_PARENTS_CACHE
    if _TRAVERSAL_ALLOWED_PARENTS_CACHE is not None:
        return _TRAVERSAL_ALLOWED_PARENTS_CACHE
    zones: "list[Path]" = []
    try:
        zones.append(_find_workspace().resolve())
    except Exception:
        pass
    try:
        memory_dir = memory_dir().resolve()
        zones.append(memory_dir)
    except Exception:
        pass
    # System temp (pytest tmp_path, atomic writes, etc.)
    import tempfile
    try:
        zones.append(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass
    # AppData/Local/Temp on Windows (claude harness uses this)
    appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
    if appdata:
        try:
            zones.append(Path(appdata).resolve())
        except Exception:
            pass
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: "list[Path]" = []
    for z in zones:
        key = str(z).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(z)
    _TRAVERSAL_ALLOWED_PARENTS_CACHE = out
    return out


def _rule15_path_traversal_guard(file_path: str) -> "tuple[bool, str]":
    """Rule 15 (U20). Return (ok, deny_msg).

    ok=True → allow (path is within an allowed zone).
    ok=False → deny with explanation.
    """
    if not file_path:
        return (True, "")
    raw = str(file_path)
    # Defense-in-depth: explicit `..` segment in raw input is suspicious even
    # if resolve() would normalize it. Don't deny outright (Path.resolve in
    # tests legitimately uses `..` via tempfile), but log resolution result.
    try:
        resolved = Path(raw).resolve()
    except (OSError, ValueError) as exc:
        return (False,
                "Rule 15 path_traversal: file_path resolution failed "
                f"({exc!s}); refusing write to ambiguous path. "
                "See HARD RULE feedback_plan_driven_filesystem_traversal_guard.")
    zones = _traversal_allowed_parents()
    if not zones:
        # No zones detected → fail-open (don't break legitimate writes)
        return (True, "")
    for z in zones:
        try:
            resolved.relative_to(z)
            return (True, "")
        except ValueError:
            continue
    zone_summary = ", ".join(str(z) for z in zones[:2])
    return (False,
            f"Rule 15 path_traversal_guard: write to {resolved} blocked. "
            f"Allowed zones: workspace + memory dir + system temp "
            f"(e.g. {zone_summary}). "
            "See HARD RULE feedback_plan_driven_filesystem_traversal_guard. "
            "If this is a legitimate cross-workspace write (rare), spawn a "
            "separate session in the target workspace.")


def main():
    """Read tool input from stdin, run checks, respond with JSON decision."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""

    if not raw.strip():
        _respond("allow")
        return

    try:
        data = json.loads(raw)
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})
    except Exception:
        _respond("allow")
        return

    # Rule 9 (2026-04-21): agent_stall block. If a prior spawn of the same
    # subagent_type produced < STALL_BPM bytes/min over > 300s, block new
    # spawns until the flag expires or operator clears it.
    if tool_name == "Task" or tool_name == "Agent":
        # Claude Code Task tool payload carries "subagent_type" key
        stype = tool_input.get("subagent_type") or ""
        if stype:
            try:
                from src.core.agent_stall_detector import pre_check as _stall_pre
                allow, reason = _stall_pre(stype)
                if not allow:
                    _respond("deny", reason or "agent_stall flag set",
                             rule="agent_stall", target=stype)
                    return
            except Exception:
                pass  # fail-open if module missing

    # Rule 14 (TU-5 2026-04-24): OWASP LLM01 — MEMEXA_GATES_OVERRIDE inline-env-assignment block.
    # Token-position-aware: rejects assignment patterns at token[0] / export / set / setx.
    # Allows search payloads (grep/git-log/python-literal) where the env name appears as ARGUMENT.
    if tool_name in ("Bash", "PowerShell"):
        _r14 = _rule14_owasp_env_inline(tool_name, tool_input)
        if _r14 is not None:
            decision, reason = _r14
            _respond(decision, reason, rule="owasp_env_inline",
                     target="MEMEXA_GATES_OVERRIDE")
            return

    # ESCAPE-PROOF: Intercept Bash commands targeting protected enforcement files.
    # Without this, Claude can do `Bash(python -c "open('task_spec.json','w').write(...)")`
    # to bypass the Write/Edit gate entirely.
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # Protected file basenames that should never be written via Bash during autopilot
        _BASH_PROTECTED = (
            "task_spec.json",
            "persistent_mode_state.json",
            "harness_state.json",
        )
        # Heuristic: command mentions a protected file AND contains a write verb
        cmd_lower = cmd.lower()
        write_verbs = (".write(", ".write_text(", "open(", "> ", ">>", "echo ",
                       "print(json.dumps", "json.dump(", "writelines(", "rename(")
        for pf in _BASH_PROTECTED:
            if pf in cmd and any(v in cmd_lower for v in write_verbs):
                # Check if autopilot is active (don't block when not in autopilot)
                try:
                    state_file = _WORKSPACE / "memexa" / "memexa" / "data" / "persistent_mode_state.json"
                    if state_file.exists():
                        st = json.loads(state_file.read_text(encoding="utf-8"))
                        if st.get("active", False):
                            _respond("deny", (
                                f"DATA PROTECTION (Bash): Command appears to write to '{pf}' "
                                f"during active autopilot mode. This is an escape-proof guard. "
                                f"Use the task_router/persistent_mode Python API instead."
                            ), rule="bash_protected", target=pf)
                            return
                except Exception:
                    pass

        # --------------------------------------------------------------
        # Rule 10 (2026-04-23): two-tier cmd retry budget + inline prime.
        # Deny when a non-retryable pattern is invoked beyond its budget.
        # Even on deny, we inject memory prime context into the reason so
        # the LLM gets the "why this is blocked" payload immediately.
        # --------------------------------------------------------------
        try:
            from src.core import cmd_retry_tracker
            decision = cmd_retry_tracker.record_and_check(cmd)
            if not decision.allow:
                context_blob = _inline_prime(cmd, decision.memory_file)
                reason = decision.reason
                if context_blob:
                    reason = reason + "\n[memory] " + context_blob
                _respond(
                    "deny", reason,
                    rule="cmd_retry_budget", target=decision.match_pattern or cmd[:60],
                )
                return
        except Exception:
            pass  # fail-open if tracker unavailable

        # --------------------------------------------------------------
        # Rule 12 (2026-04-28, U12-B): cost budget guard.
        # Deny subagent-spawn-pattern Bash commands when active_tid has
        # crossed 100% of cost_budget_usd. Reads <task_dir>/cost_budget_blocked
        # flag written by cost_meter._write_block_flag(); does NOT compute
        # budget here (would regress hot-path; flag-file is the witness).
        # Non-spawn commands (e.g. `git log`, `python -m pytest`) always
        # allowed even on block — only subagent spawns are denied.
        # Per HARD RULE feedback_value_resolution_chain_explicit (2026-04-25):
        # active_tid resolution chain is Tier-1 env / Tier-2 flag / Tier-3 None.
        # --------------------------------------------------------------
        try:
            reason = _check_cost_budget_block(cmd)
            if reason is not None:
                _respond(
                    "deny", reason,
                    rule="cost_budget_exceeded", target=cmd[:60],
                )
                return
        except Exception:
            pass  # fail-open: budget guard never breaks legitimate commands

        # --------------------------------------------------------------
        # Rule 17 (TU-7 backfill 2026-05-02): Sonnet quarantine cap + HMAC.
        # Deny commands that would invoke Sonnet quarantine extraction once
        # the daily cap (default 50) is crossed OR the counter HMAC has been
        # tampered. Witness is data/sonnet_quarantine_blocked.flag, written
        # by src.core.sonnet_quarantine_counter.increment().
        # --------------------------------------------------------------
        try:
            reason = _check_sonnet_quarantine_block(cmd)
            if reason is not None:
                _respond(
                    "deny", reason,
                    rule="sonnet_quarantine_blocked", target=cmd[:60],
                )
                return
        except Exception:
            pass  # fail-open if guard infra broken; CEO reviews flag manually

        # --------------------------------------------------------------
        # Rule 18 (2026-05-05): memory_runaway_guard.
        # Deny known-OOM-prone pipelines (backfill_*_driver, batch_extract,
        # extract_chat_*, mlx_lm.server, pytest -n) when guardrail daemon has
        # tripped data/memory_runaway_blocked.flag (current-process VMS >=
        # threshold_block_gb, default 40). Witness file written by
        # src.core.memory_guardrail._write_flag(). Reference incident:
        # 2026-05-02 89.6 GB python.exe → pagefile.sys grew to 92 GB.
        # --------------------------------------------------------------
        try:
            reason = _check_memory_runaway_block(cmd)
            if reason is not None:
                _respond(
                    "deny", reason,
                    rule="memory_runaway_blocked", target=cmd[:60],
                )
                return
        except Exception:
            pass  # fail-open: guard module missing → never blocks legitimate work

        # --------------------------------------------------------------
        # Rule 11 (2026-04-23): tool-triggered memory prime.
        # Never denies. Attaches memory pattern hits as additionalContext
        # for commands whose memory docs we want the LLM to remember.
        # --------------------------------------------------------------
        try:
            ctx = _inline_prime(cmd)
            if ctx:
                _respond("allow", context=ctx, rule="memory_prime", target=cmd[:60])
                return
        except Exception:
            pass

        _respond("allow")
        return

    file_path = tool_input.get("file_path", "")
    if not file_path:
        _respond("allow")
        return

    # Compute relative path once for all rules
    relative = _get_relative(file_path)

    # ============================================================
    # Rule 0 (2026-05-04, post-Phase 0-2 commit per CEO directive):
    # When autopilot is active for task X, ALL Write/Edit operations
    # to files inside that task's task_dir are auto-allowed without
    # any further rule checks. Rationale: autopilot pipeline owns the
    # task_dir and its discipline is enforced by Stage 1-6 council/
    # reviewer/plan_retro/ac_verifier — pretool_gate prompts here are
    # noise that breaks the "submit /memexa-pilot, walk away" UX.
    # Rule 16 (autopilot_active.json) and Rule 7 (task_spec/persistent
    # state) still apply because their Write paths are NOT inside the
    # task_dir; they live at workspace-level critical-state locations.
    #
    # Multi-source tid resolution (LIVE-witnessed 2026-05-04 18:14Z bug:
    # MEMEXA_ACTIVE_TASK_ID env stale → wrong tid → Rule 0 misses):
    # check ALL of {env, autopilot_active.json, harness_state.last_task_id}
    # — if path contains ANY of them in `.claude/harness/tasks/<tid>/`,
    # bypass.
    # ============================================================
    try:
        candidate_tids: list = []
        # 1. env var (may be stale)
        _env_tid = os.environ.get("MEMEXA_ACTIVE_TASK_ID", "").strip()
        if _env_tid:
            candidate_tids.append(_env_tid)
        # 2. autopilot_active.json (most authoritative for "now")
        try:
            ws = _find_workspace()
            flag = ws / ".claude" / "harness" / "autopilot_active.json"
            if flag.exists():
                data = json.loads(flag.read_text(encoding="utf-8"))
                tid_val = data.get("task_id")
                if isinstance(tid_val, str) and tid_val:
                    candidate_tids.append(tid_val)
        except (OSError, json.JSONDecodeError):
            pass
        # 3. harness_state.last_task_id (recent commit)
        try:
            hs_path = ws / ".claude" / "config" / "harness_state.json"
            if hs_path.exists():
                hs = json.loads(hs_path.read_text(encoding="utf-8"))
                last_tid = hs.get("last_task_id", "").strip()
                if last_tid:
                    candidate_tids.append(last_tid)
        except (OSError, json.JSONDecodeError, NameError):
            pass

        if candidate_tids:
            fp_norm = file_path.replace("\\", "/")
            for tid in candidate_tids:
                tid_marker = f"/.claude/harness/tasks/{tid}/"
                if tid_marker in fp_norm:
                    _respond("allow",
                             context=f"Rule 0: autopilot task_dir bypass (tid={tid})",
                             rule="autopilot_task_dir_bypass",
                             target=file_path)
                    return
    except Exception:
        pass  # fail-safe: never crash the gate

    # Rule 15 (U20 2026-04-28): plan-driven filesystem traversal guard.
    # Mechanically enforces HARD RULE feedback_plan_driven_filesystem_traversal_guard
    # for Write/Edit operations. Resolves file_path absolutely; rejects writes to
    # any location outside the allowed zones (workspace + memory dir + tempdir).
    # `..` traversal is normalised by resolve(); residual `..` after resolve means
    # symlink cycle or resolution failure → deny by default (fail-safe).
    try:
        ok, msg = _rule15_path_traversal_guard(file_path)
    except Exception:
        ok, msg = True, ""  # never crash the gate
    if not ok:
        _respond("deny", msg, rule="path_traversal_guard", target=file_path)
        return

    # Rule 1: Root directory control
    ok, msg = check_root_directory(file_path)
    if not ok:
        _respond("deny", msg, rule="root_dir", target=file_path)
        return

    # Rule 2: L3 protection (ask user to confirm)
    ok, msg = check_l3_protection(file_path)
    if not ok:
        _respond("ask", msg, rule="l3_protection", target=file_path)
        return

    # Rule 3: Python syntax (Write only — Edit doesn't have full content)
    if tool_name == "Write":
        content = tool_input.get("content", "")
        if content and file_path.endswith(".py"):
            ok, msg = check_python_syntax(content, file_path)
            if not ok:
                _respond("deny", msg, rule="ast_syntax", target=file_path)
                return

    # Rule 13 (TU-R7-lite 2026-04-23): env_allowlist.json protection.
    # Write to env_allowlist.json must go through ci_env_grep_gate CLI with
    # --ceo-approved flag, not direct Write (which LLM could do as
    # precursor to adding unauthorized env var reads). Security-reviewer B2 fix.
    if tool_name in ("Write", "Edit"):
        fp_norm = file_path.replace("\\", "/").lower()
        if fp_norm.endswith("/memexa/data/env_allowlist.json") or \
           fp_norm.endswith("memexa/memexa/data/env_allowlist.json"):
            _respond(
                "deny",
                "Rule 13 env_allowlist.json: direct Write/Edit not permitted. "
                "Use: python -m src.core.ci_env_grep_gate add-allowed "
                "<NAME> --ceo-approved --reason '<text>'. This enforces OWASP "
                "LLM01 out-of-band CEO signoff for new MEMEXA_* env reads.",
                rule="env_allowlist_protection",
                target=file_path,
            )
            return

    # Rule 16 (autopilot_pi 2026-04-30): autopilot_active.json write protection.
    # Direct Write/Edit to .claude/harness/autopilot_active.json bypasses
    # set_flag() ValueError in persistent_mode.activate() — historically this
    # path enabled 11 parallel-collision incidents (#1-#11) over 4 weeks.
    # Default fail-closed; override via env MEMEXA_AUTOPILOT_FLAG_WRITE_AUTHORIZED
    # with len(reason.strip()) >= 80 (security-iter1-1 fix).
    # Per LIVE-witnessed collision #11 2026-04-30 02:23Z + HARD RULE
    # feedback_parallel_autopilot_collision_gate.
    if tool_name in ("Write", "Edit"):
        if Path(file_path).name == "autopilot_active.json":
            _AUTH_ENV = "MEMEXA_AUTOPILOT_FLAG_WRITE_AUTHORIZED"
            reason_raw = os.environ.get(_AUTH_ENV, "")
            reason_stripped = reason_raw.strip()
            if reason_stripped and len(reason_stripped) >= 80:
                # Allowed — emit trace with reason hash for forensics
                try:
                    import hashlib
                    rh = hashlib.sha256(reason_stripped.encode("utf-8")).hexdigest()[:16]
                    _emit_trace_v("autopilot_flag_write_authorized", {
                        "file_path": file_path,
                        "tool_name": tool_name,
                        "reason_sha256_hex": rh,
                        "reason_len_post_strip": len(reason_stripped),
                        "caller_pid": os.getpid(),
                    })
                except Exception:
                    pass
                _respond("allow",
                         context=f"Rule 16 autopilot_flag write authorized (reason len={len(reason_stripped)}).",
                         rule="autopilot_flag_authorized", target=file_path)
                return
            # Denied — emit trace + advise
            try:
                _emit_trace_v("autopilot_flag_write_blocked", {
                    "file_path": file_path,
                    "tool_name": tool_name,
                    "caller_pid": os.getpid(),
                    "reason_set": bool(reason_raw),
                    "reason_len_post_strip": len(reason_stripped),
                })
            except Exception:
                pass
            _respond(
                "deny",
                "Rule 16 autopilot_flag_protection: direct Write/Edit to "
                "autopilot_active.json is forbidden. Use: "
                "python -m src.core.persistent_mode activate <desc> autopilot "
                "(or mark_completed). This prevents parallel-collision bypass "
                "of set_flag() ValueError. Override (rare): set env "
                f"{_AUTH_ENV} with reason ≥80 chars (post-strip). "
                "See HARD RULE feedback_parallel_autopilot_collision_gate.",
                rule="autopilot_flag_protection",
                target=file_path,
            )
            return

    # Rule 12 (TU-R5 2026-04-23): cold_start_meter enforcement.
    # Write (not Edit) of CLAUDE.md or MEMORY.md that exceeds byte limit
    # is blocked unless MEMEXA_ALLOW_COLD_START_OVER=1 is set.
    # This closes the own-goal from 20260423_200000_reduce_cold_start where
    # cold_start_meter.py was built but never wired — CLAUDE.md could
    # re-inflate with no enforcement. Edit is excluded because diff-apply
    # may transiently exceed limits before a later edit brings it back down.
    if tool_name == "Write":
        fp_lower = file_path.replace("\\", "/").lower()
        content = tool_input.get("content", "")
        is_claude_md = fp_lower.endswith("/claude.md") or fp_lower == "claude.md"
        is_memory_md = fp_lower.endswith("/memory/memory.md") or fp_lower.endswith("\\memory\\memory.md")
        if (is_claude_md or is_memory_md) and content:
            size_bytes = len(content.encode("utf-8"))
            # Thresholds mirror cold_start_meter.LIMITS
            if is_claude_md and "memexa" in fp_lower:
                limit = 500  # memexa/CLAUDE.md
            elif is_claude_md:
                limit = 20000  # workspace CLAUDE.md
            else:
                limit = 24000  # MEMORY.md
            if size_bytes > limit and os.environ.get(
                "MEMEXA_ALLOW_COLD_START_OVER", "0"
            ) != "1":
                _respond(
                    "deny",
                    f"Rule 12 cold_start_meter: Write of {Path(file_path).name} "
                    f"is {size_bytes} bytes > limit {limit}. This would inflate "
                    f"cold-start context (see cold_start_meter.py). Options: "
                    f"(a) trim content, (b) move detail to .claude/reference/, "
                    f"(c) set MEMEXA_ALLOW_COLD_START_OVER=1 (operator override, "
                    f"logged).",
                    rule="cold_start_meter",
                    target=file_path,
                )
                return

    # Rule 4: Scope validation enforcement
    # If scope_validation_pending.flag exists, block or remind based on task complexity
    if file_path.endswith(".py"):
        try:
            scope_flag = _WORKSPACE / "memexa" / "memexa" / "data" / "scope_validation_pending.flag"
            if scope_flag.exists():
                # TTL: auto-expire after 30 minutes
                import time as _time
                flag_age = _time.time() - scope_flag.stat().st_mtime
                if flag_age > 1800:  # 30 min TTL
                    scope_flag.unlink(missing_ok=True)
                else:
                    # Check task complexity: deny for complex, allow+remind for others
                    # Default to True (fail-closed) to prevent race condition bypass
                    is_complex = True
                    try:
                        spec_file = _WORKSPACE / "memexa" / "memexa" / "data" / "task_spec.json"
                        if spec_file.exists():
                            spec = json.loads(spec_file.read_text(encoding="utf-8"))
                            is_complex = spec.get("complexity") == "complex"
                        else:
                            is_complex = False  # No task_spec = not a tracked complex task
                    except Exception:
                        pass  # Default is_complex=True: fail-closed on error

                    scope_msg = (
                        "SCOPE VALIDATION PENDING: Answer the 4 scope questions before writing code. "
                        "(1. What problem? 2. Impact of not doing? 3. Simpler alternative? "
                        "4. Minimum viable implementation?) "
                        "Then: Bash(rm memexa/memexa/data/scope_validation_pending.flag)"
                    )
                    if is_complex:
                        _respond("deny", scope_msg, rule="scope_flag", target=file_path)
                    else:
                        _respond("allow", context=scope_msg, rule="scope_flag", target=file_path)
                    return
        except Exception:
            pass

    # Rule 5: Retry tracker — warn if same file edited 3+ times
    if file_path.endswith(".py"):
        try:
            from src.core.retry_tracker import can_retry
            file_key = Path(file_path).name
            allowed, reason = can_retry(file_key)
            if not allowed:
                _respond("allow", context=(
                    f"WARNING: {file_key} edited 3+ times without resolving issues. "
                    f"{reason} Consider stopping edits and escalating."
                ), rule="retry_warn", target=file_path)
                return
        except Exception:
            pass

    # Rule 6: PLAN GATE — complex tasks must have approved plan before writing .py
    if file_path.endswith(".py"):
        try:
            spec_file = _WORKSPACE / "memexa" / "memexa" / "data" / "task_spec.json"
            if spec_file.exists():
                spec = json.loads(spec_file.read_text(encoding="utf-8"))
                if spec.get("complexity") == "complex" and spec.get("status") == "in_progress":
                    criteria = {c["id"]: c.get("verified", False)
                                for c in spec.get("acceptance_criteria", [])}
                    if "plan_approved" in criteria and not criteria["plan_approved"]:
                        task_desc = spec.get("user_prompt", "the current task")[:200]
                        _respond("deny", (
                            "PLAN GATE: Complex task requires a plan before writing code. "
                            "Call: Agent(subagent_type='architect', model='opus', "
                            f"prompt='Design implementation plan for: {task_desc}. "
                            "Write plan to .claude/plans/<name>.md with 2+ approaches, "
                            "risk list, acceptance criteria.') "
                            "Then: python -c \"from src.core.task_router import verify_criteria; "
                            "verify_criteria('plan_approved', 'path/to/plan.md')\""
                        ), rule="plan_gate", target=file_path)
                        return
        except Exception:
            pass

    # Rule 7: DATA FILE PROTECTION — block direct Write/Edit to enforcement state files
    # These files are managed by hooks/agents via Bash(python ...), not Write/Edit.
    # Blocking Write/Edit prevents the easiest tampering vector.
    protected_basenames = {"task_spec.json", "persistent_mode_state.json"}
    protected_full_paths = [
        "memexa/memexa/data/task_spec.json",
        "memexa/memexa/data/persistent_mode_state.json",
    ]
    # Check by full relative path (primary) OR basename fallback (for CJK path garbling)
    basename = Path(file_path).name
    matched = False
    if relative:
        matched = relative.replace("\\", "/") in protected_full_paths
    if not matched and basename in protected_basenames:
        # Basename fallback: check parent dir contains "data" to avoid false positives
        parent_name = Path(file_path).parent.name
        if parent_name == "data":
            matched = True
    if matched:
        _respond("deny", (
            f"DATA PROTECTION: '{basename}' is an enforcement state file. "
            f"It cannot be modified via Write/Edit. "
            f"Use the task_router/persistent_mode Python API instead."
        ), rule="data_protection", target=file_path)
        return

    # Rule 8 (Planning-infra 2026-04-21): plan_v*.md revision gate
    plan_match = re.search(r"plan_v(\d+)\.md$", file_path.replace("\\", "/"))
    if plan_match:
        ok, msg = _check_plan_revision(file_path, int(plan_match.group(1)),
                                       tool_name, tool_input)
        if not ok:
            _respond("deny", msg, rule="plan_revision", target=file_path)
            return

    _respond("allow", rule="default", target=file_path)


def _check_plan_revision(file_path: str, version: int,
                         tool_name: str, tool_input: dict) -> tuple:
    """Gate for plan_v*.md writes. Returns (ok, msg).

    B2 fix (plan_v3): version-skip cap — reject plan_v<N>.md where N
    exceeds max(existing) + 1. Also: for N>=2, fail-CLOSED on internal
    exceptions (was fail-open → plan_v999 could bypass axis_lock).
    """
    try:
        from pathlib import Path as _P
        p = _P(file_path)
        task_id = p.parent.name

        # Legacy plans in .claude/plans/ (not under tasks/<id>/) skip
        fp_normalized = str(p).replace("\\", "/")
        if "harness/tasks" not in fp_normalized and "/tasks/" not in fp_normalized:
            return (True, "legacy_plan_skipped")

        # B2: version-skip cap — disallow jumping from vN to vN+M (M>1).
        # LOG-4 fix: also cap on fresh task dir (N>0 with no existing plans).
        try:
            td = p.parent
            if td.exists():
                existing_versions = []
                for pf in td.glob("plan_v*.md"):
                    stem = pf.stem
                    if stem.startswith("plan_v"):
                        suffix = stem[len("plan_v"):]
                        if suffix.isdigit():
                            existing_versions.append(int(suffix))
                if existing_versions:
                    max_existing = max(existing_versions)
                    # New version allowed only if it equals max or max+1
                    # (overwrite existing OR linear increment).
                    if version > max_existing + 1:
                        return (False,
                                f"plan_v{version} blocked: "
                                f"version_skip from v{max_existing} "
                                f"(next allowed: v{max_existing + 1})")
                else:
                    # LOG-4: fresh dir. Only v0 allowed as initial write.
                    # Stops plan_v3.md as a first write (suspicious).
                    if version > 0:
                        return (False,
                                f"plan_v{version} blocked: "
                                f"version_skip on empty dir "
                                f"(first plan must be v0)")
        except Exception:
            # allow-silent: version-cap is defense-in-depth; existing
            # _check_plan_revision logic below is the primary gate
            pass

        if version == 0:
            # v0: best-effort check for recent council synthesis event
            ev = _recent_trace_event("council_synthesis_complete", task_id,
                                     max_age_seconds=1800)
            if not ev:
                return (True, "v0_bootstrap_allowed")
            return (True, f"v0_council_ok")

        # v1+: parse content
        if tool_name == "Write":
            content = tool_input.get("content", "")
        else:
            content = tool_input.get("new_string", "")
        if not content:
            return (True, "no_content_skipped")

        # Autopilot bypass (2026-05-05, CEO directive): when the active
        # autopilot task owns this plan dir, skip BOTH textual marker checks
        # AND axis_lock. Reasons:
        #  (1) Markers: autopilot pipeline already runs council + reviewer +
        #      plan_retro discipline internally; re-requiring markers at
        #      write-time fires a permission prompt for routine writes (esp.
        #      Stage 1 REVISE→v<N+1> iterations and Stage 4.5 RETRO PATCHES).
        #  (2) Axis_lock: for Edit operations, tool_input.new_string is a
        #      fragment (e.g. just "## RETRO PATCHES\n| RP-1 |..."), and
        #      _parse_plan_markdown(fragment) yields an empty-TU spec, which
        #      check_revision then flags as "TU removal" → false-positive DENY.
        #      The structural integrity guarantee axis_lock provides is
        #      already covered by the autopilot pipeline's reviewer/Stage 4.5.
        # Version-skip cap (above) is kept — that defends a different attack
        # surface (a write proposing plan_v999 to bypass linear discipline).
        active_tid = _resolve_active_tid_for_budget()
        autopilot_owns_plan = bool(active_tid and active_tid == task_id)
        revision_reason_text: "str | None" = None

        if autopilot_owns_plan:
            _emit_trace_v("plan_revision_autopilot_bypass", {
                "task_id": task_id,
                "version": version,
                "tool_name": tool_name,
            })
            return (True, f"plan_v{version} autopilot_bypass")

        reason_m = re.search(
            r"(?:Revision\s*reason|revision_reason)\s*[:：]\s*([^\n]{10,})",
            content, re.IGNORECASE)
        if not reason_m:
            return (False, f"plan_v{version} blocked: revision_reason required (≥10 chars)")
        revision_reason_text = reason_m.group(1).strip()

        verdict_m = re.search(
            r"(?:Reviewer\s*verdict|reviewer_verdict).*?(APPROVED|REVISE_REQUIRED|REJECTED)",
            content, re.IGNORECASE | re.DOTALL)
        if not verdict_m:
            return (False, f"plan_v{version} blocked: reviewer verdict required")
        verdict = verdict_m.group(1).upper()
        if verdict != "APPROVED":
            return (False, f"plan_v{version} blocked: reviewer verdict = {verdict}")

        # Axis_lock vs v<N-1> (non-autopilot path only)
        try:
            from src.core.plan_spec import load_plan, _parse_plan_markdown
            from src.core.axis_lock import check_revision
            old_spec = load_plan(task_id, version=version - 1)
            new_spec = _parse_plan_markdown(content, task_id, version)
            if revision_reason_text:
                new_spec.revision_reason = revision_reason_text
            violations = check_revision(new_spec, old_spec)
            block_viols = [v for v in violations if v.severity == "block"]
            if block_viols:
                return (False, f"plan_v{version} axis_lock: {[v.kind for v in block_viols[:3]]}")
        except FileNotFoundError:
            return (True, f"v{version-1}_missing_allowed")
        except Exception as e:
            _emit_trace_v("axis_lock_internal_error",
                          {"task_id": task_id, "err": str(e)[:200]})
            # B2 fix (plan_v3): for N>=2, fail-CLOSED on exceptions.
            # Prior fail-open let plan_v999 ride through on any error.
            # N=0, 1 still fail-open (legitimate bootstrap cases).
            if version >= 2:
                return (False,
                        f"plan_v{version} blocked: axis_lock_fail_closed "
                        f"({type(e).__name__})")
            return (True, f"axis_lock_fail_open: {type(e).__name__}")

        return (True, f"plan_v{version} revision ok")
    except Exception as e:
        # Outer catch — same fail-closed rule for N>=2
        if version >= 2:
            return (False,
                    f"plan_v{version} blocked: "
                    f"plan_revision_check_error_fail_closed ({type(e).__name__})")
        return (True, f"plan_revision_check_error_fail_open: {type(e).__name__}")


def _recent_trace_event(event_type: str, task_id: str,
                        max_age_seconds: int = 1800) -> dict:
    try:
        from src.core.trace_sink import _trace_file
        import time
        p = _trace_file()
        if not p.exists():
            return {}
        now = time.time()
        for line in reversed(p.read_text(encoding="utf-8",
                                          errors="replace").splitlines()[-500:]):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") != event_type:
                continue
            payload = ev.get("payload", {}) or {}
            if task_id and payload.get("task_id") not in (task_id, None):
                continue
            try:
                from datetime import datetime
                ts_s = ev.get("ts", "")
                ts = datetime.fromisoformat(ts_s.rstrip("Z")).timestamp()
                if now - ts > max_age_seconds:
                    return {}
            except Exception:
                pass
            return ev
        return {}
    except Exception:
        return {}


def _emit_trace_v(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


if __name__ == "__main__":
    main()
