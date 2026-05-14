"""Layer A — utterance_merger (TU-2 of plan_v0 batch_quality_uplift).

Merge same-sender consecutive short msgs into single utterance + drop L1 noise.

Algorithm:
  1. L1 prefilter: drop msg if `len(content) < 4` or content is pure
     punctuation/digit/emoji/whitespace.
  2. Merge two adjacent msgs A and B iff
       (a) A.sender_display_name == B.sender_display_name (same speaker)
       (b) B.ts - A.ts < gap_sec (default 30s)
       (c) A.content's last char NOT in {。!?！？.}  (no sentence-end)
     i.e. multi-msg fragments treated as one logical utterance.

Output dict schema (forward-compat with cut_batches' List[dict]):
  - all original keys preserved (sender, sender_display_name, chat_name, ...)
  - `merged_n`: int (# of source msgs combined; 1 if not merged)
  - `merged_content`: str (newline-joined contents)
  - `ts_start`: float (first source msg ts)
  - `ts_end`: float (last source msg ts)
  - `content`: kept as merged_content for downstream compat
  - `ts`: kept as ts_end for downstream compat (e.g. cut_batches still uses .ts)

Cross-sender NEVER merge (HARD invariant).
Cross-batch (gap > BATCH_GAP_SEC = 1800s) NEVER merge even same sender
(per M-arch-3; this guarantees mergerged utterances respect downstream batch
boundaries).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Default same-sender merge gap (seconds). 30s aligns with IM typing-burst
# heuristic (LangChain ConversationBuffer uses similar window).
DEFAULT_GAP_SEC = int(os.environ.get("MEMEXA_UTTERANCE_MERGE_GAP_SEC", "30"))

# Hard upper bound — even if same sender, never merge across batch boundary
# (must remain ≤ batch_chat_extract.BATCH_GAP_SEC = 1800).
BATCH_GAP_SEC = 1800

# L1 noise: pure punctuation / digit / emoji / whitespace; or len<4.
# We drop "11", "（）", "哦呜" (3 chars), "。", "？？", emoji combos.
# Keep "睡觉呢" (3 chars but real word) — boundary case: tighten to len < 4.
_PUNCT_DIGIT_EMOJI_RE = re.compile(
    r"^[\s\W\d_]*$",  # whitespace / non-word / digit / underscore only
    re.UNICODE,
)

# Sentence-end punctuation set (Chinese + English). If last char of A is
# in this set, A is a complete sentence — DO NOT merge with following B.
_SENTENCE_END = set("。！？.!?；;…")


def _is_noise(content: str) -> bool:
    """L1 prefilter: True if content is too short or pure-punct/emoji/digit."""
    s = (content or "").strip()
    if not s:
        return True
    # Allow "ok"/"hi" 2-char words by exempting len-3 short Chinese affirmatives;
    # but per spec drop everything <4 chars to remove "11"/"（）"/emoji noise.
    if len(s) < 4:
        return True
    if _PUNCT_DIGIT_EMOJI_RE.match(s):
        return True
    return False


def _sender_key(msg: Dict[str, Any]) -> str:
    """Sender attribution fallback chain (per M-arch-1):
    sender_display_name → sender_wxid_hash → sender → "unknown".
    """
    for key in ("sender_display_name", "sender_wxid_hash", "sender"):
        v = msg.get(key)
        if v:
            return str(v)
    return "unknown"


def _last_char_is_sentence_end(content: str) -> bool:
    s = (content or "").rstrip()
    if not s:
        return False
    return s[-1] in _SENTENCE_END


def _can_merge(prev: Dict[str, Any], cur: Dict[str, Any], gap_sec: int) -> bool:
    """True iff prev and cur can be merged into one utterance.

    Invariants:
      - same sender (HARD)
      - gap < min(gap_sec, BATCH_GAP_SEC) (HARD)
      - prev's last char is NOT sentence-end (soft; if sentence ended,
        prev is logically complete, do not merge with cur even same sender)
    """
    if _sender_key(prev) != _sender_key(cur):
        return False
    gap = float(cur.get("ts", 0)) - float(prev.get("ts_end", prev.get("ts", 0)))
    if gap >= min(gap_sec, BATCH_GAP_SEC):
        return False
    prev_content = prev.get("merged_content") or prev.get("content") or ""
    if _last_char_is_sentence_end(prev_content):
        return False
    return True


def _seed_utterance(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Initialize a single-msg utterance dict from a raw msg."""
    out = dict(msg)
    ts_val = float(msg.get("ts", 0))
    out["merged_n"] = 1
    out["merged_content"] = str(msg.get("content", "") or "")
    out["ts_start"] = ts_val
    out["ts_end"] = ts_val
    # Keep `content` and `ts` synced to merged values for downstream compat.
    out["content"] = out["merged_content"]
    out["ts"] = ts_val
    return out


