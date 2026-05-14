"""
ac_verifier.py — Acceptance-criterion verify_cmd runner + evidence writer (2026-04-21).

For each AC in a plan, runs its verify_cmd as subprocess (list form, no
shell interpretation) and appends the outcome to `evidence.jsonl` in the
task dir. Also emits `ac_verified` / `ac_verify_failed` trace events.

Contract:
  - verify_cmd runs in a subprocess with env allowlist (same pattern as
    memory_write_hook) + 60s default timeout
  - evidence.jsonl entry shape: EvidenceEntry dataclass (see plan_spec.py)
  - Exit 124 reserved for timeouts
  - NEVER raises on subprocess failure; always writes an evidence entry

CLI:
  python -m memexa.core.ac_verifier run <task_id> [--ac <AC_ID>] [--timeout 60]
  python -m memexa.core.ac_verifier status <task_id>
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Module-level imports for patching: tests monkeypatch these names directly
# (e.g. monkeypatch.setattr("memexa.core.ac_verifier.task_dir", ...)).
try:
    from memexa.core.task_dir_layout import task_dir
except Exception:  # pragma: no cover
    task_dir = None  # type: ignore

try:
    from memexa.core.plan_spec import get_latest, load_evidence
except Exception:  # pragma: no cover
    get_latest = None  # type: ignore
    load_evidence = None  # type: ignore


# U4 plan_v3 TU-4: --task-dir CLI override (SEC-1 hardened anchor)
# _COMPILE_TIME_WORKSPACE is __file__-derived so MEMEXA_TASK_DIR env cannot
# shift the traversal anchor (per SEC-1 fix: memexa/core/ac_verifier.py →
# parents[3] = workspace root).
_COMPILE_TIME_WORKSPACE: Path = Path(__file__).resolve().parents[3]
_TASK_DIR_OVERRIDE: Optional[Path] = None


def _resolve_task_dir(task_id: str) -> Path:
    """U4 plan_v3 TU-4: helper wrapping task_dir() with optional override.

    When --task-dir CLI flag is set (writes _TASK_DIR_OVERRIDE module attr),
    returns the override; otherwise falls back to task_dir(task_id) which
    honors MEMEXA_TASK_DIR env / workspace default.
    """
    if _TASK_DIR_OVERRIDE is not None:
        return _TASK_DIR_OVERRIDE
    if task_dir is None:  # pragma: no cover
        raise RuntimeError("task_dir not importable")
    return task_dir(task_id)


# Subprocess env allowlist — mirrors memory_write_hook SEC-13 pattern.
_SUBPROCESS_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "PYTHONPATH", "PYTHONIOENCODING",
    "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME",
    "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
    "MEMEXA_HOOK_FAST", "MEMEXA_GRAPHITI_ENABLED",
    "MEMEXA_GRAPH_RETRIEVE", "MEMEXA_DUAL_LLM_MOCK",
    "MEMEXA_ACTIVE_TASK_ID", "MEMEXA_TASK_DIR",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "MEMEXA_PAIRED_EVAL_MODEL",
    "MEMEXA_QWEN3_URL", "MEMEXA_GEMMA_URL",
)


def _filtered_env() -> Dict[str, str]:
    return {k: os.environ[k] for k in _SUBPROCESS_ENV_ALLOWLIST if k in os.environ}


# I-3 (Phase 4, 2026-05-04): stripped-env helper for per-TU verify_cmd.
# Excludes MEMEXA_ACTIVE_TASK_ID so verify_cmd cannot rely on env channel —
# forces Tier-2 (autopilot_active.json) fallback exercise. Subset matches
# session_gate.py commit-gate stripped-env (per skill §6.3).
_STRIPPED_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "PYTHONPATH", "PYTHONIOENCODING",
    "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA",
)


def _stripped_env() -> Dict[str, str]:
    """Return env dict matching session_gate.py commit-gate stripped subset.

    MEMEXA_ACTIVE_TASK_ID NOT included → forces Tier-2 fallback exercise.
    Used by verify_ac when env_mode='stripped' for high-risk TUs.
    """
    return {k: os.environ[k] for k in _STRIPPED_ENV_ALLOWLIST if k in os.environ}


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _sha256_short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]


def verify_ac(task_id: str, ac_id: str, verify_cmd: str,
              timeout: int = 60, env_mode: str = "filtered") -> Dict:
    """Execute verify_cmd in a subprocess; append entry to evidence.jsonl.

    Returns the evidence entry as dict. NEVER raises.

    Args:
        env_mode: "filtered" (default) — _filtered_env (allowlist incl
                  MEMEXA_ACTIVE_TASK_ID); "stripped" (I-3, 2026-05-04) —
                  _stripped_env (no MEMEXA_ACTIVE_TASK_ID, forces Tier-2
                  fallback exercise per skill §6.3, for high-risk TUs).

    SECURITY: verify_cmd is parsed via shlex.split + passed as list to
    subprocess.Popen. No shell interpretation. Any `&&`, `$(...)`, `|`
    etc. in the AC verify_cmd requires explicit `/bin/sh -c` wrapping
    by the author, at which point they've accepted shell risk.
    """
    if not verify_cmd:
        entry = _build_entry(ac_id, "", 1, "empty_verify_cmd", 0, "")
        _append_evidence(task_id, entry)
        _trace_failed(task_id, ac_id, "empty_verify_cmd")
        return entry

    # Default to `/bin/sh -c` (POSIX) or `cmd.exe /c` (Windows) for
    # verify_cmd because ACs often use pipes / heredocs. This is
    # explicit shell invocation, not subprocess.Popen shell=True.
    # Rationale: verify_cmds are authored in plan.md by architects and
    # reviewers; they're effectively part of the codebase. Any author
    # can already ship a Python file with os.system — verify_cmd shell
    # is not an additional attack surface.
    if os.name == "nt":
        cmd_list = ["cmd.exe", "/c", verify_cmd]
    else:
        cmd_list = ["/bin/sh", "-c", verify_cmd]

    start = time.time()
    stdout = ""
    stderr = ""
    exit_code = 1
    # I-3: pick env based on env_mode
    _env = _stripped_env() if env_mode == "stripped" else _filtered_env()
    try:
        r = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_env,
            timeout=timeout,
            cwd=_task_workdir(task_id),
        )
        stdout = r.stdout
        stderr = r.stderr
        exit_code = r.returncode
    except subprocess.TimeoutExpired as e:
        exit_code = 124  # convention
        stderr = f"timeout after {timeout}s"
    except Exception as e:
        stderr = f"{type(e).__name__}: {e}"
        exit_code = 1
    duration_ms = int((time.time() - start) * 1000)

    entry = _build_entry(ac_id, verify_cmd, exit_code, stderr, duration_ms, stdout)
    _append_evidence(task_id, entry)
    if exit_code == 0:
        _trace_verified(task_id, ac_id, duration_ms)
    else:
        _trace_failed(task_id, ac_id, stderr[:120])
    return entry


def _build_entry(ac_id: str, verify_cmd: str, exit_code: int,
                 stderr: str, duration_ms: int, stdout: str,
                 pre_state: str = "", post_state: str = "",
                 expected_trace: Optional[List[str]] = None,
                 trace_assertion_result: Optional[Dict[str, Any]] = None) -> Dict:
    """B-1 (2026-05-04): extended evidence schema.

    Optional opt-in fields:
      - pre_state: 1-line description of symptom before fix
      - post_state: 1-line description of expected state after fix
      - expected_trace: list of trace event_type names that must fire
      - trace_assertion_result: {fired: [...], missing: [...], ok: bool}
        populated by B-2 trace_sink tail during cmd run
    Legacy callers (verify_ac without these args) get unchanged behavior.
    """
    entry = {
        "ac_id": ac_id,
        "verify_cmd": verify_cmd[:500],
        "stdout_sha": _sha256_short(stdout),          # legacy 16-hex (Commit 1-2 compat)
        "stdout_sha256": hashlib.sha256(              # NEW full 64-hex (AC-B4)
            stdout.encode("utf-8", errors="replace")
        ).hexdigest(),
        "exit_code": int(exit_code),
        "stderr_excerpt": (stderr or "")[:400],
        "duration_ms": int(duration_ms),
        "ts": _now_iso(),
        "schema_version": 2,  # B-1: marks new contract
    }
    if pre_state:
        entry["pre_state"] = pre_state[:200]
    if post_state:
        entry["post_state"] = post_state[:200]
    if expected_trace:
        entry["expected_trace"] = list(expected_trace)[:10]
    if trace_assertion_result:
        entry["trace_assertion"] = trace_assertion_result
    return entry


def assert_expected_trace(expected: List[str], since_ts: float,
                          window_events: int = 500) -> Dict[str, Any]:
    """B-2 (2026-05-04): scan last N trace events for expected event_types.

    Returns {fired: [...present...], missing: [...absent...], ok: bool}.
    fail-soft on read errors → ok=True with note (don't block on observability).

    Implementation note: trace_sink.read_traces(since_iso=...) internal compare
    has a float-vs-str ts type drift (LIVE-witnessed 2026-05-04 18:08Z), so we
    read raw last N events directly from the file. This is forward-compatible
    when read_traces gets fixed; we just lose the precision filter.
    """
    result: Dict[str, Any] = {"fired": [], "missing": [], "ok": True}
    if not expected:
        return result
    expected_set = set(expected)
    try:
        from memexa.core.trace_sink import _trace_file
        fp = _trace_file()
        if not fp.exists():
            result["note"] = "trace_file_absent"
            result["missing"] = sorted(expected_set)
            result["ok"] = False
            return result
        # Read last N lines (cap window_events)
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-window_events:]:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            etype = ev.get("event") or ev.get("event_type") or ev.get("type")
            if etype in expected_set and etype not in result["fired"]:
                result["fired"].append(etype)
        result["missing"] = sorted(expected_set - set(result["fired"]))
        result["ok"] = len(result["missing"]) == 0
    except Exception as e:
        result["note"] = f"trace_read_error: {type(e).__name__}"
        result["ok"] = True  # fail-soft on observability
    return result


def _task_workdir(task_id: str) -> str:
    """Run verify_cmd from the workspace root by default (ACs reference
    relative paths like 'memexa/memexa/core/...').
    """
    try:
        from memexa.core.task_dir_layout import _workspace_root
        return str(_workspace_root())
    except Exception:
        return str(Path.cwd())


def _append_evidence(task_id: str, entry: Dict) -> None:
    """U10 (long_term_plan_v2 BL-2) — single-writer evidence.jsonl append.

    Both serial and parallel paths call this function (single writer per
    logic-iter1-8 fix). FileLock timeout 30s; on filelock.Timeout we RAISE
    OSError (NOT silent log+continue) per logic-iter1-1/security-iter2
    4-state status semantics. Caller responsible for catching and recording
    `lock_timeout` as the AC's status.
    """
    d = _resolve_task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "evidence.jsonl"
    try:
        from filelock import FileLock, Timeout as FilelockTimeout
        try:
            with FileLock(str(p) + ".lock", timeout=30.0):
                with open(p, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except FilelockTimeout as exc:
            raise OSError(f"_append_evidence lock_timeout: {p}.lock") from exc
    except ImportError:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _run_one_ac_worker(task_id: str, ac, timeout: int) -> Dict:
    """U10 worker: run one AC's verify_cmd, return result dict.

    Uses subprocess.run(capture_output=True, timeout, text=True) triple per
    HARD RULE feedback_long_running_gpu_subprocess_discipline lite (Windows
    pipe drain). Returns dict with status in
    {ok, failed, timed_out, lock_timeout}.

    Per security-iter2-3: re-resolve task_dir inside worker (NO module-level
    mutation). _filtered_env() does not include MEMEXA_TASK_DIR.
    """
    if not ac.verify_cmd:
        return {"ac_id": ac.id, "status": "skipped", "exit_code": -1,
                "reason": "no verify_cmd"}
    workdir = _task_workdir(task_id)
    cmd = ac.verify_cmd
    start = time.time()
    # security-iter2-3: env construction excludes MEMEXA_TASK_DIR per allowlist guard
    env = {k: v for k, v in os.environ.items() if k != "MEMEXA_TASK_DIR"}
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=workdir,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
        duration_ms = int((time.time() - start) * 1000)
        entry = _build_entry(ac.id, cmd, r.returncode, r.stderr or "",
                             duration_ms, r.stdout or "")
        status = "ok" if r.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        entry = _build_entry(ac.id, cmd, 124, "TimeoutExpired",
                             duration_ms, "")
        status = "timed_out"
    entry["status"] = status
    try:
        _append_evidence(task_id, entry)
    except OSError as exc:
        # filelock timeout → 4-state lock_timeout per logic-iter1-1
        entry["status"] = "lock_timeout"
        entry["stderr_excerpt"] = (entry.get("stderr_excerpt") or "")[:300] + f" | LOCK_TIMEOUT: {exc}"
        # cannot append; surface via return only
    return entry


def _run_parallel(task_id: str, acs, timeout: int, parallel_n: int) -> List[Dict]:
    """U10 parallel batch using ThreadPoolExecutor.

    Caller already validated parallel_n in [1,8] at argparse. N=1 path SHOULD
    be served by serial loop in `verify_all`; here we always use the executor
    when called (low overhead at N=1 too).

    Emits trace `ac_verify_parallel_batch` once at start.
    """
    import concurrent.futures
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("ac_verify_parallel_batch",
                          {"task_id": task_id, "ac_count": len(acs),
                           "parallel_n": parallel_n})
    except Exception:
        pass
    results: List[Dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel_n) as pool:
        futures = {pool.submit(_run_one_ac_worker, task_id, ac, timeout): ac.id
                   for ac in acs}
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                ac_id = futures[fut]
                results.append({"ac_id": ac_id, "status": "failed",
                                "exit_code": -1,
                                "stderr_excerpt": str(exc)[:300]})
    return results


def _trace_verified(task_id: str, ac_id: str, duration_ms: int) -> None:
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("ac_verified",
                          {"task_id": task_id, "ac_id": ac_id,
                           "duration_ms": duration_ms})
    except Exception:
        pass
    # L-4 (Phase 3, 2026-05-04): on AC-verified success, clear any prior
    # live_findings record for this AC so Stage 5 commit-gate (L-5) unblocks.
    # fail-soft: never block ac_verifier on observability infra.
    try:
        from memexa.core.failure_cluster_detector import clear_live_finding
        clear_live_finding(task_id, ac_id)
    except (ImportError, AttributeError):
        pass
    except Exception:
        pass


def _trace_failed(task_id: str, ac_id: str, reason: str) -> None:
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("ac_verify_failed",
                          {"task_id": task_id, "ac_id": ac_id,
                           "reason": reason[:200]})
    except Exception:
        pass
    # L-1 (Phase 3, 2026-05-04): route Stage 6 LIVE-finding to failure_cluster
    # so re_planner can pick it up. fail-soft: never block ac_verifier on
    # observability infra. live_gap is the new root_cause enum (B-4).
    try:
        from memexa.core.failure_cluster_detector import report_live_finding
        report_live_finding(task_id=task_id, ac_id=ac_id,
                            reason=reason[:200], root_cause="live_gap")
    except ImportError:
        pass  # detector may be loading; skip
    except Exception:
        pass


def verify_all(task_id: str, only_pending: bool = True,
               timeout: int = 60) -> List[Dict]:
    """Run verify_cmd for all (or only-pending) ACs in the latest plan."""
    spec = get_latest(task_id)
    existing = load_evidence(task_id) if only_pending else {}
    results = []
    for ac in spec.acceptance_criteria:
        if only_pending and ac.id in existing and existing[ac.id].exit_code == 0:
            continue  # already verified
        if not ac.verify_cmd:
            continue  # cannot verify; upstream gate will block
        r = verify_ac(task_id, ac.id, ac.verify_cmd, timeout=timeout)
        results.append(r)
    return results


def _record_reviewer_attestation(
    task_id: str,
    ac_id: str,
    reviewer_id: str,
    cmd: str,
    stdout_sha256: str,
) -> bool:
    """Find matching evidence entry and annotate with reviewer attestation.

    Reads evidence.jsonl, finds the entry where (verify_cmd ~ cmd AND
    stdout_sha256 == stdout_sha256), then appends 'verified_by' to that
    entry and rewrites the file atomically.

    Returns True if a matching entry was found and annotated, False otherwise.

    The write path uses _atomic_io.write_text_atomic (preferred) or
    a tmp+os.replace fallback to prevent partial-write corruption.

    Sanitize: reviewer_id and cmd are sanitized per HARD RULE
    feedback_sanitize_llm_reflected_text.md before writing.
    """
    import re as _re

    def _sanitize(s: str, max_len: int = 200) -> str:
        return _re.sub(r"[\x00-\x1f\x7f]", "", s)[:max_len]

    try:
        d = _resolve_task_dir(task_id)
        p = d / "evidence.jsonl"
        if not p.exists():
            return False

        # Read all lines
        try:
            from filelock import FileLock
            lock_ctx = FileLock(str(p) + ".lock", timeout=5.0)
        except ImportError:
            lock_ctx = None  # type: ignore

        def _read_lines():
            return p.read_text(encoding="utf-8", errors="replace").splitlines()

        if lock_ctx is not None:
            with lock_ctx:
                lines = _read_lines()
        else:
            lines = _read_lines()

        # Parse all entries; find matching one
        norm_sha = (stdout_sha256 or "").lower()
        parsed: List[Dict] = []
        matched_idx = -1
        for i, line in enumerate(lines):
            if not line.strip():
                parsed.append(None)  # type: ignore
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                parsed.append(None)  # type: ignore
                continue
            parsed.append(entry)
            if matched_idx == -1:
                ev_sha = entry.get("stdout_sha256", "")
                ev_cmd = entry.get("verify_cmd", "")
                if (
                    isinstance(ev_sha, str)
                    and ev_sha.lower() == norm_sha
                    and str(ev_cmd).strip() == str(cmd).strip()
                    and entry.get("ac_id") == ac_id
                ):
                    matched_idx = i

        if matched_idx == -1:
            return False

        # Annotate matched entry
        target_entry = parsed[matched_idx]
        existing_verifiers = target_entry.get("verified_by", [])
        if not isinstance(existing_verifiers, list):
            existing_verifiers = []
        safe_reviewer = _sanitize(reviewer_id)
        if safe_reviewer not in existing_verifiers:
            existing_verifiers = existing_verifiers + [safe_reviewer]
        target_entry = {**target_entry, "verified_by": existing_verifiers}
        parsed[matched_idx] = target_entry

        # Rebuild JSONL
        new_lines = []
        for entry in parsed:
            if entry is None:
                new_lines.append("")
            else:
                new_lines.append(json.dumps(entry, ensure_ascii=False))
        new_content = "\n".join(new_lines)
        if new_lines:
            new_content += "\n"

        # Atomic write
        written = False
        try:
            from memexa.core.atomic_io import atomic_write_text
            if lock_ctx is not None:
                with lock_ctx:
                    atomic_write_text(p, new_content, encoding="utf-8")
            else:
                atomic_write_text(p, new_content, encoding="utf-8")
            written = True
        except (ImportError, Exception):
            pass

        if not written:
            # Fallback: tmp + os.replace
            import tempfile
            dir_path = p.parent
            tmp_fd = tempfile.NamedTemporaryFile(
                mode="w", dir=dir_path, suffix=".tmp",
                delete=False, encoding="utf-8",
            )
            tmp_path = Path(tmp_fd.name)
            try:
                tmp_fd.write(new_content)
                tmp_fd.flush()
                import os as _os
                _os.fsync(tmp_fd.fileno())
                tmp_fd.close()
                _os.replace(tmp_path, p)
                written = True
            except Exception:
                tmp_fd.close()
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise

        return written

    except Exception as exc:
        logger.warning("_record_reviewer_attestation failed: %s", exc)
        return False


def status_summary(task_id: str) -> Dict:
    try:
        spec = get_latest(task_id)
    except FileNotFoundError:
        return {"error": "no_plan", "task_id": task_id}
    ev = load_evidence(task_id)
    verified = [ac.id for ac in spec.acceptance_criteria
                if ac.id in ev and ev[ac.id].exit_code == 0]
    failed = [ac.id for ac in spec.acceptance_criteria
              if ac.id in ev and ev[ac.id].exit_code != 0]
    pending = [ac.id for ac in spec.acceptance_criteria
               if ac.id not in ev]

    # AC-B4: add verified_by_reviewers per AC
    verified_by_reviewers: Dict[str, List[str]] = {}
    try:
        d = _resolve_task_dir(task_id)
        p = d / "evidence.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    aid = entry.get("ac_id", "")
                    vb = entry.get("verified_by", [])
                    if isinstance(vb, list) and vb:
                        verified_by_reviewers[aid] = vb
                except (json.JSONDecodeError, Exception):
                    continue
    except Exception:
        pass

    # all_verified: each verified AC must have at least 1 reviewer in verified_by
    all_verified = bool(verified) and all(
        ac_id in verified_by_reviewers and len(verified_by_reviewers[ac_id]) > 0
        for ac_id in verified
    )

    return {
        "task_id": task_id,
        "total_acs": len(spec.acceptance_criteria),
        "verified": verified,
        "failed": failed,
        "pending": pending,
        "verified_by_reviewers": verified_by_reviewers,
        "all_verified": all_verified,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv) -> int:
    # U4 plan_v3 TU-4 (A2 reset discipline): clear override at every CLI entry
    # to prevent pytest pollution between invocations.
    global _TASK_DIR_OVERRIDE
    _TASK_DIR_OVERRIDE = None

    import argparse
    p = argparse.ArgumentParser(prog="ac_verifier")
    sub = p.add_subparsers(dest="subcmd", required=True)
    p_r = sub.add_parser("run")
    p_r.add_argument("task_id")
    p_r.add_argument("--ac", default=None, help="single AC_ID; default all")
    p_r.add_argument("--timeout", type=int, default=60)
    p_r.add_argument("--force", action="store_true", help="re-run verified ACs")
    p_r.add_argument("--cmd", dest="inline_cmd", default=None,
                     help="inline verify_cmd for --ac mode (bypass plan parse)")
    p_r.add_argument("--task-dir", dest="task_dir_arg", default=None,
                     help="explicit task_dir override (workspace-relative or absolute)")
    # U10 (long_term_plan_v2 BL-2): --parallel N for ThreadPoolExecutor batch
    # 2x2 matrix per security-iter2 / verifier-B4: reject 0/-1/9/abc with exit 2
    def _parallel_int(s):
        try:
            n = int(s)
        except (ValueError, TypeError):
            raise argparse.ArgumentTypeError(f"--parallel must be int, got {s!r}")
        if n < 1 or n > 8:
            raise argparse.ArgumentTypeError(f"--parallel must be 1..8, got {n}")
        return n
    p_r.add_argument("--parallel", type=_parallel_int, default=1,
                     help="parallel worker count (1-8, default 1)")
    p_s = sub.add_parser("status")
    p_s.add_argument("task_id")
    p_s.add_argument("--task-dir", dest="task_dir_arg", default=None,
                     help="explicit task_dir override (workspace-relative or absolute)")
    args = p.parse_args(argv[1:])

    # U4 plan_v3 TU-4: --task-dir CLI override (SEC-1 hardened anchor)
    if getattr(args, "task_dir_arg", None) is not None:
        td_path = Path(args.task_dir_arg).resolve()
        if not td_path.exists() or not td_path.is_dir():
            print(f"--task-dir does not exist or is not a dir: {td_path}",
                  file=sys.stderr)
            return 2
        try:
            td_path.relative_to(_COMPILE_TIME_WORKSPACE)
        except ValueError:
            print(f"--task-dir outside workspace: {td_path}", file=sys.stderr)
            return 2
        # C2 fix: assign via module attribute (NOT globals())
        import memexa.core.ac_verifier as _self
        _self._TASK_DIR_OVERRIDE = td_path

    if args.subcmd == "run":
        # AC-B4 harness-context guard: reject --cmd override when running
        # inside an active harness task to prevent reviewer forgery.
        active_tid = os.environ.get("MEMEXA_ACTIVE_TASK_ID")
        if args.inline_cmd is not None and active_tid:
            print(
                f"Refusing arbitrary --cmd in harness context "
                f"(active task={active_tid}). "
                "Use the plan's declared verify_cmd instead.",
                file=sys.stderr,
            )
            return 2

        if args.ac:
            # Single AC mode
            if args.inline_cmd:
                r = verify_ac(args.task_id, args.ac, args.inline_cmd,
                              timeout=args.timeout)
            else:
                from memexa.core.plan_spec import get_latest
                spec = get_latest(args.task_id)
                ac = next((a for a in spec.acceptance_criteria if a.id == args.ac), None)
                if not ac:
                    print(f"AC {args.ac} not found", file=sys.stderr)
                    return 1
                r = verify_ac(args.task_id, ac.id, ac.verify_cmd, timeout=args.timeout)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0 if r["exit_code"] == 0 else 1
        else:
            # U10: dispatch to parallel batch when N>1; serial otherwise
            if getattr(args, "parallel", 1) > 1:
                from memexa.core.plan_spec import get_latest
                spec = get_latest(args.task_id)
                existing = load_evidence(args.task_id) if not args.force else {}
                acs_to_run = [
                    a for a in spec.acceptance_criteria
                    if a.verify_cmd and (
                        args.force or a.id not in existing
                        or existing[a.id].exit_code != 0
                    )
                ]
                rs = _run_parallel(args.task_id, acs_to_run,
                                   args.timeout, args.parallel)
            else:
                rs = verify_all(args.task_id, only_pending=not args.force,
                                timeout=args.timeout)
            print(json.dumps({"ran": len(rs),
                              "failures": [r for r in rs if r.get("exit_code", 1) != 0]},
                             ensure_ascii=False, indent=2))
            return 0 if all(r.get("exit_code", 1) == 0 for r in rs) else 1

    if args.subcmd == "status":
        s = status_summary(args.task_id)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
