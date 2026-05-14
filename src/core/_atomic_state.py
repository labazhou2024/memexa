"""Atomic state-file helper — single source of truth for JSON RMW safety.

Ships as part of the 2026-04-20 audit-fix Cluster 4 response. The SEC-R1-004
finding identified ad-hoc `read_text` + mutate + `write_text` sequences in
several state files (harness_state.json, staleness briefing side-effects, etc.)
that corrupt under concurrent hooks. This module centralizes the pattern:

  * tmp-file + os.replace for atomic write
  * optional FileLock (cross-process) around the entire read-modify-write
  * UTF-8 with ensure_ascii=False for CJK content
  * Never raises on IO error — returns False and logs.

Usage:
    from src.core._atomic_state import atomic_update_json

    def _mutate(state):
        state["counter"] = state.get("counter", 0) + 1
        return state

    ok = atomic_update_json(
        path=Path("my_state.json"),
        mutator=_mutate,
        default={"counter": 0},
        lock_path=Path("my_state.lock"),  # optional
    )
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

try:
    from filelock import FileLock, Timeout as FilelockTimeout
    _FILELOCK_OK = True
except ImportError:  # pragma: no cover — filelock is a required dep
    _FILELOCK_OK = False

logger = logging.getLogger(__name__)

Mutator = Callable[[Dict[str, Any]], Dict[str, Any]]
JsonShape = Union[Dict[str, Any], list]


def _read_json(path: Path, default: JsonShape) -> JsonShape:
    if not path.exists():
        return _clone_default(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("_atomic_state: unreadable %s, returning default", path)
        return _clone_default(default)


def _clone_default(default: JsonShape) -> JsonShape:
    if isinstance(default, dict):
        return dict(default)
    if isinstance(default, list):
        return list(default)
    return default


def _atomic_write(path: Path, data: JsonShape) -> bool:
    """tmp-file + os.replace. Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
        return True
    except OSError as e:
        logger.warning("_atomic_state: write failed for %s: %s", path, e)
        return False


def atomic_update_json(
    path: Path,
    mutator: Mutator,
    *,
    default: Optional[JsonShape] = None,
    lock_path: Optional[Path] = None,
    lock_timeout: float = 10.0,
) -> bool:
    """Read-modify-write a JSON file atomically.

    Args:
        path: target JSON file
        mutator: pure function taking the loaded dict (or default) and
                 returning the new dict to write. If it returns None the
                 file is NOT modified.
        default: value to seed mutator with when file is missing/corrupt
                 (defaults to {})
        lock_path: optional companion .lock file for cross-process safety
        lock_timeout: how long to wait for lock (seconds)

    Returns:
        True if the file was updated (or intentionally no-op'd), False on
        lock timeout or write failure. Never raises.
    """
    if default is None:
        default = {}

    def _run():
        data = _read_json(path, default)
        try:
            new_data = mutator(data)
        except Exception as e:
            logger.warning(
                "_atomic_state: mutator raised for %s: %s — skipping write",
                path, e,
            )
            return False
        if new_data is None:
            # Explicit no-op
            return True
        return _atomic_write(path, new_data)

    if lock_path is None or not _FILELOCK_OK:
        return _run()

    lock = FileLock(str(lock_path), timeout=lock_timeout)
    try:
        with lock:
            return _run()
    except FilelockTimeout:
        logger.warning("_atomic_state: lock timeout on %s after %ss",
                       lock_path, lock_timeout)
        return False


def atomic_read_json(
    path: Path,
    *,
    default: Optional[JsonShape] = None,
) -> JsonShape:
    """Lock-free read with same error handling as atomic_update_json."""
    if default is None:
        default = {}
    return _read_json(path, default)
