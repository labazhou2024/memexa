"""Batch-aware chat extraction (CEO 2026-05-04 architecture pivot + 4-layer uplift).

Replaces single-msg paired_extract with **(chat_room, time_window) batch
analysis**: collect N consecutive msgs in same chat_room with gap ≤30min,
hand the entire batch JSON to dual LLM (Qwen + Gemma) for fact extraction.

**Plan_v0 batch_quality_uplift (2026-05-04) 4-layer uplift**:
  Layer A (utterance_merger): same-sender merge + L1 noise prefilter
  Layer B (cut_batches): 30min-gap batch cut (works on utterance list)
  Layer C (batch_classifier): 5-class router + type-specific prompt
  Layer D (memory-aware extractor): 30d top-N facts summary inject + cosine dedup
  Layer E (episode_chain_builder): 24h cross-batch episode_id assignment

Batch boundary algorithm (post-uplift):
  0. raw msgs → utterance_merger → utterance list (Layer A)
  1. Group utterances by chat_room
  2. Sort by ts_start
  3. Cut new batch when gap[i] = u[i].ts_start - u[i-1].ts_end > BATCH_GAP_SEC (1800s)
  4. Special case 1v1 short query (≤10 msgs, no reply within 30min after last):
     mark batch.is_unresolved_query = True (Layer C rule-based shortcut →
     unresolved_query type).

Output FactRow includes (in addition to existing schema):
  - batch_type (Layer C classification)
  - episode_id (Layer E cross-batch chain UUID-16, "" if singleton)

Usage:
  python -m memexa.extraction.batch_chat_extract --since-days 1 --max-batches 5
  python -m memexa.extraction.batch_chat_extract --since-days 2 --no-enqueue --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

BATCH_GAP_SEC = int(os.environ.get("MEMEXA_BATCH_GAP_SEC", "3600"))  # 1 h (CEO 2026-05-05: 不要拆碎连续聊天)
SHORT_1V1_THRESHOLD = 10  # msgs
UNRESOLVED_REPLY_WAIT_SEC = int(os.environ.get("MEMEXA_UNRESOLVED_REPLY_WAIT_SEC", "3600"))  # 1 h
MAX_MSGS_PER_BATCH_LLM = int(os.environ.get("MEMEXA_MAX_MSGS_PER_BATCH_LLM", "2000"))  # CEO 2026-05-05: 提到 2000 实际无上限 (>500 msgs 已极少；>2000 LLM 推理可能超 timeout)


@dataclass
class ChatBatch:
    """One coherent slice of conversation (≤30min gap)."""
    chat_room_id: str
    chat_room_display: str
    is_group_chat: bool
    msgs: List[dict] = field(default_factory=list)
    is_unresolved_query: bool = False

    @property
    def start_ts(self) -> float:
        return self.msgs[0].get("ts", 0) if self.msgs else 0

    @property
    def end_ts(self) -> float:
        return self.msgs[-1].get("ts", 0) if self.msgs else 0

    @property
    def n_msgs(self) -> int:
        return len(self.msgs)

    @property
    def n_unique_senders(self) -> int:
        return len({m.get("sender_display_name") or m.get("sender") for m in self.msgs})


def cut_batches(msgs: List[dict], now_ts: Optional[float] = None) -> List[ChatBatch]:
    """Apply batch-cutting algorithm to msgs (already grouped by chat_room).

    Returns list of ChatBatch (oldest first). Mutates input order (sorts by ts).
    """
    if not msgs:
        return []
    msgs = sorted(msgs, key=lambda m: m.get("ts", 0))
    if now_ts is None:
        now_ts = time.time()

    chat_id = msgs[0].get("chat_name", "")
    chat_display = msgs[0].get("chat_display_name") or chat_id
    is_group = bool(msgs[0].get("is_group_chat", False))

    batches: List[ChatBatch] = []
    cur = ChatBatch(chat_room_id=chat_id, chat_room_display=chat_display,
                    is_group_chat=is_group, msgs=[msgs[0]])
    for i in range(1, len(msgs)):
        gap = msgs[i].get("ts", 0) - msgs[i - 1].get("ts", 0)
        if gap > BATCH_GAP_SEC:
            batches.append(cur)
            cur = ChatBatch(chat_room_id=chat_id, chat_room_display=chat_display,
                            is_group_chat=is_group, msgs=[])
        cur.msgs.append(msgs[i])
    if cur.msgs:
        batches.append(cur)

    # Mark unresolved 1v1 short queries
    for b in batches:
        if (not b.is_group_chat
                and b.n_msgs <= SHORT_1V1_THRESHOLD
                and b.n_unique_senders == 1
                and (now_ts - b.end_ts) > UNRESOLVED_REPLY_WAIT_SEC):
            b.is_unresolved_query = True
    return batches


def _build_batch_llm_payload(batch: ChatBatch) -> dict:
    """JSON payload sent to LLM (paired Qwen + Gemma) for batch extraction."""
    return {
        "chat_room": batch.chat_room_display,
        "is_group_chat": batch.is_group_chat,
        "batch_start_ts": datetime.fromtimestamp(batch.start_ts, tz=timezone.utc).isoformat(),
        "batch_end_ts": datetime.fromtimestamp(batch.end_ts, tz=timezone.utc).isoformat(),
        "n_msgs": batch.n_msgs,
        "is_unresolved_query": batch.is_unresolved_query,
        "messages": [
            {
                "ts": datetime.fromtimestamp(m.get("ts", 0), tz=timezone.utc).isoformat(),
                "sender": m.get("sender_display_name") or m.get("sender", "?"),
                "content": str(m.get("content", "") or "")[:500],
            }
            for m in batch.msgs[:MAX_MSGS_PER_BATCH_LLM]
        ],
    }


def _build_batch_extract_prompt(batch: ChatBatch) -> str:
    """Prompt for dual LLM batch extraction.

    Asks LLM to:
    - Read the WHOLE conversation slice
    - Output facts only when conversation has actionable / informative content
    - Each fact tagged with sender (the speaker who originated the fact) +
      ts (the msg ts where the fact was stated)
    - Empty array if conversation is just chitchat/noise/unanswered query
    """
    payload = _build_batch_llm_payload(batch)
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    kind = "群聊" if batch.is_group_chat else "1对1对话"
    unresolved_hint = (
        " (注意: 这是未回复的1v1短问询; 仅提取问询事实，标记为 pending)"
        if batch.is_unresolved_query else ""
    )
    return (
        "/no_think\n"
        f"分析这段{kind}对话{unresolved_hint}（半小时内无中断）。提取真正有价值的事实。\n"
        "每个事实必须:\n"
        "  - subject: 说出该事实的人（用 sender 名字，不要用'我'/'TA'等代词）\n"
        "  - predicate: 谓词（动词或关系，如 plans_with / says / dislikes / scheduled_for）\n"
        "  - object: 客体（人/事/物/时间/地点）\n"
        "  - sender: 该事实的发言人 (从对话中识别)\n"
        "  - ts: 该事实出现的时间戳 (ISO 格式, 从对应 message 的 ts)\n\n"
        "如果对话只是闲聊、问候、表情、无信息量 → 输出 []\n"
        "如果只是单方面问询无回应 → 仅提取问询本身的事实 (1-2 条)\n\n"
        "对话数据:\n"
        f"{payload_json}\n\n"
        "输出 JSON 数组:\n[{\"s\": ..., \"p\": ..., \"o\": ..., \"sender\": ..., \"ts\": ...}, ...]\n\n"
        "JSON:"
    )


def _build_batch_extract_prompt_routed(
    batch: ChatBatch,
    prompt_template: str,
    memory_summary: str = "",
) -> str:
    """Build batch extract prompt = type-specific template + memory + payload.

    prompt_template comes from batch_prompts.get_prompt(routing_key).
    memory_summary (optional) is markdown bullet list of recent facts.
    """
    payload = _build_batch_llm_payload(batch)
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    mem_block = ""
    if memory_summary:
        mem_block = (
            "\n本 chat_room 已有以下 facts，请只提 NEW info (不要重复)：\n"
            + memory_summary + "\n\n"
        )
    return (
        prompt_template + "\n\n" + mem_block
        + "对话数据:\n" + payload_json
        + "\n\n输出 JSON 数组:\n"
        + '[{"s": ..., "p": ..., "o": ..., "sender": ..., "ts": ...}, ...]\n\n'
        + "JSON:"
    )


def extract_batch_paired(batch: ChatBatch,
                         classification: Optional[Dict[str, Any]] = None,
                         memory_summary: str = "") -> List[dict]:
    """Dual-LLM (Qwen + Gemma) batch extract with Layer C/D injection.

    Args:
      batch: ChatBatch
      classification: result from batch_classifier.classify_batch (optional;
        if None, called inline). Schema {type, confidence, raw, fallback_reason}.
      memory_summary: Layer D markdown summary (empty → no inject; per
        chat_room_memory_summary fallback contract).

    Returns:
      list of fact dicts (paired_eval-attested + post-hoc dedup-applied).
    """
    if batch.n_msgs == 0:
        return []

    # Layer C: classify if not pre-supplied
    if classification is None:
        from memexa.extraction.batch_classifier import classify_batch, resolve_routing
        classification = classify_batch(batch)
    else:
        from memexa.extraction.batch_classifier import resolve_routing
    routing_key = resolve_routing(classification)

    # Layer C prompt template
    from memexa.extraction.batch_prompts import get_prompt
    prompt_template = get_prompt(routing_key)

    prompt = _build_batch_extract_prompt_routed(batch, prompt_template,
                                                memory_summary)

    # Reuse paired_eval HTTP infra (Qwen 18080 + Gemma 18081)
    from memexa.core.paired_eval import (
        _http_call_sync,
        _get_validated_host,
        _DEFAULT_PRIMARY_PORT,
        _DEFAULT_SECONDARY_PORT,
        _PAIRED_EVAL_MODEL,
        CrossModelUnavailableError,
        _emit,
        _write_disagreement,
    )
    host = _get_validated_host()
    p_url = f"http://{host}:{_DEFAULT_PRIMARY_PORT}/v1/chat/completions"
    s_url = f"http://{host}:{_DEFAULT_SECONDARY_PORT}/v1/chat/completions"
    sec_model = _PAIRED_EVAL_MODEL
    if "/" not in sec_model:
        sec_model = f"mlx-community/{sec_model}"

    r_p = _http_call_sync(p_url, prompt, "mlx-community/Qwen3-14B-4bit",
                           timeout=240, max_tokens=2560)
    r_s = _http_call_sync(s_url, prompt, sec_model, timeout=240, max_tokens=2560)
    if not (r_p.get("ok") and r_s.get("ok")):
        _emit("cross_model_unavailable", {
            "phase": "batch_extract",
            "p_status": r_p.get("status_code"), "s_status": r_s.get("status_code"),
        })
        raise CrossModelUnavailableError("batch_extract: one of paired_eval ports down")

    facts_p = _parse_batch_facts(r_p["text"])
    facts_s = _parse_batch_facts(r_s["text"])

    # Batch-mode quorum (OR with attestation tier per source):
    # - both LLMs agree (Tier-1 STRICT) → paired_v1
    # - only Qwen → single_qwen_v1 (still accepted; ALL go through 27B if disagree)
    # - only Gemma → single_gemma_v1
    # Tier-1 STRICT on batch is too strict (LLM wording varies); soft-quorum
    # accepts but flags. Disagreement queue captures BOTH-side single-LLM facts
    # so 27B can later confirm/reject.
    set_p = {(f["s"].strip().lower(), f["p"].strip().lower(), f["o"].strip().lower()): f
             for f in facts_p}
    set_s = {(f["s"].strip().lower(), f["p"].strip().lower(), f["o"].strip().lower()): f
             for f in facts_s}
    common_keys = set(set_p) & set(set_s)
    only_p = set(set_p) - common_keys
    only_s = set(set_s) - common_keys

    accepted = []
    for k in common_keys:
        f = dict(set_p[k]); f["paired_eval_attested"] = "paired_v1"
        accepted.append(f)
    for k in only_p:
        f = dict(set_p[k]); f["paired_eval_attested"] = "single_qwen_v1"
        accepted.append(f)
    for k in only_s:
        f = dict(set_s[k]); f["paired_eval_attested"] = "single_qwen_v1"  # alias for arbiter eligibility
        accepted.append(f)

    # CEO 2026-05-05 Tier 3 optimization: only send TRUE conflicts to DeepSeek arbiter.
    # Conflict definition: same (s,o) different p OR same (s,p) different o (mutually exclusive).
    # Complementary facts (different s+p+o, or different s alone) → both KEPT as single_v1, NOT sent to DeepSeek.
    # Effect: ~50%+ DeepSeek API calls eliminated for batches where two LLMs cover different angles.
    only_p_keys = list(only_p)
    only_s_keys = list(only_s)
    qwen_so_to_p = {(k[0], k[2]): k[1] for k in only_p_keys}
    qwen_sp_to_o = {(k[0], k[1]): k[2] for k in only_p_keys}
    gemma_so_to_p = {(k[0], k[2]): k[1] for k in only_s_keys}
    gemma_sp_to_o = {(k[0], k[1]): k[2] for k in only_s_keys}

    conflict_p_keys = set()
    conflict_s_keys = set()
    for kq in only_p_keys:
        sq, pq, oq = kq
        # mutually-exclusive conflict: same (s,o) but different p in gemma side
        if (sq, oq) in gemma_so_to_p and gemma_so_to_p[(sq, oq)] != pq:
            conflict_p_keys.add(kq)
            kg = (sq, gemma_so_to_p[(sq, oq)], oq)
            if kg in only_s:
                conflict_s_keys.add(kg)
        # OR same (s,p) different o
        if (sq, pq) in gemma_sp_to_o and gemma_sp_to_o[(sq, pq)] != oq:
            conflict_p_keys.add(kq)
            kg = (sq, pq, gemma_sp_to_o[(sq, pq)])
            if kg in only_s:
                conflict_s_keys.add(kg)
    # Mirror for gemma
    for kg in only_s_keys:
        sg, pg, og = kg
        if (sg, og) in qwen_so_to_p and qwen_so_to_p[(sg, og)] != pg:
            conflict_s_keys.add(kg)
        if (sg, pg) in qwen_sp_to_o and qwen_sp_to_o[(sg, pg)] != og:
            conflict_s_keys.add(kg)

    disagreements_p = [{"triple": list(k), "source": "qwen3", "fact": set_p[k]}
                        for k in conflict_p_keys]
    disagreements_s = [{"triple": list(k), "source": "gemma", "fact": set_s[k]}
                        for k in conflict_s_keys]

    if disagreements_p or disagreements_s:
        # CEO 2026-05-05: include context_text so DeepSeek arbiter can ground
        # decisions in actual conversation (prev: arbiter saw only (s,p,o) and
        # rejected as 'unknown entities'). Cap content per msg + total batch.
        ctx_msgs = []
        for m in batch.msgs[:60]:  # first 60 msgs cap
            sender = m.get("sender_display_name") or m.get("sender", "?")
            content = str(m.get("content", "") or "")[:200]
            if content:
                ctx_msgs.append(f"{sender}: {content}")
        context_text = "\n".join(ctx_msgs)[:4000]  # 4 KB cap
        _write_disagreement({
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "text_sha256": hashlib.sha256(
                (batch.chat_room_id + str(batch.start_ts)).encode("utf-8")
            ).hexdigest()[:16],
            "n_agreed": len(accepted),
            "n_disagreed": len(disagreements_p) + len(disagreements_s),
            "disagreements": disagreements_p + disagreements_s,
            "ttl_days": 30,
            "context_text": context_text,
            "batch_meta": {
                "chat_room": batch.chat_room_display,
                "is_group": batch.is_group_chat,
                "n_msgs": batch.n_msgs,
                "is_unresolved": batch.is_unresolved_query,
            },
        })

    # Layer D: post-hoc dedup against memory summary (drop facts too similar
    # to already-known facts).
    accepted_pre = list(accepted)
    if memory_summary:
        from memexa.extraction.chat_room_memory_summary import is_duplicate_fact
        existing_lines = [ln.strip("- ").strip() for ln in
                           memory_summary.splitlines() if ln.strip()]
        deduped = []
        for f in accepted:
            new_text = f"({f['s']}, {f['p']}, {f['o']})"
            if not is_duplicate_fact(new_text, existing_lines):
                deduped.append(f)
        accepted = deduped

    # Annotate batch_type on each accepted fact (for downstream AC-V3 verify).
    btype = classification.get("type", "informative")
    for f in accepted:
        f.setdefault("batch_type", btype)
        f.setdefault("classifier_confidence",
                     classification.get("confidence", 0.0))

    _emit("batch_chat_extract_done", {
        "chat_room": batch.chat_room_display[:40],
        "n_msgs": batch.n_msgs,
        "n_qwen": len(facts_p), "n_gemma": len(facts_s),
        "n_agreed_pre_dedup": len(accepted_pre),
        "n_agreed_post_dedup": len(accepted),
        "is_unresolved": batch.is_unresolved_query,
        "batch_type": btype,
        "classifier_confidence": classification.get("confidence", 0.0),
    })
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("batch_extract_routed", {
            "batch_type": btype,
            "confidence": classification.get("confidence", 0.0),
            "prompt_used": routing_key,
            "n_msgs": batch.n_msgs,
            "memory_inject": bool(memory_summary),
        })
        if memory_summary:
            write_trace_event("memory_dedup_applied", {
                "n_facts_pre": len(accepted_pre),
                "n_facts_post": len(accepted),
                "n_dropped": len(accepted_pre) - len(accepted),
            })
    except Exception:  # pragma: no cover
        pass
    return accepted


def _parse_batch_facts(raw: str) -> List[dict]:
    """Parse JSON array from LLM. Each fact must have s, p, o (sender/ts optional)."""
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        arr = json.loads(raw[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(arr, list):
        return []
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        s = str(it.get("s", "")).strip()
        p = str(it.get("p", "")).strip()
        o = str(it.get("o", "")).strip()
        if not (s and p and o):
            continue
        out.append({
            "s": s, "p": p, "o": o,
            "sender": str(it.get("sender", "") or s),
            "ts": str(it.get("ts", "") or ""),
        })
    return out


def build_factrows_from_batch(batch: ChatBatch, facts: List[dict],
                              batch_type: str = "informative") -> List[dict]:
    """Convert LLM-extracted facts to FactRows (P0-15-field schema + batch_type)."""
    rows = []
    for f in facts:
        # ts: prefer LLM-cited per-fact ts, else batch_end_ts
        try:
            valid_at = f.get("ts") or datetime.fromtimestamp(
                batch.end_ts, tz=timezone.utc).isoformat()
            datetime.fromisoformat(valid_at.replace("Z", "+00:00"))  # validate
        except Exception:
            valid_at = datetime.fromtimestamp(
                batch.end_ts, tz=timezone.utc).isoformat()
        sender = f.get("sender", "")
        sender_hash = hashlib.sha256(sender.encode("utf-8")).hexdigest()[:16] if sender else ""
        chat_hash = hashlib.sha256(batch.chat_room_id.encode("utf-8")).hexdigest()[:16]
        receiver_display = ""
        receiver_hash = ""
        if not batch.is_group_chat and sender != batch.chat_room_id:
            receiver_display = batch.chat_room_display
            receiver_hash = chat_hash
        elif not batch.is_group_chat:
            receiver_display = "我"
        fid = hashlib.sha256(
            f"batch\x1f{chat_hash}\x1f{sender_hash}\x1f{valid_at}\x1f{f['s']}\x1f{f['p']}\x1f{f['o']}".encode("utf-8"),
        ).hexdigest()
        rows.append({
            "id": fid,
            "source_kind": "wechat_batch",
            "source_offset": f"batch:{batch.chat_room_id}:{int(batch.start_ts)}-{int(batch.end_ts)}",
            "extracted_by": "backfill-wechat",
            "extraction_prompt_sha": "batch-paired-v1",
            "confidence": 0.85,
            "valid_at": valid_at,
            "invalidated_at": None,
            "canonical_subject": f["s"],
            "predicate": f["p"],
            "object": f["o"],
            "episode_id": "",
            "tentative": batch.is_unresolved_query,
            "corroborated_by": [],
            "contradicted_by": [],
            "validator_run": True,  # batch path = validated by paired_eval
            "paired_eval_attested": f.get("paired_eval_attested", "paired_v1"),
            "chat_room_id_hash": chat_hash,
            "chat_room_display_name": batch.chat_room_display,
            "sender_wxid_hash": sender_hash,
            "sender_display_name": sender,
            "receiver_display_name": receiver_display,
            "receiver_wxid_hash": receiver_hash,
            "is_group_chat": batch.is_group_chat,
            "batch_start_ts": datetime.fromtimestamp(batch.start_ts, tz=timezone.utc).isoformat(),
            "batch_end_ts": datetime.fromtimestamp(batch.end_ts, tz=timezone.utc).isoformat(),
            "batch_n_msgs": batch.n_msgs,
            "is_unresolved_query": batch.is_unresolved_query,
            "batch_type": batch_type,
            "episode_id": "",  # filled by main() via Layer E build_episodes
        })
    return rows


def _enqueue_outbox(factrows: List[dict], outbox_dir: Path) -> Optional[Path]:
    """Write factrows to win_keystone_outbox JSON for Mac sync.

    Returns path to written file, or None on failure. Per
    backfill_outbox_schema_v1 contract.
    """
    try:
        outbox_dir.mkdir(parents=True, exist_ok=True)
        ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        path = outbox_dir / f"{ts_iso}__batch_chat_extract.json"
        payload = {
            "task": "batch_chat_extract",
            "schema_version": 1,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "n_factrows": len(factrows),
            "factrows": factrows,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                                   default=str), encoding="utf-8")
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("batch_outbox_enqueued", {
                "n_factrows": len(factrows),
                "file_path": str(path)[-80:],
            })
        except Exception:  # pragma: no cover
            pass
        return path
    except (OSError, IOError) as e:
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("outbox_write_failed", {
                "error": f"{type(e).__name__}: {e}",
            })
        except Exception:  # pragma: no cover
            pass
        return None


def _split_oversized_batches(batches: List[ChatBatch]
                              ) -> Tuple[List[ChatBatch], int]:
    """Split any batch with n_msgs > MAX_MSGS_PER_BATCH_LLM into sub-batches.

    Preserves chat_room metadata + chronological order. Each sub-batch is a
    fresh ChatBatch holding a slice of msgs. Returns (new_batches, n_splits).

    Without this, _build_batch_llm_payload truncates batch.msgs[:30] and the
    tail of long conversations never reaches the LLM (silent data loss).
    """
    out: List[ChatBatch] = []
    n_split = 0
    for b in batches:
        if b.n_msgs <= MAX_MSGS_PER_BATCH_LLM:
            out.append(b)
            continue
        n_split += 1
        # Slice into chunks of MAX_MSGS_PER_BATCH_LLM, preserve metadata.
        # Each chunk inherits chat_room_id/display + is_group_chat. We do NOT
        # propagate is_unresolved_query because that's a per-conversation flag
        # and split sub-batches no longer represent "the whole 1v1 query".
        for start in range(0, b.n_msgs, MAX_MSGS_PER_BATCH_LLM):
            chunk = b.msgs[start:start + MAX_MSGS_PER_BATCH_LLM]
            sub = ChatBatch(
                chat_room_id=b.chat_room_id,
                chat_room_display=b.chat_room_display,
                is_group_chat=b.is_group_chat,
                msgs=chunk,
                is_unresolved_query=False,
            )
            out.append(sub)
    return out, n_split


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since-days", type=int, default=1)
    p.add_argument("--max-batches", type=int, default=99999,
                   help="cap on batches selected (default: 99999 ≈ unlimited; "
                        "set lower for cost-bounded smoke tests)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--skip-muted", action="store_true", default=True)
    p.add_argument("--no-enqueue", action="store_true",
                   help="dev mode: skip outbox enqueue, just print JSON")
    p.add_argument("--no-lifecycle", action="store_true",
                   help="dev mode: skip ensure_dual_alive + idle exit + 27B sync")
    args = p.parse_args(argv)

    # mac_memory_systemic plan_v0 TU-5: ensure mlx dual alive BEFORE
    # any LLM call. First cold start can take 90-120s (mlx model load).
    if not args.no_lifecycle and not args.dry_run:
        try:
            from memexa.core import mlx_lifecycle
            if not mlx_lifecycle.ensure_dual_alive(timeout=300):
                print(json.dumps({"error": "mlx_dual_alive_failed",
                                   "advice": "ssh primary-host launchctl status check"},
                                  indent=2))
                return 1
        except Exception as e:
            print(json.dumps({"error": f"mlx_lifecycle_exception: {e}"},
                              indent=2))
            return 1

    # Read msgs from WeChat DB
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from memexa.wechat_db import WeChatDBReader
    from memexa.extraction.wechat_batch_ingest import _wxmessage_to_dict
    from memexa.extraction.utterance_merger import merge_utterances
    from memexa.extraction.batch_classifier import classify_batch
    from memexa.extraction.chat_room_memory_summary import get_chat_room_summary
    from memexa.core.episode_chain_builder import build_episodes

    r = WeChatDBReader()
    r.initialize()
    if not r.enc_keys:
        print(json.dumps({"error": "no_enc_keys"}))
        return 1

    since_ts = time.time() - args.since_days * 86400
    msgs_raw = r.read_after(since_ts, chat_name=None)

    # Apply mute filter
    muted = set()
    if args.skip_muted:
        try:
            muted = set(json.loads(
                (Path(__file__).resolve().parents[2] / "data" /
                 "wechat_mute_skiplist.json").read_text(encoding="utf-8")
            ).get("muted_wxids", []))
        except Exception:
            pass

    msgs = []
    for m in msgs_raw:
        d = _wxmessage_to_dict(m)
        if not d.get("content"):
            continue
        if d.get("chat_name") in muted:
            continue
        msgs.append(d)

    # Group by chat_room (for L1+L3 utterance merging per chat_room)
    by_chat: Dict[str, List[dict]] = {}
    for d in msgs:
        by_chat.setdefault(d.get("chat_name", ""), []).append(d)

    # Layer A: utterance_merger applied per chat_room
    n_msgs_pre_merge = len(msgs)
    n_utterances_total = 0
    all_batches: List[ChatBatch] = []
    for chat_id, chat_msgs in by_chat.items():
        utterances = merge_utterances(chat_msgs)
        n_utterances_total += len(utterances)
        all_batches.extend(cut_batches(utterances))

    # 2026-05-04 fix CAUTION #2: split batches with n_msgs > MAX_MSGS_PER_BATCH_LLM
    # so all msgs reach the LLM (previous behavior silently truncated batch.msgs[:30]).
    n_pre_split = len(all_batches)
    all_batches, n_split_events = _split_oversized_batches(all_batches)
    if n_split_events > 0:
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("batch_split_oversized", {
                "n_pre_split": n_pre_split,
                "n_post_split": len(all_batches),
                "n_oversized_batches_split": n_split_events,
                "max_msgs_per_batch": MAX_MSGS_PER_BATCH_LLM,
            })
        except Exception:  # pragma: no cover
            pass

    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("cut_batches_post_merge", {
            "n_msgs_pre_merge": n_msgs_pre_merge,
            "n_utterances_in": n_utterances_total,
            "n_batches": len(all_batches),
        })
    except Exception:  # pragma: no cover
        pass
    all_batches.sort(key=lambda b: -b.n_msgs)
    selected = all_batches[:args.max_batches]
    n_dropped = len(all_batches) - len(selected)
    if n_dropped > 0:
        # 2026-05-04 fix CRITICAL #1: max-batches cap silently dropped batches.
        # Loud warn + trace so CEO sees data loss BEFORE fact extraction completes.
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("batches_silently_dropped", {
                "n_total": len(all_batches),
                "n_selected": len(selected),
                "n_dropped": n_dropped,
                "drop_pct": round(100 * n_dropped / max(1, len(all_batches)), 1),
                "max_batches_arg": args.max_batches,
                "advice": "rerun with --max-batches 99999 for full backfill",
            })
        except Exception:  # pragma: no cover
            pass
        print(f"[WARN] batches_silently_dropped: {n_dropped}/{len(all_batches)} "
              f"({round(100 * n_dropped / max(1, len(all_batches)), 1)}%) "
              f"— rerun with --max-batches 99999 to capture all",
              file=sys.stderr)

    summary = {
        "n_msgs_read": n_msgs_pre_merge,
        "n_utterances_post_merge": n_utterances_total,
        "merge_ratio": round(n_msgs_pre_merge / max(1, n_utterances_total), 3),
        "n_chat_rooms": len(by_chat),
        "n_batches_total": len(all_batches),
        "n_batches_selected": len(selected),
        "n_batches_dropped_by_cap": n_dropped,
        "n_unresolved_1v1": sum(1 for b in selected if b.is_unresolved_query),
        "skiplist_size": len(muted),
        "results": [],
    }

    all_factrows: List[dict] = []
    # 2026-05-04 fix CAUTION #5: per-batch error visibility + cross-model streak abort
    # 2026-05-05 robust_bg_driver TU-2: per-batch wall-clock timeout
    n_batches_failed = 0
    n_batches_succeeded = 0
    cross_model_streak = 0
    CROSS_MODEL_STREAK_ABORT = 3
    PER_BATCH_TIMEOUT_S = float(os.environ.get(
        "MEMEXA_PER_BATCH_TIMEOUT_S", "420"))  # 7 min default; covers ~2 LLM calls × 180s
    failed_chat_rooms: List[str] = []
    aborted_streak = False
    for b in selected:
        batch_info = {
            "chat_room": b.chat_room_display,
            "is_group": b.is_group_chat,
            "n_msgs": b.n_msgs,
            "n_senders": b.n_unique_senders,
            "is_unresolved": b.is_unresolved_query,
            "batch_start": datetime.fromtimestamp(b.start_ts, tz=timezone.utc).isoformat(),
            "batch_end": datetime.fromtimestamp(b.end_ts, tz=timezone.utc).isoformat(),
        }
        if args.dry_run:
            batch_info["dry_run"] = True
        else:
            t_batch_start = time.time()
            try:
                # Layer C: classify
                classification = classify_batch(b)
                # Layer D: memory summary (empty if hindsight-api unreachable)
                memory_summary = get_chat_room_summary(b.chat_room_display)
                # Layer extract: paired_eval + dedup
                facts = extract_batch_paired(b, classification=classification,
                                             memory_summary=memory_summary)
                # TU-2 watchdog: if per-batch took > PER_BATCH_TIMEOUT_S, treat as
                # degraded — emit trace + still accept facts (don't waste them) but
                # abort streak counter early to avoid further long-batches
                _batch_elapsed = time.time() - t_batch_start
                if _batch_elapsed > PER_BATCH_TIMEOUT_S:
                    try:
                        from memexa.core.trace_sink import write_trace_event
                        write_trace_event("batch_per_batch_slow", {
                            "elapsed_sec": round(_batch_elapsed, 1),
                            "limit_sec": PER_BATCH_TIMEOUT_S,
                            "chat_room": b.chat_room_display[:60],
                        })
                    except Exception:
                        pass
                rows = build_factrows_from_batch(
                    b, facts, batch_type=classification.get("type", "informative"))
                all_factrows.extend(rows)
                batch_info["classified_type"] = classification.get("type")
                batch_info["classifier_confidence"] = classification.get(
                    "confidence", 0.0)
                batch_info["memory_summary_size"] = len(memory_summary or "")
                batch_info["n_facts_extracted"] = len(rows)
                batch_info["sample_facts"] = [
                    {"s": r["canonical_subject"], "p": r["predicate"],
                     "o": r["object"], "sender": r["sender_display_name"]}
                    for r in rows[:3]
                ]
                n_batches_succeeded += 1
                cross_model_streak = 0  # reset on any success
            except Exception as e:
                err_name = type(e).__name__
                batch_info["error"] = f"{err_name}: {e}"
                n_batches_failed += 1
                failed_chat_rooms.append(b.chat_room_display)
                if err_name == "CrossModelUnavailableError":
                    cross_model_streak += 1
                    if cross_model_streak >= CROSS_MODEL_STREAK_ABORT:
                        # Both mlx servers down for 3 consecutive batches → abort.
                        # Continuing burns CPU + spam-fails, no chance of recovery
                        # without lifecycle re-arm. Caller can re-run after fix.
                        try:
                            from memexa.core.trace_sink import write_trace_event
                            write_trace_event("batch_run_aborted_cross_model_streak", {
                                "n_consecutive_fails": cross_model_streak,
                                "n_batches_processed": n_batches_succeeded + n_batches_failed,
                                "n_batches_remaining": len(selected) - (n_batches_succeeded + n_batches_failed),
                                "advice": "ssh primary-host launchctl list | grep mlx; rerun after fix",
                            })
                        except Exception:  # pragma: no cover
                            pass
                        print(f"[ABORT] {cross_model_streak} consecutive cross-model fails — "
                              f"aborting after {n_batches_succeeded}+{n_batches_failed} batches; "
                              f"check mlx_lm.server on Mac and re-run.",
                              file=sys.stderr)
                        aborted_streak = True
                        summary["results"].append(batch_info)
                        break
        summary["results"].append(batch_info)
    summary["n_batches_succeeded"] = n_batches_succeeded
    summary["n_batches_failed"] = n_batches_failed
    summary["batch_error_rate"] = round(
        n_batches_failed / max(1, n_batches_succeeded + n_batches_failed), 3)
    summary["failed_chat_rooms_uniq"] = sorted(set(failed_chat_rooms))[:20]
    summary["aborted_cross_model_streak"] = aborted_streak

    # Layer E: episode_chain across all factrows (post-extract aggregation)
    if all_factrows:
        ep_mapping = build_episodes(all_factrows)
        for fr in all_factrows:
            fr["episode_id"] = ep_mapping.get(fr["id"], "")
        summary["n_factrows_total"] = len(all_factrows)
        summary["n_factrows_in_episode"] = sum(
            1 for fr in all_factrows if fr.get("episode_id"))
        summary["n_episodes"] = len(set(fr["episode_id"]
                                         for fr in all_factrows
                                         if fr.get("episode_id")))
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("factrows_episode_id_assigned", {
                "n_factrows_total": len(all_factrows),
                "n_with_episode": summary["n_factrows_in_episode"],
            })
        except Exception:  # pragma: no cover
            pass

    # TU-1: outbox enqueue (unless --no-enqueue or --dry-run)
    if all_factrows and not args.no_enqueue and not args.dry_run:
        outbox_dir = Path(__file__).resolve().parents[2] / "data" / "win_keystone_outbox"
        path = _enqueue_outbox(all_factrows, outbox_dir)
        if path:
            summary["outbox_enqueued"] = str(path)[-100:]

    # mac_memory_systemic plan_v0 TU-8: trigger 27B inline arbitration if
    # any disagreement entries are pending. Fault-tolerant: failure does
    # NOT block batch (facts already in outbox / will sync to PG).
    if not args.no_lifecycle and not args.dry_run:
        try:
            from memexa.core import arbiter_27b_inline
            # 2026-05-04 fix CAUTION #3: 50 → 500 to keep up with backfill volume.
            # 30-day backfill yields ~1k batches → ≤500 disagreement entries
            # typical; 500 cap absorbs the burst without leaving residue for
            # the next call. `drain_until_empty` is the post-backfill bulk
            # cleaner (see arbiter_27b_inline.drain_until_empty).
            arb_result = arbiter_27b_inline.trigger_inline_arbitration(
                max_items=500, dry_run=False)
            summary["inline_arbitration"] = arb_result
            try:
                from memexa.core.trace_sink import write_trace_event
                write_trace_event("batch_inline_arb_invoked", {
                    "n_pending_at_start": arb_result.get("n_pending_at_start", 0),
                    "n_arbitrated": arb_result.get("n_arbitrated", 0),
                    "swap_to_27b_ok": arb_result.get("swap_to_27b_ok", False),
                    "elapsed_sec": arb_result.get("elapsed_sec", 0),
                })
            except Exception:  # pragma: no cover
                pass
        except Exception as e:
            summary["inline_arbitration_error"] = f"{type(e).__name__}: {e}"

    # mac_memory_systemic plan_v0 TU-5: arm idle exit timer
    if not args.no_lifecycle and not args.dry_run:
        try:
            from memexa.core import mlx_lifecycle
            mlx_lifecycle.schedule_idle_exit(idle_min=20)
        except Exception as e:  # pragma: no cover
            summary["idle_exit_schedule_error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


def run_for_cron(since_days: int = 1, max_batches: int = 20,
                 no_enqueue: bool = False) -> dict:
    """Programmatic API for cron 6h path (avoids subprocess + argparse).

    Captures main()'s JSON stdout, parses, returns dict with
    {n_msgs_read, n_utterances_post_merge, merge_ratio, n_batches_total,
     n_batches_selected, n_factrows_total, n_episodes, outbox_enqueued, return_code}.

    Trace event: batch_chat_extract_run_for_cron_completed (payload subset).
    """
    import io
    from contextlib import redirect_stdout
    argv = ["--since-days", str(since_days), "--max-batches", str(max_batches), "--json"]
    if no_enqueue:
        argv.append("--no-enqueue")
    buf = io.StringIO()
    rc = 1
    try:
        with redirect_stdout(buf):
            rc = main(argv=argv)
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 1
    output = buf.getvalue().strip()
    summary: dict = {}
    if output:
        try:
            summary = json.loads(output)
        except json.JSONDecodeError:
            summary = {"raw_output": output[-500:]}
    summary["return_code"] = rc
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("batch_chat_extract_run_for_cron_completed", {
            "since_days": since_days,
            "max_batches": max_batches,
            "n_msgs_read": summary.get("n_msgs_read", 0),
            "n_factrows_total": summary.get("n_factrows_total", 0),
            "outbox_enqueued": bool(summary.get("outbox_enqueued")),
            "return_code": rc,
        })
    except Exception:  # pragma: no cover
        pass
    return summary


if __name__ == "__main__":
    sys.exit(main())
