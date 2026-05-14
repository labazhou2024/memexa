"""Convert legacy v3 prompt.json (data/extract_archive/<date>/<batch>/prompt.json)
to L0 v5 input format.

Old format (v3 daemon-driven):
  {
    "prompt": "<full LLM prompt str with embedded JSON: {chat_room, messages, ...}>",
    "classified_type": "...",
    "classifier_confidence": ...,
    "routing_key": "...",
    "memory_summary": "..."
  }

New v5 format (what l0_worker_v2_ustc expects):
  {
    "batch_id": "<batch_hash>",
    "chat_room": "...",
    "room_hash": "32hex",
    "batch_window_local": "ISO ~ ISO",
    "sender_list": [{wxid_hash, alias_in_manifest_or_None, is_self}],
    "manifest_slice": {persons, organizations, ...},
    "messages": [{ts, wxid_hash, content}, ...],
    "chinese_calendar_window": {...} | None,
    "user_calendar_window": {...} | None,
  }

Spec: docs/l0_v5/MASTER_PLAN.md §11

Usage:
  python convert_extract_archive_to_v5.py \
    --src data/extract_archive \
    --out data/l0_v5/input_batches \
    --start-date 2026-01-01 --end-date 2026-05-06 \
    --manifest-path data/identity_manifest.yaml
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("convert_v3_to_v5")


def _wxid_hash(wxid: str) -> str:
    return hashlib.sha256(wxid.encode("utf-8")).hexdigest()[:16]


def _chat_room_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:32]


# Pattern: extract the embedded JSON block from old prompt.
# Old prompt has "对话数据:\n{ ... }" or "对话:\n{ ... }" (Chinese label) or naked JSON near end.
EMBEDDED_JSON_LABELS = ("对话数据:", "对话:", "聊天数据:", "messages:", "input:", "data:")


def extract_embedded_json(prompt_text: str) -> Optional[Dict[str, Any]]:
    """Try several strategies to recover the embedded chat JSON."""
    # Strategy 1: find label-followed brace block
    for label in EMBEDDED_JSON_LABELS:
        idx = prompt_text.find(label)
        if idx >= 0:
            after = prompt_text[idx + len(label):]
            brace_start = after.find("{")
            if brace_start < 0:
                continue
            # find matching close brace
            depth = 0
            for i, ch in enumerate(after[brace_start:]):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(after[brace_start:brace_start + i + 1])
                        except json.JSONDecodeError:
                            break
                        break

    # Strategy 2: find any top-level brace block containing "messages"
    for m in re.finditer(r"\{[^{}]*\"messages\"[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
                         prompt_text, re.DOTALL):
        try:
            obj = json.loads(m.group(0))
            if "messages" in obj:
                return obj
        except json.JSONDecodeError:
            continue

    # Strategy 3: scan all braces, attempt parse
    for start in range(len(prompt_text)):
        if prompt_text[start] != "{":
            continue
        depth = 0
        for i in range(start, len(prompt_text)):
            if prompt_text[i] == "{":
                depth += 1
            elif prompt_text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(prompt_text[start:i + 1])
                        if isinstance(obj, dict) and "messages" in obj:
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break

    return None


def convert_old_prompt(
    old_data: Dict[str, Any],
    batch_id: str,
    manifest_slice: Optional[Dict[str, Any]] = None,
    chinese_calendar_window: Optional[Dict[str, str]] = None,
    user_calendar_window: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Returns new-format prompt dict, or None on failure."""
    prompt_text = old_data.get("prompt") or ""
    if not prompt_text:
        return None

    embedded = extract_embedded_json(prompt_text)
    if embedded is None:
        return None

    chat_room = embedded.get("chat_room", "")
    raw_msgs = embedded.get("messages") or []
    batch_start = embedded.get("batch_start_ts", "")
    batch_end = embedded.get("batch_end_ts", batch_start)

    if not chat_room or not raw_msgs:
        return None

    room_hash = _chat_room_hash(chat_room)

    # Build messages with wxid_hash (no raw wxid)
    messages: List[Dict[str, Any]] = []
    senders_seen: Dict[str, str] = {}  # sender_name → wxid_hash (synthetic)
    for m in raw_msgs:
        sender = m.get("sender") or m.get("from") or "?unknown"
        ts = m.get("ts") or m.get("timestamp") or batch_start
        content = m.get("content") or m.get("text") or ""
        # synthetic wxid_hash from sender name (real wxids not available in v3 prompts)
        if sender not in senders_seen:
            senders_seen[sender] = _wxid_hash(f"v3_synth_{chat_room}_{sender}")
        messages.append({
            "ts": ts,
            "wxid_hash": senders_seen[sender],
            "wxid": None,  # real wxid not available in old format
            "sender": sender,  # display name preserved
            "content": content,
        })

    # Build sender_list (deduped)
    sender_list = [
        {
            "wxid_hash": h,
            "sender_name": s,
            "alias_in_manifest_or_None": None,  # filled by dispatcher when manifest_slice known
            "is_self": False,  # filled by dispatcher
        }
        for s, h in senders_seen.items()
    ]

    new_data: Dict[str, Any] = {
        "batch_id": batch_id,
        "chat_room": chat_room,
        "room_hash": room_hash,
        "batch_window_local": f"{batch_start} ~ {batch_end}",
        "sender_list": sender_list,
        "manifest_slice": manifest_slice or {
            "persons": {}, "organizations": {},
            "inanimate": {}, "public_figures": {},
        },
        "messages": messages,
        "schema_v_input": "v5",
    }

    if chinese_calendar_window:
        new_data["chinese_calendar_window"] = chinese_calendar_window
    if user_calendar_window:
        new_data["user_calendar_window"] = user_calendar_window

    # Preserve some v3 metadata for routing
    if "classified_type" in old_data:
        new_data["v3_classified_type"] = old_data["classified_type"]
    if "routing_key" in old_data:
        new_data["v3_routing_key"] = old_data["routing_key"]

    return new_data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=Path("data/extract_archive"))
    parser.add_argument("--out", type=Path, default=Path("data/l0_v5/input_batches"))
    parser.add_argument("--start-date", type=str, default=None,
                        help="ISO date inclusive (e.g. 2026-01-01)")
    parser.add_argument("--end-date", type=str, default=None,
                        help="ISO date inclusive")
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    if not args.src.exists():
        logger.error(f"src {args.src} not found")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)

    # Filter by date range
    def _in_range(date_str: str) -> bool:
        if not args.start_date and not args.end_date:
            return True
        if args.start_date and date_str < args.start_date:
            return False
        if args.end_date and date_str > args.end_date:
            return False
        return True

    n_total = 0
    n_converted = 0
    n_failed = 0
    n_skipped = 0
    for date_dir in sorted(args.src.iterdir()):
        if not date_dir.is_dir():
            continue
        if not _in_range(date_dir.name):
            continue
        for batch_dir in date_dir.iterdir():
            if not batch_dir.is_dir():
                continue
            old_path = batch_dir / "prompt.json"
            if not old_path.exists():
                continue
            n_total += 1
            if args.max_batches and n_converted >= args.max_batches:
                break

            try:
                old_data = json.loads(old_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"  read fail {old_path}: {e}")
                n_failed += 1
                continue

            batch_id = batch_dir.name
            new_data = convert_old_prompt(old_data, batch_id=batch_id)
            if new_data is None:
                n_skipped += 1
                continue

            out_dir = args.out / date_dir.name / batch_id
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "prompt.json"
            out_path.write_text(
                json.dumps(new_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            n_converted += 1

    logger.info(
        f"convert: total_seen={n_total} converted={n_converted} "
        f"skipped={n_skipped} failed={n_failed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
