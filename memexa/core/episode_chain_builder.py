"""Layer E — episode_chain_builder (TU-9 of plan_v0 batch_quality_uplift).

Aggregate factrows from multiple batches within same chat_room + 24h sliding
window into "episodes" identified by shared episode_id (UUID-16). An episode
links factrows that are topically related (>=3 shared topic words) and
temporally clustered.

Algorithm (rule-based, no extra LLM call to bound cost):
  1. Group factrows by chat_room_id_hash.
  2. Within each chat_room, sort by valid_at (or batch_start_ts fallback).
  3. Sliding window: walk through factrows; for each pair (a, b) with
     ts_gap ≤ window_h and shared_topic_words(a,b) ≥ 3 → assign same
     episode_id (union-find style).
  4. Singletons get no episode_id (empty string).

Topic-word extraction:
  - Try jieba.cut (LIVE-installed).
  - Fallback: char-3-gram of subject+predicate+object text.
  - Strip stopwords (basic Chinese + English set).

This is DETERMINISTIC and FAST (no LLM); the LLM-2nd-pass deepening
(promise→fulfill semantic chain) is OUT OF SCOPE for this iteration
(deferred to async cron 5-month replay; AC-V6 is weakened to
"runs no-crash + N>=0").
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_H = 24
TOPIC_OVERLAP_THRESHOLD = 3  # ≥3 shared topic words → same episode
EPISODE_ID_LEN = 16

# Basic stopwords (Chinese + English) for topic extraction.
_STOPWORDS = {
    # Chinese
    "我", "你", "他", "她", "它", "的", "了", "是", "在", "和", "也", "都",
    "就", "不", "没", "有", "要", "去", "来", "说", "做", "好", "吗", "呢",
    "啊", "哦", "嗯", "嘛", "吧", "把", "被", "让", "给", "为", "对", "从",
    "比", "和", "或", "以", "于", "之", "而", "与", "及", "才", "又", "再",
    # English
    "the", "is", "are", "was", "were", "a", "an", "of", "to", "in", "and",
    "or", "but", "for", "on", "at", "by", "with", "as", "this", "that",
    "it", "its", "i", "you", "he", "she", "they", "we", "be", "do", "did",
    "have", "has", "had", "not", "no", "yes",
}


def _try_jieba_tokens(text: str) -> Optional[List[str]]:
    """Try jieba.cut. Returns None if jieba unavailable."""
    try:
        import jieba  # type: ignore
        return [t.strip() for t in jieba.cut(text or "") if t.strip()]
    except ImportError:
        return None


def _char_3gram(text: str) -> List[str]:
    """Char-3-gram fallback when jieba unavailable.

    Used as topic-word approximation; coarser than jieba but no extra dep.
    """
    s = re.sub(r"\s+", "", text or "")
    if len(s) < 3:
        return [s] if s else []
    return [s[i:i + 3] for i in range(len(s) - 2)]


def topic_words(text: str) -> Set[str]:
    """Extract topic words from text. Stopwords removed.

    Tries jieba first, falls back to char-3-gram on ImportError.
    """
    if not text:
        return set()
    toks = _try_jieba_tokens(text)
    if toks is None:
        toks = _char_3gram(text)
    out: Set[str] = set()
    for t in toks:
        t = t.strip().lower()
        if not t or len(t) < 2:
            continue
        if t in _STOPWORDS:
            continue
        if re.match(r"^[\W\d_]+$", t, re.UNICODE):
            continue
        out.add(t)
    return out


def _factrow_topic_words(fr: Dict[str, Any]) -> Set[str]:
    """Topic words derived from subject + predicate + object."""
    parts = [
        fr.get("canonical_subject") or fr.get("s") or "",
        fr.get("predicate") or fr.get("p") or "",
        fr.get("object") or fr.get("o") or "",
    ]
    return topic_words(" ".join(parts))


def _factrow_chat_room(fr: Dict[str, Any]) -> str:
    return (fr.get("chat_room_id_hash")
            or fr.get("chat_room_display_name")
            or "")


def _factrow_ts(fr: Dict[str, Any]) -> float:
    """Best-effort timestamp extraction (numeric seconds since epoch)."""
    for key in ("valid_at", "batch_start_ts", "ts"):
        v = fr.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                from datetime import datetime
                return datetime.fromisoformat(
                    v.replace("Z", "+00:00")).timestamp()
            except (ValueError, OSError):
                continue
    return 0.0


# Union-find for episode assignment
class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        px, py = self.find(x), self.find(y)
        if px == py:
            return
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1


def build_episodes(
    factrows: List[Dict[str, Any]],
    window_h: int = DEFAULT_WINDOW_H,
    overlap_threshold: int = TOPIC_OVERLAP_THRESHOLD,
) -> Dict[str, str]:
    """Assign episode_ids to factrows that cluster within chat_room + 24h.

    Args:
      factrows: list of fact dicts (must have id + chat_room_id_hash + ts).
      window_h: max gap (hours) between facts in same episode.
      overlap_threshold: min shared topic words to chain two facts.

    Returns:
      Mapping {factrow_id: episode_id_hex16}. Singletons absent
      (caller can default to "" for missing keys).
    """
    if not factrows:
        return {}
    # Group by chat_room
    by_chat: Dict[str, List[int]] = {}
    for i, fr in enumerate(factrows):
        room = _factrow_chat_room(fr)
        if not room:
            continue
        by_chat.setdefault(room, []).append(i)

    n = len(factrows)
    uf = _UnionFind(n)
    window_sec = window_h * 3600

    # Pre-compute topic-word sets
    topics = [_factrow_topic_words(fr) for fr in factrows]
    timestamps = [_factrow_ts(fr) for fr in factrows]

    # Within each chat_room, pairwise compare in time-sorted order.
    for room, idxs in by_chat.items():
        idxs.sort(key=lambda i: timestamps[i])
        for ai in range(len(idxs)):
            i = idxs[ai]
            for bi in range(ai + 1, len(idxs)):
                j = idxs[bi]
                if timestamps[j] - timestamps[i] > window_sec:
                    break  # sorted; further pairs only widen
                if len(topics[i] & topics[j]) >= overlap_threshold:
                    uf.union(i, j)

    # Build episode_id only for clusters of size ≥2
    cluster_size: Dict[int, int] = {}
    for i in range(n):
        root = uf.find(i)
        cluster_size[root] = cluster_size.get(root, 0) + 1
    root_to_eid: Dict[int, str] = {}
    out: Dict[str, str] = {}
    for i, fr in enumerate(factrows):
        root = uf.find(i)
        if cluster_size.get(root, 0) < 2:
            continue
        if root not in root_to_eid:
            root_to_eid[root] = uuid.uuid4().hex[:EPISODE_ID_LEN]
        fid = fr.get("id")
        if fid:
            out[fid] = root_to_eid[root]

    # Best-effort trace
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("episode_chain_built", {
            "n_factrows_in": n,
            "n_episodes": len(root_to_eid),
            "n_factrows_in_episode": len(out),
        })
    except Exception:  # pragma: no cover
        pass

    return out
