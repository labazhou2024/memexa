"""TU-3 + TU-4 of 2026 backfill plan_v2 §3 — graph_memory_v2 lineage edge writer.

Per plan §TaskUnits TU-3 Action 4: enrich graph_memory_v2.write_fact with
lineage edges for L1-L4 layered lineage (TU-4 + TU-11 implement L1+L2 + L3+L4).

TU-4 additions (R-4 observation density invariant):
  - link_observations_temporal: ±60min window, same source, max 5 outgoing
  - link_observations_entity_cooccur: shared canonical_entity, ±24h, max 3 outgoing
  - _observation_temporal_window_neighbors: helper returning sorted neighbors

Strategy: side-channel lineage edge log in data/lineage_edges.jsonl, parallel
to the canonical fact write. This avoids modifying graph_memory_v2 internals
which are protected by parallel autopilot sessions.

Edge schema:
  {
    edge_id: str (sha256[:32] of (src, dst, edge_kind)),
    src_fact_id: str,
    dst_fact_id: str,
    edge_kind: str (∈ EDGE_KIND_ENUM),
    weight: float (0.0-1.0),
    metadata: dict (e.g. cosine_score for L2; commit_sha for L4),
    created_at: float,
  }

EDGE_KIND_ENUM:
  - L1: temporal_next, temporal_prev (event chain via chat_room/person+time)
  - L2: topic_thread (bge-m3 cosine ≥ τ=0.7)
  - L3: causal_link (LLM-derived; TU-11)
  - L4: decision_lineage (anchored on git commit + plan_retro_gate RPs; TU-11)
  - observation_temporal_proximity (TU-4): same-source ±60min observation link
  - observation_entity_cooccur (TU-4): shared canonical entity ±24h observation link

axis_anchor: [C:cli:graph_v2_lineage]
trace event: lineage_edge_written / observation_link_created_temporal /
             observation_link_created_entity
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from src.core.fact_schema import HASH_LEN


DEFAULT_LINEAGE_PATH = Path("data/lineage_edges.jsonl")

# TU-4 observation link tuning constants
OBS_TEMPORAL_WINDOW_MINUTES: int = 60   # ±60 min same-source window
OBS_TEMPORAL_MAX_OUTGOING: int = 5      # max outgoing temporal links per obs
OBS_ENTITY_WINDOW_HOURS: int = 24       # ±24 h entity cooccur window
OBS_ENTITY_MAX_OUTGOING: int = 3        # max outgoing entity links per obs


EDGE_KIND_ENUM: FrozenSet[str] = frozenset({
    # L1 event chain
    "temporal_next",
    "temporal_prev",
    # L2 topic thread
    "topic_thread",
    # L3 causal (TU-11)
    "causal_link",
    # L4 decision (TU-11)
    "decision_lineage",
    # TU-4 observation density
    "observation_temporal_proximity",
    "observation_entity_cooccur",
})


def edge_id(src: str, dst: str, edge_kind: str) -> str:
    """Deterministic edge_id from (src, dst, edge_kind)."""
    raw = f"{src}|{dst}|{edge_kind}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:HASH_LEN]


def validate_edge(edge: Dict[str, Any]) -> tuple[bool, str]:
    """Schema check on lineage edge dict."""
    if not isinstance(edge, dict):
        return False, "edge_not_dict"

    required = {"src_fact_id", "dst_fact_id", "edge_kind", "weight"}
    actual = set(edge.keys())
    missing = required - actual
    if missing:
        return False, f"edge_missing_fields:{','.join(sorted(missing))}"

    if edge["edge_kind"] not in EDGE_KIND_ENUM:
        return False, f"edge_kind_unknown:{edge['edge_kind']!r}"

    if not isinstance(edge["src_fact_id"], str) or not edge["src_fact_id"]:
        return False, "edge_src_fact_id_empty"

    if not isinstance(edge["dst_fact_id"], str) or not edge["dst_fact_id"]:
        return False, "edge_dst_fact_id_empty"

    w = edge["weight"]
    if not isinstance(w, (int, float)) or not (0.0 <= float(w) <= 1.0):
        return False, f"edge_weight_out_of_range:{w}"

    return True, ""


def write_edge(
    src_fact_id: str,
    dst_fact_id: str,
    edge_kind: str,
    weight: float = 1.0,
    metadata: Optional[Dict[str, Any]] = None,
    lineage_path: Optional[Path] = None,
) -> tuple[bool, str]:
    """Append a lineage edge to data/lineage_edges.jsonl.

    Returns (ok, reason_or_edge_id).
    """
    edge = {
        "src_fact_id": src_fact_id,
        "dst_fact_id": dst_fact_id,
        "edge_kind": edge_kind,
        "weight": float(weight),
    }
    ok, reason = validate_edge(edge)
    if not ok:
        return False, reason

    eid = edge_id(src_fact_id, dst_fact_id, edge_kind)
    entry = {
        "edge_id": eid,
        **edge,
        "metadata": metadata or {},
        "created_at": time.time(),
    }

    path = lineage_path or DEFAULT_LINEAGE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        return False, f"write_failed:{e}"

    return True, eid


def read_edges(
    lineage_path: Optional[Path] = None,
    edge_kind: Optional[str] = None,
    src: Optional[str] = None,
    dst: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Filtered read of lineage edges.

    Returns list of edge dicts. Filters: edge_kind, src, dst (combinable).
    """
    path = lineage_path or DEFAULT_LINEAGE_PATH
    if not path.exists():
        return []

    edges: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if edge_kind and entry.get("edge_kind") != edge_kind:
                continue
            if src and entry.get("src_fact_id") != src:
                continue
            if dst and entry.get("dst_fact_id") != dst:
                continue
            edges.append(entry)
    return edges


