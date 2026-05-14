"""TU-4 of 2026 backfill plan_v2 §3 — fact_lineage L1+L2 (rule-based + bge-m3).

Per plan §TaskUnits TU-4: 4-dim lineage framework.
  L1 event_chain: rule-based, 0 LLM. Groupby (chat_room OR person_uuid) +
                  time sort → temporal_next/temporal_prev edges.
  L2 topic_thread: bge-m3 cosine ≥ τ=0.7 → connected components, min_size=5.
  L3 causal_link: STUB; TU-11 implements via DeepSeek-reasoner top-1k.
  L4 decision_lineage: STUB; TU-11 implements via git+plan_retro_gate anchors.

Per plan §Architecture invariant 5: L1+L2 全建; L3+L4 仅 top-1k batch precompute
(TU-11). Module constants τ=TAU_DEFAULT=0.7, MIN_SIZE_DEFAULT=5 are PINNED.

axis_anchor: [C:cli:fact_lineage_l1_l2]
trace events: lineage_l1_built / lineage_l2_clustered / lineage_l3_stub_called /
              lineage_l4_stub_called

Per plan §TaskUnits TU-4 Pass criteria (AC-12 L1+L2 portion):
  - L1: 100-msg synthetic chat → 99 edges (n-1 chain length)
  - L2 cluster correctness on 50-fact corpus (5 threads × 10 facts ground truth)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from memexa.core.fact_schema import HASH_LEN
from memexa.core.graph_v2_lineage import write_edge, edge_id


# ---- Pinned module constants (per plan TU-4 + commit memory note) ---------

TAU_DEFAULT: float = 0.7      # cosine threshold for L2 topic clustering
MIN_SIZE_DEFAULT: int = 5     # minimum thread size (smaller = noise)


# ---- Edge dataclass --------------------------------------------------------

@dataclass(frozen=True)
class LineageEdge:
    """Lineage edge between two facts."""
    src_fact_id: str
    dst_fact_id: str
    edge_kind: str  # ∈ EDGE_KIND_ENUM (graph_v2_lineage)
    weight: float = 1.0
    metadata: Tuple[Tuple[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "src_fact_id": self.src_fact_id,
            "dst_fact_id": self.dst_fact_id,
            "edge_kind": self.edge_kind,
            "weight": self.weight,
            "metadata": dict(self.metadata),
        }


# ---- L1: rule-based event chain (0 LLM) -----------------------------------

def _chain_key(fact: Dict[str, Any]) -> Optional[str]:
    """Partition key for L1 chain.

    Prefers `chat_room_hash` (for chat-realtime / backfill-wechat / qq facts)
    over `canonical_subject` (for narrative / git / structured facts).

    Returns None if neither key is usable (skip from chain).
    """
    # Chat-realtime/backfill-chat may have chat_room_hash via metadata_builder
    room = fact.get("chat_room_hash")
    if isinstance(room, str) and room:
        return f"chat:{room}"

    subj = fact.get("canonical_subject", "")
    if isinstance(subj, str) and subj.startswith("person_"):
        return f"person:{subj}"

    return None


def build_event_chain(
    facts: List[Dict[str, Any]],
    persist: bool = False,
    lineage_path: Optional[Any] = None,
    emit_trace: Optional[Callable] = None,
) -> List[LineageEdge]:
    """L1 event_chain: rule-based temporal_next edges.

    Algorithm (deterministic, 0 LLM):
      1. Filter facts with usable _chain_key (chat_room_hash OR person_)
      2. Group by chain_key
      3. Within each group, sort by valid_at ascending
      4. Connect adjacent pairs with temporal_next edge

    Per plan AC-12: 100-msg single chat → 99 edges (n-1).

    Args:
        facts: list of fact dicts (FactRow.to_dict() or chat metadata).
        persist: if True, write each edge via graph_v2_lineage.write_edge.
        lineage_path: optional override path for persistence.
        emit_trace: optional dependency injection.

    Returns: list of LineageEdge (in-memory; persisted only if persist=True).
    """
    # Group by chain_key
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for f in facts:
        key = _chain_key(f)
        if key is None:
            continue
        groups.setdefault(key, []).append(f)

    edges: List[LineageEdge] = []
    for key, group in groups.items():
        # Per logic-iter1-5 fix: parse to datetime to handle mixed timezones
        # (UTC vs +08:00); fall back to lex sort for unparseable strings.
        from datetime import datetime as _dt
        def _parse_or_lex(x):
            v = str(x.get("valid_at", ""))
            try:
                return (0, _dt.fromisoformat(v.replace("Z", "+00:00")))
            except (ValueError, TypeError):
                return (1, v)  # tier-2: lexicographic for unparseable
        group_sorted = sorted(group, key=_parse_or_lex)
        for i in range(len(group_sorted) - 1):
            src = group_sorted[i].get("id", "")
            dst = group_sorted[i + 1].get("id", "")
            if not src or not dst:
                continue
            edge = LineageEdge(
                src_fact_id=src,
                dst_fact_id=dst,
                edge_kind="temporal_next",
                weight=1.0,
                metadata=(("chain_key", key),),
            )
            edges.append(edge)
            if persist:
                write_edge(src, dst, "temporal_next", weight=1.0,
                           metadata={"chain_key": key},
                           lineage_path=lineage_path)

    if emit_trace is not None:
        try:
            emit_trace("lineage_l1_built", {
                "groups": len(groups),
                "edges": len(edges),
                "input_facts": len(facts),
            })
        except Exception:
            pass

    return edges


# ---- L2: bge-m3 cosine clustering -----------------------------------------

def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length float vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _connected_components_above_threshold(
    n: int,
    similarity_pairs: List[Tuple[int, int, float]],
    threshold: float,
) -> List[List[int]]:
    """Union-Find on pairs (i, j, sim) where sim >= threshold.

    Returns list of components, each a list of indices.
    """
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i, j, sim in similarity_pairs:
        if sim >= threshold:
            union(i, j)

    components: Dict[int, List[int]] = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)
    return list(components.values())


def cluster_topic_threads(
    facts: List[Dict[str, Any]],
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    tau: float = TAU_DEFAULT,
    min_size: int = MIN_SIZE_DEFAULT,
    persist: bool = False,
    lineage_path: Optional[Any] = None,
    emit_trace: Optional[Callable] = None,
) -> Dict[str, List[str]]:
    """L2 topic_thread: bge-m3 cosine ≥ τ → connected components ≥ min_size.

    Per plan §TaskUnits TU-4 + AC-12:
      - τ=0.7 (PINNED in module constant TAU_DEFAULT)
      - min_size=5 (PINNED in MIN_SIZE_DEFAULT)
      - cluster ≥200 thread on full backfill (post-Phase 1+2)

    Args:
        facts: list of fact dicts; needs `id` and `object` fields.
        embed_fn: callable(text) → List[float]; if None, uses null embedder
                  (returns no clusters; for development without bge-m3 LIVE).
        tau: cosine threshold; default 0.7.
        min_size: minimum cluster size; default 5.
        persist: write topic_thread edges to lineage log.
        lineage_path: override path.
        emit_trace: optional trace emitter.

    Returns: dict mapping thread_id → list of fact_id.
    """
    if not facts:
        return {}

    # Defensive: filter facts with usable text (object field)
    indexed = [(i, f) for i, f in enumerate(facts)
               if isinstance(f.get("object"), str) and f["object"].strip()
               and isinstance(f.get("id"), str) and f["id"]]

    if not indexed:
        return {}

    # Compute embeddings
    if embed_fn is None:
        # No embedder available → return empty (production must inject Mac bge-m3)
        if emit_trace is not None:
            try:
                emit_trace("lineage_l2_clustered", {
                    "clusters": 0, "input_facts": len(facts),
                    "skipped_reason": "no_embed_fn",
                })
            except Exception:
                pass
        return {}

    embeddings: List[List[float]] = []
    for _, f in indexed:
        try:
            vec = embed_fn(f["object"])
            if not isinstance(vec, list) or not all(isinstance(x, (int, float)) for x in vec):
                vec = []
        except Exception:
            vec = []
        embeddings.append(list(vec))

    # All-pairs cosine (O(n^2); acceptable for n ≤ 5000 batches)
    n = len(indexed)
    pairs: List[Tuple[int, int, float]] = []
    for i in range(n):
        if not embeddings[i]:
            continue
        for j in range(i + 1, n):
            if not embeddings[j]:
                continue
            sim = _cosine_similarity(embeddings[i], embeddings[j])
            if sim >= tau:
                pairs.append((i, j, sim))

    components = _connected_components_above_threshold(n, pairs, tau)
    components_by_size = [c for c in components if len(c) >= min_size]

    threads: Dict[str, List[str]] = {}
    for thread_idx, comp in enumerate(components_by_size):
        thread_id = f"thread_{thread_idx:04d}"
        fact_ids = [indexed[i][1]["id"] for i in comp]
        threads[thread_id] = fact_ids
        # Persist as topic_thread edges (star: first fact in cluster → others)
        if persist and fact_ids:
            anchor = fact_ids[0]
            for other in fact_ids[1:]:
                write_edge(anchor, other, "topic_thread", weight=tau,
                           metadata={"thread_id": thread_id, "tau": tau},
                           lineage_path=lineage_path)

    if emit_trace is not None:
        try:
            emit_trace("lineage_l2_clustered", {
                "clusters": len(threads),
                "input_facts": len(facts),
                "tau": tau,
                "min_size": min_size,
            })
        except Exception:
            pass

    return threads


# ---- L3+L4 stubs (TU-11 implements) ---------------------------------------

def tag_causal(facts: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """L3 stub: causal_link tagging via DeepSeek-reasoner on top-1k out_degree.

    Per plan §TaskUnits TU-4 + TU-11 (deferred): full implementation lives in
    tools/backfill_lineage_l3_l4.py (TU-11).
    """
    raise NotImplementedError("L3 causal tagging is implemented in TU-11; this module defines L1+L2 only")


def derive_decision_lineage(facts: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """L4 stub: decision_lineage anchored on git commits + plan_retro_gate RPs.

    Per plan §TaskUnits TU-4 + TU-11 (deferred): full implementation in TU-11.
    """
    raise NotImplementedError("L4 decision lineage is implemented in TU-11; this module defines L1+L2 only")


__all__ = [
    "TAU_DEFAULT",
    "MIN_SIZE_DEFAULT",
    "LineageEdge",
    "build_event_chain",
    "cluster_topic_threads",
    "tag_causal",
    "derive_decision_lineage",
]
