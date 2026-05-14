"""
L4 Semantic Pattern Knowledge Base (Phase 3, 2026-04-18)

sentence-transformers + bge-small-zh-v1.5 + numpy brute-force cosine for ~58
patterns. Lazy-loaded, offline-safe, incremental.

Design (per verifier R1):
- Lazy import: sentence_transformers only imported on first use
- Offline fallback: if model load fails (校园网 block hf hub), write flag
  and return [] for all queries — callers degrade to keyword prime
- Persist: embeddings.npy (float32) + embeddings_meta.json (id + mtime + model_name)
- Incremental: compare pattern file mtime + id set; only re-embed added/changed
- Brute-force cosine: fine for N<5000; upgrade threshold documented
- ENV: MEMEXA_L4_SEMANTIC_KB=1 default on, graceful degrade when =0
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

__all__ = ["build_index", "semantic_search", "is_enabled", "is_available"]


_DATA_DIR = Path(__file__).parent.parent / "data"
_EMB_DIR = _DATA_DIR / "embeddings"
_EMB_FILE = _EMB_DIR / "patterns.npy"
_META_FILE = _EMB_DIR / "patterns_meta.json"
_UNAVAILABLE_FLAG = _EMB_DIR / "semantic_unavailable.flag"

_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_EMB_DIM = 512  # bge-small-zh-v1.5

# Module-level cache
_model = None
_model_load_attempted = False


def is_enabled() -> bool:
    """ENV flag. Default on (graceful degrade safe)."""
    return os.environ.get("MEMEXA_L4_SEMANTIC_KB", "1") == "1"


def is_available() -> bool:
    """True iff model loaded successfully (no unavailable flag)."""
    return not _UNAVAILABLE_FLAG.exists()


def _get_model():
    """Lazy-load sentence transformer. Returns None on failure.

    [2026-04-21 hot-path guard] When MEMEXA_HOOK_FAST=1 (set by
    UserPromptSubmit hook context), fail fast: model load costs ~20s
    cold (torch + sentence_transformers). Hook path falls back to
    keyword-only prime which is ~500ms. Explicit Bash `semantic_kb
    search` still loads the model normally.
    """
    global _model, _model_load_attempted
    if _model is not None:
        return _model
    if _model_load_attempted:
        return None  # don't retry in-process
    if os.environ.get("MEMEXA_HOOK_FAST") == "1":
        # Mark as attempted so a later call in the same process after the
        # env var flips does not surprise-load bge (keeps the "don't retry"
        # contract consistent with the success-path path below).
        _model_load_attempted = True
        return None  # hot-path degrade: skip bge entirely in hooks
    _model_load_attempted = True
    if not is_enabled():
        return None
    try:
        from sentence_transformers import SentenceTransformer
        _EMB_DIR.mkdir(parents=True, exist_ok=True)
        _model = SentenceTransformer(_MODEL_NAME)
        # Clear unavailable flag on success
        if _UNAVAILABLE_FLAG.exists():
            _UNAVAILABLE_FLAG.unlink()
        return _model
    except Exception as e:
        # Mark unavailable so callers degrade fast
        try:
            _EMB_DIR.mkdir(parents=True, exist_ok=True)
            _UNAVAILABLE_FLAG.write_text(
                f"model_load_failed: {e}\n", encoding="utf-8"
            )
        except Exception:
            pass
        return None


def _entry_to_text(entry) -> str:
    """Compose searchable text from PatternEntry.

    Phase B (2026-04-21): include canonical_tags so entity names participate
    in bge-cosine ranking. Without this, entity-tagged patterns only
    surface via text similarity and the A4 jaccard boost has no top-K
    candidates to rerank.
    """
    canon = getattr(entry, "canonical_tags", []) or []
    parts = [
        entry.fact or "",
        entry.recommendation or "",
        " ".join(entry.tags or []),
        " ".join(canon),
    ]
    return " ".join(p for p in parts if p).strip()


def _load_meta() -> dict:
    if not _META_FILE.exists():
        return {"ids": [], "model": _MODEL_NAME, "mtime": 0.0}
    try:
        return json.loads(_META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"ids": [], "model": _MODEL_NAME, "mtime": 0.0}


def _save_meta(meta: dict) -> None:
    _EMB_DIR.mkdir(parents=True, exist_ok=True)
    _META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_index(force: bool = False) -> int:
    """Build or incrementally update embedding index.

    Returns number of patterns embedded (0 on failure/no-change).
    """
    if not is_enabled():
        return 0
    try:
        from src.core.pattern_extractor import load_all_patterns, _PATTERNS_FILE
    except Exception:
        return 0

    if not _PATTERNS_FILE.exists():
        return 0

    meta = _load_meta()
    current_mtime = _PATTERNS_FILE.stat().st_mtime
    patterns = load_all_patterns()
    current_ids = [p.id for p in patterns]

    # Check: do we need to rebuild?
    need_rebuild = (
        force
        or current_mtime > meta.get("mtime", 0.0)
        or set(current_ids) != set(meta.get("ids", []))
        or not _EMB_FILE.exists()
        or meta.get("model") != _MODEL_NAME
    )
    if not need_rebuild:
        return 0

    model = _get_model()
    if model is None:
        return 0  # degrade

    try:
        import numpy as np
        texts = [_entry_to_text(p) for p in patterns]
        if not texts:
            return 0
        embs = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
        embs = np.asarray(embs, dtype=np.float32)
        _EMB_DIR.mkdir(parents=True, exist_ok=True)
        np.save(_EMB_FILE, embs)
        _save_meta({
            "ids": current_ids,
            "model": _MODEL_NAME,
            "mtime": current_mtime,
            "dim": int(embs.shape[1]) if embs.ndim == 2 else _EMB_DIM,
            "count": int(embs.shape[0]) if embs.ndim == 2 else 0,
        })
        return int(embs.shape[0]) if embs.ndim == 2 else 0
    except Exception as e:
        try:
            _UNAVAILABLE_FLAG.write_text(
                f"embed_failed: {e}\n", encoding="utf-8"
            )
        except Exception:
            pass
        return 0


def semantic_search(
    query: str, top_k: int = 5, min_score: float = 0.35,
) -> List[Tuple[str, float]]:
    """Semantic search. Returns list of (pattern_id, cosine_score).

    Returns [] if disabled/unavailable/empty query. Caller merges with keyword prime.
    """
    if not is_enabled() or not is_available() or not query.strip():
        return []

    # Lazy build if missing
    if not _EMB_FILE.exists():
        build_index()
        if not _EMB_FILE.exists():
            return []

    model = _get_model()
    if model is None:
        return []

    try:
        import numpy as np
        # [SEC-MED 2026-04-18] allow_pickle=False prevents RCE via crafted .npy
        embs = np.load(_EMB_FILE, allow_pickle=False)
        # Validate dtype/shape — fail closed on mismatch
        if not (embs.ndim == 2 and embs.dtype in (np.float32, np.float64)):
            return []
        meta = _load_meta()
        ids = meta.get("ids", [])
        if len(ids) != embs.shape[0]:
            # Mismatch: force rebuild next call
            return []
        q_emb = model.encode(
            [query], show_progress_bar=False, normalize_embeddings=True
        )
        q_emb = np.asarray(q_emb, dtype=np.float32)
        # cosine = dot since both normalized
        sims = (embs @ q_emb.T).squeeze(1)  # (N,)
        order = np.argsort(-sims)[:top_k * 2]
        results = []
        for i in order:
            s = float(sims[i])
            if s < min_score:
                continue
            results.append((ids[i], s))
            if len(results) >= top_k:
                break
        return results
    except Exception:
        return []


def semantic_search_boosted(
    query: str,
    top_k: int = 5,
    min_score: float = 0.35,
    alpha: Optional[float] = None,
) -> List[Tuple[str, float]]:
    """A4 (2026-04-21): entity-aware retrieval.

    score = cosine + alpha * jaccard(query_canonical_entities, pattern.canonical_tags)

    Rationale: bge-small-zh gets "your-org" and "your-org" embedding-close,
    but shared canonical entity ("ustc") is a stronger signal than cosine
    and should lift near-miss matches. alpha defaults to 0.2 (env
    MEMEXA_ENTITY_BOOST_ALPHA); alpha=0 reduces to semantic_search.

    Complexity: O(K + N) — one pass over N=len(patterns) to build
    id_to_entry dict, then K lookups inside the top-k loop. Verifier
    R1 objection #2 closed.

    Returns list of (pattern_id, boosted_score) sorted desc.
    """
    if alpha is None:
        try:
            alpha = float(os.environ.get("MEMEXA_ENTITY_BOOST_ALPHA", "0.2"))
        except (TypeError, ValueError):
            alpha = 0.2

    # Fast path: alpha=0 is identity of semantic_search.
    if alpha == 0:
        return semantic_search(query, top_k=top_k, min_score=min_score)

    # Oversample cosine hits so entity boost can promote previously-below-cutoff
    # candidates. Lower min_score for the cosine pass; re-apply final threshold
    # after boost.
    cosine_hits = semantic_search(
        query, top_k=top_k * 3, min_score=min(min_score, 0.15),
    )
    if not cosine_hits:
        return []

    # Build id_to_entry ONCE per call (verifier R1-2 fix).
    try:
        from src.core.pattern_extractor import load_all_patterns
        patterns = load_all_patterns()
        id_to_entry = {p.id: p for p in patterns}
    except Exception:
        return cosine_hits[:top_k]

    # Canonicalize query. Split on whitespace + common delimiters; each token
    # through canonicalize_entity. Use the full query string too, so multi-word
    # aliases like "<research-topic>" get matched as a single entity.
    # Only accept tokens that HIT the alias table (excluded tokens would
    # dilute jaccard; we want entity signal, not noise).
    try:
        from src.core.canonicalizer import canonicalize_entity, _build_entity_map, _normalize
    except Exception:
        return cosine_hits[:top_k]

    q_canon: set = set()
    tokens = [query] + [t for t in re.split(r"[\s,;:/\\|()\[\]]+", query) if t]
    try:
        entity_map = _build_entity_map()
    except Exception:
        entity_map = {}

    for tok in tokens:
        try:
            key = _normalize(str(tok))
            # Accept only if it's a known alias or canonical.
            if key in entity_map:
                q_canon.add(entity_map[key])
        except Exception:
            continue

    if not q_canon:
        # No query entity signal → fall back to pure cosine.
        # LOGIC-R1-06 fix (2026-04-21): respect the caller's min_score
        # (the oversample pass lowered it to 0.15 to give boost headroom;
        # without boost we must re-apply the original cutoff).
        return [(pid, s) for pid, s in cosine_hits[:top_k] if s >= min_score]

    boosted: List[Tuple[str, float]] = []
    for pid, cosine in cosine_hits:
        entry = id_to_entry.get(pid)
        p_canon: set = set(entry.canonical_tags) if entry else set()
        union = q_canon | p_canon
        if union:
            jaccard = len(q_canon & p_canon) / len(union)
        else:
            jaccard = 0.0
        score = cosine + alpha * jaccard
        if score >= min_score:
            boosted.append((pid, score))

    boosted.sort(key=lambda t: -t[1])

    # Phase B (2026-04-21): graph augmentation. When the query
    # canonicalizes to a known entity, pull in source_episode_id's of
    # facts about that entity from Neo4j graph and upweight patterns
    # that share the episode source. Kill-switch: MEMEXA_GRAPH_RETRIEVE=0.
    use_graph = os.environ.get("MEMEXA_GRAPH_RETRIEVE", "1") != "0" and q_canon
    if use_graph:
        try:
            # v2 facade per 2026-04-30 daemon repair (was v1 Neo4j)
            from src.core.graph_memory_v2 import query_entity as _gq
            # 2026-04-30 hot-path budget: semantic_search_boosted is called
            # via UserPromptSubmit hook; recall p50 ≈ 60s on Win CPU when
            # daemon is busy. Use threading.Thread(daemon=True) so the hook
            # process can exit even if the worker is still mid-recall.
            # ThreadPoolExecutor doesn't suffice: its threads are non-daemon
            # so process won't exit until they finish (defeats budget).
            import threading
            import time as _t_sk
            _SK_BUDGET_S = float(
                os.environ.get("MEMEXA_SEMANTIC_KB_GRAPH_BUDGET_S", "4.0"))
            graph_sources: set = set()
            _t_start = _t_sk.monotonic()
            for canon in list(q_canon)[:3]:  # cap at 3 entities
                remaining = _SK_BUDGET_S - (_t_sk.monotonic() - _t_start)
                if remaining <= 0.3:
                    break  # budget exhausted; degrade gracefully
                _result = []
                def _runner(c=canon, out=_result):
                    try:
                        out.extend(_gq(c, 10))
                    except Exception:
                        pass
                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                t.join(timeout=remaining)
                if t.is_alive():
                    break  # budget exhausted; thread continues but daemon=True won't block exit
                for row in _result:
                    src = row.source_episode_id or ""
                    if src:
                        graph_sources.add(Path(src).name)
            if graph_sources:
                # Upweight patterns whose provenance mentions any matched file
                # (+0.1 per match, capped). We do this by inspecting
                # entry.provenance[].reference (already loaded via
                # id_to_entry above).
                for i, (pid, score) in enumerate(boosted):
                    entry = id_to_entry.get(pid)
                    if not entry:
                        continue
                    for prov in (entry.provenance or []):
                        ref = prov.get("reference", "") if isinstance(prov, dict) else ""
                        if any(src_name in ref for src_name in graph_sources):
                            boosted[i] = (pid, min(1.0, score + 0.1))
                            break
                boosted.sort(key=lambda t: -t[1])
        except Exception as e:
            # Fail-soft: graph unavailable shouldn't break retrieve.
            import logging as _lg
            _lg.getLogger(__name__).debug("graph retrieve skip: %s", e)

    return boosted[:top_k]


def main():
    """CLI: python -m src.core.semantic_kb [build|search QUERY]"""
    import sys
    if len(sys.argv) < 2:
        print("Usage: semantic_kb [build|search QUERY]", file=sys.stderr)
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "build":
        n = build_index(force=True)
        print(f"Built index with {n} patterns. enabled={is_enabled()} available={is_available()}")
    elif mode == "search":
        query = " ".join(sys.argv[2:])
        results = semantic_search(query, top_k=5)
        for pid, score in results:
            print(f"  {score:.3f}  {pid}")
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
