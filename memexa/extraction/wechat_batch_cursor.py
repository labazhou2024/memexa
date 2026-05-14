"""WeChat batch ingest cursor — persistent state for daily-batch mode.

Schema v2 (sec-2 + sec-5 hardened):
  {
    "schema_version": 2,
    "global_last_ingested_ts": float | null,        # canonical "we tried up to here"
    "per_chat": {
      "<chat_hash>": {                              # sha256(chat_name)[:16]
        "last_ingested_ts": float,
        "last_ingested_msg_sha": str,               # sha256[:32] (128-bit)
        "last_advance_at_utc": str
      }
    },
    "daemon_last_seen_ts": float | null,
    "updated_at": str
  }

HARD RULE: chat_hash dict key MUST be sha256(chat_name)[:16] — raw wxid /
display name FORBIDDEN (would persist PII to disk per sec-2 fix).

Corrupt-recovery: missing file or JSON parse fail → returns default with
global_last_ingested_ts = now - 7d (lookback clamp per R-1).
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from memexa.core.atomic_io import atomic_write_json, safe_read_json


def emit(event: str, payload: dict) -> None:
    """Soft trace emit; ignores unknown events (best-effort observability)."""
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass

_DEFAULT_LOOKBACK_SEC = 7 * 86400
_SCHEMA_VERSION = 2
# Closure A plan_v3 CRITICAL fix (consistency-iter3-2 + RP-7 + RP-24 + RP-27):
# unify chat_hash + msg_sha to SAME HASH_LEN=32 (was 16/32 split). Single
# source: memexa.chat.metadata_builder.HASH_LEN. The previous [:16] for
# chat_hash key violated AC-U10-15 + AC-U10-25 + RP-5 (128-bit preimage floor).
from memexa.chat.metadata_builder import HASH_LEN
_HASH_LEN_KEY = HASH_LEN   # was 16; now 32 (128-bit preimage resistance)
_HASH_LEN_MSG = HASH_LEN   # was 32; same constant now


def chat_hash(chat_name: str) -> str:
    """sec-2 + RP-5: derive PII-free per-chat key. sha256(name)[:HASH_LEN] = sha256(name)[:32].

    Per consistency-iter3-2 CRITICAL fix: HASH_LEN=32 (128-bit preimage resistance);
    single source via memexa.chat.metadata_builder.HASH_LEN constant.
    """
    if not chat_name:
        chat_name = ""
    return hashlib.sha256(chat_name.encode("utf-8")).hexdigest()[:HASH_LEN]


def _default_cursor() -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "global_last_ingested_ts": time.time() - _DEFAULT_LOOKBACK_SEC,
        "per_chat": {},
        "daemon_last_seen_ts": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


class CursorReader:
    """Thread-safe cursor reader (cursor file is append-replace; reads are atomic)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict:
        """Return cursor dict; corrupt/missing → default with 7d lookback clamp."""
        if not self.path.exists():
            d = _default_cursor()
            try:
                emit("wechat_cursor_read", {"path": str(self.path), "result": "missing_default"})
            except Exception:
                pass
            return d
        loaded = safe_read_json(self.path)
        if loaded is None or not isinstance(loaded, dict):
            d = _default_cursor()
            try:
                emit("wechat_cursor_read", {"path": str(self.path), "result": "corrupt_default"})
            except Exception:
                pass
            return d
        if loaded.get("schema_version") != _SCHEMA_VERSION:
            # v1→v2 migration: not implemented; raise so caller knows
            raise NotImplementedError(
                f"cursor schema_version={loaded.get('schema_version')} != {_SCHEMA_VERSION}; "
                "no migration path; backup + remove file to reset"
            )
        try:
            emit("wechat_cursor_read", {"path": str(self.path), "result": "ok",
                                         "global_ts": loaded.get("global_last_ingested_ts"),
                                         "per_chat_count": len(loaded.get("per_chat", {}))})
        except Exception:
            pass
        return loaded


class CursorWriter:
    """Atomic cursor writer (tmp+rename via atomic_io)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def write_global(self, ts: float, daemon_last_seen_ts: Optional[float] = None) -> None:
        """Advance global_last_ingested_ts atomically. AFTER outbox-fsync."""
        cur = CursorReader(self.path).read()
        cur["global_last_ingested_ts"] = float(ts)
        if daemon_last_seen_ts is not None:
            cur["daemon_last_seen_ts"] = float(daemon_last_seen_ts)
        cur["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.path, cur)
        try:
            emit("wechat_cursor_advanced", {"scope": "global", "ts": ts})
        except Exception:
            pass

    def write_per_chat(self, chat_name: str, ts: float, msg_sha: str) -> None:
        """Advance per-chat ts atomically. AFTER graph-write success per chat
        (logic-iter2-1: per-chat is graph-write, not outbox-fsync).
        """
        if len(msg_sha) != _HASH_LEN_MSG:
            raise ValueError(
                f"msg_sha must be {_HASH_LEN_MSG} hex chars (128-bit); got len={len(msg_sha)}"
            )
        ch = chat_hash(chat_name)
        cur = CursorReader(self.path).read()
        cur["per_chat"][ch] = {
            "last_ingested_ts": float(ts),
            "last_ingested_msg_sha": msg_sha,
            "last_advance_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        cur["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.path, cur)
        try:
            emit("wechat_cursor_advanced", {"scope": "per_chat", "chat_hash": ch, "ts": ts})
        except Exception:
            pass


def lookback_clamp(cursor_global_ts: Optional[float], now: Optional[float] = None,
                   lookback_sec: int = _DEFAULT_LOOKBACK_SEC) -> float:
    """Compute since_ts = max(cursor, now - lookback). Never returns epoch-0
    (R-1 防 7B+ msg flood)."""
    n = now if now is not None else time.time()
    floor = n - lookback_sec
    if cursor_global_ts is None:
        return floor
    return max(float(cursor_global_ts), floor)
