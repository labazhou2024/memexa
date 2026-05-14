"""U5 (long_term_plan_v2.md, 2026-04-27) — plan_v_latest pointer indirection.

Provides canonical "latest plan version" indirection for autopilot plan_v<N>.md
lifecycle. Replaces ad-hoc glob+max parse duplicated across plan_retro_gate,
plan_uniformity_check, ac_verifier, and test fixtures.

Design (per BL-9 of long_term_plan_v2.md):
- Pointer file `<task_dir>/plan_v_latest.md`: text file (NOT a real symlink)
  containing one filename like `plan_v3.md\n`. Mirrors `task_dir_layout._latest`
  pattern (proven Windows non-admin safe; os.symlink fails WinError 1314 without
  SeCreateSymbolicLinkPrivilege).
- Atomic write: tmp file + os.replace + 3× exponential retry (OneDrive race tolerance).
- chmod 0o444 on older plan_v<M>.md (M < version) — best-effort: 3-state status
  ("ok" all chmod ok + pointer ok; "best_effort" pointer ok + ≥1 chmod failed;
  "failed" pointer write failed → raises OSError) per HARD RULE
  feedback_partial_fix_explicit_unknown_state.
- Reparse-point guard (HARD RULE feedback_ntfs_junction_reparse_point): pointer
  file AND target file checked via st_file_attributes & 0x400 on Windows.
- task_id traversal guard (security-iter2-1 HIGH): _VALID_TASK_ID_PATTERN check
  + resolve().relative_to(tasks_root) defense-in-depth.

Note (security-iter2-2): MEMEXA_TASK_DIR env in task_dir_layout.tasks_root() lacks
parent-allowlist guard (pre-existing in task_dir_layout). This module's task_id
validation closes the surface for adversarial task_id; full env-allowlist tracked
as U18 deferred. Future maintainers: keep plan_versioning AND plan_retro_gate
env-handling aligned (currently plan_retro_gate._task_dir hardcodes the base path
and is NOT env-aware; this is an intentional divergence — plan_retro_gate consumers
ignore MEMEXA_TASK_DIR while plan_versioning honors it).

Public API:
  set_latest_plan(task_id, version) -> dict (status, version, pointer_path, ...)
  get_latest_plan_path(task_id) -> Optional[Path]
  read_pointer(task_id) -> Optional[str]
  is_immutable(plan_path) -> bool
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout as FilelockTimeout

from src.core.task_dir_layout import task_dir, tasks_root

logger = logging.getLogger(__name__)

_VALID_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_POINTER_LINE_PATTERN = re.compile(r"^plan_v(\d+)\.md$")
_POINTER_FORBIDDEN = ("..", "/", "\\", ":", "\x00")
_POINTER_FILENAME = "plan_v_latest.md"
_POINTER_LOCK_NAME = ".plan_versioning.lock"
_LOCK_TIMEOUT = 10.0
_REPARSE_FILE_ATTRIBUTE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _validate_task_id(task_id: str) -> Path:
    """Validate task_id and return the resolved task_dir.

    Raises ValueError if task_id is malformed or escapes tasks_root.
    """
    if not task_id or not _VALID_TASK_ID_PATTERN.match(task_id):
        raise ValueError(f"invalid task_id: {task_id!r}")
    d = task_dir(task_id)
    try:
        d_resolved = d.resolve()
        d_resolved.relative_to(tasks_root().resolve())
    except (ValueError, OSError) as exc:
        raise ValueError(f"task_id escapes tasks_root: {task_id!r}") from exc
    return d


def _validate_pointer_line(line: str) -> Optional[str]:
    """Return cleaned pointer-line if schema-valid; None otherwise.

    Schema: ^plan_v(\\d+)\\.md$ AND no traversal chars (.., /, \\, :, NUL).
    """
    if line is None:
        return None
    cleaned = line.strip()
    if not cleaned:
        return None
    for forbidden in _POINTER_FORBIDDEN:
        if forbidden in cleaned:
            return None
    if not _POINTER_LINE_PATTERN.match(cleaned):
        return None
    return cleaned


def _safe_target_check(p: Path) -> bool:
    """Return True if p is safe to follow (NOT a NTFS reparse point).

    On Windows, checks st_file_attributes & 0x400 (FILE_ATTRIBUTE_REPARSE_POINT).
    On POSIX where the attribute is absent, returns True (no reparse concept).
    """
    if not p.exists():
        return True  # absence handled by caller
    try:
        st = p.stat()
    except OSError:
        return False
    attr = getattr(st, "st_file_attributes", 0) or 0
    if attr & _REPARSE_FILE_ATTRIBUTE:
        logger.warning("plan_versioning: reparse-point detected at %s; skipping", p)
        return False
    return True


def _glob_max_fallback(d: Path) -> Optional[Path]:
    """Glob plan_v*.md in d, return file with max integer version. None if empty.

    Mirrors plan_retro_gate._find_latest_plan logic. ValueError-tolerant
    (skips suffixes like '0_backup' — security-iter2-6 fix).
    """
    if not d.is_dir():
        return None
    candidates = []
    for p in d.glob("plan_v*.md"):
        if p.name == _POINTER_FILENAME:
            continue
        suffix = p.stem[len("plan_v"):]
        try:
            n = int(suffix)
        except ValueError:
            continue
        candidates.append((n, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    # security-iter2-3 extension: skip reparse-pointed files in glob fallback too
    for _, p in candidates:
        if _safe_target_check(p):
            return p
    return None


def _emit_trace(event: str, payload: dict) -> None:
    """Best-effort trace event (fail-soft)."""
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:  # pragma: no cover (best-effort)
        logger.debug("plan_versioning trace emit failed: %s", event, exc_info=True)


def _atomic_write_pointer(d: Path, target_filename: str, max_retries: int = 3) -> bool:
    """Write pointer file atomically (tmp + os.replace + retry). Returns True on success."""
    pointer = d / _POINTER_FILENAME
    tmp = d / (_POINTER_FILENAME + ".tmp")
    content = (
        "# auto-generated by plan_versioning v1; do not edit by hand\n"
        f"{target_filename}\n"
    )
    delay = 0.05
    for attempt in range(max_retries):
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(str(tmp), str(pointer))
            return True
        except OSError as e:
            logger.debug("pointer write attempt %d failed: %s", attempt + 1, e)
            if attempt == max_retries - 1:
                # logic-iter1-1 fix: clean up stale tmp on permanent failure
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                return False
            time.sleep(delay)
            delay *= 2
    return False


def set_latest_plan(task_id: str, version: int) -> dict:
    """Set plan_v<version>.md as the latest plan for task_id.

    1. Validates task_id (traversal guard).
    2. Asserts plan_v<version>.md exists in task_dir; raises FileNotFoundError otherwise.
    3. Acquires FileLock; chmod older plan_v<M>.md (M < version) to 0o444 (best-effort).
    4. Atomic write of `<task_dir>/plan_v_latest.md` containing `plan_v<version>.md`.

    Returns dict {status, version, pointer_path, immutable_count, chmod_failed_count, chmod_failures}.
    Raises FileNotFoundError if target missing; ValueError if task_id invalid;
    OSError if pointer write fails after 3 retries.
    """
    d = _validate_task_id(task_id)
    if not isinstance(version, int) or version < 0:
        raise ValueError(f"version must be non-negative int, got {version!r}")
    target = d / f"plan_v{version}.md"
    if not target.is_file():
        raise FileNotFoundError(f"plan_v{version}.md missing in {d}")

    lock_path = d / _POINTER_LOCK_NAME
    immutable_count = 0
    chmod_failed_count = 0
    chmod_failures: list = []

    try:
        with FileLock(str(lock_path), timeout=_LOCK_TIMEOUT):
            # Step A: chmod older versions FIRST (concurrency-safe ordering)
            for p in d.glob("plan_v*.md"):
                if p.name == _POINTER_FILENAME or p.name == target.name:
                    continue
                suffix = p.stem[len("plan_v"):]
                try:
                    m = int(suffix)
                except ValueError:
                    continue
                if m >= version:
                    continue
                try:
                    os.chmod(p, 0o444)
                    if os.access(p, os.W_OK):
                        # Windows ACL: chmod silently no-op on ACL-controlled files
                        # logic-iter1-2 fix: don't double-count as immutable
                        chmod_failed_count += 1
                        chmod_failures.append(str(p))
                        _emit_trace("plan_versioning_immutable_chmod_failed", {
                            "task_id": task_id[:200], "plan_path": str(p)[:300].replace("\n", " ").replace("\r", " "),
                            "errno": 0, "errmsg": "post-chmod still W_OK (Windows ACL?)",
                        })
                    else:
                        immutable_count += 1
                except OSError as e:
                    chmod_failed_count += 1
                    chmod_failures.append(str(p))
                    _emit_trace("plan_versioning_immutable_chmod_failed", {
                        "task_id": task_id[:200], "plan_path": str(p)[:300].replace("\n", " ").replace("\r", " "),
                        "errno": e.errno or 0, "errmsg": str(e)[:200].replace("\n", " "),
                    })

            # Step B: write pointer LAST
            if not _atomic_write_pointer(d, target.name):
                raise OSError(f"pointer write failed after retries: {d / _POINTER_FILENAME}")
    except FilelockTimeout as e:
        raise OSError(f"plan_versioning FileLock timeout: {lock_path}") from e

    status = "ok" if chmod_failed_count == 0 else "best_effort"
    pointer_path = str(d / _POINTER_FILENAME)
    _emit_trace("plan_versioning_symlink_created", {
        "task_id": task_id, "version": version, "pointer_path": pointer_path,
        "immutable_count": immutable_count, "chmod_failed_count": chmod_failed_count,
        "status": status,
    })
    return {
        "status": status, "version": version, "pointer_path": pointer_path,
        "immutable_count": immutable_count, "chmod_failed_count": chmod_failed_count,
        "chmod_failures": chmod_failures,
    }


def read_pointer(task_id: str) -> Optional[str]:
    """Low-level pointer read. Returns first non-comment, non-empty, schema-valid line.

    Returns None if pointer absent / corrupt / schema-invalid / reparse-point.
    Does NOT raise on adversarial task_id (returns None) — for read-side robustness.
    """
    try:
        d = _validate_task_id(task_id)
    except ValueError:
        return None
    pointer = d / _POINTER_FILENAME
    if not pointer.is_file():
        return None
    if not _safe_target_check(pointer):
        return None
    try:
        text = pointer.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return _validate_pointer_line(s)
    return None


def get_latest_plan_path(task_id: str) -> Optional[Path]:
    """Resolve the latest plan_v<N>.md path for task_id.

    Order:
    1. read pointer file; if valid + target exists + target safe → return target.
    2. Otherwise fall back to glob-max (mirrors legacy _find_latest_plan).
    3. Return None if neither path yields a file.

    Per HARD RULE feedback_priority_inverted_fallback_2x2_matrix, all 4 cells
    of (pointer exists/missing) × (target exists/missing) handled.
    """
    try:
        d = _validate_task_id(task_id)
    except ValueError:
        return None
    if not d.is_dir():
        return None

    target_name = read_pointer(task_id)
    if target_name is not None:
        target = d / target_name
        if target.is_file() and _safe_target_check(target):
            return target
        # pointer exists but target missing/unsafe → fall through to glob-max
        logger.debug("plan_versioning pointer→%s: target missing/unsafe; falling back",
                     target_name)
    return _glob_max_fallback(d)


def is_immutable(plan_path: Path) -> bool:
    """Return True if plan_path is not writable (read-only at fs level).

    Note: on Windows, returns True iff the read-only bit is set; ACL deny entries
    are not consulted (HARD RULE feedback_partial_fix_explicit_unknown_state best-effort).
    """
    try:
        return not os.access(plan_path, os.W_OK)
    except OSError:
        return False


__all__ = [
    "set_latest_plan", "get_latest_plan_path", "read_pointer", "is_immutable",
]
