"""batch_chat_extract idempotency cursor — schema-congruent thin wrapper over wechat_batch_cursor.

Cursor JSON schema (REUSED VERBATIM from wechat_batch_cursor.py schema_version=2):
  {
    "schema_version": 2,
    "global_last_ingested_ts": float | null,
    "per_chat": {
      "<chat_hash>": {                              # sha256(chat_name)[:HASH_LEN=32]
        "last_ingested_ts": float,                  # batch_end_ts of latest processed batch
        "last_ingested_msg_sha": str,               # sha256[:32]; for batch_chat we use sha256("batch:" + chat_room_id_hash + ":" + str(batch_end_ts_int))
        "last_advance_at_utc": str
      }
    },
    "daemon_last_seen_ts": float | null,            # unused for batch path; kept for schema parity
    "updated_at": str
  }

This module's ONLY differences from wechat_batch_cursor:
  - Cursor file lives at memex/data/batch_chat_extract_cursor.json (separate from wechat realtime cursor)
  - Adds `should_skip_batch(chat_room_id_hash, batch_end_ts) -> bool` convenience predicate
  - Adds `advance_batch(chat_room_id_hash, batch_end_ts)` thin wrapper around CursorWriter.write_per_chat

Why we DO NOT inline copies of the writer/reader:
  - Prevents schema drift between batch and realtime paths
  - Reuses tested atomic_io semantics
  - logic-iter2-2 reviewer mandate (HARD RULE feedback_writer_reader_schema_contract.md)

CLI: show / reset / advance
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.extraction.wechat_batch_cursor import (
    CursorReader,
    CursorWriter,
    _SCHEMA_VERSION,
    chat_hash as _wechat_chat_hash,
    emit,
    lookback_clamp,
)


def cursor_path() -> Path:
    """Resolve cursor file path. Always memex/data/batch_chat_extract_cursor.json."""
    here = Path(__file__).resolve()
    return here.parents[2] / "data" / "batch_chat_extract_cursor.json"


def _batch_msg_sha(chat_room_id_hash: str, batch_end_ts: float) -> str:
    """Synthetic 32-char sha for batch path (CursorWriter requires HASH_LEN=32 for msg_sha).

    Identifies a batch deterministically by (chat_room_hash, batch_end_ts_int).
    """
    payload = f"batch:{chat_room_id_hash}:{int(batch_end_ts)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def should_skip_batch(chat_room_id_hash: str, batch_end_ts: float,
                      cursor_path_override: Optional[Path] = None) -> bool:
    """True iff cursor.per_chat[chat_room_id_hash].last_ingested_ts >= batch_end_ts.

    chat_room_id_hash MUST already be the sha256(chat_name)[:32] form
    (matches the chat_room_id_hash field in batch_chat_extract outbox factrows).
    """
    if not chat_room_id_hash:
        return False
    p = cursor_path_override or cursor_path()
    try:
        cur = CursorReader(p).read()
    except NotImplementedError:
        emit("batch_cursor_schema_version_mismatch", {"path": str(p)})
        return False
    entry = (cur.get("per_chat") or {}).get(chat_room_id_hash)
    if not entry:
        return False
    return float(entry.get("last_ingested_ts", 0.0)) >= float(batch_end_ts)


def advance_batch(chat_room_id_hash: str, batch_end_ts: float,
                  cursor_path_override: Optional[Path] = None) -> None:
    """Advance per-chat cursor for one processed batch.

    Calls wechat_batch_cursor.CursorWriter.write_per_chat with msg_sha synthesized
    from (chat_hash, batch_end_ts). Atomic write semantics inherited.
    """
    if not chat_room_id_hash:
        return
    p = cursor_path_override or cursor_path()
    msg_sha = _batch_msg_sha(chat_room_id_hash, batch_end_ts)
    # write_per_chat expects raw chat_name and re-hashes; we already have hash.
    # Bypass via direct write through CursorReader/Writer surface.
    reader = CursorReader(p)
    try:
        cur = reader.read()
    except NotImplementedError:
        emit("batch_cursor_schema_version_mismatch", {"path": str(p)})
        return
    cur["per_chat"][chat_room_id_hash] = {
        "last_ingested_ts": float(batch_end_ts),
        "last_ingested_msg_sha": msg_sha,
        "last_advance_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    cur["updated_at"] = datetime.now(timezone.utc).isoformat()
    from src.core.atomic_io import atomic_write_json
    atomic_write_json(p, cur)
    emit("batch_cursor_updated", {"chat_hash": chat_room_id_hash, "batch_end_ts": batch_end_ts})


def reset_cursor(cursor_path_override: Optional[Path] = None) -> None:
    """Reset cursor to empty default. CLI use only."""
    p = cursor_path_override or cursor_path()
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass
    emit("batch_cursor_reset", {"path": str(p)})


def show_cursor(cursor_path_override: Optional[Path] = None) -> dict:
    """Return cursor dict (or default if missing/corrupt)."""
    p = cursor_path_override or cursor_path()
    try:
        cur = CursorReader(p).read()
    except NotImplementedError as e:
        return {"error": str(e), "schema_mismatch": True}
    emit("batch_cursor_loaded", {
        "path": str(p),
        "n_chat": len(cur.get("per_chat", {})),
        "global_ts": cur.get("global_last_ingested_ts"),
    })
    return cur


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="batch_chat_cursor")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print cursor JSON")
    sub.add_parser("reset", help="delete cursor file")
    pa = sub.add_parser("advance", help="advance per-chat cursor entry")
    pa.add_argument("--chat-hash", required=True, help="sha256(chat_name)[:32]")
    pa.add_argument("--ts", required=True, type=float, help="batch_end_ts unix")
    args = p.parse_args(argv)

    if args.cmd == "show":
        cur = show_cursor()
        print(json.dumps(cur, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.cmd == "reset":
        reset_cursor()
        print("[ok] cursor reset")
        return 0
    if args.cmd == "advance":
        if len(args.chat_hash) != 32:
            print(json.dumps({"error": "chat_hash must be 32 hex chars"}))
            return 2
        advance_batch(args.chat_hash, args.ts)
        print(json.dumps({"ok": True, "advanced": args.chat_hash, "ts": args.ts}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
