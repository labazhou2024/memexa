"""qq_history_to_batches.py — Build L0 v5 prompt.json batches from QQ history.

Modes (any one suffices):

  A. NapCat live  (preferred when QQ.exe + NapCat plugin running)
     reads /get_group_msg_history paginated; respects mute skiplist;
     outputs one batch JSON per (chat × 30-min window).

  B. NapCat dump  (offline; user-provided JSON cache dump)
     accepts data/qq_chat_history_dump.json with same schema NapCat
     would have returned. Used when NapCat is unreachable but user
     pre-extracted history (e.g. from a different session).

  C. NT-DB        (decrypted QQ NT sqlite)
     uses memexa.qq_db.QQDBReader (requires QQ.exe running for SQLCipher
     key extraction); on this machine the local NT db is 2024-08 stale
     so this mode is documented but expected to be skipped — keep code
     path live for future deployment.

Per CEO directive 2026-05-06:
  - 完全复刻微信 (30-min windows, sender_list, manifest_slice, etc.)
  - inline voice STT (silk_to_text) when segment.type=='record' encountered
  - mute skiplist drops groups; tier_map injects room_tier_hint + ttl_hint
  - skip already-processed batches via .done sentinel files

Output structure:
  data/l0_v5_qq/input_batches/<date>/<batch_hash>/prompt.json
  - batch_hash = sha256("qq" + chat_uid + window_start + window_end)[:16]
  - guarantees no collision with WeChat batches (different prefix)

Idempotent: re-running on same range is a no-op (sha-keyed dedup).
"""
from __future__ import annotations


import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Add l0_v5_qq/code to sys.path so we can import sibling modules
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from memexa.extraction.qq.silk_to_text import (  # noqa: E402  pylint: disable=wrong-import-position
    transcribe_voice_file,
    embed_voice_marker,
    VOICE_TAG,
)

_REPO = Path(__file__).resolve().parents[3]
_DATA = _REPO / "data"
_BATCHES_OUT = _DATA / "l0_v5_qq" / "input_batches"
_DEFAULT_CHAT_IDS = _DATA / "qq_chat_ids.json"
_DEFAULT_SKIPLIST = _DATA / "qq_mute_skiplist.json"
_DEFAULT_TIER_MAP = _DATA / "qq_room_tier_map.json"
_DEFAULT_NAPCAT = os.environ.get("MEMEXA_NAPCAT_URL", "http://127.0.0.1:5700")
_DEFAULT_DUMP = _DATA / "qq_chat_history_dump.json"

WINDOW_MINUTES = 30
WINDOW_SECONDS = WINDOW_MINUTES * 60
MAX_MSGS_PER_BATCH = 200


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]


def _sha32(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:32]


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ────────────────────────── napcat ──────────────────────────

def _napcat_get(base: str, endpoint: str, params: Dict[str, Any],
                timeout: float = 10.0) -> Optional[dict]:
    """OneBot v11 over HTTP — uses POST with JSON body (NapCat conventional)."""
    url = f"{base.rstrip('/')}{endpoint}"
    body = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None


def napcat_health(base: str) -> bool:
    return _napcat_get(base, "/get_status", {}, timeout=3.0) is not None


def napcat_paginate_friend(
    base: str, user_id: int,
    since_ts: float, until_ts: float,
    page_count: int = 100, max_pages: int = 30,
) -> List[dict]:
    """Paginate /get_friend_msg_history for a single friend."""
    out: List[dict] = []
    last_seq = 0
    seen_msgids = set()
    for _ in range(max_pages):
        r = _napcat_get(base, "/get_friend_msg_history", {
            "user_id": user_id, "message_seq": last_seq,
            "count": page_count,
        })
        if not r or r.get("status") not in ("ok", 0, "0"):
            break
        msgs = (r.get("data") or {}).get("messages") or []
        if not msgs:
            break
        new_msgs = []
        oldest_ts = float("inf")
        for m in msgs:
            mid = m.get("message_id") or m.get("msg_id")
            if mid in seen_msgids:
                continue
            seen_msgids.add(mid)
            ts = float(m.get("time") or 0)
            if ts == 0:
                continue
            oldest_ts = min(oldest_ts, ts)
            if since_ts <= ts <= until_ts:
                new_msgs.append(m)
        out.extend(new_msgs)
        if oldest_ts < since_ts:
            break
        try:
            last_seq = min(int(m.get("message_seq") or m.get("msg_seq") or 0)
                           for m in msgs if (m.get("message_seq") or m.get("msg_seq")))
        except ValueError:
            break
    out.sort(key=lambda m: float(m.get("time") or 0))
    return out


