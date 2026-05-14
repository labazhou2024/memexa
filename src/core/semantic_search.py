"""
Semantic Search for Pattern Knowledge Base

Uses TF-IDF vectorization for semantic similarity matching.
Falls back to keyword matching if sklearn is not available.

Two modes:
1. TF-IDF mode (when scikit-learn available): cosine similarity on TF-IDF vectors
2. Fallback mode: bag-of-words with Jaccard similarity (stdlib only)
"""

import math
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Lazy sklearn import - availability determines which mode is used
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as sklearn_cosine
    _SKLEARN_AVAILABLE = True
except Exception:
    # Catch ImportError, ValueError (binary incompatibility), and any other
    # exception that may occur when sklearn is installed but broken.
    _SKLEARN_AVAILABLE = False

from .pattern_extractor import PatternEntry, load_all_patterns, _PATTERNS_FILE


# ── Singleton index cache ──

_index_instance: Optional["SemanticIndex"] = None


def _get_index(patterns_file: Optional[Path] = None) -> "SemanticIndex":
    """Return cached SemanticIndex singleton, rebuilding if file changed."""
    global _index_instance
    target_file = patterns_file or _PATTERNS_FILE
    if _index_instance is None or _index_instance._patterns_file != target_file:
        _index_instance = SemanticIndex(target_file)
    return _index_instance


def invalidate_index() -> None:
    """Force rebuild on next access (call after knowledge base updates)."""
    global _index_instance
    _index_instance = None


# ── Tokenizer ──

# CJK Unified Ideographs range
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# English word pattern
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
# Stop words (minimal set for English + Chinese)
_STOP_WORDS = {
    "the", "a", "an", "is", "in", "of", "to", "for", "and", "or", "not",
    "be", "are", "was", "were", "it", "this", "that", "by", "at", "on",
    "with", "as", "if", "so", "but", "from", "use", "used", "will",
    # Common Chinese stops
    "的", "了", "在", "是", "有", "和", "与", "但", "也", "就", "到",
    "为", "对", "于", "中", "以", "而", "或", "等", "个", "从",
}


def _tokenize(text: str) -> List[str]:
    """
    Tokenize mixed Chinese+English text.

    Strategy:
    - CJK characters: split individually (each char is a token)
    - English/ASCII: split by non-alphanumeric boundaries
    - Filter stop words and single-char English tokens
    """
    tokens = []
    # Extract CJK chars
    for ch in _CJK_RE.findall(text.lower()):
        tokens.append(ch)
    # Extract English words
    for word in _WORD_RE.findall(text.lower()):
        if len(word) >= 2 and word not in _STOP_WORDS:
            tokens.append(word)
    return tokens


def _entry_to_text(entry: PatternEntry) -> str:
    """Combine all searchable fields of a PatternEntry into one string."""
    parts = [
        entry.fact,
        entry.recommendation,
        " ".join(entry.tags),
        " ".join(entry.affected_files),
        entry.type,
    ]
    return " ".join(p for p in parts if p)


# ── Core class ──

class SemanticIndex:
    """
    Semantic index over the pattern knowledge base.

    Uses TF-IDF (sklearn) when available, otherwise falls back to
    bag-of-words with Jaccard similarity using only stdlib.
    """

    def __init__(self, patterns_file: Path) -> None:
        self._patterns_file = patterns_file
        self._patterns: List[PatternEntry] = []
        self._tfidf_matrix = None          # sklearn sparse matrix (TF-IDF mode)
        self._vectorizer = None            # TfidfVectorizer instance
        self._bow_docs: List[Dict[str, int]] = []  # term frequency dicts (fallback)
        self._bow_doc_norms: List[float] = []       # L2 norms for cosine (fallback)
        self._mode = "empty"               # "tfidf" | "bow" | "empty"

        self._load()

    def _load(self) -> None:
        """Load patterns from file and build appropriate index."""
        # Temporarily patch module-level _PATTERNS_FILE if needed
        import src.core.pattern_extractor as pe
        original = pe._PATTERNS_FILE
        if self._patterns_file != original:
            pe._PATTERNS_FILE = self._patterns_file
        try:
            self._patterns = load_all_patterns()
        finally:
            pe._PATTERNS_FILE = original

        if not self._patterns:
            self._mode = "empty"
            return

        if _SKLEARN_AVAILABLE:
            self._build_tfidf_index()
        else:
            self._build_bow_index()

    def _build_tfidf_index(self) -> None:
        """Build TF-IDF matrix using sklearn TfidfVectorizer."""
        corpus = [_entry_to_text(e) for e in self._patterns]
        self._vectorizer = TfidfVectorizer(
            analyzer=lambda text: _tokenize(text),
            min_df=1,
            sublinear_tf=True,
        )
        try:
            self._tfidf_matrix = self._vectorizer.fit_transform(corpus)
            self._mode = "tfidf"
        except ValueError:
            # Empty vocabulary (all tokens are stop words)
            self._build_bow_index()

    def _build_bow_index(self) -> None:
        """Build bag-of-words index using stdlib only."""
        self._bow_docs = []
        self._bow_doc_norms = []
        for entry in self._patterns:
            tokens = _tokenize(_entry_to_text(entry))
            tf = dict(Counter(tokens))
            norm = math.sqrt(sum(v * v for v in tf.values())) or 1.0
            self._bow_docs.append(tf)
            self._bow_doc_norms.append(norm)
        self._mode = "bow"

    def _cosine_similarity(self, query_vec: Dict[str, float], doc_vec: Dict[str, int], doc_norm: float) -> float:
        """
        Compute cosine similarity between a query vector and a document vector.
        Both are represented as term -> weight dicts. Uses stdlib only.
        """
        if doc_norm == 0:
            return 0.0
        query_norm = math.sqrt(sum(v * v for v in query_vec.values())) or 1.0
        dot = sum(query_vec.get(t, 0.0) * doc_vec.get(t, 0.0) for t in query_vec)
        return dot / (query_norm * doc_norm)

    def search(self, query: str, limit: int = 10) -> List[Tuple[float, PatternEntry]]:
        """
        Search for patterns relevant to the query string.

        Returns:
            List of (score, entry) tuples sorted by descending relevance.
            Score is in [0, 1].
        """
        if self._mode == "empty" or not self._patterns:
            return []

        if self._mode == "tfidf":
            return self._search_tfidf(query, limit)
        else:
            return self._search_bow(query, limit)

    def _search_tfidf(self, query: str, limit: int) -> List[Tuple[float, PatternEntry]]:
        """TF-IDF cosine similarity search using sklearn."""
        try:
            query_vec = self._vectorizer.transform([query])
            scores = sklearn_cosine(query_vec, self._tfidf_matrix).flatten()
            # Get top results
            top_indices = scores.argsort()[::-1][:limit]
            results = []
            for idx in top_indices:
                score = float(scores[idx])
                if score > 0:
                    results.append((score, self._patterns[idx]))
            return results
        except Exception:
            # Fallback gracefully on any sklearn error
            return self._search_bow(query, limit)

    def _search_bow(self, query: str, limit: int) -> List[Tuple[float, PatternEntry]]:
        """Bag-of-words cosine similarity search using stdlib."""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        # Build query TF vector
        query_tf: Dict[str, float] = {}
        for t in query_tokens:
            query_tf[t] = query_tf.get(t, 0.0) + 1.0

        results = []
        for i, (doc_vec, doc_norm) in enumerate(zip(self._bow_docs, self._bow_doc_norms)):
            score = self._cosine_similarity(query_tf, doc_vec, doc_norm)
            if score > 0:
                results.append((score, self._patterns[i]))

        results.sort(key=lambda x: x[0], reverse=True)
        return results[:limit]


