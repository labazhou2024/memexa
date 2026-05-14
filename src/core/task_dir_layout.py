"""Task directory layout for 10h+ autopilot (plan v2, 2026-04-21 Phase A A1).

Per-task on-disk state for crash-resumable autopilot:

    .claude/harness/tasks/
      _latest                          # textfile: current task_id pointer
      <task_id>/                       # task_id = YYYYMMDD_HHMMSS_<slug12>
        plan.md                        # human-readable todo (machine-regenerated)
        state.json                     # single source of truth
        trace.jsonl                    # append-only events (filelock + thread lock)
        .lock                          # companion filelock for state.json RMW
        scratch/                       # free-form artifacts

Verifier R1/R2 compliance:
  - #1 trace.jsonl: per-task threading.Lock + filelock.FileLock (mirrors event_bus.log_event)
  - #2 state.json RMW: delegates to _atomic_state.atomic_update_json (Alt-4 reuse)
  - #5 OneDrive race: MEMEX_TASK_DIR env var; os.replace 3× exponential retry
  - #6 _latest pointer: textfile + tmp+os.replace (no symlink branch; Windows non-admin safe)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from filelock import FileLock, Timeout as FilelockTimeout

from src.core._atomic_state import atomic_update_json, atomic_read_json

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------
# Path resolution (MEMEX_TASK_DIR escape hatch for OneDrive)
# --------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _workspace_root() -> Path:
    """Resolve workspace (parent of memex/). Robust to CWD."""
    return Path(__file__).resolve().parent.parent.parent.parent


def tasks_root() -> Path:
    """Base directory for all task dirs.

    Honors MEMEX_TASK_DIR env for OneDrive escape. Falls back to
    .claude/harness/tasks/ inside the workspace.
    """
    env = os.environ.get("MEMEX_TASK_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return _workspace_root() / ".claude" / "harness" / "tasks"


def task_dir(task_id: str) -> Path:
    """Resolve absolute path of a task directory."""
    return tasks_root() / task_id


def _slugify(slug: str, max_len: int = 12) -> str:
    cleaned = _SLUG_RE.sub("_", slug or "task")
    cleaned = cleaned.strip("_") or "task"
    return cleaned[:max_len]


def _now_utc_stamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# --------------------------------------------------------------------
# Core API
# --------------------------------------------------------------------

def create_task_dir(slug: str) -> str:
    """Atomically create a new task dir and seed files. Returns task_id.

    Side effect (W1-4, 2026-05-04 hotfix per LIVE collision incident):
    Stale scope_validation_pending.flag (>1h old) is auto-unlinked here so
    that pretool_gate Rule scope_flag does not deny .py writes for a fresh
    autopilot task that inherited a flag from a prior aborted session.
    Fresh flag (<1h) is preserved (real CEO action item).
    """
    _cleanup_stale_scope_flag()
    task_id = f"{_now_utc_stamp()}_{_slugify(slug)}"
    d = task_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "scratch").mkdir(exist_ok=True)
    # Seed state.json, plan.md, trace.jsonl via _atomic_state (filelock-safe)
    init_state = {
        "task_id": task_id,
        "current_phase": None,
        "current_unit_idx": -1,
        "units": [],
        "started_at": time.time(),
        "last_updated": time.time(),
    }
    atomic_update_json(
        d / "state.json",
        lambda _: init_state,
        lock_path=d / ".lock",
    )
    # plan.md auto-generated banner
    plan_md = (
        "<!-- AUTO-GENERATED from state.json by task_unit_scheduler. "
        "Edit state.json instead; this file is regenerated on each mark_done. -->\n"
        f"# Task: {slug}\nStarted: {init_state['started_at']}\n"
    )
    (d / "plan.md").write_text(plan_md, encoding="utf-8")
    # Empty trace.jsonl
    (d / "trace.jsonl").touch()
    return task_id


def _cleanup_stale_scope_flag(stale_after_sec: int = 86400) -> bool:
    """Unlink scope_validation_pending.flag if older than threshold (24h).

    W1-4b (2026-05-04, per security-reviewer S-3): threshold widened from
    1h → 24h because legitimate CEO scope-question may take >1h to answer
    (sleep, meeting, ambiguous answer needing research). 24h matches the
    emergency-token TTL semantics — autopilot is patient, but past 24h a
    flag without CEO ack is genuinely stale.

    Returns True if a stale flag was removed. False if no flag, or flag is
    fresh (<stale_after_sec). pretool_gate Rule scope_flag denies .py writes
    when this flag exists during complex tasks; fresh flag = real CEO action,
    stale flag = leftover from aborted session that must not block new work.

    Side effect on cleanup: emit `scope_flag_auto_dismissed_warning` trace
    + append entry to data/pending_approvals.json so CEO has audit trail
    (NOT silent removal). Per S-3 audit-trail requirement.
    """
    try:
        flag = _workspace_root() / "memex" / "memex" / "data" / "scope_validation_pending.flag"
        if not flag.exists():
            return False
        age = time.time() - flag.stat().st_mtime
        if age < stale_after_sec:
            return False
        # Audit trail BEFORE unlink (so we never lose record on transient FS error)
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("scope_flag_auto_dismissed_warning", {
                "age_sec": int(age),
                "threshold_sec": int(stale_after_sec),
                "flag_mtime": flag.stat().st_mtime,
            })
        except Exception:
            pass  # fail-soft; logger.info is the fallback
        flag.unlink()
        logger.info("task_dir_layout: removed stale scope_flag (age=%.0fs >= %ds)",
                    age, stale_after_sec)
        return True
    except Exception:
        return False


def current_task_id() -> Optional[str]:
    """Read _latest pointer. Returns None if missing/empty/dead."""
    ptr = tasks_root() / "_latest"
    if not ptr.exists():
        return None
    try:
        content = ptr.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not content:
        return None
    # Verify the target task dir still exists
    if not task_dir(content).is_dir():
        logger.info("_latest points to missing task %s; treating as absent", content)
        return None
    return content


def set_current(task_id: str, max_retries: int = 3) -> bool:
    """Atomically update _latest pointer. 3× exponential backoff for OneDrive.

    Textfile only (no symlink) for Windows non-admin compatibility.
    """
    root = tasks_root()
    root.mkdir(parents=True, exist_ok=True)
    ptr = root / "_latest"
    tmp = root / "_latest.tmp"
    delay = 0.05
    for attempt in range(max_retries):
        try:
            tmp.write_text(task_id, encoding="utf-8")
            os.replace(str(tmp), str(ptr))
            return True
        except OSError as e:
            if attempt == max_retries - 1:
                logger.warning("set_current failed after %d retries: %s", max_retries, e)
                return False
            time.sleep(delay)
            delay *= 2
    return False  # unreachable but pleases linter


# --------------------------------------------------------------------
# state.json RMW (delegates to _atomic_state)
# --------------------------------------------------------------------

def load_state(task_id: str) -> Optional[Dict[str, Any]]:
    """Read state.json for task. Returns None if task dir missing."""
    d = task_dir(task_id)
    if not d.is_dir():
        return None
    state = atomic_read_json(d / "state.json", default={})
    if not state:
        return None
    return state if isinstance(state, dict) else None


def update_state(task_id: str, mutator) -> bool:
    """R-M-W state.json via _atomic_state (Alt-4 reuse, verifier R1 #2).

    mutator: takes current state dict, returns new state dict (or None for no-op).
    Returns True on success or intentional no-op, False on failure.
    """
    d = task_dir(task_id)
    if not d.is_dir():
        return False
    return atomic_update_json(
        d / "state.json",
        mutator,
        lock_path=d / ".lock",
        lock_timeout=10.0,
    )


# --------------------------------------------------------------------
# trace.jsonl append (filelock + threading.Lock, verifier R1 #1)
# --------------------------------------------------------------------

_trace_locks: Dict[str, threading.Lock] = {}
_trace_locks_guard = threading.Lock()


def _get_thread_lock(task_id: str) -> threading.Lock:
    with _trace_locks_guard:
        lock = _trace_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _trace_locks[task_id] = lock
        return lock


def append_trace(task_id: str, event_type: str, payload: Optional[Dict] = None,
                 timeout: float = 5.0) -> bool:
    """Append-only JSONL trace with cross-process and intra-process locks.

    Mirrors event_bus.log_event pattern. JSON lines can exceed PIPE_BUF
    (512B) so O_APPEND atomicity is NOT sufficient on Windows.
    """
    d = task_dir(task_id)
    if not d.is_dir():
        return False
    entry = {
        "ts": time.time(),
        "event": event_type,
        "payload": payload or {},
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    trace_file = d / "trace.jsonl"
    lock_file = d / ".trace.lock"
    thread_lock = _get_thread_lock(task_id)
    try:
        with thread_lock:
            with FileLock(str(lock_file), timeout=timeout):
                with open(trace_file, "a", encoding="utf-8") as f:
                    f.write(line)
        return True
    except FilelockTimeout:
        logger.warning("append_trace: lock timeout on %s", trace_file)
        return False
    except OSError as e:
        logger.warning("append_trace: IO failure %s: %s", trace_file, e)
        return False


# ----------------------------------------------------------------------------
# TU-2 (autopilot_pi 2026-04-30): in-module CLI for `python -m src.core.task_dir_layout`.
# Closes the autopilot v2.0 §0.1 documentation drift (skill claimed CLI existed).
# Subcommands: create / current.
# Failure modes: argparse error → exit 2; uncaught exception → exit 2.
# ----------------------------------------------------------------------------

def _main_create(slug: str, no_set_current: bool = False) -> int:
    """Create a new task_dir for `slug`; print task_id; return exit code."""
    try:
        tid = create_task_dir(slug)
    except Exception as exc:
        sys.stderr.write(f"create_task_dir failed: {exc!s}\n")
        return 2
    if not no_set_current:
        try:
            set_current(tid)
        except Exception as exc:
            sys.stderr.write(f"set_current({tid}) warning: {exc!s}\n")
            # Non-fatal — task_dir created. Caller may set later.
    # Emit trace event so Stage 6 ac_verifier can witness the call site.
    try:
        append_trace(tid, "task_dir_create_via_cli", {
            "slug": slug,
            "tid_short": tid[:24],
            "set_current_bool": (not no_set_current),
        })
    except Exception:
        pass  # non-fatal
    sys.stdout.write(tid + "\n")
    return 0


def _main_current() -> int:
    """Print the current task_id from `_latest`."""
    tid = current_task_id()
    if tid is None:
        sys.stderr.write("no _latest pointer\n")
        return 1
    sys.stdout.write(tid + "\n")
    return 0


def _build_arg_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m src.core.task_dir_layout",
        description="task_dir_layout CLI (TU-2 autopilot_pi).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create", help="Create a new task_dir for slug")
    p_create.add_argument("slug", help="Slug (free-form ASCII identifier)")
    p_create.add_argument("--no-set-current", action="store_true",
                          help="Do not update _latest pointer.")
    sub.add_parser("current", help="Print current task_id from _latest")
    return p


def main(argv=None) -> int:
    p = _build_arg_parser()
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    try:
        if args.cmd == "create":
            return _main_create(args.slug, no_set_current=args.no_set_current)
        if args.cmd == "current":
            return _main_current()
        return 2
    except Exception as exc:
        sys.stderr.write(f"task_dir_layout CLI uncaught: {exc!s}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
