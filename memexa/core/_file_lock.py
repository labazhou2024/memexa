"""Cross-platform advisory file lock for short critical sections.

Implementation note (2026-04-23 fix):
  Windows `msvcrt.locking(fd, LK_LOCK, n)` locks a byte range starting at the
  file's current position. With append-mode writes, the actual write offset
  is the file's end (and can race across processes). Locking offset-0 of the
  target file does NOT serialise appends because lock range and write range
  don't overlap.

  Fix: use a SEPARATE lock file (`<path>.lock`). All writers acquire byte-0
  of the lock file as a mutex; the mutex doesn't overlap the real target file
  but serialises entry into the critical section, which is what we need.

Usage:
    with locked_open(path, "a", encoding="utf-8") as f:
        f.write(line); f.flush()
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

_LOG = logging.getLogger(__name__)

# S4 fix: don't permanently silence the "running unlocked" warning after
# first occurrence. We log every N-th failure so a persistently-unlocked
# process still raises the flag periodically (every ~10s of contention).
_UNLOCKED_COUNTER = 0
_WARN_EVERY = 50  # warn once per 50 failed acquisitions

_LOCK_RETRIES = 200           # 200 * 50ms = 10s worst case
_LOCK_RETRY_SLEEP = 0.05


def _warn_unlocked(reason: str) -> None:
    """Warn every N-th failed acquisition so a persistently-unlocked process
    surfaces its state in logs (S4 fix vs one-shot silencing)."""
    global _UNLOCKED_COUNTER
    _UNLOCKED_COUNTER += 1
    if _UNLOCKED_COUNTER == 1 or _UNLOCKED_COUNTER % _WARN_EVERY == 0:
        _LOG.warning(
            "locked_open running UNLOCKED (count=%d): %s",
            _UNLOCKED_COUNTER, reason,
        )


def _acquire_mutex(lock_path: Path) -> tuple:
    """Return (lock_fh, is_locked). lock_fh MUST be held until release."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Use os.open for portable O_RDWR+O_CREAT semantics; no 'a' append nuances.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fh = os.fdopen(fd, "r+b")
    locked = False

    if sys.platform == "win32":
        try:
            import msvcrt
            for _ in range(_LOCK_RETRIES):
                try:
                    # LK_NBLCK = non-blocking; fail fast and retry in Python so
                    # we don't sit in a 10s kernel wait that then throws.
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(_LOCK_RETRY_SLEEP)
            if not locked:
                _warn_unlocked("msvcrt.locking could not acquire after 10s")
        except ImportError:
            _warn_unlocked("msvcrt unavailable on this Python build")
    else:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        except Exception as e:
            _warn_unlocked(f"fcntl.flock unavailable: {e}")

    return fh, locked


def _release_mutex(fh: IO, locked: bool) -> None:
    try:
        if locked:
            if sys.platform == "win32":
                import msvcrt
                try:
                    os.lseek(fh.fileno(), 0, os.SEEK_SET)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                try:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    finally:
        try:
            fh.close()
        except Exception:
            pass


@contextmanager
def locked_open(path: Path | str, mode: str = "a", encoding: str | None = "utf-8") -> Iterator[IO]:
    """Open `path` for append (or other mode) serialised across processes
    via a neighbour `<path>.lock` mutex file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    lock_fh, locked = _acquire_mutex(lock_path)
    try:
        f = open(target, mode, encoding=encoding)
        try:
            yield f
        finally:
            try:
                f.flush()
            except Exception:
                pass
            f.close()
    finally:
        _release_mutex(lock_fh, locked)
