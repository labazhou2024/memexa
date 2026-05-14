"""WeChat batch dedup oracle — 2-layer (jsonl seen_set + graph episode_id).

Layer 1 (jsonl): in-memory frozenset of sha256(content)[:32] from
data/win_keystone_outbox/realtime__wechat.jsonl. ~30 days of msgs at typical
load (~100k entries) ≤ 8MB RAM at 32 hex chars.

Layer 2 (graph): HTTP query Hindsight by episode_id. Cached lru_cache(1024).

sec-1 fix: graph query payload is ONLY {episode_id: str}, no PII fields.
sec-5 fix: hash length = 128-bit (sha256[:32]), adversarial preimage 2^127.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Tuple

def emit(event: str, payload: dict) -> None:
    """Soft trace emit; ignores unknown events (best-effort observability)."""
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass

# Closure A plan_v3 RP-7 + RP-24 + RP-27: single-source HASH_LEN constant
from memexa.chat.metadata_builder import HASH_LEN as _HASH_LEN  # 32 hex (128-bit; sec-5)
_EPISODE_ID_RE = re.compile(rf"^[a-f0-9]{{{_HASH_LEN}}}$")
_GRAPH_QUERY_TIMEOUT_S = 3.0


def _msg_hash(msg: dict) -> str:
    """sha256(ts|sender|content[:500])[:32].

    sec-5: 128-bit reduces adversarial preimage to 2^127 ops — infeasible.
    """
    ts = msg.get("ts") or msg.get("timestamp") or ""
    sender = msg.get("sender") or msg.get("sender_uuid") or ""
    content = (msg.get("content") or "")[:500]
    raw = f"{ts}|{sender}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_HASH_LEN]


def _load_jsonl_hashes(jsonl_path: Path) -> frozenset[str]:
    """Scan jsonl once; build frozenset of msg hashes."""
    hashes: set[str] = set()
    if not jsonl_path.exists():
        return frozenset()
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # rec may be either a top-level msg dict or wrap a "message" key
                msg_dict = rec.get("message", rec) if isinstance(rec, dict) else None
                if isinstance(msg_dict, dict):
                    hashes.add(_msg_hash(msg_dict))
    except OSError:
        return frozenset()
    return frozenset(hashes)


class DedupOracle:
    """2-layer dedup: jsonl primary + graph secondary."""

    def __init__(self, jsonl_path: Path, hindsight_url: Optional[str] = None,
                 bank: str = "memory_full"):
        self.jsonl_path = Path(jsonl_path)
        self._jsonl_hashes = _load_jsonl_hashes(self.jsonl_path)
        self.hindsight_url = (
            hindsight_url
            or os.environ.get("MEMEXA_HINDSIGHT_URL")
            or os.environ.get("HINDSIGHT_API_URL")
            or "http://127.0.0.1:8888"
        ).rstrip("/")
        self.bank = bank

    def is_seen_jsonl(self, msg: dict) -> bool:
        """Layer 1 — fast O(1) hashset lookup."""
        return _msg_hash(msg) in self._jsonl_hashes

    def is_seen_graph(self, episode_id: str) -> bool:
        """Layer 2 — slow Hindsight HTTP query, cached.

        sec-1: payload is episode_id ONLY. No msg content/sender/ts crosses
        the network. Pre-validate format (32-char lowercase hex regex)
        defense-in-depth before HTTP send.
        """
        if not episode_id or not _EPISODE_ID_RE.match(episode_id):
            return False
        if len(episode_id) > 100:
            return False  # sec-1 defense-in-depth max length
        return _graph_episode_lookup_cached(self.hindsight_url, self.bank, episode_id)

    def is_already_ingested(self, msg: dict, episode_id: str = "") -> Tuple[bool, str]:
        """Combined check. Returns (seen, source) where source ∈
        {"jsonl","graph","both","new"}."""
        in_jsonl = self.is_seen_jsonl(msg)
        in_graph = self.is_seen_graph(episode_id) if episode_id else False
        try:
            emit("wechat_dedup_check", {
                "jsonl_hit": in_jsonl, "graph_hit": in_graph,
                "episode_id_len": len(episode_id) if episode_id else 0,
            })
        except Exception:
            pass
        if in_jsonl and in_graph:
            return True, "both"
        if in_jsonl:
            return True, "jsonl"
        if in_graph:
            return True, "graph"
        return False, "new"

    def refresh_jsonl(self) -> int:
        """Re-scan jsonl (after batch run that may have written new lines).
        Returns new hash count."""
        self._jsonl_hashes = _load_jsonl_hashes(self.jsonl_path)
        return len(self._jsonl_hashes)


@lru_cache(maxsize=1024)
def _graph_episode_lookup_cached(base_url: str, bank: str, episode_id: str) -> bool:
    """sec-1: pre-validated episode_id only; HTTP GET (no body).

    Uses Hindsight memories endpoint; returns True if any document with
    matching episode_id metadata exists.
    """
    try:
        url = f"{base_url}/v1/default/banks/{bank}/memories?episode_id={episode_id}&limit=1"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_GRAPH_QUERY_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
            body = resp.read()
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return False
            results = data.get("results") or data.get("memories") or []
            return len(results) > 0
    except Exception:
        # Network error / timeout: treat as not-seen so we don't false-positive
        # (allowing duplicate is recoverable; missing is not)
        return False