def _extend_utterance(utt: Dict[str, Any], msg: Dict[str, Any]) -> None:
    """Merge msg into existing utterance dict (in place)."""
    msg_content = str(msg.get("content", "") or "")
    msg_ts = float(msg.get("ts", 0))
    utt["merged_n"] += 1
    utt["merged_content"] = (utt["merged_content"] + "\n" + msg_content).strip()
    utt["ts_end"] = msg_ts
    utt["content"] = utt["merged_content"]
    utt["ts"] = msg_ts  # cut_batches reads .ts; use ts_end so gap calc is on tail


def merge_utterances(
    msgs: List[Dict[str, Any]],
    gap_sec: int = DEFAULT_GAP_SEC,
) -> List[Dict[str, Any]]:
    """Apply L1 prefilter + same-sender short-gap merge.

    Args:
      msgs: list of raw msg dicts (must have `ts` numeric and either
        `content` str and `sender_display_name` or fallback sender keys).
      gap_sec: max gap (seconds) between adjacent same-sender msgs to merge.

    Returns:
      list of utterance dicts (each is forward-compatible with raw msg
      dict; new fields: merged_n, merged_content, ts_start, ts_end).
    """
    if not msgs:
        return []
    # Stable sort by ts (don't disturb tie-order).
    sorted_msgs = sorted(msgs, key=lambda m: float(m.get("ts", 0)))

    # L1 prefilter
    clean = [m for m in sorted_msgs if not _is_noise(m.get("content"))]
    if not clean:
        return []

    out: List[Dict[str, Any]] = [_seed_utterance(clean[0])]
    for m in clean[1:]:
        prev = out[-1]
        if _can_merge(prev, m, gap_sec):
            _extend_utterance(prev, m)
        else:
            out.append(_seed_utterance(m))

    # Best-effort trace (fail-soft if trace_sink unavailable).
    try:
        from src.core.trace_sink import write_trace_event
        ratio = len(sorted_msgs) / max(1, len(out))
        write_trace_event("utterance_merged", {
            "n_msgs_pre": len(sorted_msgs),
            "n_clean_post_l1": len(clean),
            "n_utterances_post": len(out),
            "ratio": round(ratio, 3),
        })
    except Exception:  # pragma: no cover
        pass

    return out


def _probe_live(since_days: int = 2) -> Dict[str, Any]:
    """LIVE smoke probe (per HARD RULE feedback_extract_chain_live_smoke_required).

    Reads real WeChat msgs from local DB, applies merge_utterances, returns
    summary stats. Used by --probe CLI flag for AC-V2 LIVE verification.
    """
    from src.wechat_db import WeChatDBReader
    from src.extraction.wechat_batch_ingest import _wxmessage_to_dict

    r = WeChatDBReader()
    r.initialize()
    since_ts = time.time() - since_days * 86400
    raw = r.read_after(since_ts, chat_name=None)
    msgs = []
    for m in raw:
        d = _wxmessage_to_dict(m)
        if d.get("content"):
            msgs.append(d)
    utts = merge_utterances(msgs)
    n_msgs = len(msgs)
    n_utts = max(1, len(utts))  # avoid div0
    return {
        "since_days": since_days,
        "n_msgs": n_msgs,
        "n_utterances": len(utts),
        "ratio": round(n_msgs / n_utts, 3),
        "n_dropped_l1": n_msgs - sum(u.get("merged_n", 1) for u in utts),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="utterance_merger (Layer A)")
    p.add_argument("--probe", action="store_true",
                   help="LIVE smoke probe on real WeChat data")
    p.add_argument("--since-days", type=int, default=2)
    p.add_argument("--json", action="store_true", default=True)
    args = p.parse_args()
    if args.probe:
        try:
            result = _probe_live(args.since_days)
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if not result.get("error") else 1
    print(json.dumps({"info": "use --probe to run LIVE smoke"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