# ---------------------------------------------------------------------------
# TU-4: Observation link helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_val: Any) -> Optional[float]:
    """Parse a timestamp value to float (Unix epoch seconds).

    Accepts:
      - float / int  → direct epoch
      - ISO-8601 str → parse via datetime
    Returns None on failure.
    """
    if ts_val is None:
        return None
    if isinstance(ts_val, (int, float)):
        return float(ts_val)
    if isinstance(ts_val, str):
        ts_val = ts_val.strip()
        if not ts_val:
            return None
        # Try ISO-8601 (with or without Z/+offset)
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(ts_val, fmt)
                # Make timezone-aware if naive
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
        # Try plain float string
        try:
            return float(ts_val)
        except (ValueError, OverflowError):
            return None
    return None


def _observation_temporal_window_neighbors(
    obs: Dict[str, Any],
    all_obs: List[Dict[str, Any]],
    minutes: int = OBS_TEMPORAL_WINDOW_MINUTES,
) -> List[Dict[str, Any]]:
    """Return neighbors within ±`minutes` of obs['ingested_at'], same source.

    Results are sorted by absolute time-delta (closest first) and exclude
    the observation itself.

    Args:
        obs:      The target observation row (must have 'id', 'ingested_at',
                  and optionally 'source_kind').
        all_obs:  Full pool of observation rows to search.
        minutes:  Half-width of the symmetric time window (default 60).

    Returns:
        List of neighbor rows sorted by |Δt| ascending.
    """
    src_ts = _parse_ts(obs.get("ingested_at"))
    if src_ts is None:
        return []

    src_id = obs.get("id")
    src_source = obs.get("source_kind", "")

    window_sec = minutes * 60.0
    neighbors: List[Tuple[float, Dict[str, Any]]] = []

    for candidate in all_obs:
        if candidate.get("id") == src_id:
            continue  # skip self
        # Same source required for temporal linking
        if candidate.get("source_kind", "") != src_source:
            continue
        cand_ts = _parse_ts(candidate.get("ingested_at"))
        if cand_ts is None:
            continue
        delta = abs(cand_ts - src_ts)
        if delta <= window_sec:
            neighbors.append((delta, candidate))

    # Sort by absolute delta ascending (closest first)
    neighbors.sort(key=lambda t: t[0])
    return [row for _, row in neighbors]


