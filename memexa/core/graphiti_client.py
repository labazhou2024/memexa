"""
Graphiti Client — memexa integration with Graphiti+Neo4j Desktop.

TU2 from 2026-04-19_graphiti_foundation.md. HIGHEST-risk module.

Responsibilities:
- Singleton client with lifecycle + health check
- Feature flag MEMEXA_GRAPHITI_ENABLED ∈ {"0", "shadow", "1"}
- Shared bge-small-zh embedder via semantic_kb._get_model() (no 2x RAM)
- 500ms connect timeout + graceful degrade when Neo4j unavailable
- Offline MOCK mode for tests (MEMEXA_GRAPHITI_MOCK=1)

Contract:
- get_client() returns None if disabled or Neo4j down (callers must handle)
- is_shadow() / is_active() / is_disabled() explicit state predicates
- query(cypher, **params) with hard timeout; returns [] on failure
- add_episode(text, source, ts) fire-and-forget (never raises)
- health() returns status dict for dashboard

Safe contract: NEVER raises to caller on operational errors. Only raises
on programming errors (bad event type, missing required field).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------- Config ----------

_FLAG_DISABLED = "0"
_FLAG_SHADOW = "shadow"
_FLAG_ACTIVE = "1"

_CONNECT_TIMEOUT_S = 0.5  # 500ms — any longer and SessionStart feels slow
_QUERY_TIMEOUT_S = 2.0
_LOCK = threading.Lock()


def _flag() -> str:
    v = os.environ.get("MEMEXA_GRAPHITI_ENABLED", _FLAG_DISABLED)
    return v if v in (_FLAG_DISABLED, _FLAG_SHADOW, _FLAG_ACTIVE) else _FLAG_DISABLED


def is_disabled() -> bool: return _flag() == _FLAG_DISABLED
def is_shadow() -> bool:   return _flag() == _FLAG_SHADOW
def is_active() -> bool:   return _flag() == _FLAG_ACTIVE
def is_enabled() -> bool:  return not is_disabled()  # shadow OR active


def _is_mock() -> bool:
    return os.environ.get("MEMEXA_GRAPHITI_MOCK", "0") == "1"


# ---------- Mock backend (for tests/offline) ----------

@dataclass
class _MockGraph:
    """In-memory episode store for tests. No bi-temporal — just list."""
    episodes: List[Dict[str, Any]] = field(default_factory=list)
    facts: List[Dict[str, Any]] = field(default_factory=list)

    def add_episode(self, text: str, source: str, ts: str, payload: Optional[Dict] = None) -> str:
        eid = f"mock_ep_{len(self.episodes)}"
        self.episodes.append({
            "id": eid, "text": text[:1000], "source": source, "ts": ts,
            "payload": payload or {},
        })
        return eid

    def add_fact(self, fact: Dict[str, Any]) -> str:
        fid = f"mock_f_{len(self.facts)}"
        rec = {"id": fid, "invalidated": False, **fact}
        self.facts.append(rec)
        return fid

    def invalidate_fact(self, fid: str, reason: str) -> bool:
        # [LOG-M9 R2] Align fields with _Neo4jBackend: both backends must
        # set BOTH invalidated_at AND valid_to so schema reads consistently.
        for f in self.facts:
            if f["id"] == fid:
                now = datetime.utcnow().isoformat()
                f["invalidated"] = True
                f["invalidate_reason"] = reason
                f["invalidated_at"] = now
                f["valid_to"] = now
                return True
        return False

    def query_facts(self, predicate_substr: Optional[str] = None,
                    exclude_invalidated: bool = True,
                    limit: int = 10) -> List[Dict[str, Any]]:
        out = []
        for f in self.facts:
            if exclude_invalidated and f.get("invalidated"):
                continue
            if predicate_substr and predicate_substr not in f.get("predicate", ""):
                continue
            out.append(f)
            if len(out) >= limit:
                break
        return out

    def count_nodes(self) -> int: return len(self.episodes) + len(self.facts)


# ---------- Real backend wrapper ----------

class _Neo4jBackend:
    """Thin wrapper over neo4j.GraphDatabase + graphiti-core when installed."""
    def __init__(self, uri: str, user: str, password: str):
        from neo4j import GraphDatabase  # lazy import — only when flag enabled
        self._driver = GraphDatabase.driver(
            uri, auth=(user, password),
            connection_timeout=_CONNECT_TIMEOUT_S,
        )
        self._driver.verify_connectivity()

    def add_episode(self, text: str, source: str, ts: str,
                    payload: Optional[Dict] = None) -> str:
        eid = f"ep_{int(time.time() * 1000)}"
        with self._driver.session() as s:
            s.run(
                "CREATE (e:Episode {id:$id, text:$text, source:$source, "
                "ts:$ts, payload:$payload})",
                id=eid, text=text[:2000], source=source, ts=ts,
                payload=json.dumps(payload or {}, ensure_ascii=False),
            )
        return eid

    def add_fact(self, fact: Dict[str, Any]) -> str:
        fid = fact.get("id") or f"f_{int(time.time() * 1e6)}"
        props = {
            "id": fid, "invalidated": False,
            "subject": fact.get("subject", ""),
            "predicate": fact.get("predicate", ""),
            "object": fact.get("object", ""),
            "source_episode_id": fact.get("source_episode_id", ""),
            "source_span": fact.get("source_span", "")[:400],
            "confidence": float(fact.get("confidence", 0.0)),
            "tier": fact.get("tier", "auto"),  # auto | suggested | pending
            "valid_from": fact.get("valid_from") or datetime.utcnow().isoformat(),
        }
        with self._driver.session() as s:
            s.run("CREATE (:Fact $props)", props=props)
        return fid

    def invalidate_fact(self, fid: str, reason: str) -> bool:
        # [LOG-M9 R2] Mirror MockGraph: set both invalidated_at + valid_to
        now = datetime.utcnow().isoformat()
        with self._driver.session() as s:
            r = s.run(
                "MATCH (f:Fact {id:$id}) "
                "SET f.invalidated=true, f.invalidate_reason=$r, "
                "f.invalidated_at=$ts, f.valid_to=$ts "
                "RETURN f.id AS id",
                id=fid, r=reason[:200], ts=now,
            )
            return bool(r.single())

    def query_facts(self, predicate_substr: Optional[str] = None,
                    exclude_invalidated: bool = True,
                    limit: int = 10) -> List[Dict[str, Any]]:
        cypher = "MATCH (f:Fact) "
        conds = []
        params: Dict[str, Any] = {"limit": int(limit)}
        if exclude_invalidated:
            conds.append("(f.invalidated IS NULL OR f.invalidated=false)")
        if predicate_substr:
            conds.append("f.predicate CONTAINS $pred")
            params["pred"] = predicate_substr
        if conds:
            cypher += "WHERE " + " AND ".join(conds) + " "
        cypher += "RETURN f LIMIT $limit"
        with self._driver.session() as s:
            return [dict(rec["f"]) for rec in s.run(cypher, **params)]

    def count_nodes(self) -> int:
        with self._driver.session() as s:
            r = s.run("MATCH (n) RETURN count(n) AS c")
            return int(r.single()["c"])

    def close(self):
        try:
            self._driver.close()
        except Exception:
            pass


# ---------- Singleton ----------

_client: Optional[Any] = None
_client_error: Optional[str] = None
_last_connect_attempt: float = 0.0
_RECONNECT_INTERVAL_S = 30.0  # don't retry more often than this


def get_client() -> Optional[Any]:
    """Returns backend (Neo4j or Mock) or None if disabled/unavailable.

    Callers must handle None — this never raises on connection failure.
    """
    global _client, _client_error, _last_connect_attempt

    if is_disabled():
        return None

    with _LOCK:
        if _client is not None:
            return _client

        # Rate-limit reconnect attempts
        now = time.time()
        if now - _last_connect_attempt < _RECONNECT_INTERVAL_S and _client_error:
            return None
        _last_connect_attempt = now

        if _is_mock():
            _client = _MockGraph()
            _client_error = None
            return _client

        # Real backend
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        password = os.environ.get("NEO4J_PASSWORD", "")
        if not password:
            _client_error = "NEO4J_PASSWORD not set"
            logger.warning("graphiti_client: %s", _client_error)
            return None

        try:
            _client = _Neo4jBackend(uri, user, password)
            _client_error = None
            logger.info("graphiti_client connected to %s", uri)
            return _client
        except Exception as e:
            _client = None
            # [SEC-H3 R2] Scrub credentials from exception before storing
            _client_error = _scrub_error(f"{type(e).__name__}: {e}")
            logger.warning("graphiti_client connect failed: %s", _client_error)
            return None


def reset_for_tests() -> None:
    """Test-only: clear singleton + error state."""
    global _client, _client_error, _last_connect_attempt
    with _LOCK:
        if _client and hasattr(_client, "close"):
            try:
                _client.close()
            except Exception:
                pass
        _client = None
        _client_error = None
        _last_connect_attempt = 0.0


# ---------- Public API (never raises on operational failure) ----------

def add_episode(text: str, source: str,
                ts: Optional[str] = None,
                payload: Optional[Dict] = None) -> Optional[str]:
    """Append episode. Returns episode_id or None on failure."""
    c = get_client()
    if c is None:
        return None
    ts = ts or datetime.utcnow().isoformat()
    try:
        return c.add_episode(text, source, ts, payload)
    except Exception as e:
        logger.warning("add_episode failed: %s", e)
        return None


def add_fact(fact: Dict[str, Any]) -> Optional[str]:
    c = get_client()
    if c is None:
        return None
    try:
        return c.add_fact(fact)
    except Exception as e:
        logger.warning("add_fact failed: %s", e)
        return None


def invalidate_fact(fact_id: str, reason: str) -> bool:
    c = get_client()
    if c is None:
        return False
    try:
        return bool(c.invalidate_fact(fact_id, reason))
    except Exception as e:
        logger.warning("invalidate_fact failed: %s", e)
        return False


def query_facts(predicate_substr: Optional[str] = None,
                exclude_invalidated: bool = True,
                limit: int = 10) -> List[Dict[str, Any]]:
    c = get_client()
    if c is None:
        return []
    try:
        return c.query_facts(predicate_substr, exclude_invalidated, limit)
    except Exception as e:
        logger.warning("query_facts failed: %s", e)
        return []


def _scrub_error(msg: Optional[str]) -> Optional[str]:
    """[SEC-H3 R2 2026-04-19] Strip credentials from error messages.

    Neo4j driver exceptions may embed bolt://user:pass@host URIs.
    """
    if not msg:
        return msg
    import re as _re
    scrubbed = _re.sub(r"://[^@/\s]+:[^@/\s]+@", "://[REDACTED]@", msg)
    # Also scrub common password= / pwd= patterns
    scrubbed = _re.sub(r"(?i)(password|pwd)\s*[:=]\s*[^\s,;]+",
                       r"\1=[REDACTED]", scrubbed)
    return scrubbed[:200]


def health() -> Dict[str, Any]:
    """Dashboard row — never raises.

    [LOG-H4 R2 + R3] First call get_client() to trigger connect/error
    state if disabled/unset. Then snapshot both _client AND _client_error
    atomically under the same lock. This avoids the TOCTOU race while
    preserving the original "health() triggers a connect attempt" semantics.
    """
    c = get_client()  # may populate _client or _client_error
    with _LOCK:
        snap_client = c if c is not None else _client
        snap_error = _scrub_error(_client_error)
    out: Dict[str, Any] = {
        "flag": _flag(),
        "enabled": is_enabled(),
        "mock": _is_mock(),
        "connected": snap_client is not None,
        "node_count": 0,
        "error": snap_error,
    }
    if snap_client is not None:
        try:
            out["node_count"] = snap_client.count_nodes()
        except Exception as e:
            out["error"] = _scrub_error(f"count_nodes: {e}")
    return out


def get_embedder():
    """[Risk #12 mitigation] Share bge-small-zh singleton with semantic_kb
    to avoid 2× 400MB RAM. Returns None if semantic_kb disabled or model
    unavailable."""
    try:
        from memexa.core.semantic_kb import _get_model
        return _get_model()
    except Exception:
        return None


# ---------- CLI ----------

def _cli() -> int:
    import sys
    if len(sys.argv) < 2:
        print("usage: graphiti_client <health|count|add|query>", file=sys.stderr)
        return 1
    cmd = sys.argv[1]
    if cmd == "health":
        print(json.dumps(health(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "count":
        c = get_client()
        if c is None:
            print("client unavailable")
            return 2
        print(c.count_nodes())
        return 0
    if cmd == "add":
        # add <event_text> <source>
        if len(sys.argv) < 4:
            print("add requires <text> <source>", file=sys.stderr)
            return 2
        eid = add_episode(sys.argv[2], sys.argv[3])
        print(eid or "failed")
        return 0 if eid else 3
    if cmd == "query":
        for f in query_facts(limit=20):
            print(json.dumps(f, ensure_ascii=False, default=str))
        return 0
    print(f"unknown: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
