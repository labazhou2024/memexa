"""V5-native WeChat batch builder.

Reads raw msgs from MicroMsg.db (via WeChatDBReader), cuts into chat batches
(reusing utterance_merger + cut_batches from phase1 — these are pure data
preprocessing, no LLM, so safe to reuse even though phase1 LLM stages are
archived), then writes V5-format prompt.json directly to
data/l0_v5/input_batches/<date>/<batch_id>/prompt.json.

Skips data/extract_archive entirely — that is the v3 phase1 path.

Usage:
  python tools/v5_wechat_batch_builder.py --start 2026-04-01 --end 2026-04-25
  python tools/v5_wechat_batch_builder.py --start 2026-05-09 --end 2026-05-09
  python tools/v5_wechat_batch_builder.py --start 2026-04-01 --end 2026-05-09 \
      --skip-existing  # don't overwrite already-built batches

Design choices:
  - manifest_slice = full identity_manifest.yaml (small enough; LLM uses to
    canonicalize); when batch is small, future ENH could trim per-batch.
  - sender_list built from msgs (deduped by wxid_hash); is_self=True for self
    wxid (looked up via reader.wxid_dir basename).
  - skip muted wxids (data/wechat_mute_skiplist.json)
  - skip empty content msgs
  - batch_id = sha256(chat_room_id || start_ts) [:16]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "data" / "l0_v5" / "input_batches"
MANIFEST_PATH = ROOT / "data" / "identity_manifest.yaml"
MUTE_SKIPLIST = ROOT / "data" / "wechat_mute_skiplist.json"


def _wxid_hash(wxid: str) -> str:
    if not wxid:
        return ""
    return hashlib.sha256(wxid.encode("utf-8")).hexdigest()[:16]


def _chat_room_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:32]


def _batch_sha(chat_room_id: str, start_ts: float) -> str:
    return hashlib.sha256(
        f"{chat_room_id}|{int(start_ts)}".encode("utf-8")
    ).hexdigest()[:16]


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {"persons": {}, "organizations": {},
                "inanimate": {}, "public_figures": {}}
    try:
        import yaml
        d = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
        return {
            "persons": d.get("persons") or {},
            "organizations": d.get("organizations") or {},
            "inanimate": d.get("inanimate") or {},
            "public_figures": d.get("public_figures") or {},
        }
    except Exception as e:
        print(f"[warn] manifest load fail: {e}", file=sys.stderr)
        return {"persons": {}, "organizations": {},
                "inanimate": {}, "public_figures": {}}


def load_muted() -> set[str]:
    if not MUTE_SKIPLIST.exists():
        return set()
    try:
        d = json.loads(MUTE_SKIPLIST.read_text(encoding="utf-8"))
        return set(d.get("muted_wxids", []))
    except Exception:
        return set()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--skip-existing", action="store_true",
                   help="skip batches whose prompt.json already exists")
    p.add_argument("--max-msgs", type=int, default=None,
                   help="limit total msgs (debug)")
    args = p.parse_args()

    from src.wechat_db import WeChatDBReader
    from src.extraction.wechat_batch_ingest import _wxmessage_to_dict
    from src.extraction.utterance_merger import merge_utterances
    from src.extraction.batch_chat_extract import (
        ChatBatch, cut_batches, _split_oversized_batches,
    )

    start_ts = dt.datetime.fromisoformat(
        f"{args.start}T00:00:00+00:00").timestamp()
    end_ts = dt.datetime.fromisoformat(
        f"{args.end}T23:59:59+00:00").timestamp()

    print(f"[init] reading WeChat DB...", flush=True)
    r = WeChatDBReader()
    if not r.initialize() or not r.enc_keys:
        print(json.dumps({"error": "wechat_db_init_failed"}))
        return 1

    # Detect self wxid from wxid_dir name (e.g., wxid_gkhgkeknbdx122_6bae)
    self_wxid = ""
    if r.wxid_dir:
        name = r.wxid_dir.name
        # strip trailing "_xxxx" suffix if present
        if "_" in name:
            parts = name.rsplit("_", 1)
            if len(parts[-1]) <= 6 and parts[-1].isalnum():
                self_wxid = parts[0]
            else:
                self_wxid = name
        else:
            self_wxid = name
    self_wxid_hash = _wxid_hash(self_wxid) if self_wxid else ""
    print(f"[init] self_wxid={self_wxid!r} hash={self_wxid_hash!r}", flush=True)

    print(f"[read] reading msgs since {args.start}...", flush=True)
    msgs_raw = r.read_after(start_ts, chat_name=None)
    print(f"[read] got {len(msgs_raw)} raw msgs", flush=True)

    muted = load_muted()
    print(f"[init] muted wxids: {len(muted)}", flush=True)

    msgs: list[dict] = []
    for m in msgs_raw:
        d = _wxmessage_to_dict(m)
        if not d.get("content"):
            continue
        if d.get("chat_name") in muted:
            continue
        if d.get("ts", 0) > end_ts:
            continue
        msgs.append(d)
        if args.max_msgs and len(msgs) >= args.max_msgs:
            break

    print(f"[filter] {len(msgs)} msgs after mute+empty filter", flush=True)

    by_chat: dict[str, list[dict]] = {}
    for d in msgs:
        by_chat.setdefault(d.get("chat_name", ""), []).append(d)

    all_batches: list[ChatBatch] = []
    for chat_id, chat_msgs in by_chat.items():
        utterances = merge_utterances(chat_msgs)
        all_batches.extend(cut_batches(utterances))
    all_batches, _split = _split_oversized_batches(all_batches)
    print(f"[batch] cut into {len(all_batches)} batches across "
          f"{len(by_chat)} chat rooms", flush=True)

    manifest_slice = load_manifest()
    print(f"[manifest] loaded {len(manifest_slice['persons'])} persons", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped_existing = 0
    skipped_oor = 0  # out of range
    for b in all_batches:
        # Filter by date: end_ts (UTC) date == one of [start..end]
        d = dt.datetime.fromtimestamp(b.end_ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        if d < args.start or d > args.end:
            skipped_oor += 1
            continue
        sha = _batch_sha(b.chat_room_id, b.start_ts)
        out_dir = args.out / d / sha
        out_path = out_dir / "prompt.json"
        if args.skip_existing and out_path.exists():
            skipped_existing += 1
            continue
        out_dir.mkdir(parents=True, exist_ok=True)

        # Build sender_list (deduped by wxid_hash)
        senders_seen: dict[str, str] = {}
        for m in b.msgs:
            wxid = m.get("sender_id") or m.get("from_id") or m.get("sender") or ""
            if not wxid:
                continue
            wh = _wxid_hash(wxid)
            display = (m.get("sender_display_name")
                       or m.get("sender_name") or wxid)
            if wh and wh not in senders_seen:
                senders_seen[wh] = display
        sender_list = [
            {
                "wxid_hash": wh,
                "sender_name": disp,
                "alias_in_manifest_or_None": None,
                "is_self": (wh == self_wxid_hash) if self_wxid_hash else False,
            }
            for wh, disp in senders_seen.items()
        ]

        # Build messages array
        v5_msgs = []
        for m in b.msgs:
            wxid = m.get("sender_id") or m.get("from_id") or m.get("sender") or ""
            v5_msgs.append({
                "ts": m.get("ts"),
                "wxid_hash": _wxid_hash(wxid) if wxid else "",
                "content": m.get("content", ""),
            })

        batch_start = dt.datetime.fromtimestamp(
            b.start_ts, tz=dt.timezone.utc).isoformat()
        batch_end = dt.datetime.fromtimestamp(
            b.end_ts, tz=dt.timezone.utc).isoformat()

        prompt_data = {
            "batch_id": sha,
            "chat_room": b.chat_room_display,
            "room_hash": _chat_room_hash(b.chat_room_display),
            "batch_window_local": f"{batch_start} ~ {batch_end}",
            "sender_list": sender_list,
            "manifest_slice": manifest_slice,
            "messages": v5_msgs,
            "schema_v_input": "v5",
            "v5_native_builder": "v5_wechat_batch_builder.py",
            "n_msgs": len(v5_msgs),
            "n_unique_senders": len(senders_seen),
            "is_group_chat": b.is_group_chat,
        }
        out_path.write_text(
            json.dumps(prompt_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        # Write meta.json sidecar
        meta_path = out_dir / "meta.json"
        meta_path.write_text(json.dumps({
            "date": d,
            "batch_id": sha,
            "chat_room_id": b.chat_room_id,
            "chat_room_display": b.chat_room_display,
            "is_group_chat": b.is_group_chat,
            "n_msgs": b.n_msgs,
            "n_unique_senders": b.n_unique_senders,
            "is_unresolved_query": b.is_unresolved_query,
            "start_ts": b.start_ts,
            "end_ts": b.end_ts,
            "schema_v_input": "v5",
            "built_by": "v5_wechat_batch_builder",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    summary = {
        "ok": True,
        "start": args.start,
        "end": args.end,
        "raw_msgs": len(msgs_raw),
        "filtered_msgs": len(msgs),
        "total_batches_cut": len(all_batches),
        "batches_written": written,
        "skipped_existing": skipped_existing,
        "skipped_out_of_range": skipped_oor,
        "out": str(args.out),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
