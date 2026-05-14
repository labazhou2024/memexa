"""qq_realtime_watcher.py — Long-running daemon that polls NapCat for new QQ messages
and routes them through the L0 v5 QQ batch pipeline.

Architecture
============
The daemon runs a perpetual loop (every --poll-min minutes) and:
  1. Loads chat list, mute skiplist, tier map from the canonical data files.
  2. For each non-muted chat, calls napcat_paginate_group / napcat_paginate_friend
     with since_ts derived from the per-chat cursor (last seen timestamp).
  3. Normalises messages via normalize_napcat_message, windows them by 30-min
     buckets, and writes prompt.json batches via build_batch_prompt + write_batch
     — exactly the same artefacts the history batcher produces.
  4. Advances the cursor on success.
  5. Writes data/qq_realtime_mode.json so qq_batch_ingest._check_realtime_mode_active
     defers batch-backfill to this daemon while the daemon is running.
  6. Emits best-effort trace events for observability.

Cursor schema (data/qq_realtime_cursor.json)
=============================================
{
  "schema_version": 1,
  "per_chat": {
    "<str(gid)>": {
      "last_seen_ts": float,        # epoch of the newest msg we processed
      "updated_at_utc": str,
      "cooloff_until": float | null  # epoch; set on 3× consecutive failures
    }
  },
  "updated_at_utc": str
}

Connection to existing infra
=============================
- napcat_paginate_group / napcat_paginate_friend / normalize_napcat_message /
  build_batch_prompt / write_batch / load_chats / load_skiplist / load_tier_map
  are all imported from data/l0_v5_qq/code/qq_history_to_batches.py.
- Output batches land at data/l0_v5_qq/input_batches/<date>/<batch_id>/prompt.json,
  identical in structure to history-batcher output — downstream consumers are agnostic.
- The qq_realtime_mode.json flag mirrors the WeChat convention respected by
  qq_batch_ingest.QQBatchIngestRunner._check_realtime_mode_active.
"""
from __future__ import annotations


import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ──────────────────────────── paths ────────────────────────────
_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"

_CHAT_IDS_PATH = _DATA / "qq_chat_ids.json"
_SKIPLIST_PATH = _DATA / "qq_mute_skiplist.json"
_TIER_MAP_PATH = _DATA / "qq_room_tier_map.json"
_BATCHES_OUT = _DATA / "l0_v5_qq" / "input_batches"
_CURSOR_PATH = _DATA / "qq_realtime_cursor.json"
_REALTIME_MODE_PATH = _DATA / "qq_realtime_mode.json"

# Inject qq_history_to_batches code directory into sys.path so its
# sibling modules (silk_to_text, etc.) resolve correctly.
_QQ_CODE_DIR = _DATA / "l0_v5_qq" / "code"
if str(_QQ_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_QQ_CODE_DIR))

from src.extraction.qq.qq_history_to_batches import (  # type: ignore[import]
    napcat_paginate_group,
    napcat_paginate_friend,
    normalize_napcat_message,
    build_batch_prompt,
    write_batch,
    load_chats,
    load_skiplist,
    load_tier_map,
    filter_chats,
    window_starts,
    napcat_health,
)

# ──────────────────────────── constants ────────────────────────────
_SCHEMA_VERSION = 1
_REALTIME_TTL_HOURS = 48        # how long realtime_mode.json remains "active"
_COOLOFF_SECONDS = 3600         # 1 h cooloff after 3× consecutive failures
_NAPCAT_DOWN_SLEEP = 300        # 5 min back-off when NapCat is unreachable
_DEFAULT_LOOKBACK_SEC = 1800    # on first run, look back 30 min


# ──────────────────────────── trace ────────────────────────────

def _emit(event: str, payload: dict) -> None:
    """Best-effort trace; silently swallowed on any import / write failure."""
    try:
        from src.core.trace_sink import write_trace_event  # type: ignore
        write_trace_event(event, payload)
    except Exception:
        pass


# ──────────────────────────── cursor ────────────────────────────

