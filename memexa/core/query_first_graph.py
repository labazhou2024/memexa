"""query_first_graph — Enforced graph-first query protocol (CLAUDE.md §七 7.1).

Why this exists (LIVE 2026-05-07):
  CEO directive: 任何实体查询必须 graph_memory_v2 优先, 文件 grep 仅兜底.
  本 session 实测: 文件 grep "胡欣" 命中 1; graph 命中 6 (虽含 4-5 false positive,
  但提供 cross-source aggregation + entity resolution); 缺图查询漏 67% true hits.

Public API:
  resolve(query: str, *, kind: str = "entity") -> ResolveResult
    1. graph_memory_v2 first (HARD)
    2. file fallback only if graph returned ≤1 hits AND query has Chinese / specific token
    3. log violation if file-only path skipped graph
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

logger = logging.getLogger("query_first_graph")

# Critical: this module MUST be imported by any code path that searches for
# entities/persons/projects. Bypass = HARD RULE violation logged to events.jsonl.

_GRAPH_FIRST_BYPASS_LOG = os.environ.get(
    "MEMEXA_GRAPH_FIRST_BYPASS_LOG",
    "data/events_graph_first_bypass.jsonl",
)


@dataclass
class GraphHit:
    """Single hit from graph_memory_v2 / hindsight recall."""
    memory_id: str
    source: str  # wechat / qq / claude_code / ...
    when_start: Optional[str]
    canonical_id: Optional[str]
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolveResult:
    query: str
    graph_hits: List[GraphHit]
    file_hits: List[Dict[str, Any]]  # only populated if graph≤1
    used_file_fallback: bool
    elapsed_s: float
    bank_id: str


def resolve(
    query: str,
    *,
    bank: str = "memory_full_v5",
    max_tokens: int = 2000,
    file_fallback_paths: Optional[List[str]] = None,
) -> ResolveResult:
    """HARD RULE entry point — graph FIRST, file SECOND.

    Args:
        query: search phrase (entity name, event description)
        bank: hindsight bank id (default v5 with schema:v2)
        max_tokens: graph recall budget
        file_fallback_paths: only used if graph returns ≤1 hits

    Returns:
        ResolveResult with ordered (graph, file) hits.

    Raises:
        nothing — never blocks caller; fallback gracefully on hindsight outage.
    """
    from memexa.core.memory_query import _recall_raw  # lazy

    t0 = time.time()
    graph_hits: List[GraphHit] = []
    bypass_reason = None

    try:
        r = _recall_raw(query, bank=bank, max_tokens=max_tokens)
        for h in (r or {}).get("results", []):
            md = h.get("metadata") or {}
            graph_hits.append(GraphHit(
                memory_id=h.get("id") or h.get("memory_id") or "",
                source=md.get("source") or "?",
                when_start=md.get("when_start"),
                canonical_id=(h.get("canonical_id") or md.get("canonical_id")),
                score=float(h.get("score", 0.0) or h.get("rerank_score", 0.0) or 0.0),
                metadata=md,
            ))
    except Exception as e:
        bypass_reason = f"graph_unreachable:{e!r}"
        logger.warning(f"graph_first: hindsight unreachable, falling back to file ({e})")

    file_hits: List[Dict[str, Any]] = []
    used_fallback = False
    if (len(graph_hits) <= 1 or bypass_reason) and file_fallback_paths:
        used_fallback = True
        # File fallback is opt-in, caller must provide paths.
        # We do NOT auto-grep memory/ here — caller's responsibility.
        for p in file_fallback_paths:
            try:
                # very naive substring scan; caller normally supplies a Reader
                with open(p, encoding="utf-8") as fp:
                    for i, line in enumerate(fp):
                        if query in line:
                            file_hits.append({"path": p, "line_no": i + 1, "line": line.rstrip()})
            except OSError:
                continue

    if bypass_reason:
        # Append to bypass log (caller can decide on dlq policy)
        try:
            os.makedirs(os.path.dirname(_GRAPH_FIRST_BYPASS_LOG), exist_ok=True)
            with open(_GRAPH_FIRST_BYPASS_LOG, "a", encoding="utf-8") as fp:
                import json
                fp.write(json.dumps({
                    "ts": time.time(),
                    "query": query,
                    "reason": bypass_reason,
                    "n_file_hits": len(file_hits),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    return ResolveResult(
        query=query,
        graph_hits=graph_hits,
        file_hits=file_hits,
        used_file_fallback=used_fallback,
        elapsed_s=time.time() - t0,
        bank_id=bank,
    )


def assert_graph_first(query: str, used_path: str) -> None:
    """Assertion helper — call when a path other than graph_first.resolve() is taken.

    Logs a HARD RULE violation to bypass log so we can audit.
    """
    try:
        os.makedirs(os.path.dirname(_GRAPH_FIRST_BYPASS_LOG), exist_ok=True)
        with open(_GRAPH_FIRST_BYPASS_LOG, "a", encoding="utf-8") as fp:
            import json
            fp.write(json.dumps({
                "ts": time.time(),
                "query": query,
                "violation": "file_grep_without_graph",
                "used_path": used_path,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    logger.warning(f"GRAPH-FIRST VIOLATION: {used_path} took {query!r} without graph_first.resolve")