def napcat_paginate_group(
    base: str, group_id: int,
    since_ts: float, until_ts: float,
    page_count: int = 100, max_pages: int = 50,
) -> List[dict]:
    """Pull group messages between two epoch timestamps via NapCat.

    NapCat's /get_group_msg_history pages by message_seq DESCENDING. We start
    at "newest" and walk backwards until ts < since_ts.
    """
    out: List[dict] = []
    last_seq = 0  # 0 = start from latest
    seen_msgids = set()
    pages = 0
    while pages < max_pages:
        pages += 1
        r = _napcat_get(base, "/get_group_msg_history", {
            "group_id": group_id,
            "message_seq": last_seq,
            "count": page_count,
        })
        if not r or r.get("status") not in ("ok", 0, "0"):
            break
        msgs = (r.get("data") or {}).get("messages") or []
        if not msgs:
            break

        new_msgs = []
        oldest_ts = float("inf")
        for m in msgs:
            mid = m.get("message_id") or m.get("msg_id")
            if mid in seen_msgids:
                continue
            seen_msgids.add(mid)
            ts = float(m.get("time") or 0)
            if ts == 0:
                continue
            oldest_ts = min(oldest_ts, ts)
            if since_ts <= ts <= until_ts:
                new_msgs.append(m)

        out.extend(new_msgs)
        if oldest_ts < since_ts:
            break
        try:
            last_seq = min(int(m.get("message_seq") or m.get("msg_seq") or 0)
                           for m in msgs if (m.get("message_seq") or m.get("msg_seq")))
        except ValueError:
            break

    out.sort(key=lambda m: float(m.get("time") or 0))
    return out


# ────────────────────────── voice handling ──────────────────────────

def _segment_text(seg: dict) -> str:
    """Convert one OneBot segment to text. Voice segments dispatch to STT."""
    t = (seg.get("type") or "").lower()
    d = seg.get("data") or {}
    if t == "text":
        return str(d.get("text", ""))
    if t == "at":
        nm = d.get("name") or d.get("qq", "")
        return f"@{nm}"
    if t == "face":
        return "[表情]"
    if t == "image":
        url = d.get("url") or d.get("file") or ""
        return f"[图片 {url[-40:] if url else ''}]"
    if t == "video":
        return "[视频]"
    if t == "record":
        return _stt_record(d)
    if t == "forward":
        return f"[合并转发 id={d.get('id', '?')}]"
    if t == "reply":
        return f"[回复 msgid={d.get('id', '?')}]"
    if t == "json":
        return f"[JSON卡片] {str(d.get('data', ''))[:200]}"
    return f"[{t}]"


def _stt_record(data: dict) -> str:
    """Inline STT on a record (voice) segment.

    Per CEO directive: 顺手处理 (inline, no daemon). If the segment carries
    a local file path, transcribe directly; otherwise return a placeholder
    (NapCat sometimes only gives a URL — we don't fetch over network here
    to keep this cheap; future enhancement can pull the file via NapCat
    /download_voice or similar).
    """
    file_field = data.get("file") or data.get("path")
    if not file_field:
        return f"{VOICE_TAG} [URL only, 跳过]"
    p = Path(str(file_field))
    if not p.is_absolute():
        p = Path("/c/Users/<USERNAME>") / p
    if not p.exists():
        return f"{VOICE_TAG} [文件不存在 {p.name}]"
    out = transcribe_voice_file(p)
    return out.get("text") or f"{VOICE_TAG} [STT 失败]"


def normalize_napcat_message(m: dict) -> Optional[dict]:
    """Convert a NapCat msg dict to L0 v5 utterance form. Voice STT happens here."""
    ts = float(m.get("time") or 0)
    if ts == 0:
        return None
    sender_id = m.get("user_id") or (m.get("sender") or {}).get("user_id") or 0
    sender_name = (m.get("sender") or {}).get("nickname") or m.get("user_id") or "?"
    raw_msg = m.get("message") or m.get("raw_message") or ""

    if isinstance(raw_msg, list):
        parts = [_segment_text(seg) for seg in raw_msg if isinstance(seg, dict)]
        content = "".join(p for p in parts if p)
    else:
        content = str(raw_msg)

    if not content.strip():
        return None

    return {
        "ts": ts,
        "ts_iso": _utc_iso(ts),
        "wxid_hash": _sha16(str(sender_id)),
        "sender_qq": sender_id,
        "sender_name": sender_name,
        "content": content[:1500],
        "msg_id": m.get("message_id") or m.get("msg_id"),
        "source_offset": f"napcat:{m.get('message_id', '?')}",
    }


