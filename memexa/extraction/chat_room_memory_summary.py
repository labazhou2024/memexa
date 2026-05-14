"""Layer D — chat_room_memory_summary (TU-7 of plan_v0 batch_quality_uplift).

Query hindsight-api for top-N facts of a given chat_room (last 30d) → format
as ≤500-token markdown summary suitable for LLM prompt injection.

Fallback contract (per B-arch-3):
  - hindsight-api unreachable / 503 / timeout 3s → return "" (empty summary)
  - LLM caller MUST handle empty summary as "no prior context, normal extract"

Cosine similarity dedup helper:
  - is_duplicate_fact(new_text, existing_texts, threshold=0.85)
  - Uses hindsight-api /embed endpoint if available; jaccard fallback otherwise.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HINDSIGHT_API_HOST = os.environ.get("MEMEXA_HINDSIGHT_API_HOST", "127.0.0.1")
HINDSIGHT_API_PORT = int(os.environ.get("MEMEXA_HINDSIGHT_API_PORT", "8888"))
HTTP_TIMEOUT_SEC = 3.0
DEFAULT_TTL_DAYS = 30
DEFAULT_TOP_N = 20
SUMMARY_MAX_CHARS = 2000  # ≈500 tokens (4 chars/token rule of thumb)
DEFAULT_DEDUP_THRESHOLD = 0.85


def _api_url(path: str, **params: Any) -> str:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return f"http://{HINDSIGHT_API_HOST}:{HINDSIGHT_API_PORT}{path}?{qs}"


def _http_get_json(url: str, timeout: float = HTTP_TIMEOUT_SEC) -> Optional[Any]:
    """GET URL, parse JSON. Returns None on any error (silent fallback)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return json.loads(data.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            ConnectionError, OSError, json.JSONDecodeError, ValueError,
            TimeoutError) as e:
        logger.debug("hindsight-api GET %s failed: %s", url, e)
        return None


def _http_post_json(url: str, payload: Dict[str, Any],
                    timeout: float = HTTP_TIMEOUT_SEC) -> Optional[Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError,
            OSError, json.JSONDecodeError, ValueError, TimeoutError) as e:
        logger.debug("hindsight-api POST %s failed: %s", url, e)
        return None


def get_chat_room_summary(
    chat_room_display: str,
    ttl_days: int = DEFAULT_TTL_DAYS,
    top_n: int = DEFAULT_TOP_N,
) -> str:
    """Return ≤500-token markdown summary of recent facts in this chat_room.

    Args:
      chat_room_display: chat_room name (used to query hindsight-api)
      ttl_days: only consider facts created in last N days
      top_n: max facts to include (sorted by confidence + recency)

    Returns:
      Markdown summary string (≤2000 chars). Empty string on fallback.
    """
    if not chat_room_display:
        return ""
    url = _api_url(
        "/memory_units",
        chat_room=chat_room_display,
        order="-confidence,-created_at",
        limit=top_n,
        ttl_days=ttl_days,
    )
    data = _http_get_json(url)
    fallback_empty = data is None
    facts: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        facts = data.get("memory_units") or data.get("results") or []
    elif isinstance(data, list):
        facts = data
    if not facts:
        _emit_trace(chat_room_display, 0, fallback_empty)
        return ""
    summary = _format_summary(facts[:top_n])
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS] + "\n  ..."
    _emit_trace(chat_room_display, len(facts), False)
    return summary


def _format_summary(facts: List[Dict[str, Any]]) -> str:
    """Format facts as bulleted markdown.

    Schema-tolerant: accepts both nested metadata + flat field forms.
    """
    now = time.time()
    out_lines = []
    for f in facts:
        s = f.get("canonical_subject") or (f.get("subject") or "")
        p = f.get("predicate") or ""
        o = f.get("object") or (f.get("canonical_object") or "")
        sender = (f.get("sender_display_name") or
                  (f.get("metadata") or {}).get("sender_display_name") or "")
        valid_at = f.get("valid_at") or f.get("created_at") or ""
        days_ago = ""
        if valid_at:
            try:
                from datetime import datetime, timezone
                ts = datetime.fromisoformat(str(valid_at).replace("Z", "+00:00"))
                days = (now - ts.timestamp()) / 86400
                days_ago = f" — {int(days)}d ago"
            except (ValueError, OSError):
                pass
        sender_part = f" — {sender}" if sender else ""
        out_lines.append(f"- ({s}, {p}, {o}){sender_part}{days_ago}")
    return "\n".join(out_lines)


def _emit_trace(chat_room: str, n_facts: int, fallback_empty: bool) -> None:
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("chat_room_summary_built", {
            "chat_room": chat_room[:40],
            "n_facts_summarized": n_facts,
            "fallback_empty": fallback_empty,
        })
    except Exception:  # pragma: no cover
        pass


# -------- Dedup helper --------

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _jaccard(a: str, b: str) -> float:
    """Token-set jaccard similarity (fallback when embed API unavailable)."""
    sa = set(_TOKEN_RE.findall((a or "").lower()))
    sb = set(_TOKEN_RE.findall((b or "").lower()))
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _embed_via_api(texts: List[str]) -> Optional[List[List[float]]]:
    """Try POST /embed for cosine similarity; None on fallback."""
    if not texts:
        return []
    url = _api_url("/embed")
    data = _http_post_json(url, {"texts": texts})
    if not isinstance(data, dict):
        return None
    embs = data.get("embeddings") or data.get("vectors")
    if not isinstance(embs, list) or len(embs) != len(texts):
        return None
    return embs


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def is_duplicate_fact(
    new_text: str,
    existing_texts: List[str],
    threshold: float = DEFAULT_DEDUP_THRESHOLD,
) -> bool:
    """True if new_text is too similar to any in existing_texts.

    Tries embed API → cosine sim ≥threshold; falls back to jaccard ≥0.7
    (lower threshold for jaccard which is coarser) on API unavailable.
    """
    if not new_text or not existing_texts:
        return False
    embs = _embed_via_api([new_text] + list(existing_texts))
    if embs is not None and len(embs) >= 2:
        new_vec = embs[0]
        for ex_vec in embs[1:]:
            if _cosine(new_vec, ex_vec) >= threshold:
                return True
        return False
    # Jaccard fallback (≥0.7 per plan_v0 §TU-8 fallback path)
    JACCARD_THRESH = 0.7
    for ex in existing_texts:
        if _jaccard(new_text, ex) >= JACCARD_THRESH:
            return True
    return False
