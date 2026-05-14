"""Autopilot-active flag — durable file-based signal that gates use to
decide fail-closed vs fail-open behavior.

Why a file, not an env var:
  - Env vars don't survive Git hook subshells (session_gate runs as a
    fresh subprocess from the git commit invocation).
  - File-based state persists across processes and is cleanable on
    session_mode.complete().

Layout: `.claude/harness/autopilot_active.json`
  {
    "activated_at": 1777020000,
    "task_id": "20260424_...",
    "max_ttl_sec": 43200
  }

TTL semantics:
  - mtime of file, compared against now - max_ttl_sec.
  - `refresh_stage()` and `record_autopilot_heartbeat()` touch the mtime
    → sliding window.
  - Default TTL = env ``MEMEXA_PERSISTENT_MAX_H`` * 3600 (default 12h).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


_FLAG_NAME = "autopilot_active.json"


def _flag_path() -> Path:
    """Compute `.claude/harness/autopilot_active.json` under workspace root.

    Override via env ``MEMEXA_AUTOPILOT_FLAG_PATH`` for test isolation —
    must resolve under workspace root OR tempfile.gettempdir() (honors
    feedback_env_override_parent_allowlist.md HARD RULE).
    """
    override = os.environ.get("MEMEXA_AUTOPILOT_FLAG_PATH", "").strip()
    if override:
        p = Path(override)
        if _is_allowed_flag_path(p):
            return p
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".claude" / "harness" / _FLAG_NAME
    )


def _is_allowed_flag_path(p: Path) -> bool:
    """Workspace-root OR tempfile.gettempdir() only. Any other path rejected."""
    import tempfile as _tempfile
    try:
        real = p.resolve(strict=False)
        workspace = Path(__file__).resolve().parent.parent.parent.parent
        tmp_root = Path(_tempfile.gettempdir()).resolve()
        for allowed in (workspace, tmp_root):
            try:
                real.relative_to(allowed)
                return True
            except ValueError:
                continue
        return False
    except OSError:
        return False


def _max_ttl_sec() -> int:
    """TTL = MEMEXA_PERSISTENT_MAX_H * 3600. Default 12h = 43200s."""
    raw = os.environ.get("MEMEXA_PERSISTENT_MAX_H", "12").strip()
    try:
        h = float(raw)
        if h <= 0:
            return 43200
        return int(h * 3600)
    except (ValueError, TypeError):
        return 43200


def set_flag(task_id: str) -> bool:
    """Write flag file. Returns True on success.

    TU-1 strict (2026-04-25 plan_v1): rejects empty/None/non-str/whitespace/
    'unknown_session' with ValueError. Caller must resolve a canonical task_id
    BEFORE calling set_flag — see persistent_mode.activate which now does
    create_task_dir + set_current first when needed.

    Rationale: previous "accept empty, write unknown_session sentinel"
    design (TU-4 of plan_v3) made autopilot_active() return True but
    session_gate could not resolve a usable task_id, so 12/17 LIVE
    gate_skipped events had reason=no_task_binding. Strict mode forces
    resolution at the source rather than papering over downstream.

    Raises:
        ValueError: empty / None / non-str / whitespace / 'unknown_session'.
        GateInfraError: filesystem write failure.
    """
    if not isinstance(task_id, str):
        raise ValueError(
            f"set_flag: task_id must be str, got {type(task_id).__name__}"
        )
    stripped = task_id.strip()
    if not stripped or stripped == "unknown_session":
        raise ValueError(
            f"set_flag: invalid task_id={task_id!r}; provide canonical task_id "
            "(persistent_mode.activate now resolves cold-session task_id "
            "via create_task_dir before calling set_flag)"
        )
    effective_tid = stripped
    p = _flag_path()

    # Rule 15 (2026-04-27, 6th-incident promotion of HARD RULE
    # feedback_parallel_autopilot_staging_collision): refuse to overwrite an
    # active autopilot flag belonging to a different task_id when that flag
    # is recent (< 30 min). Override via MEMEXA_AUTOPILOT_FORCE_TAKEOVER=1.
    # Incidents this CEO: a971ed9↔081e3d0, 3938cbe, 953f453, 8972d12, U10+U11
    # mid-session, U12 mid-session — 6 total.
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
            existing_tid = (existing.get("task_id") or "").strip()
            existing_age_sec = time.time() - float(existing.get("activated_at", 0))
            collision_window_sec = min(
                int(existing.get("max_ttl_sec", 43200)),
                1800,  # 30 min collision window
            )
            if (existing_tid and existing_tid != effective_tid
                    and 0 <= existing_age_sec < collision_window_sec):
                if not os.environ.get("MEMEXA_AUTOPILOT_FORCE_TAKEOVER", "").strip():
                    try:
                        from memexa.core.trace_sink import write_trace_event
                        write_trace_event("autopilot_flag_set", {
                            "task_id": effective_tid,
                            "rejected_reason": "parallel_collision",
                            "existing_tid": existing_tid,
                            "existing_age_sec": int(existing_age_sec),
                        })
                    except Exception:
                        pass
                    raise ValueError(
                        f"set_flag: parallel autopilot collision detected. "
                        f"Existing autopilot_active.json belongs to task_id={existing_tid!r} "
                        f"(age={int(existing_age_sec)}s < {collision_window_sec}s window). "
                        f"Refusing to overwrite with task_id={effective_tid!r}. "
                        f"Either: (a) wait for the other session to complete, OR "
                        f"(b) set MEMEXA_AUTOPILOT_FORCE_TAKEOVER=1 to force takeover "
                        f"(documented incident #6 of HARD RULE "
                        f"feedback_parallel_autopilot_staging_collision)."
                    )
        except (OSError, json.JSONDecodeError, ValueError) as e:
            # If the error IS our collision exception, re-raise.
            if isinstance(e, ValueError) and "parallel autopilot collision" in str(e):
                raise
            # Otherwise corrupt/unreadable existing flag → overwrite is safe.
            pass

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "activated_at": time.time(),
            "task_id": effective_tid,
            "max_ttl_sec": _max_ttl_sec(),
        }
        # Atomic write via tmp+rename
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
        # Observable trace
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("autopilot_flag_set", {
                "task_id": effective_tid,
            })
        except Exception:
            pass  # trace fail-soft; flag file is the authoritative signal
        return True
    except OSError as e:
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("gate_infra_error", {
                "where": "_autopilot_flag.set_flag",
                "error": str(e)[:200],
            })
        except Exception:
            pass  # trace fail-soft
        from memexa.core._errors import GateInfraError
        raise GateInfraError(
            "set_flag write failed",
            where="_autopilot_flag.set_flag",
            context={"error": str(e)[:200]},
        ) from e


def clear_flag() -> bool:
    """Remove the flag file. Returns True iff a file was removed."""
    try:
        p = _flag_path()
        if p.exists():
            p.unlink()
            return True
        return False
    except OSError:
        return False


def refresh_flag() -> bool:
    """Touch flag file's mtime (sliding-window TTL refresh). No-op if absent."""
    try:
        p = _flag_path()
        if not p.exists():
            return False
        os.utime(p, None)
        return True
    except OSError:
        return False


