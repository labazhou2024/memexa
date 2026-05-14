"""TU-U5-1 (2026-04-26): tombstone-by-tag invalidation wrapper for Hindsight.

Hindsight 0.5.4 has no PATCH endpoint and `delete_memory` is destructive.
We use the tombstone-by-tag pattern (Mem0-style soft-delete):

  invalidate.write_tombstone(chunk_id) calls client.retain with:
    content = "[INVALIDATED]"
    tags    = [f"invalidated:{chunk_id}"]
    metadata = {"invalidated_chunk_id": chunk_id, "reason": ..., "tombstone_at": iso}

  invalidate.list_tombstoned() recalls all "invalidated:*" tagged memories
  and returns the set of chunk_ids that have a tombstone marker.
  5-minute cache (5min cap chosen empirically; CEO can clear via _CACHE.clear()).

  graph_memory_v2._apply_anti_halluc_wrappers consumes list_tombstoned() to
  drop tombstoned facts from recall results.

Per autopilot v2.0 plan_v1 + logic-iter1-4 (LIVE probe AC-11 verifies tag
filter behavior in Hindsight before this is wired into the production
recall path).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from typing import Any, Optional

from src.core.hindsight_client import HindsightHttpClient, get_client

# Cache: maps "tombstoned_set" -> (set, monotonic_timestamp)
_CACHE: dict[str, tuple[set[str], float]] = {}
_CACHE_TTL_S = 300  # 5 minutes

_TRACE_LOG_PATH = os.environ.get("MEMEXA_INVALIDATE_TRACE_LOG", "")


def _emit_trace(event: str, payload: dict[str, Any]) -> None:
    if not _TRACE_LOG_PATH:
        return
    try:
        rec = {"event": event, "ts": time.time(), **payload}
        with open(_TRACE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _iso_now() -> str:
    return _dt.datetime.utcnow().isoformat() + "Z"


def write_tombstone(
    chunk_id: str,
    reason: str = "disk_missing",
    client: Optional[HindsightHttpClient] = None,
) -> dict[str, Any]:
    """Mark a Hindsight chunk_id as invalidated via tombstone-by-tag.

    Idempotent: if chunk_id is already tombstoned (via list_tombstoned cache
    or a fresh recall), returns {"skipped": True, "reason": "idempotent",
    "chunk_id": ...} without writing.

    Returns the Hindsight retain operation dict on write, or the skip dict.
    """
    if not chunk_id:
        raise ValueError("chunk_id must be non-empty")
    if client is None:
        client = get_client()
    # Idempotency: skip if already tombstoned
    if is_tombstoned(chunk_id, client=client):
        _emit_trace("tombstone_skipped_idempotent",
                    {"chunk_id": chunk_id, "reason": reason})
        return {"skipped": True, "idempotent": True, "chunk_id": chunk_id}
    metadata = {
        "invalidated_chunk_id": chunk_id,
        "reason": reason,
        "tombstone_at": _iso_now(),
    }
    op = client.retain(
        content="[INVALIDATED]",
        tags=[f"invalidated:{chunk_id}"],
        metadata=metadata,
    )
    _emit_trace("tombstone_written",
                {"chunk_id": chunk_id, "reason": reason})
    # Invalidate cache so next list_tombstoned reflects the new tombstone
    _CACHE.pop("tombstoned_set", None)
    return op


def list_tombstoned(client: Optional[HindsightHttpClient] = None,
                    force_refresh: bool = False) -> set[str]:
    """Return set of chunk_ids that have a tombstone marker.

    Cached for _CACHE_TTL_S seconds; pass force_refresh=True to bypass cache.

    Implementation note (per logic-iter1-4 AC-11 LIVE probe): if Hindsight
    `recall(tags=...)` does not behave as exact tag filter, this function
    falls back to scanning all "[INVALIDATED]" memories and parsing
    metadata.invalidated_chunk_id.
    """
    cached = _CACHE.get("tombstoned_set")
    if cached and not force_refresh:
        s, ts = cached
        if time.monotonic() - ts < _CACHE_TTL_S:
            _emit_trace("tombstone_list_cache_hit", {"size": len(s)})
            return s
    if client is None:
        client = get_client()
    out: set[str] = set()
    success = False
    try:
        # Primary path: tag-filtered recall
        raw = client.recall(query="[INVALIDATED]", tags=["invalidated:*"], max_tokens=8192)
        results = raw.get("results", []) if isinstance(raw, dict) else []
        for r in results:
            md = r.get("metadata") or r.get("memory_metadata") or {}
            cid = md.get("invalidated_chunk_id")
            if cid:
                out.add(str(cid))
            # Fallback: parse from tags
            for t in (r.get("tags") or []):
                if isinstance(t, str) and t.startswith("invalidated:"):
                    out.add(t.split(":", 1)[1])
        success = True
    except Exception:
        # FIX logic-iter1-2: do NOT cache empty set on Hindsight outage
        # (otherwise stale-empty cache lets tombstoned facts leak through
        # for the full TTL window). Return empty (fail-open) WITHOUT caching.
        _emit_trace("tombstone_list_refresh_failed", {})
        return out
    # Only cache when query succeeded
    if success:
        _CACHE["tombstoned_set"] = (out, time.monotonic())
        _emit_trace("tombstone_list_refreshed", {"size": len(out)})
    return out


def is_tombstoned(chunk_id: str,
                  client: Optional[HindsightHttpClient] = None) -> bool:
    """Convenience: check if a single chunk_id has been tombstoned."""
    if not chunk_id:
        return False
    return chunk_id in list_tombstoned(client=client)


def clear_cache() -> None:
    """CEO escape hatch: clear cache (e.g. after manual tombstone via API)."""
    _CACHE.clear()
