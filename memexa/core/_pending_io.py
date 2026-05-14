"""
Shared I/O layer for pending_approvals.json  (H4 fix, 2026-04-20).

promotion_engine and staleness_audit both load-mutate-save this JSON file.
Before this module, each carried its own lock-free load/save, so a
concurrent promote() <-> staleness_audit cycle could clobber each other
mid-update (lost update bug). Now both paths import from here and share
one FileLock on _PENDING_APPROVALS_LOCK.

Usage
-----
    from memexa.core._pending_io import pending_approvals_lock, _resolve_pending_paths

    with pending_approvals_lock():
        items = load_pending(pending_file)
        # mutate items
        save_pending(pending_file, items)

The lockfile path is derived from the same MEMEXA_DATA_DIR env override
the consuming modules honour, keeping test isolation working.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

_LOCK_TIMEOUT_SEC = 10.0


def default_lock_path(pending_file: Path) -> Path:
    """Derive the lockfile path alongside the pending_approvals file."""
    return pending_file.with_suffix(pending_file.suffix + ".lock")


@contextmanager
def pending_approvals_lock(pending_file: Path):
    """Acquire the shared FileLock for pending_approvals.json.

    Fails open (logs + still yields) only if filelock is not installed
    — not expected in production since R3 E3 mandates filelock.
    """
    lock_path = default_lock_path(pending_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from filelock import FileLock
    except ImportError:
        # Degrade to no-op lock. Governance will still function (albeit
        # with the pre-H4 race exposure) rather than hard-fail startup.
        yield
        return
    lock = FileLock(str(lock_path), timeout=_LOCK_TIMEOUT_SEC)
    with lock:
        yield


def load_pending(pending_file: Path) -> List[Dict[str, Any]]:
    """Read + parse pending_approvals.json. Empty list on missing/corrupt."""
    if not pending_file.exists():
        return []
    try:
        data = json.loads(pending_file.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_pending(pending_file: Path, items: List[Dict[str, Any]]) -> None:
    """Atomically write pending_approvals.json via tmp+replace."""
    import os as _os
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = pending_file.with_suffix(pending_file.suffix + ".tmp")
    tmp.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _os.replace(tmp, pending_file)