# ── Hybrid prime function ──

def hybrid_prime(
    files: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    work_type: Optional[str] = None,
    query_text: Optional[str] = None,
    limit: int = 10,
    patterns_file: Optional[Path] = None,
) -> List[PatternEntry]:
    """
    Hybrid retrieval combining keyword scoring with semantic search.

    Weights:
    - 40% keyword score (from existing prime() logic, normalized to [0,1])
    - 60% semantic score (TF-IDF or BOW cosine similarity)

    When query_text is None, semantic component uses a query built from
    keywords + work_type. Falls back gracefully to keyword-only if the
    semantic index is empty or search fails.

    Args:
        files: File paths for glob matching (forwarded to prime())
        keywords: Keywords for text matching (forwarded to prime())
        work_type: Work type for type-based scoring
        query_text: Free-text query for semantic search
        limit: Maximum results to return
        patterns_file: Override patterns file path (for testing)

    Returns:
        Deduplicated list of PatternEntry sorted by hybrid score.
    """
    from .pattern_extractor import prime as keyword_prime

    # Build semantic query from available signals
    if query_text is None:
        parts = []
        if keywords:
            parts.extend(keywords)
        if work_type:
            parts.append(work_type)
        query_text = " ".join(parts)

    # --- Keyword scoring ---
    # Get a larger pool to score against (2x limit)
    kw_pool_size = limit * 2
    try:
        kw_results = keyword_prime(
            files=files,
            keywords=keywords,
            work_type=work_type,
            limit=kw_pool_size,
        )
    except Exception:
        kw_results = []

    # Build score map from keyword results: id -> normalized rank score
    kw_scores: Dict[str, float] = {}
    if kw_results:
        for rank, entry in enumerate(kw_results):
            # Linear decay: top entry = 1.0, last = 1/len
            kw_scores[entry.id] = 1.0 - (rank / len(kw_results))

    # --- Semantic scoring ---
    sem_scores: Dict[str, float] = {}
    if query_text.strip():
        try:
            target_file = patterns_file or _PATTERNS_FILE
            index = _get_index(target_file)
            sem_results = index.search(query_text, limit=kw_pool_size)
            if sem_results:
                max_score = sem_results[0][0] if sem_results[0][0] > 0 else 1.0
                for score, entry in sem_results:
                    sem_scores[entry.id] = score / max_score
        except Exception:
            pass  # Semantic search failed — continue with keyword only

    # --- Merge ---
    # Collect all candidate entries from both pools
    all_entries: Dict[str, PatternEntry] = {}
    for entry in kw_results:
        all_entries[entry.id] = entry
    if query_text.strip():
        try:
            target_file = patterns_file or _PATTERNS_FILE
            index = _get_index(target_file)
            for _, entry in index.search(query_text, limit=kw_pool_size):
                all_entries[entry.id] = entry
        except Exception:
            pass

    if not all_entries:
        return []

    # Compute hybrid scores
    KEYWORD_WEIGHT = 0.4
    SEMANTIC_WEIGHT = 0.6

    hybrid: List[Tuple[float, PatternEntry]] = []
    for eid, entry in all_entries.items():
        kw_s = kw_scores.get(eid, 0.0)
        sem_s = sem_scores.get(eid, 0.0)

        # If no semantic signal at all, fall back to pure keyword
        if not sem_scores:
            score = kw_s
        elif not kw_scores:
            score = sem_s
        else:
            score = KEYWORD_WEIGHT * kw_s + SEMANTIC_WEIGHT * sem_s

        hybrid.append((score, entry))

    hybrid.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in hybrid[:limit]]