# ────────────────────────── windowing & batch build ──────────────────────────

def window_starts(since_ts: float, until_ts: float) -> List[Tuple[float, float]]:
    """Yield aligned 30-min windows in [since, until] inclusive of partials."""
    aligned = (int(since_ts) // WINDOW_SECONDS) * WINDOW_SECONDS
    out = []
    t = float(aligned)
    while t < until_ts:
        out.append((t, t + WINDOW_SECONDS))
        t += WINDOW_SECONDS
    return out


def build_batch_prompt(
    chat_meta: dict,
    window_start: float,
    window_end: float,
    msgs: List[dict],
    self_qq: int,
    manifest_slice: Optional[dict],
    tier_entry: Optional[dict],
) -> Tuple[str, dict]:
    """Build (batch_hash, prompt_dict) for one (chat × window).

    Returns ("", None) if msgs empty.
    """
    if not msgs:
        return "", {}
    chat_uid = str(chat_meta.get("id"))
    chat_room = chat_meta.get("label") or chat_uid
    batch_id = _sha16(
        f"qq:{chat_uid}:{int(window_start)}:{int(window_end)}"
    )
    room_hash = _sha32(chat_room)

    senders_seen: Dict[str, dict] = {}
    out_msgs: List[dict] = []
    for m in msgs:
        wh = m["wxid_hash"]
        if wh not in senders_seen:
            senders_seen[wh] = {
                "wxid_hash": wh,
                "alias_in_manifest_or_None": m.get("sender_name"),
                "is_self": (str(m.get("sender_qq")) == str(self_qq)),
                "qq_id": m.get("sender_qq"),
            }
        out_msgs.append({
            "ts": m["ts_iso"],
            "wxid_hash": wh,
            "content": m["content"],
        })

    sender_list = list(senders_seen.values())

    prompt_dict = {
        "batch_id": batch_id,
        "chat_room": chat_room,
        "room_hash": room_hash,
        "batch_window_local": (
            f"{datetime.fromtimestamp(window_start).strftime('%Y-%m-%dT%H:%M')} ~ "
            f"{datetime.fromtimestamp(window_end).strftime('%Y-%m-%dT%H:%M')}"
        ),
        "batch_window_epoch": [int(window_start), int(window_end)],
        "sender_list": sender_list,
        "messages": out_msgs[:MAX_MSGS_PER_BATCH],
        "manifest_slice": manifest_slice or {"persons": {}, "organizations": {},
                                              "inanimate": {}, "public_figures": {}},
        "chinese_calendar_window": None,
        "user_calendar_window": None,
        # QQ-specific hints (consumed by qq_pass2_prompt wrapper):
        "source": "qq",
        "room_tier_hint": (tier_entry or {}).get("room_tier", 1),
        "ttl_hint": (tier_entry or {}).get("ttl_policy"),
        "qq_chat_kind": chat_meta.get("kind", "group"),
        "qq_group_id": int(chat_uid) if chat_uid.isdigit() else None,
    }
    return batch_id, prompt_dict


def write_batch(
    batch_id: str, prompt_dict: dict, out_root: Path,
) -> Tuple[Path, bool]:
    """Write batch's prompt.json under <out_root>/<date>/<batch_id>/.

    Returns (path, was_new). was_new=False ⇒ skipped already-existing.
    """
    iso = prompt_dict.get("batch_window_local", "").split(" ")[0]
    date_str = iso[:10] if len(iso) >= 10 else "unknown"
    target_dir = out_root / date_str / batch_id
    target = target_dir / "prompt.json"
    sentinel = target_dir / ".done"
    if sentinel.exists() and target.exists():
        return target, False
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prompt_dict, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    sentinel.write_text(json.dumps({
        "written_at_utc": _utc_iso(time.time()),
        "n_msgs": len(prompt_dict.get("messages") or []),
        "batch_id": batch_id,
    }, ensure_ascii=False), encoding="utf-8")
    return target, True


# ────────────────────────── chat list & filtering ──────────────────────────

def load_chats(chat_ids_path: Path) -> List[dict]:
    if not chat_ids_path.exists():
        return []
    try:
        d = json.loads(chat_ids_path.read_text(encoding="utf-8"))
        return d.get("chats") or []
    except (OSError, json.JSONDecodeError):
        return []


def load_skiplist(path: Path) -> Tuple[set, set]:
    if not path.exists():
        return set(), set()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return (set(int(x) for x in (d.get("muted_group_ids") or [])),
                set(int(x) for x in (d.get("muted_user_ids") or [])))
    except (OSError, json.JSONDecodeError):
        return set(), set()


def load_tier_map(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d.get("tier_map") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def filter_chats(chats: List[dict],
                 muted_groups: set, muted_users: set,
                 only_kind: Optional[str] = None,
                 max_chats: Optional[int] = None,
                 include_friends: bool = True) -> List[dict]:
    out = []
    for c in chats:
        kind = c.get("kind")
        cid = c.get("id")
        if only_kind and kind != only_kind:
            continue
        if kind == "friend" and not include_friends:
            continue
        if kind == "group" and int(cid) in muted_groups:
            continue
        if kind == "friend" and int(cid) in muted_users:
            continue
        out.append(c)
        if max_chats and len(out) >= max_chats:
            break
    return out


# ────────────────────────── volume guard ──────────────────────────

def msgs_per_day_guard(msgs: List[dict], threshold: int = 200) -> Tuple[bool, float]:
    """Inspect normalized msgs; return (drop, msgs_per_day)."""
    if len(msgs) < 5:
        return False, 0.0
    timestamps = sorted(m["ts"] for m in msgs)
    span_days = max(1.0, (timestamps[-1] - timestamps[0]) / 86400.0)
    rate = len(timestamps) / span_days
    return (rate >= threshold, rate)


# ────────────────────────── orchestration ──────────────────────────

def run_napcat_mode(
    napcat_url: str,
    chats: List[dict],
    self_qq: int,
    since_ts: float, until_ts: float,
    tier_map: Dict[str, dict],
    out_root: Path,
    manifest_slice: Optional[dict] = None,
    threshold_per_day: int = 200,
) -> dict:
    if not napcat_health(napcat_url):
        return {"mode": "napcat", "status": "napcat_unreachable", "url": napcat_url}

    counters = {
        "n_chats_attempted": 0, "n_chats_with_msgs": 0,
        "n_chats_volume_dropped": 0,
        "n_batches_written": 0, "n_batches_skipped_existing": 0,
        "n_msgs_total": 0, "n_voice_transcribed": 0,
    }

    for chat in chats:
        kind = chat.get("kind")
        if kind not in ("group", "friend"):
            continue
        cid = int(chat["id"])
        counters["n_chats_attempted"] += 1
        if kind == "group":
            raw = napcat_paginate_group(napcat_url, cid, since_ts, until_ts)
        else:
            raw = napcat_paginate_friend(napcat_url, cid, since_ts, until_ts)
        if not raw:
            continue
        msgs = [normalize_napcat_message(m) for m in raw]
        msgs = [m for m in msgs if m]
        if not msgs:
            continue
        counters["n_chats_with_msgs"] += 1
        counters["n_voice_transcribed"] += sum(
            1 for m in msgs if m["content"].startswith(VOICE_TAG)
        )
        drop, rate = msgs_per_day_guard(msgs, threshold_per_day)
        if drop:
            counters["n_chats_volume_dropped"] += 1
            logger.info("dropping group %s (%.0f msgs/day ≥ %d)",
                        chat.get("label", gid), rate, threshold_per_day)
            continue
        counters["n_msgs_total"] += len(msgs)
        tier_entry = tier_map.get(str(cid)) if kind == "group" else None
        for ws, we in window_starts(since_ts, until_ts):
            window_msgs = [m for m in msgs if ws <= m["ts"] < we]
            if not window_msgs:
                continue
            bid, pd = build_batch_prompt(
                chat, ws, we, window_msgs, self_qq,
                manifest_slice, tier_entry,
            )
            if not bid:
                continue
            _, was_new = write_batch(bid, pd, out_root)
            if was_new:
                counters["n_batches_written"] += 1
            else:
                counters["n_batches_skipped_existing"] += 1
    return {"mode": "napcat", "status": "ok", "counters": counters}


def run_dump_mode(
    dump_path: Path,
    chats: List[dict],
    self_qq: int,
    since_ts: float, until_ts: float,
    tier_map: Dict[str, dict],
    out_root: Path,
    manifest_slice: Optional[dict] = None,
    threshold_per_day: int = 200,
) -> dict:
    """Read a NapCat-format dump JSON: {chats: {<gid>: [msg, ...], ...}}."""
    if not dump_path.exists():
        return {"mode": "dump", "status": "dump_missing", "path": str(dump_path)}
    try:
        dump = json.loads(dump_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"mode": "dump", "status": f"dump_parse_error={e!r}"}

    chat_msgs = dump.get("chats") or {}
    counters = {
        "n_chats_attempted": 0, "n_chats_with_msgs": 0,
        "n_chats_volume_dropped": 0,
        "n_batches_written": 0, "n_batches_skipped_existing": 0,
        "n_msgs_total": 0, "n_voice_transcribed": 0,
    }
    for chat in chats:
        if chat.get("kind") != "group":
            continue
        gid = int(chat["id"])
        counters["n_chats_attempted"] += 1
        raw = chat_msgs.get(str(gid)) or chat_msgs.get(gid) or []
        if not raw:
            continue
        msgs = [normalize_napcat_message(m) for m in raw]
        msgs = [m for m in msgs if m and since_ts <= m["ts"] <= until_ts]
        if not msgs:
            continue
        counters["n_chats_with_msgs"] += 1
        counters["n_voice_transcribed"] += sum(
            1 for m in msgs if m["content"].startswith(VOICE_TAG))
        drop, rate = msgs_per_day_guard(msgs, threshold_per_day)
        if drop:
            counters["n_chats_volume_dropped"] += 1
            continue
        counters["n_msgs_total"] += len(msgs)
        tier_entry = tier_map.get(str(gid))
        for ws, we in window_starts(since_ts, until_ts):
            window_msgs = [m for m in msgs if ws <= m["ts"] < we]
            if not window_msgs:
                continue
            bid, pd = build_batch_prompt(
                chat, ws, we, window_msgs, self_qq,
                manifest_slice, tier_entry,
            )
            if not bid:
                continue
            _, was_new = write_batch(bid, pd, out_root)
            if was_new:
                counters["n_batches_written"] += 1
            else:
                counters["n_batches_skipped_existing"] += 1
    return {"mode": "dump", "status": "ok", "counters": counters}


def cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["napcat", "dump", "auto"], default="auto")
    ap.add_argument("--napcat-url", default=_DEFAULT_NAPCAT)
    ap.add_argument("--dump", default=str(_DEFAULT_DUMP))
    ap.add_argument("--chat-ids", default=str(_DEFAULT_CHAT_IDS))
    ap.add_argument("--skiplist", default=str(_DEFAULT_SKIPLIST))
    ap.add_argument("--tier-map", default=str(_DEFAULT_TIER_MAP))
    ap.add_argument("--out-root", default=str(_BATCHES_OUT))
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end-date", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--threshold-per-day", type=int, default=200)
    ap.add_argument("--self-qq", type=int, default=None)  # MEMEXA_QQ_ID env or identity.yaml
    ap.add_argument("--max-chats", type=int, default=None)
    ap.add_argument("--manifest-path", default="data/identity_manifest.yaml")
    args = ap.parse_args()

    chats = load_chats(Path(args.chat_ids))
    if not chats:
        print(json.dumps({"error": "no_chat_ids"}))
        return 2

    muted_groups, muted_users = load_skiplist(Path(args.skiplist))
    tier_map = load_tier_map(Path(args.tier_map))
    chats = filter_chats(chats, muted_groups, muted_users,
                         only_kind=None, max_chats=args.max_chats,
                         include_friends=True)

    since_ts = datetime.strptime(args.start_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp()
    until_ts = (datetime.strptime(args.end_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc) + timedelta(days=1)).timestamp()

    manifest_slice = None  # caller can pre-inject; default to empty (Pass-1 will fill)

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    result: Optional[dict] = None
    if args.mode in ("napcat", "auto"):
        r = run_napcat_mode(args.napcat_url, chats, args.self_qq,
                            since_ts, until_ts, tier_map, out_root,
                            manifest_slice, args.threshold_per_day)
        if r.get("status") == "ok" and r["counters"]["n_chats_with_msgs"] > 0:
            result = r
        elif args.mode == "napcat":
            result = r
    if result is None and args.mode in ("dump", "auto"):
        r2 = run_dump_mode(Path(args.dump), chats, args.self_qq,
                           since_ts, until_ts, tier_map, out_root,
                           manifest_slice, args.threshold_per_day)
        result = r2

    print(json.dumps(result or {"status": "no_data_source_available"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
