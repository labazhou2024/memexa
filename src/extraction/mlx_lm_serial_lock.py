"""mlx_lm_serial_lock — Cross-process file-based serial lock for MLX-LM servers.

Used to serialise access to on-demand Mac MLX-LM model slots (e.g. Gemma-31B on
:18081) so that only one ingestion pipeline loads the model at a time, avoiding
OOM on the 36 GB M4 Max.

Lock file: memexa/data/MLX_SERVER_LOCK.flag
Format   : JSON {"owner": str, "model": str, "acquired_at": float,
                 "ttl": int, "token": str}

Race-safety strategy
--------------------
We use atomic write via a temporary file + os.rename (POSIX-rename is atomic on
local filesystems; on Windows it requires replacing the target if it exists — we
handle that with os.replace for the tmp file write but still check O_CREAT|O_EXCL
semantics by using a temp-file-then-rename pattern):

  1. Write desired JSON to <lock>.tmp  (overwrite ok)
  2. Try to acquire exclusively:
       - Read current lock file (if any).  If missing → try rename.
       - If present and NOT stale → wait poll_sec, retry.
       - If present and stale  → steal: os.replace(<lock>.tmp, lock_path).
       - If missing             → os.replace(<lock>.tmp, lock_path) and confirm.
  3. After rename, re-read file and verify token matches (handles two concurrent
     stealers — only one wins the rename race on most FS).

CLI
---
  python -m src.extraction.mlx_lm_serial_lock status
  python -m src.extraction.mlx_lm_serial_lock force-break
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path resolution — ASCII-only, no hard-coded CWD with non-ASCII characters.
# Resolve relative to this file's location (repo/memexa/dispatch/...).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "data"
LOCK_FILE = _DATA_DIR / "MLX_SERVER_LOCK.flag"

# Per-process mutex so that two threads in the same process don't race on the
# tmp-file rename.
_PROCESS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_lock() -> Optional[dict]:
    """Read and parse the lock file. Returns None if absent or corrupt."""
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_tmp(payload: dict) -> Path:
    """Write payload to a sibling .tmp file and return its path."""
    tmp = LOCK_FILE.with_suffix(".flag.tmp")
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return tmp


def _is_stale(lock_data: dict, now: Optional[float] = None) -> bool:
    """Return True if acquired_at + ttl < now."""
    if not isinstance(lock_data, dict):
        return True
    now = now if now is not None else time.time()
    acquired_at = lock_data.get("acquired_at", 0.0)
    ttl = lock_data.get("ttl", 0)
    return (acquired_at + ttl) < now


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def acquire(
    model: str,
    ttl_sec: int = 600,
    owner: str = "anon",
    poll_sec: float = 2.0,
    timeout_sec: int = 1800,
) -> Optional[str]:
    """Block until the serial lock is acquired.

    Parameters
    ----------
    model       : Model identifier stored in the lock file (informational).
    ttl_sec     : Seconds until lock is considered stale and may be stolen.
    owner       : Human-readable owner tag (process name / hostname).
    poll_sec    : Seconds between polling attempts when lock is held.
    timeout_sec : Give up and return None after this many seconds.

    Returns
    -------
    An opaque hex token string on success, or None if timeout exceeded.
    Token must be passed to release() to release the lock.
    """
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")

    token = uuid.uuid4().hex
    deadline = time.time() + timeout_sec

    while True:
        now = time.time()
        if now >= deadline:
            return None

        with _PROCESS_LOCK:
            existing = _read_lock()
            if existing is None or _is_stale(existing, now):
                # Lock is free or stale — try to take it.
                payload = {
                    "owner": owner,
                    "model": model,
                    "acquired_at": now,
                    "ttl": ttl_sec,
                    "token": token,
                }
                tmp = _write_tmp(payload)
                try:
                    # os.replace is atomic-ish on Windows (unlike os.rename
                    # which raises FileExistsError when target exists).
                    os.replace(str(tmp), str(LOCK_FILE))
                except OSError:
                    # Another process won the replace race; clean up tmp.
                    try:
                        os.remove(str(tmp))
                    except OSError:
                        pass
                    # Fall through to re-check below.

                # Confirm we actually own the lock (re-read after replace).
                confirmed = _read_lock()
                if confirmed is not None and confirmed.get("token") == token:
                    return token
                # Another writer beat us; keep looping.
            # else: lock is held and not stale — wait outside process lock.

        time.sleep(poll_sec)


def release(token: str) -> bool:
    """Release the lock iff the stored token matches.

    Returns True if the lock was released, False if the token did not match
    (no-op — idempotent).
    """
    if not isinstance(token, str) or not token:
        return False

    with _PROCESS_LOCK:
        existing = _read_lock()
        if existing is None:
            # Already gone — idempotent success.
            return True
        if existing.get("token") != token:
            return False
        try:
            os.remove(str(LOCK_FILE))
            return True
        except FileNotFoundError:
            # Someone else removed it simultaneously — still counts as success.
            return True
        except OSError:
            return False


def status() -> dict:
    """Return current lock state.

    Returns
    -------
    dict with keys:
      held      : bool
      owner     : str  (empty string if not held)
      model     : str
      expires_at: float (epoch seconds; 0.0 if not held)
      stale     : bool
    """
    data = _read_lock()
    if data is None:
        return {"held": False, "owner": "", "model": "", "expires_at": 0.0, "stale": False}

    now = time.time()
    acquired_at = data.get("acquired_at", 0.0)
    ttl = data.get("ttl", 0)
    expires_at = acquired_at + ttl
    stale = expires_at < now
    return {
        "held": True,
        "owner": data.get("owner", ""),
        "model": data.get("model", ""),
        "expires_at": expires_at,
        "stale": stale,
    }


def force_break(reason: str = "") -> bool:
    """Emergency: delete lock unconditionally.

    Returns True if the file existed and was removed (or was already absent
    during the remove attempt), False on unexpected OS error.
    """
    with _PROCESS_LOCK:
        existed = LOCK_FILE.exists()
        if not existed:
            return False
        try:
            os.remove(str(LOCK_FILE))
            return True
        except FileNotFoundError:
            # Already gone between exists() and remove() — still counts.
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: list) -> int:
    """python -m src.extraction.mlx_lm_serial_lock {status|force-break}"""
    import sys

    cmd = argv[1] if len(argv) > 1 else "status"

    if cmd == "status":
        s = status()
        print(json.dumps(s, indent=2))
        return 0

    if cmd == "force-break":
        reason = argv[2] if len(argv) > 2 else "manual CLI"
        ok = force_break(reason)
        if ok:
            print(f"Lock broken (reason: {reason!r})")
        else:
            print("Lock was not held (nothing to break)")
        return 0

    print(f"Unknown command: {cmd!r}. Use: status | force-break", file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv))