def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_cursor() -> dict:
    if _CURSOR_PATH.exists():
        try:
            d = json.loads(_CURSOR_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("schema_version") == _SCHEMA_VERSION:
                return d
        except (OSError, json.JSONDecodeError):
            pass
    return {"schema_version": _SCHEMA_VERSION, "per_chat": {}, "updated_at_utc": _utc_iso_now()}


def _save_cursor(cursor: dict) -> None:
    cursor["updated_at_utc"] = _utc_iso_now()
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        tmp = _CURSOR_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cursor, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CURSOR_PATH)
    except OSError:
        pass


def _get_chat_cursor(cursor: dict, chat_id: str) -> dict:
    return cursor.setdefault("per_chat", {}).setdefault(chat_id, {
        "last_seen_ts": 0.0,
        "updated_at_utc": None,
        "cooloff_until": None,
    })


def _set_chat_cursor_ok(cursor: dict, chat_id: str, last_seen_ts: float) -> None:
    entry = _get_chat_cursor(cursor, chat_id)
    entry["last_seen_ts"] = last_seen_ts
    entry["updated_at_utc"] = _utc_iso_now()
    entry["cooloff_until"] = None  # clear cooloff on success


def _mark_cooloff(cursor: dict, chat_id: str) -> None:
    entry = _get_chat_cursor(cursor, chat_id)
    entry["cooloff_until"] = time.time() + _COOLOFF_SECONDS
    entry["updated_at_utc"] = _utc_iso_now()


def _is_in_cooloff(cursor: dict, chat_id: str) -> bool:
    entry = cursor.get("per_chat", {}).get(chat_id, {})
    cooloff = entry.get("cooloff_until")
    if cooloff and time.time() < float(cooloff):
        return True
    return False


# ──────────────────────────── realtime mode flag ────────────────────────────

def _write_realtime_mode_flag(ttl_hours: int = _REALTIME_TTL_HOURS) -> None:
    payload = {
        "enabled_at": time.time(),
        "ttl_hours": ttl_hours,
        "written_by": "qq_realtime_watcher",
        "written_at_utc": _utc_iso_now(),
    }
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        tmp = _REALTIME_MODE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_REALTIME_MODE_PATH)
    except OSError:
        pass


def _remove_realtime_mode_flag() -> None:
    try:
        if _REALTIME_MODE_PATH.exists():
            _REALTIME_MODE_PATH.unlink()
    except OSError:
        pass


# ──────────────────────────── one poll round ────────────────────────────

