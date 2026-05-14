"""
task_binding.py — Active task_id resolution + propagation (2026-04-21).

Closes v1-B1 blocker: `MEMEX_ACTIVE_TASK_ID` env var + fallback to
`task_dir_layout.current_task_id()`. Provides subprocess env injection
for subagent spawns (R-11 mitigation).

Contract:
  - get_active_task_id() resolves env → _latest pointer → None
  - bind_task(task_id) sets env + task_dir_layout._latest
  - unbind_task() clears env (preserves _latest for audit)
  - propagate_to_subprocess(env) adds MEMEX_ACTIVE_TASK_ID to env dict
  - All functions FAIL-OPEN on task_dir_layout failures (OneDrive lock etc)

Callers: session_gate rule-7/8, task_complete_gate, ac_verifier,
planning-council agent (propagates to 5 experts).
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)


_ENV_VAR = "MEMEX_ACTIVE_TASK_ID"


def get_active_task_id() -> Optional[str]:
    """Return current task_id from env → task_dir_layout._latest → None.

    Fail-open: if task_dir_layout access fails (OneDrive lock etc.),
    falls back to env var only, and if that's also unset, returns None
    with a trace event.
    """
    val = os.environ.get(_ENV_VAR, "").strip()
    if val:
        return val
    try:
        from src.core.task_dir_layout import current_task_id
        latest = current_task_id()
        if latest:
            return latest
    except Exception as e:
        # ANCHOR-5 fail-open
        _trace_degraded(str(e))
    return None


def bind_task(task_id: str) -> bool:
    """Make task_id the active task: set env + _latest pointer.

    Returns True on success. Failure to set _latest does NOT prevent
    env-only binding; we return True as long as at least one layer
    succeeded.
    """
    if not task_id:
        return False
    os.environ[_ENV_VAR] = str(task_id)
    ptr_ok = False
    try:
        from src.core.task_dir_layout import set_current
        ptr_ok = set_current(task_id)
    except Exception as e:
        logger.warning("bind_task: set_current failed: %s (env still set)", e)
        ptr_ok = False
    _trace_set(task_id, ptr_ok)
    return True  # env-only still counts as bound


def unbind_task() -> None:
    """Clear env binding. Keeps _latest pointer (audit trail).

    After unbind, get_active_task_id will still return the last
    current_task_id from disk — this is intentional so new sessions
    can resume.
    """
    os.environ.pop(_ENV_VAR, None)
    _trace_clear()


def propagate_to_subprocess(env: Optional[Dict[str, str]] = None,
                            task_id: Optional[str] = None) -> Dict[str, str]:
    """Build env dict for subprocess.Popen.

    Starts from `env` (or os.environ if None), ensures
    MEMEX_ACTIVE_TASK_ID is set to either `task_id` (explicit arg) or
    `get_active_task_id()` (resolution chain). If neither produces a
    task_id, the env var is removed from the output — do not pass an
    empty string through.
    """
    if env is None:
        out = dict(os.environ)
    else:
        out = dict(env)
    resolved = task_id if task_id else get_active_task_id()
    if resolved:
        out[_ENV_VAR] = str(resolved)
    else:
        out.pop(_ENV_VAR, None)
    return out


# ---------------------------------------------------------------------------
# Trace helpers (fail-soft; never raise)
# ---------------------------------------------------------------------------


def _trace_set(task_id: str, ptr_ok: bool) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("task_binding_set", {"task_id": task_id, "ptr_ok": ptr_ok})
    except Exception:
        pass


def _trace_clear() -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("task_binding_cleared", {})
    except Exception:
        pass


def _trace_degraded(reason: str) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("task_binding_degraded", {"reason": reason[:200]})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="task_binding")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    p_b = sub.add_parser("bind")
    p_b.add_argument("task_id")
    sub.add_parser("unbind")
    p_pr = sub.add_parser("propagate")
    p_pr.add_argument("--task-id", default=None)
    args = p.parse_args(argv[1:])

    if args.cmd == "show":
        tid = get_active_task_id()
        print(tid or "")
        return 0 if tid else 1
    if args.cmd == "bind":
        ok = bind_task(args.task_id)
        print("bound" if ok else "failed")
        return 0 if ok else 1
    if args.cmd == "unbind":
        unbind_task()
        print("unbound")
        return 0
    if args.cmd == "propagate":
        env = propagate_to_subprocess(task_id=args.task_id)
        # Print only the relevant env
        val = env.get(_ENV_VAR, "")
        print(val)
        return 0 if val else 1
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv))
