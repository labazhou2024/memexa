"""chat_context_provider.py — shared helper for chat context injection.

TU-4 Closure B (2026-05-01): single chokepoint for all 5 agent touchpoints.

Security design:
- ChatFact TypedDict exposes ONLY {topic, predicate, confidence, chat_room_hash,
  date_iso}. NEVER message_body or raw query (RP-SEC-8 PII strip).
- Cache keyed by (role, session_id, topic_hash); max 128 entries per namespace;
  TTL=60s (RP-SEC-6 cache isolation).
- Error boundary: get_chat_context is the LLM-context boundary. ALL exceptions
  are caught here; on CapabilityRequired emit trace + return []; on DB errors
  emit trace + return [] (RP-LOG-4).
- Execution order in query_entity (RP-LOG-1): capability gate FIRST → flag
  check SECOND → PG SELECT THIRD.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Dict, List, Optional, Tuple

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict  # type: ignore


# ---------------------------------------------------------------------------
# ChatFact schema (PII-stripped)
# ---------------------------------------------------------------------------

class ChatFact(TypedDict):
    """Safe LLM-context schema. NEVER include message_body or raw query."""
    topic: str
    predicate: str
    confidence: float
    chat_room_hash: str
    date_iso: str


# ---------------------------------------------------------------------------
# Session-scoped cache (RP-SEC-6)
# ---------------------------------------------------------------------------

_CACHE_MAX_ENTRIES = 128
_CACHE_TTL_SEC = 60


class _SessionScopedCache:
    """LRU-style cache keyed by (role, session_id, topic_hash).

    Namespace = (role, session_id); enforces max 128 entries per namespace.
    TTL=60s per entry (RP-SEC-6 cache isolation).
    """

    def __init__(self) -> None:
        # {namespace_key: {topic_hash: (ts, List[ChatFact])}}
        self._store: Dict[Tuple[str, str], Dict[str, Tuple[float, List[ChatFact]]]] = {}

    def _namespace_key(self, role: str, session_id: str) -> Tuple[str, str]:
        return (role, session_id)

    def _topic_hash(self, topic: str) -> str:
        return hashlib.sha256(topic.encode("utf-8", errors="replace")).hexdigest()[:16]

    def get(self, role: str, session_id: str, topic: str) -> Optional[List[ChatFact]]:
        """Return cached result or None if miss/expired."""
        ns = self._namespace_key(role, session_id)
        th = self._topic_hash(topic)
        namespace = self._store.get(ns)
        if namespace is None:
            return None
        entry = namespace.get(th)
        if entry is None:
            return None
        ts, facts = entry
        if (time.monotonic() - ts) > _CACHE_TTL_SEC:
            del namespace[th]
            return None
        return facts

    def put(self, role: str, session_id: str, topic: str, facts: List[ChatFact]) -> None:
        """Store result; evict oldest entry if namespace at capacity."""
        ns = self._namespace_key(role, session_id)
        th = self._topic_hash(topic)
        if ns not in self._store:
            self._store[ns] = {}
        namespace = self._store[ns]
        # Evict oldest if at capacity
        if len(namespace) >= _CACHE_MAX_ENTRIES and th not in namespace:
            oldest_th = next(iter(namespace))
            del namespace[oldest_th]
        namespace[th] = (time.monotonic(), facts)


_cache = _SessionScopedCache()


# ---------------------------------------------------------------------------
# Trace emission (best-effort)
# ---------------------------------------------------------------------------

def _emit_trace(event: str, payload: dict) -> None:
    """Emit trace event to MEMEXA_GMV2_STUB_TRACE_LOG (best-effort)."""
    path = os.environ.get("MEMEXA_GMV2_STUB_TRACE_LOG", "")
    if not path:
        return
    try:
        rec = {"event": event, "ts": time.time(), **payload}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Sanitizer: FactRow → ChatFact (drop PII fields)
# ---------------------------------------------------------------------------

def _sanitize_to_chat_fact(fact_row) -> Optional[ChatFact]:
    """Convert a FactRow or dict to a ChatFact, stripping all PII fields.

    Returns None if mandatory fields are missing.
    """
    if hasattr(fact_row, "object_canon"):
        # FactRow dataclass
        text = fact_row.object_canon or ""
        predicate = fact_row.predicate or ""
        confidence = float(fact_row.confidence or 0.0)
        md = fact_row.metadata or {}
    elif isinstance(fact_row, dict):
        text = fact_row.get("object_canon") or fact_row.get("topic") or ""
        predicate = fact_row.get("predicate") or ""
        confidence = float(fact_row.get("confidence") or 0.0)
        md = fact_row.get("metadata") or {}
    else:
        return None

    # Derive chat_room_hash and date_iso from metadata
    chat_room_hash = (
        md.get("chat_room_hash") or
        md.get("chat_hash") or
        ""
    )
    date_iso = (
        md.get("observed_at") or
        md.get("date_iso") or
        md.get("timestamp") or
        ""
    )

    # NEVER include message_body or raw query
    return ChatFact(
        topic=text[:200],  # truncate to limit context size
        predicate=predicate[:100],
        confidence=confidence,
        chat_room_hash=str(chat_room_hash)[:32],
        date_iso=str(date_iso)[:30],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_chat_context(
    topic: str,
    role: str,
    session_id: str,
    capability_token: Optional[bytes] = None,
) -> List[ChatFact]:
    """Get chat context facts for a topic, role, and session.

    This is the LLM-context boundary: ALL exceptions are caught here.
    Returns [] on any error; emits distinguishable trace events per error type.

    Args:
        topic: Query topic to look up in graph.
        role: Agent role (e.g. "chief-researcher", "architect").
        session_id: Unique session identifier for cache scoping.
        capability_token: Optional pre-minted capability token bytes.

    Returns:
        List of ChatFact dicts (PII-stripped). Never raises.
    """
    # Cache hit
    cached = _cache.get(role, session_id, topic)
    if cached is not None:
        return cached

    facts: List[ChatFact] = []
    try:
        from src.core.graph_memory_v2 import query_entity, CapabilityRequired
        raw_facts = query_entity(
            topic,
            limit=10,
            extracted_by="chat-realtime",
            capability_token=capability_token,
        )
        for rf in raw_facts:
            cf = _sanitize_to_chat_fact(rf)
            if cf is not None:
                # Ensure no message_body field leaks through
                assert "message_body" not in cf, "PII strip failure: message_body present"
                facts.append(cf)

    except Exception as exc:  # noqa: BLE001
        exc_type = type(exc).__name__
        # Check for CapabilityRequired by type name (avoids import-order issues)
        if exc_type == "CapabilityRequired" or "CapabilityRequired" in str(type(exc)):
            _emit_trace("chat_context_capability_denied", {
                "role": role,
                "topic_hash": hashlib.sha256(topic.encode()).hexdigest()[:16],
            })
        elif "OperationalError" in exc_type or "psycopg2" in exc_type:
            _emit_trace("chat_context_db_unreachable", {
                "role": role,
                "exc": exc_type,
            })
        else:
            _emit_trace("chat_context_db_unreachable", {
                "role": role,
                "exc": exc_type,
                "detail": str(exc)[:100],
            })
        return []

    _cache.put(role, session_id, topic, facts)
    return facts
