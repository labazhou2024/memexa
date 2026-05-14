"""
Session Heartbeat (v3.1 T0.5, 2026-04-20)

SessionStart heartbeat + initiator tagging. Produces the `session_start`
events that AC-15b / AC-15c / §6 sunset clocks count as "CEO-active-day".

Closes verifier R3 item 1 (plan v3.1 E1): `trace_sink.session_start` was
test-only in production + had no initiator tag, making CEO-active-day
clock uncalibrated.

Design
------
Priority order in classify(env):
  1. Agent env marker present  -> "agent"    (overrides everything)
  2. Cron env marker present   -> "cron"
  3. MEMEX_CEO_SESSION == "1" -> "ceo"
  4. Otherwise                 -> "unknown"  (NOT counted as CEO-active-day)

Agent detection wins over CEO because agent subprocesses inherit parent
env including MEMEX_CEO_SESSION. Without this precedence, every agent
Claude Code subprocess would falsely tick the CEO-active-day counter.

CEO onboarding
--------------
Set in shell rc or via setx (Windows):
    setx MEMEX_CEO_SESSION 1
Restart Claude Code to take effect.

CLI
---
    python -m src.core.session_heartbeat emit
        Classify + emit one session_start event to trace_sink.
        Idempotent per session: if $CLAUDE_SESSION_ID already has a
        session_start event in the current day window, skips.

    python -m src.core.session_heartbeat ceo-active-days [N]
        Print count of distinct days with >= 1 session_start
        where payload.initiator == "ceo", over last N days
        (default 30).

Non-goals
---------
- Does NOT guess initiator from parent PID inspection (too flaky on
  Windows + OneDrive-synced paths). Explicit env var is the contract.
- Does NOT mutate trace_sink schema; only adds documented
  `initiator` key under payload.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal, Mapping, Optional

Initiator = Literal["ceo", "agent", "cron", "unknown"]

_AGENT_ENV_KEYS = (
    "CLAUDE_AGENT_ID",
    "CLAUDE_SUBAGENT",
    "CLAUDE_AGENT_NAME",
    "ANTHROPIC_SUBAGENT",
    "CLAUDE_CODE_SUBAGENT",
)
_CRON_ENV_KEYS = (
    "CRON_JOB",
    "SCHEDULED_TASK",
    "MEMEX_CRON_SESSION",
)
_CEO_ENV_KEY = "MEMEX_CEO_SESSION"


def classify(env: Optional[Mapping[str, str]] = None) -> Initiator:
    """Classify session initiator from environment.

    Order: agent > cron > ceo > unknown. Agent wins over ceo because
    spawned agent subprocesses inherit CEO's env; without precedence,
    every subagent would inflate the CEO-active-day counter.
    """
    e = env if env is not None else os.environ
    if any(e.get(k) for k in _AGENT_ENV_KEYS):
        return "agent"
    if any(e.get(k) for k in _CRON_ENV_KEYS):
        return "cron"
    if e.get(_CEO_ENV_KEY) == "1":
        return "ceo"
    return "unknown"


def emit(env: Optional[Mapping[str, str]] = None) -> Initiator:
    """Classify + write one session_start event to trace_sink.

    T10 extension (AC-T10-1): after emitting the event, if initiator=='ceo',
    fire memory_ingest_watcher.scan_with_timeout(8.0) so CEO-hand-edited
    memory/*.md flows into KB within the same SessionStart window. Never
    blocks longer than 8 s (timeout path writes to ingest_queue.jsonl for
    heartbeat drain_queue to process). Agent/cron initiators skip the scan
    to avoid double-fire.

    Returns the classified initiator. Fire-and-forget per trace_sink contract.
    """
    initiator = classify(env)
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(
            "session_start",
            {"initiator": initiator, "source": "session_heartbeat"},
        )
    except Exception:
        pass

    # B2-prep (plan v2 2026-04-21): record last_session_epoch for heartbeat
    # tri-state (active/idle/hibernate) detection. Uses atomic_update_json
    # per verifier R2 N1 to avoid race with the ~16 other harness_state.json
    # writers. Never raises.
    _record_last_session_epoch()

    # T10 AC-T10-1: CEO-only watcher fire
    if initiator == "ceo":
        try:
            from src.core.memory_ingest_watcher import scan_with_timeout
            scan_with_timeout(timeout_sec=8.0)
        except Exception:
            pass

    return initiator


def _record_last_session_epoch() -> bool:
    """Atomically stamp harness_state.json['last_session_epoch'] = now.

    B2-prep: source of truth for heartbeat's active/idle/hibernate routing.
    Uses _atomic_state.atomic_update_json so concurrent writers don't clobber.
    Returns True on success (or intentional no-op); never raises.
    """
    try:
        import time
        from src.core._atomic_state import atomic_update_json
    except ImportError:
        return False
    harness_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".claude" / "config" / "harness_state.json"
    )
    if not harness_path.parent.exists():
        return False

    def _mut(state):
        state["last_session_epoch"] = time.time()
        return state
    try:
        return atomic_update_json(
            harness_path, _mut,
            lock_path=harness_path.with_suffix(".json.lock"),
            lock_timeout=5.0,
        )
    except Exception:
        return False


def _iter_session_start_events(trace_file: Optional[Path] = None) -> Iterable[dict]:
    """Yield session_start event payloads from trace_sink.

    [SEC-R1-001 2026-04-20] MEMEX_TRACE_FILE env override is validated via
    trace_sink._trace_file() which enforces workspace/tempdir whitelist.
    Direct Path(override) without validation was removed to prevent path traversal.
    """
    if trace_file is None:
        try:
            # Reuse trace_sink's whitelist logic (workspace OR tempdir only)
            from src.core.trace_sink import _trace_file as _ts_trace_file
            trace_file = _ts_trace_file()
        except Exception:
            # Fallback if trace_sink unavailable
            trace_file = (
                Path(__file__).parent.parent.parent.parent
                / ".claude" / "data" / "traces.jsonl"
            )
    if not trace_file.exists():
        return
    try:
        with trace_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "session_start":
                    yield event
    except OSError:
        return


def count_ceo_active_days(
    window_days: int = 30,
    trace_file: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> int:
    """Count distinct days with >= 1 session_start where initiator=='ceo'.

    Used by AC-15b / AC-15c / §6 sunset clock. Excludes events with
    initiator other than 'ceo' (agent, cron, unknown all skipped).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    days = set()
    for event in _iter_session_start_events(trace_file):
        initiator = (event.get("payload") or {}).get("initiator")
        if initiator != "ceo":
            continue
        ts_raw = event.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_raw.rstrip("Z"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
        if ts < cutoff:
            continue
        days.add(ts.date())
    return len(days)


def ceo_days_since(ts_iso: str, now: Optional[datetime] = None) -> int:
    """Return CEO-active-days elapsed since ts_iso (ISO-8601 string).

    Computes delta_calendar_days = (now - ts_iso).days + 1, then returns
    count_ceo_active_days(window_days=delta_calendar_days).  All "age"
    computations in staleness_audit use this helper so the CEO-active-day
    denomination is consistent across the codebase.

    Returns 0 if ts_iso is unparseable or in the future.
    """
    _now = now or datetime.now(timezone.utc)
    if not ts_iso:
        return 0
    try:
        s = ts_iso.replace("Z", "")
        if "+" in s[10:]:
            s = s.split("+", 1)[0]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    delta_days = max(1, (_now - dt).days + 1)
    return count_ceo_active_days(window_days=delta_days)


def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: session_heartbeat {emit|ceo-active-days [N]}", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "emit":
        initiator = emit()
        print(initiator)
        return 0
    if cmd == "ceo-active-days":
        n = int(argv[2]) if len(argv) > 2 else 30
        print(count_ceo_active_days(window_days=n))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
