"""
EventBus — Structured event stream for agent-to-agent communication.

Primary roles:
  1. Append-only event log (data/events.jsonl)
  2. Inter-agent communication channel (agents publish + subscribe by category)
  3. Post-mortem and debugging data source
  4. Input for big-loop Q2, evolution, and pattern extraction

Event categories (OpenHands-inspired):
  GATE    — gate decisions (commit-gate, push-gate, plan-gate, exit-gate)
  REVIEW  — code review results (findings, verdicts, scores)
  TEST    — test results (pass/fail counts, regressions, flaky)
  BUILD   — build/compile events
  AGENT   — agent lifecycle (spawn, complete, error)
  SESSION — session start/end, compaction
  LEARN   — pattern extraction, knowledge base updates, correction captures
  USER    — user corrections, approval decisions

Log rotation: when events.jsonl exceeds MAX_EVENTS lines, older events
are archived to data/events_archive/YYYY-MM-DD_events.jsonl.

Concurrency: Two-layer locking (threading.Lock + filelock.FileLock).
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import filelock

logger = logging.getLogger(__name__)

_UTC = timezone.utc
_DATA_DIR = Path(__file__).parent.parent / "data"
_EVENTS_FILE = _DATA_DIR / "events.jsonl"
_ARCHIVE_DIR = _DATA_DIR / "events_archive"

# Rotation thresholds
MAX_EVENTS = 5000       # Rotate when file exceeds this many lines
KEEP_EVENTS = 1000      # Keep this many recent events after rotation
_ROTATION_CHECK_INTERVAL = 50  # Check rotation every N writes
_write_counter = 0

# Two-layer locking: in-process + cross-process
_thread_lock = threading.Lock()
_file_lock = filelock.FileLock(str(_EVENTS_FILE) + ".lock", timeout=10)

# Config cache (avoid parsing YAML on every log_event call)
_cached_event_bus_enabled: Optional[bool] = None


def _is_event_bus_enabled() -> bool:
    """Check if event_bus feature flag is enabled (cached)."""
    global _cached_event_bus_enabled
    if _cached_event_bus_enabled is None:
        try:
            from ..config_loader import load_config
            cfg = load_config()
            _cached_event_bus_enabled = cfg.get("feature_flags", {}).get("event_bus", True)
        except Exception:
            _cached_event_bus_enabled = True
    return _cached_event_bus_enabled


# Event categories for agent-to-agent subscription
CATEGORIES = {
    "GATE": ["commit_gate", "push_gate", "plan_gate", "exit_gate", "delete_gate", "review_gate"],
    "REVIEW": ["code_review", "review_result", "review_finding"],
    "TEST": ["pytest_result", "test_baseline", "flaky_test", "regression"],
    "AGENT": ["agent_spawn", "agent_complete", "agent_error", "agent_timeout"],
    "SESSION": ["session_start", "session_end", "compact", "dream_start", "dream_complete"],
    "LEARN": ["pattern_extracted", "correction_captured", "knowledge_compiled"],
    "USER": ["user_correction", "approval_decision"],
}

# Reverse lookup: event_type -> category
_TYPE_TO_CATEGORY = {}
for _cat, _types in CATEGORIES.items():
    for _t in _types:
        _TYPE_TO_CATEGORY[_t] = _cat


def log_event(
    event_type: str,
    agent: str = "system",
    details: Optional[Dict[str, Any]] = None,
    *,
    session_id: str = "",
    category: str = "",
) -> None:
    """Append a structured event to events.jsonl.

    Thread-safe and process-safe via two-layer locking.
    Automatically checks for rotation every _ROTATION_CHECK_INTERVAL writes.

    Args:
        category: Event category (GATE/REVIEW/TEST/AGENT/SESSION/LEARN/USER).
                  Auto-detected from event_type if not provided.
    """
    global _write_counter

    if not _is_event_bus_enabled():
        return

    # Auto-detect category from event_type
    if not category:
        category = _TYPE_TO_CATEGORY.get(event_type, "")

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": datetime.now(tz=_UTC).isoformat(),
        "type": event_type,
        "category": category,
        "agent": agent,
        "session": session_id,
        "details": details or {},
    }
    line = json.dumps(event, ensure_ascii=False) + "\n"

    try:
        with _thread_lock:
            with _file_lock:
                with open(_EVENTS_FILE, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                _write_counter += 1
                needs_rotation = _write_counter >= _ROTATION_CHECK_INTERVAL
                if needs_rotation:
                    _write_counter = 0
    except Exception as e:
        logger.warning("EventBus write failed: %s", e)
        return

    # Rotation check outside write lock (re-acquires locks internally)
    if needs_rotation:
        try:
            _maybe_rotate()
        except Exception as e:
            logger.warning("EventBus rotation check failed: %s", e)


def read_events(last_n: int = 100) -> List[Dict]:
    """Read the last N events efficiently by seeking from end of file.

    Uses a reverse-read strategy: seeks to the end and reads backwards
    in chunks until enough lines are found. O(last_n) memory, not O(file_size).
    Thread-safe: acquires in-process lock to avoid reading during rotation.
    """
    if not _EVENTS_FILE.exists():
        return []

    with _thread_lock:
        with _file_lock:
            try:
                lines = _tail_lines(_EVENTS_FILE, last_n)
            except Exception:
                # Fallback: read whole file (old behavior)
                try:
                    lines = _EVENTS_FILE.read_text(encoding="utf-8").strip().splitlines()
                    lines = lines[-last_n:]
                except Exception:
                    return []

    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip corrupted lines gracefully
    return result


def query_events(
    event_type: Optional[str] = None,
    agent: Optional[str] = None,
    category: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 100,
) -> List[Dict]:
    """Query events with optional filters.

    Reads up to MAX_EVENTS events from tail, applies filters,
    returns up to `limit` results in chronological order.

    Args:
        event_type: Filter by exact event type match (e.g. "big_loop_start")
        agent: Filter by exact agent match (e.g. "big_loop")
        category: Filter by event category (GATE/REVIEW/TEST/AGENT/SESSION/LEARN/USER)
        since: Include events with ts >= since (ISO 8601 string)
        until: Include events with ts <= until (ISO 8601 string)
        limit: Maximum number of results to return (default 100)

    Returns:
        List of event dicts matching all specified filters, chronological order.
    """
    all_events = read_events(last_n=MAX_EVENTS)

    filtered = []
    for e in all_events:
        if event_type is not None and e.get("type") != event_type:
            continue
        if agent is not None and e.get("agent") != agent:
            continue
        if category is not None and e.get("category") != category:
            # Fallback: check if event_type belongs to the requested category
            if e.get("type") not in CATEGORIES.get(category, []):
                continue
        ts = e.get("ts", "")
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        filtered.append(e)
        if len(filtered) >= limit:
            break

    return filtered


def count_events_by_type(last_n: int = 10000) -> Dict[str, int]:
    """Count events grouped by type (for dashboard / Q2 analysis)."""
    events = read_events(last_n=last_n)
    counts: Dict[str, int] = {}
    for e in events:
        t = e.get("type", "unknown")
        counts[t] = counts.get(t, 0) + 1
    return counts


def rotate_events(
    max_events: int = MAX_EVENTS,
    keep_events: int = KEEP_EVENTS,
) -> Optional[str]:
    """Force rotate events.jsonl.

    Archives old events and keeps only the most recent `keep_events`.
    Atomic via os.replace() under both locks.
    Returns the archive file path if rotation occurred, None otherwise.
    """
    if not _EVENTS_FILE.exists():
        return None

    with _thread_lock:
        with _file_lock:
            lines = _EVENTS_FILE.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) <= max_events:
                return None
            return _do_rotate(lines, keep_events)


def get_event_count() -> int:
    """Get approximate event count without loading full file."""
    if not _EVENTS_FILE.exists():
        return 0
    try:
        # Count newlines efficiently
        count = 0
        with open(_EVENTS_FILE, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                count += chunk.count(b"\n")
        return count
    except Exception:
        return 0


# --- Internal helpers ---

def _tail_lines(filepath: Path, n: int, chunk_size: int = 8192) -> List[str]:
    """Read last N lines of a file efficiently by seeking from end.

    Reads backwards in chunks of chunk_size bytes until at least N lines
    are found. Much faster than reading the entire file for large files.
    """
    with open(filepath, "rb") as f:
        f.seek(0, 2)  # Seek to end
        file_size = f.tell()

        if file_size == 0:
            return []

        lines_found: List[bytes] = []
        remaining = file_size
        fragment = b""

        while remaining > 0 and len(lines_found) < n + 1:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            f.seek(remaining)
            chunk = f.read(read_size)
            chunk = chunk + fragment
            parts = chunk.split(b"\n")
            fragment = parts[0]
            lines_found = parts[1:] + lines_found

        # The fragment is the beginning of the first line
        if fragment:
            lines_found = [fragment] + lines_found

    # Decode and return last N non-empty lines
    decoded = []
    for raw in lines_found:
        try:
            line = raw.decode("utf-8").strip()
            if line:
                decoded.append(line)
        except UnicodeDecodeError:
            continue

    return decoded[-n:]


def _maybe_rotate():
    """Check if rotation is needed and perform it (under locks)."""
    if not _EVENTS_FILE.exists():
        return

    with _thread_lock:
        with _file_lock:
            event_count = get_event_count()
            if event_count > MAX_EVENTS:
                lines = _EVENTS_FILE.read_text(encoding="utf-8").strip().splitlines()
                _do_rotate(lines, KEEP_EVENTS)


def _do_rotate(lines: List[str], keep_events: int) -> str:
    """Perform atomic rotation: archive old, keep recent via os.replace().

    Caller MUST hold both _thread_lock and _file_lock.
    """
    _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(tz=_UTC).strftime("%Y-%m-%d_%H%M%S")
    archive_file = _ARCHIVE_DIR / f"{ts}_events.jsonl"

    # 1. Write old events to archive (only if there are events to archive)
    archive_lines = lines[:-keep_events] if len(lines) > keep_events else []
    archive_created = False
    if archive_lines:
        archive_file.write_text(
            "\n".join(archive_lines) + "\n", encoding="utf-8"
        )
        archive_created = True

    # 2. Write kept events to temp file (same dir for same-filesystem rename)
    recent = lines[-keep_events:]
    tmp_file = _EVENTS_FILE.with_suffix(".tmp")
    tmp_file.write_text(
        "\n".join(recent) + "\n", encoding="utf-8"
    )

    # 3. Atomic replace (safe on NTFS and POSIX)
    os.replace(str(tmp_file), str(_EVENTS_FILE))

    logger.info(
        "EventBus rotated: archived %d events to %s, kept %d recent",
        len(archive_lines), archive_file.name, len(recent),
    )
    return str(archive_file) if archive_created else ""


def _reset_for_testing(data_dir: Path, events_file: Path) -> None:
    """Reset module state for test isolation. Test-only.

    Updates file paths, lock targets, and config cache so tests
    use isolated temp directories without touching production data.
    """
    global _DATA_DIR, _EVENTS_FILE, _ARCHIVE_DIR
    global _write_counter, _cached_event_bus_enabled, _file_lock
    _DATA_DIR = data_dir
    _EVENTS_FILE = events_file
    _ARCHIVE_DIR = data_dir / "events_archive"
    _write_counter = 0
    _cached_event_bus_enabled = None
    _file_lock = filelock.FileLock(str(events_file) + ".lock", timeout=10)
