"""PreCompact hook: snapshot critical state before context compression.

Safety net for context compaction events. Saves harness_state and recent
patterns to disk so they survive compression.

Hook input:
    {
      "hook_event_name": "PreCompact",
      "trigger": "manual|auto",
      "transcript_path": "...",
      "session_id": "..."
    }

Retention: keep latest 5 snapshots in .claude/sessions/, archive older.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from memexa.core._hook_utils import (  # noqa: E402
    read_hook_input,
    emit_decision,
    log_hook_event,
    atomic_json_write,
    safe_load_json,
    get_workspace_paths,
)


_HOOK_NAME = "precompact"
_PATHS = get_workspace_paths()
_SNAPSHOTS_DIR = _PATHS["workspace"] / ".claude" / "sessions"
_ARCHIVE_DIR = _SNAPSHOTS_DIR / "archive"

_MAX_LIVE_SNAPSHOTS = 5


def _take_snapshot(trigger: str, session_id: str) -> Path:
    """Snapshot harness + active counter + persistent_mode state."""
    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    snap_file = _SNAPSHOTS_DIR / f"precompact_{ts}.json"

    snapshot = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "trigger": trigger,
        "session_id": session_id,
        "harness": safe_load_json(_PATHS["harness"]),
        "agent_active_count": safe_load_json(_PATHS["data"] / "agent_active_count.json"),
        "persistent_mode": safe_load_json(_PATHS["data"] / "persistent_mode_state.json"),
        "task_spec": safe_load_json(_PATHS["data"] / "task_spec.json"),
    }
    atomic_json_write(snap_file, snapshot)
    return snap_file


def _rotate_snapshots() -> int:
    """Archive snapshots older than _MAX_LIVE_SNAPSHOTS. Returns count rotated."""
    if not _SNAPSHOTS_DIR.exists():
        return 0
    snaps = sorted(_SNAPSHOTS_DIR.glob("precompact_*.json"))
    if len(snaps) <= _MAX_LIVE_SNAPSHOTS:
        return 0
    _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    rotated = 0
    for old in snaps[:-_MAX_LIVE_SNAPSHOTS]:
        try:
            old.rename(_ARCHIVE_DIR / old.name)
            rotated += 1
        except OSError:
            pass
    return rotated


def main() -> int:
    data = read_hook_input()
    if not data:
        return 0

    trigger = data.get("trigger", "auto")
    session_id = data.get("session_id", "")

    snap_file = _take_snapshot(trigger, session_id)
    rotated = _rotate_snapshots()

    log_hook_event(
        event_type="precompact_snapshot",
        hook_name=_HOOK_NAME,
        details={
            "trigger": trigger,
            "session_id": session_id,
            "snapshot_file": snap_file.name,
            "snapshots_rotated": rotated,
        },
    )

    emit_decision(decision="allow", hook_event_name="PreCompact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
