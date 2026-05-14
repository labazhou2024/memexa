"""Layer C — batch_classifier (TU-4 of plan_v0 batch_quality_uplift).

Single-LLM (Qwen :18080) 5-class router for ChatBatch:
  chitchat / planning / sharing / argument / unresolved_query

Decision protocol:
  - If batch.is_unresolved_query (cut_batches flag) → "unresolved_query"
    + confidence=1.0 (rule-based shortcut, skip LLM)
  - Else: LLM classify with confidence 0.0-1.0
  - If confidence < 0.7 → fallback "informative" (PROMPT_INFORMATIVE used)
  - On LLM error → fallback "informative"

Output:
  {"type": str, "confidence": float, "raw": str, "fallback_reason": str|None}

This is intentionally Qwen-only (not paired_eval) to keep latency low —
classification is a coarse routing decision, not the extraction itself.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

VALID_TYPES = {"chitchat", "planning", "sharing", "argument", "unresolved_query"}
CONFIDENCE_FALLBACK_THRESHOLD = 0.7
CLASSIFIER_TIMEOUT_SEC = 60
CLASSIFIER_MAX_TOKENS = 256

_CLASSIFIER_PROMPT = (
    "/no_think\n"
    "你是对话类型分类器。读以下 batch (≤30 msg, 同一 chat_room, ≤30min)，"
    "判断这段对话的 *主要类型*:\n\n"
    "  - chitchat: 闲聊/亲昵互动/问候/无具体事项\n"
    "  - planning: 含约定/计划/承诺/时间安排\n"
    "  - sharing: 分享链接/图片/话题/资源\n"
    "  - argument: 观点分歧/讨论/争论\n"
    "  - unresolved_query: 1v1 短问询无回应 (caller 已 flag, 但你也判断)\n\n"
    "**输出严格 JSON**: "
    "{\"type\": \"<one of 5>\", \"confidence\": <0.0-1.0>}\n"
    "不要解释, 不要前缀。\n\n"
    "对话:\n"
)


def _build_classifier_prompt(batch_payload: Dict[str, Any]) -> str:
    """Build classifier prompt = system instruction + batch JSON."""
    payload_json = json.dumps(batch_payload, ensure_ascii=False, indent=2)
    return _CLASSIFIER_PROMPT + payload_json + "\n\nJSON:"


def _parse_classifier_output(raw: str) -> Dict[str, Any]:
    """Parse LLM JSON output. Returns dict with type/confidence keys.

    On parse failure or invalid type: returns informative fallback dict.
    """
    if not raw:
        return _fallback("empty_output")
    # Find first JSON object
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return _fallback("no_json_braces")
    try:
        obj = json.loads(raw[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return _fallback("json_decode_error")
    if not isinstance(obj, dict):
        return _fallback("not_dict")
    btype = str(obj.get("type", "")).strip().lower()
    if btype not in VALID_TYPES:
        return _fallback(f"invalid_type:{btype[:30]}")
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return {"type": btype, "confidence": conf, "raw": raw[:300],
            "fallback_reason": None}


def _fallback(reason: str) -> Dict[str, Any]:
    """Build informative-fallback result with reason annotation."""
    return {"type": "informative", "confidence": 0.0,
            "raw": "", "fallback_reason": reason}


def classify_batch(batch: Any) -> Dict[str, Any]:
    """Classify a ChatBatch into 5-class + confidence.

    Args:
      batch: ChatBatch instance with .chat_room_display, .is_group_chat,
        .start_ts, .end_ts, .n_msgs, .is_unresolved_query, .msgs
        (or any object with these attrs; duck-typed for testing).

    Returns:
      dict {"type": str, "confidence": float, "raw": str,
            "fallback_reason": str|None}
    """
    # Rule-based shortcut: unresolved_query flag from cut_batches
    if getattr(batch, "is_unresolved_query", False):
        result = {"type": "unresolved_query", "confidence": 1.0,
                  "raw": "rule_based_unresolved_flag",
                  "fallback_reason": None}
        _emit_trace(batch, result)
        return result

    # Build payload + LLM call
    payload = _batch_to_payload(batch)
    prompt = _build_classifier_prompt(payload)
    try:
        from src.core.paired_eval import (
            _http_call_sync,
            _get_validated_host,
            _DEFAULT_PRIMARY_PORT,
        )
        host = _get_validated_host()
        url = f"http://{host}:{_DEFAULT_PRIMARY_PORT}/v1/chat/completions"
        r = _http_call_sync(url, prompt, "mlx-community/Qwen3-14B-4bit",
                            timeout=CLASSIFIER_TIMEOUT_SEC,
                            max_tokens=CLASSIFIER_MAX_TOKENS)
        if not r.get("ok"):
            result = _fallback(f"qwen_status_{r.get('status_code')}")
            _emit_trace(batch, result)
            return result
        result = _parse_classifier_output(r["text"])
    except Exception as e:  # pragma: no cover (LIVE-only failure mode)
        result = _fallback(f"exception:{type(e).__name__}")
    _emit_trace(batch, result)
    return result


def _batch_to_payload(batch: Any) -> Dict[str, Any]:
    """Compact batch JSON for classifier (truncate msg content to keep prompt small)."""
    msgs = getattr(batch, "msgs", []) or []
    return {
        "chat_room": getattr(batch, "chat_room_display", "?")[:60],
        "is_group_chat": bool(getattr(batch, "is_group_chat", False)),
        "n_msgs": len(msgs),
        "is_unresolved_query": bool(getattr(batch, "is_unresolved_query", False)),
        "messages": [
            {
                "sender": (m.get("sender_display_name")
                           or m.get("sender", "?"))[:30],
                "content": str(m.get("content", "") or "")[:120],
            }
            for m in msgs[:30]
        ],
    }


def _emit_trace(batch: Any, result: Dict[str, Any]) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("batch_classified", {
            "chat_room": str(getattr(batch, "chat_room_display", ""))[:40],
            "n_msgs": len(getattr(batch, "msgs", []) or []),
            "type": result["type"],
            "confidence": result["confidence"],
            "fallback_reason": result.get("fallback_reason"),
        })
    except Exception:  # pragma: no cover
        pass


def resolve_routing(classifier_result: Dict[str, Any]) -> str:
    """Apply confidence threshold → return prompt-key for batch_prompts.get_prompt.

    If classifier said valid type but confidence < threshold → "informative".
    """
    btype = classifier_result.get("type", "informative")
    conf = float(classifier_result.get("confidence", 0.0))
    if btype == "informative":
        return "informative"
    if conf < CONFIDENCE_FALLBACK_THRESHOLD:
        return "informative"
    return btype