def link_observations_temporal(
    obs_row: Dict[str, Any],
    neighbor_rows: List[Dict[str, Any]],
    lineage_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Create ≤OBS_TEMPORAL_MAX_OUTGOING temporal proximity links for obs_row.

    Links observations within ±OBS_TEMPORAL_WINDOW_MINUTES from the same
    source.  Applies nearest-first ordering and caps at max 5 outgoing.

    Args:
        obs_row:       Source observation (must have 'id', 'ingested_at',
                       'source_kind').
        neighbor_rows: Pre-filtered pool (from
                       _observation_temporal_window_neighbors or caller-
                       supplied subset).  Already assumed to be same-source
                       and within the time window; this function re-validates
                       the window as defense-in-depth.
        lineage_path:  Override for lineage JSONL path (default
                       DEFAULT_LINEAGE_PATH).

    Returns:
        List of written edge dicts (may be empty if no valid neighbors or if
        obs_row lacks required fields).

    Trace event: observation_link_created_temporal (emitted per link written)
    """
    src_id = obs_row.get("id")
    src_ts = _parse_ts(obs_row.get("ingested_at"))
    if not src_id or src_ts is None:
        return []

    window_sec = OBS_TEMPORAL_WINDOW_MINUTES * 60.0
    written: List[Dict[str, Any]] = []
    count = 0

    for neighbor in neighbor_rows:
        if count >= OBS_TEMPORAL_MAX_OUTGOING:
            break
        dst_id = neighbor.get("id")
        if not dst_id or dst_id == src_id:
            continue
        # Defense-in-depth: re-validate window
        cand_ts = _parse_ts(neighbor.get("ingested_at"))
        if cand_ts is None or abs(cand_ts - src_ts) > window_sec:
            continue

        ok, result = write_edge(
            src_fact_id=str(src_id),
            dst_fact_id=str(dst_id),
            edge_kind="observation_temporal_proximity",
            weight=1.0,
            metadata={
                "delta_seconds": round(abs(cand_ts - src_ts), 3),
                "source_kind": obs_row.get("source_kind", ""),
                "trace_event": "observation_link_created_temporal",
            },
            lineage_path=lineage_path,
        )
        if ok:
            written.append({
                "edge_id": result,
                "src": str(src_id),
                "dst": str(dst_id),
                "edge_kind": "observation_temporal_proximity",
            })
            count += 1

    return written


def link_observations_entity_cooccur(
    obs_row: Dict[str, Any],
    neighbor_rows: List[Dict[str, Any]],
    lineage_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Create ≤OBS_ENTITY_MAX_OUTGOING entity co-occurrence links for obs_row.

    Links observations that share at least one canonical entity and were
    ingested within ±OBS_ENTITY_WINDOW_HOURS.

    Args:
        obs_row:       Source observation (must have 'id', 'ingested_at',
                       'canonical_entities': list[str]).
        neighbor_rows: Pool of candidate observations.  Each must have 'id',
                       'ingested_at', 'canonical_entities'.  entity_resolver
                       canonicalization is assumed to have been applied before
                       calling this function.
        lineage_path:  Override for lineage JSONL path.

    Returns:
        List of written edge dicts.

    Trace event: observation_link_created_entity (emitted per link written)
    """
    src_id = obs_row.get("id")
    src_ts = _parse_ts(obs_row.get("ingested_at"))
    if not src_id or src_ts is None:
        return []

    # Canonical entities for source obs (may be a list, set, or None)
    src_entities_raw = obs_row.get("canonical_entities") or []
    src_entities: frozenset = frozenset(
        str(e).strip().lower() for e in src_entities_raw if e
    )
    if not src_entities:
        # No canonical entities → no entity co-occur links possible
        return []

    window_sec = OBS_ENTITY_WINDOW_HOURS * 3600.0
    written: List[Dict[str, Any]] = []
    count = 0

    for neighbor in neighbor_rows:
        if count >= OBS_ENTITY_MAX_OUTGOING:
            break
        dst_id = neighbor.get("id")
        if not dst_id or dst_id == src_id:
            continue
        cand_ts = _parse_ts(neighbor.get("ingested_at"))
        if cand_ts is None or abs(cand_ts - src_ts) > window_sec:
            continue

        # Shared entity check
        cand_entities_raw = neighbor.get("canonical_entities") or []
        cand_entities: frozenset = frozenset(
            str(e).strip().lower() for e in cand_entities_raw if e
        )
        shared = src_entities & cand_entities
        if not shared:
            continue

        ok, result = write_edge(
            src_fact_id=str(src_id),
            dst_fact_id=str(dst_id),
            edge_kind="observation_entity_cooccur",
            weight=min(1.0, len(shared) / max(1, len(src_entities))),
            metadata={
                "shared_entities": sorted(shared)[:10],  # cap for storage
                "delta_seconds": round(abs(cand_ts - src_ts), 3),
                "trace_event": "observation_link_created_entity",
            },
            lineage_path=lineage_path,
        )
        if ok:
            written.append({
                "edge_id": result,
                "src": str(src_id),
                "dst": str(dst_id),
                "edge_kind": "observation_entity_cooccur",
                "shared_entities": sorted(shared)[:10],
            })
            count += 1

    return written


__all__ = [
    "DEFAULT_LINEAGE_PATH",
    "EDGE_KIND_ENUM",
    "OBS_TEMPORAL_WINDOW_MINUTES",
    "OBS_TEMPORAL_MAX_OUTGOING",
    "OBS_ENTITY_WINDOW_HOURS",
    "OBS_ENTITY_MAX_OUTGOING",
    "edge_id",
    "validate_edge",
    "write_edge",
    "read_edges",
    "_parse_ts",
    "_observation_temporal_window_neighbors",
    "link_observations_temporal",
    "link_observations_entity_cooccur",
]