def _poll_once(
    chats: List[dict],
    cursor: dict,
    fail_counts: Dict[str, int],
    napcat_url: str,
    self_qq: int,
    tier_map: Dict[str, dict],
    out_root: Path,
) -> dict:
    """Execute one poll sweep across all chats.

    Returns stats dict: {n_chats_polled, n_msgs_new, n_batches_written, errors}.
    Mutates cursor and fail_counts in-place; caller is responsible for persisting.
    """
    now = time.time()
    n_polled = 0
    n_msgs_new = 0
    n_batches = 0
    errors: List[str] = []

    for chat in chats:
        chat_id = str(chat["id"])
        kind = chat.get("kind", "group")

        if _is_in_cooloff(cursor, chat_id):
            continue

        entry = _get_chat_cursor(cursor, chat_id)
        last_seen = float(entry.get("last_seen_ts") or 0)
        since_ts = last_seen if last_seen > 0 else now - _DEFAULT_LOOKBACK_SEC
        until_ts = now

        try:
            if kind == "group":
                raw_msgs = napcat_paginate_group(napcat_url, int(chat_id), since_ts, until_ts)
            else:
                raw_msgs = napcat_paginate_friend(napcat_url, int(chat_id), since_ts, until_ts)
        except Exception as exc:
            err_str = repr(exc)
            fail_counts[chat_id] = fail_counts.get(chat_id, 0) + 1
            _emit("qq_realtime_chat_failed", {
                "gid": chat_id, "err": err_str, "attempt": fail_counts[chat_id]
            })
            if fail_counts[chat_id] >= 3:
                _mark_cooloff(cursor, chat_id)
                fail_counts[chat_id] = 0
            errors.append(f"{chat_id}:{err_str[:60]}")
            continue

        n_polled += 1

        if not raw_msgs:
            # no new messages — advance last_seen_ts to now so next round
            # starts from a recent baseline (avoids re-fetching on every poll)
            if last_seen == 0:
                _set_chat_cursor_ok(cursor, chat_id, now)
            fail_counts[chat_id] = 0
            continue

        # Normalise
        norm_msgs = [normalize_napcat_message(m) for m in raw_msgs]
        norm_msgs = [m for m in norm_msgs if m]
        if not norm_msgs:
            fail_counts[chat_id] = 0
            continue

        fail_counts[chat_id] = 0
        n_msgs_new += len(norm_msgs)

        tier_entry = tier_map.get(chat_id) if kind == "group" else None
        newest_ts = max(m["ts"] for m in norm_msgs)

        # Window and write batches
        min_ts = min(m["ts"] for m in norm_msgs)
        for ws, we in window_starts(min_ts, newest_ts + 1):
            window_msgs = [m for m in norm_msgs if ws <= m["ts"] < we]
            if not window_msgs:
                continue
            bid, pd = build_batch_prompt(
                chat, ws, we, window_msgs, self_qq,
                manifest_slice=None,
                tier_entry=tier_entry,
            )
            if not bid:
                continue
            _, was_new = write_batch(bid, pd, out_root)
            if was_new:
                n_batches += 1

        _set_chat_cursor_ok(cursor, chat_id, newest_ts)

    return {
        "n_chats_polled": n_polled,
        "n_msgs_new": n_msgs_new,
        "n_batches_written": n_batches,
        "errors": errors,
    }


# ──────────────────────────── daemon main ────────────────────────────