def autopilot_active() -> bool:
    """Return True iff flag file exists AND mtime within TTL AND content parses.

    Negative cases (all return False):
      - flag file missing
      - mtime expired (older than max_ttl_sec)
      - JSON malformed / unreadable
    """
    try:
        p = _flag_path()
        if not p.exists():
            return False
        st = p.stat()
        age = time.time() - st.st_mtime
        if age > _max_ttl_sec():
            return False
        # Validate JSON parses (don't strictly require task_id — mtime
        # refresh during edits can leave payload stale but file intact)
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return True
    except OSError:
        return False


def flag_info() -> Optional[dict]:
    """Return the flag file's parsed content + age, or None if absent/bad."""
    try:
        p = _flag_path()
        if not p.exists():
            return None
        st = p.stat()
        payload = json.loads(p.read_text(encoding="utf-8"))
        payload["_age_sec"] = int(time.time() - st.st_mtime)
        payload["_path"] = str(p)
        return payload
    except (OSError, json.JSONDecodeError):
        return None


def _cli(argv: Optional[list] = None) -> int:
    """CLI: `python -m memexa.core._autopilot_flag {show|clear|active}`."""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="memexa.core._autopilot_flag")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print flag state + content")
    sub.add_parser("clear", help="remove the flag (override lock-out)")
    sub.add_parser("active", help="exit 0 if active, 1 otherwise")
    args = p.parse_args(argv)
    if args.cmd == "show":
        info = flag_info()
        if info:
            print(json.dumps(info, ensure_ascii=False, indent=2))
            print(f"active: {autopilot_active()}")
            return 0
        print("flag absent")
        return 0
    if args.cmd == "clear":
        ok = clear_flag()
        print(f"cleared: {ok}")
        return 0
    if args.cmd == "active":
        return 0 if autopilot_active() else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