def run_daemon(
    poll_min: float,
    napcat_url: str,
    self_qq: int,
    max_rounds: Optional[int],
    chat_ids_path: Path,
    skiplist_path: Path,
    tier_map_path: Path,
    out_root: Path,
) -> int:
    """Main daemon loop. Returns exit code (0 = clean shutdown)."""

    # ── graceful shutdown via SIGINT / SIGTERM ──
    _stop = {"flag": False, "reason": ""}

    def _handle_signal(sig, _frame):
        _stop["flag"] = True
        _stop["reason"] = f"signal_{sig}"

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, OSError):
        pass  # SIGTERM not available on Windows; SIGINT suffices

    # ── load static config (reloaded each round to pick up live changes) ──
    chats_all = load_chats(chat_ids_path)
    muted_groups, muted_users = load_skiplist(skiplist_path)
    tier_map = load_tier_map(tier_map_path)
    chats = filter_chats(chats_all, muted_groups, muted_users,
                         only_kind=None, include_friends=True)

    print(json.dumps({
        "event": "qq_realtime_started",
        "n_chats": len(chats),
        "poll_min": poll_min,
        "napcat_url": napcat_url,
        "self_qq": self_qq,
    }, ensure_ascii=False))
    sys.stdout.flush()

    _emit("qq_realtime_started", {"n_chats": len(chats), "poll_min": poll_min})
    _write_realtime_mode_flag()

    cursor = _load_cursor()
    fail_counts: Dict[str, int] = {}
    out_root.mkdir(parents=True, exist_ok=True)

    round_num = 0
    poll_interval = poll_min * 60.0

    try:
        while not _stop["flag"]:
            if max_rounds is not None and round_num >= max_rounds:
                _stop["reason"] = "max_rounds"
                break

            # ── health check ──
            if not napcat_health(napcat_url):
                print(json.dumps({
                    "event": "qq_realtime_napcat_down",
                    "round": round_num,
                    "sleep_sec": _NAPCAT_DOWN_SLEEP,
                }, ensure_ascii=False))
                sys.stdout.flush()
                _emit("qq_realtime_napcat_down", {"round": round_num})
                # sleep in short slices so SIGINT is responsive
                for _ in range(_NAPCAT_DOWN_SLEEP):
                    if _stop["flag"]:
                        break
                    time.sleep(1)
                continue

            # Reload config each round (mute list may have been updated live)
            chats_all = load_chats(chat_ids_path)
            muted_groups, muted_users = load_skiplist(skiplist_path)
            tier_map = load_tier_map(tier_map_path)
            chats = filter_chats(chats_all, muted_groups, muted_users,
                                 only_kind=None, include_friends=True)

            t0 = time.time()
            stats = _poll_once(chats, cursor, fail_counts, napcat_url,
                               self_qq, tier_map, out_root)
            dur_ms = int((time.time() - t0) * 1000)

            _save_cursor(cursor)
            _write_realtime_mode_flag()  # renew TTL each successful round

            round_result = {
                "event": "qq_realtime_round",
                "round": round_num,
                "n_chats_polled": stats["n_chats_polled"],
                "n_msgs_new": stats["n_msgs_new"],
                "n_batches_written": stats["n_batches_written"],
                "dur_ms": dur_ms,
                "errors": stats["errors"],
            }
            print(json.dumps(round_result, ensure_ascii=False))
            sys.stdout.flush()
            _emit("qq_realtime_round", {
                "round": round_num,
                "n_chats_polled": stats["n_chats_polled"],
                "n_msgs_new": stats["n_msgs_new"],
                "n_batches_written": stats["n_batches_written"],
                "dur_ms": dur_ms,
            })

            round_num += 1

            if _stop["flag"] or (max_rounds is not None and round_num >= max_rounds):
                _stop["reason"] = _stop.get("reason") or "max_rounds"
                break

            # ── sleep until next poll (interruptible) ──
            deadline = time.time() + poll_interval
            while time.time() < deadline and not _stop["flag"]:
                time.sleep(min(1.0, deadline - time.time()))

    finally:
        _save_cursor(cursor)
        _remove_realtime_mode_flag()
        stop_reason = _stop.get("reason") or "unknown"
        print(json.dumps({
            "event": "qq_realtime_stopped",
            "reason": stop_reason,
            "rounds_completed": round_num,
        }, ensure_ascii=False))
        sys.stdout.flush()
        _emit("qq_realtime_stopped", {"reason": stop_reason, "rounds_completed": round_num})

    return 0


# ──────────────────────────── CLI ────────────────────────────

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m src.extraction.qq_realtime_watcher",
        description="QQ realtime daemon — polls NapCat and writes L0 v5 batches.",
    )
    ap.add_argument("--poll-min", type=float, default=30.0,
                    help="Poll interval in minutes (default: 30)")
    ap.add_argument("--max-rounds", type=int, default=None,
                    help="Stop after N rounds (default: infinite; use 1 for smoke test)")
    ap.add_argument("--napcat-url", default=os.environ.get("MEMEXA_NAPCAT_URL",
                                                            "http://127.0.0.1:3000"),
                    help="NapCat OneBot HTTP base URL (default: http://127.0.0.1:3000)")
    ap.add_argument("--self-qq", type=int, default=None,  # MEMEXA_QQ_ID env or identity.yaml
                    help="Self QQ number (used in batch sender_list is_self flag)")
    ap.add_argument("--chat-ids", default=str(_CHAT_IDS_PATH))
    ap.add_argument("--skiplist", default=str(_SKIPLIST_PATH))
    ap.add_argument("--tier-map", default=str(_TIER_MAP_PATH))
    ap.add_argument("--out-root", default=str(_BATCHES_OUT))
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    return run_daemon(
        poll_min=args.poll_min,
        napcat_url=args.napcat_url,
        self_qq=args.self_qq,
        max_rounds=args.max_rounds,
        chat_ids_path=Path(args.chat_ids),
        skiplist_path=Path(args.skiplist),
        tier_map_path=Path(args.tier_map),
        out_root=Path(args.out_root),
    )


if __name__ == "__main__":
    sys.exit(main())
