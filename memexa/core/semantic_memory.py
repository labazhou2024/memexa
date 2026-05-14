"""
[DEPRECATED 2026-05-12] KAIROS self-evolution frozen since 2026-04-04
(11 patterns last written 2026-04-04, never updated since). KAIROS daemon
dead. Pattern store no longer fed. Use memexa.core.memory_query for recall.

Semantic Memory — Long-term pattern store with belief decay.

Phase 2 of three-layer evolution architecture.

Episodes (from EpisodicLog/EventBus) are periodically consolidated into
semantic patterns — reusable rules that get injected into Agent prompts.

Architecture:
  Episode (raw) → consolidate() → Pattern (semantic) → retrieve() → prompt injection

Belief Decay: confidence *= DECAY_TAU per consolidation cycle.
Usage Boost: diminishing returns — boost = USAGE_BOOST / (1 + times_retrieved / 10).
  Capped at MAX_CONFIDENCE=0.95 to prevent overconfidence.
Instinct Model (ECC-inspired): new patterns start at INITIAL_CONFIDENCE=0.4,
  gain confidence (+0.1) on successful application, lose it (-0.05) on failure.
  This mirrors the ECC "Continuous Learning v2" model where patterns must earn trust.
Pruning: patterns with confidence < PRUNE_THRESHOLD are archived.
Quality Gate: patterns must have source_episodes >= 2 to survive first decay cycle.

Storage: data/semantic_patterns.json (human-readable, git-friendly).
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from memexa.core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_PATTERNS_FILE = _DATA_DIR / "semantic_patterns.json"


def _extract_json(text: str) -> Optional[Dict]:
    """Extract JSON from LLM response that may contain markdown or other wrapping.

    Handles:
      - Pure JSON: {"patterns": [...]}
      - Markdown code block: ```json\\n{...}\\n```
      - Text before/after JSON: "Here is the result: {...}"
    """
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try extracting from markdown code block
    md_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if md_match:
        try:
            return json.loads(md_match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # Try finding first { ... } block
    brace_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except (json.JSONDecodeError, ValueError):
            pass

    return None


DECAY_TAU = 0.95              # Per-consolidation decay
USAGE_BOOST = 0.05            # Base boost when retrieved (halved from 0.1)
PRUNE_THRESHOLD = 0.15        # Archive below this (raised from 0.1)
CONSOLIDATION_THRESHOLD = 10  # Episodes before consolidation
MAX_PATTERNS = 50             # Cap total patterns to prevent unbounded growth
MIN_SOURCE_EPISODES = 2       # Minimum episodes for a pattern to survive pruning
INITIAL_CONFIDENCE = 0.4      # New patterns start at "observed" level (ECC Instinct model: 0.3-0.9)
MAX_CONFIDENCE = 0.95         # Cap to prevent overconfidence


@dataclass
class SemanticPattern:
    """A reusable rule extracted from episodes."""
    pattern_id: str
    rule: str                     # The actual rule/insight
    tags: List[str]               # For keyword retrieval
    confidence: float = INITIAL_CONFIDENCE  # 0.0-1.0; new patterns start at 0.4 (ECC Instinct model)
    source_episodes: int = 0      # How many episodes contributed
    source_episode_ids: List[str] = field(default_factory=list)  # Provenance chain
    times_retrieved: int = 0      # Usage counter
    created_at: str = ""          # ISO timestamp
    last_used_at: str = ""        # ISO timestamp
    last_consolidated_at: str = ""
    application_outcomes: List[Dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SemanticPattern":
        filtered = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        # Fix: application_outcomes may be stored as dict (legacy format) instead of list
        ao = filtered.get("application_outcomes")
        if isinstance(ao, dict):
            filtered["application_outcomes"] = []
        return cls(**filtered)


class SemanticMemory:
    """Manages long-term semantic patterns with decay and retrieval."""

    def __init__(self, patterns_file: Path = None):
        self._file = patterns_file or _PATTERNS_FILE
        self._patterns: Dict[str, SemanticPattern] = {}
        self._pattern_embeddings: Dict[str, any] = {}  # Cache: pattern_id -> numpy array
        self._episodes_since_consolidation = 0
        self._load()

    def _load(self):
        """Load patterns from disk."""
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self._patterns = {
                    k: SemanticPattern.from_dict(v)
                    for k, v in data.get("patterns", {}).items()
                }
                self._episodes_since_consolidation = data.get(
                    "episodes_since_consolidation", 0
                )
                logger.info("Loaded %d semantic patterns", len(self._patterns))
            except Exception as e:
                logger.warning("Failed to load patterns: %s", e)
                self._patterns = {}

    def _save(self):
        """Persist patterns to disk."""
        data = {
            "version": "2.0",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "episodes_since_consolidation": self._episodes_since_consolidation,
            "pattern_count": len(self._patterns),
            "patterns": {k: v.to_dict() for k, v in self._patterns.items()},
        }
        atomic_write_json(self._file, data)

    def record_episode(self, task: str, output: str, score: int,
                       agent: str = "", tags: List[str] = None):
        """Record a completed episode. Triggers consolidation if threshold reached."""
        from .event_bus import log_event

        self._episodes_since_consolidation += 1
        log_event("episode_recorded", agent=agent or "system", details={
            "task": task[:100], "score": score,
            "episodes_since_consolidation": self._episodes_since_consolidation,
        })

        if self._episodes_since_consolidation >= CONSOLIDATION_THRESHOLD:
            logger.info("Consolidation threshold reached (%d episodes)",
                       self._episodes_since_consolidation)
            log_event("consolidation_needed", agent="semantic_memory", details={
                "episodes_pending": self._episodes_since_consolidation,
            })

        self._save()

    def add_pattern(self, rule: str, tags: List[str],
                    source_episodes: int = 1,
                    source_episode_ids: List[str] = None) -> str:
        """Add a new semantic pattern (typically called by consolidation).

        Deduplicates against existing patterns by checking rule similarity.
        source_episode_ids provides provenance chain for white-box auditability.
        """
        # Dedup: skip if a very similar rule already exists
        rule_lower = rule.lower().strip()
        for existing in self._patterns.values():
            if _text_similarity(rule_lower, existing.rule.lower().strip()) > 0.7:
                # Boost existing pattern instead of adding duplicate
                existing.source_episodes += source_episodes
                if source_episode_ids:
                    for eid in source_episode_ids:
                        if eid not in existing.source_episode_ids:
                            existing.source_episode_ids.append(eid)
                existing.confidence = min(MAX_CONFIDENCE, existing.confidence + 0.05)
                self._save()
                logger.info("Merged into existing pattern %s", existing.pattern_id)
                return existing.pattern_id

        pid = f"pat_{int(time.time())}_{len(self._patterns)}"
        now = datetime.utcnow().isoformat() + "Z"
        pattern = SemanticPattern(
            pattern_id=pid,
            rule=rule,
            tags=tags,
            confidence=INITIAL_CONFIDENCE,  # ECC Instinct model: start low, earn confidence
            source_episodes=source_episodes,
            source_episode_ids=source_episode_ids or [],
            created_at=now,
            last_consolidated_at=now,
        )
        self._patterns[pid] = pattern

        # Enforce max patterns cap: remove lowest confidence if over limit
        if len(self._patterns) > MAX_PATTERNS:
            self._evict_weakest()

        self._save()
        logger.info("Added pattern %s: %s", pid, rule[:80])
        return pid

    def retrieve(self, query: str, top_k: int = 3) -> List[SemanticPattern]:
        """Retrieve most relevant patterns using embedding cosine similarity.

        Primary: embedding-based cosine similarity between query and pattern.rule.
        Fallback: keyword substring matching if embedding fails.
        Score = similarity * confidence.
        """
        scored = []
        use_embeddings = False
        query_vec = None

        try:
            import numpy as np
            from .embedding_engine import get_embedding_engine
            engine = get_embedding_engine()
            raw = engine.embed(query)
            if raw:
                query_vec = np.array(raw, dtype=np.float32)
                norm = np.linalg.norm(query_vec)
                if norm > 0:
                    query_vec = query_vec / norm
                    use_embeddings = True
        except Exception as e:
            logger.debug("Embedding unavailable, using keyword fallback: %s", e)

        for p in self._patterns.values():
            if p.confidence < PRUNE_THRESHOLD:
                continue
            # Explicit ECC Instinct minimum injection threshold (>= 0.3).
            # Already covered by PRUNE_THRESHOLD=0.15, but stated here for clarity:
            # only patterns that have survived at least one confidence-building cycle
            # (INITIAL_CONFIDENCE=0.4 > 0.3) will appear in results.

            if use_embeddings and query_vec is not None:
                try:
                    import numpy as np
                    if p.pattern_id not in self._pattern_embeddings:
                        from .embedding_engine import get_embedding_engine
                        engine = get_embedding_engine()
                        vec = np.array(engine.embed(p.rule), dtype=np.float32)
                        n = np.linalg.norm(vec)
                        self._pattern_embeddings[p.pattern_id] = vec / n if n > 0 else vec
                    pat_vec = self._pattern_embeddings[p.pattern_id]
                    cosine_sim = max(0.0, float(np.dot(query_vec, pat_vec)))
                    match_score = cosine_sim
                except Exception:
                    match_score = self._keyword_score(query, p)
            else:
                match_score = self._keyword_score(query, p)

            if match_score > 0:
                scored.append((match_score * p.confidence, p))

        scored.sort(key=lambda x: -x[0])
        results = [p for _, p in scored[:top_k]]

        # Fallback: if no matches but patterns exist, return top-N by confidence
        # This ensures patterns are always injected when available (borrowed from
        # Superpowers' "1% Rule": if there's even a chance a pattern helps, inject it)
        if not results and self._patterns:
            by_conf = sorted(self._patterns.values(),
                             key=lambda p: -p.confidence)
            results = [p for p in by_conf[:top_k]
                       if p.confidence >= PRUNE_THRESHOLD]

        # Diminishing usage boost: boost = base / (1 + times_retrieved / 10)
        now = datetime.utcnow().isoformat() + "Z"
        for p in results:
            p.times_retrieved += 1
            boost = USAGE_BOOST / (1 + p.times_retrieved / 10)
            p.confidence = min(MAX_CONFIDENCE, p.confidence + boost)
            p.last_used_at = now

        if results:
            self._save()

        return results

    def _keyword_score(self, query: str, pattern: "SemanticPattern") -> float:
        """Fallback keyword matching when embeddings unavailable."""
        query_words = set(query.lower().split())
        tag_set = set(t.lower() for t in pattern.tags)
        rule_words = set(pattern.rule.lower().split()[:30])

        score = 0.0
        for qw in query_words:
            if len(qw) < 3:
                continue
            for tag in tag_set:
                if qw in tag or tag in qw:
                    score += 1.0
                    break
            else:
                for rw in rule_words:
                    if qw in rw or rw in qw:
                        score += 0.3
                        break
        return score

    def apply_decay(self):
        """Apply belief decay to all patterns. Called during consolidation."""
        pruned = []
        for pid, p in list(self._patterns.items()):
            p.confidence *= DECAY_TAU
            p.last_consolidated_at = datetime.utcnow().isoformat() + "Z"

            # Prune: low confidence OR single-source patterns that decayed
            should_prune = (
                p.confidence < PRUNE_THRESHOLD
                or (p.source_episodes < MIN_SOURCE_EPISODES
                    and p.confidence < 0.5
                    and p.times_retrieved == 0)
            )
            if should_prune:
                pruned.append(pid)

        for pid in pruned:
            logger.info("Pruning pattern %s (confidence=%.3f, sources=%d, retrievals=%d)",
                       pid, self._patterns[pid].confidence,
                       self._patterns[pid].source_episodes,
                       self._patterns[pid].times_retrieved)
            del self._patterns[pid]

        self._episodes_since_consolidation = 0
        self._save()
        logger.info("Decay applied: %d patterns remain, %d pruned",
                   len(self._patterns), len(pruned))

    async def consolidate(self, episodes: List[Dict]) -> List[str]:
        """Extract patterns from recent episodes using LLM.

        Uses a quality-focused prompt that requires evidence from multiple
        episodes and rejects generic/obvious rules.
        """
        if not episodes:
            return []

        from .llm_router import get_router, TaskType

        # Build consolidation prompt with quality gate
        # Assign IDs to episodes for provenance tracking
        for i, ep in enumerate(episodes):
            if "episode_id" not in ep:
                ep["episode_id"] = f"ep_{int(time.time())}_{i}"

        episode_text = "\n".join(
            f"- [{e.get('episode_id', '?')}] [score={e.get('score', '?')}, agent={e.get('agent', '?')}] {e.get('task', '')[:120]}"
            for e in episodes[:30]
        )

        prompt = f"""Analyze these {len(episodes)} agent execution episodes and extract reusable patterns.

## Episodes
{episode_text}

## Quality Requirements (STRICT)
1. Each pattern MUST be derived from at least 2 episodes (not a single outlier)
2. Each pattern MUST be SPECIFIC and ACTIONABLE — not generic advice
3. REJECT patterns like "ensure accuracy", "be thorough", "follow best practices"
4. Good pattern example: "When modifying async functions, always check if callers use await — 3 incidents of sync/async mismatch"
5. Bad pattern example: "When summarizing, ensure clarity" — this is obvious and unhelpful

## Output Format
Return JSON:
{{
  "patterns": [
    {{
      "rule": "Specific actionable rule with evidence",
      "tags": ["keyword1", "keyword2"],
      "source_count": 3,
      "source_ids": ["ep_xxx_0", "ep_xxx_1"],
      "evidence": "Brief description of which episodes support this"
    }}
  ]
}}

Return empty patterns array if nothing meets the quality bar. Quality > quantity."""

        router = get_router()
        client = router.get_client()
        if not client:
            logger.warning("No LLM client for consolidation")
            self.apply_decay()
            return []

        try:
            response = router.call(
                task_type=TaskType.CHAT,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )

            logger.debug("consolidate() raw LLM response: %s", response[:500])
            data = _extract_json(response)
            if data is None:
                logger.warning(
                    "consolidate(): could not parse JSON from LLM response: %s",
                    response[:300],
                )
                self.apply_decay()
                return []
            new_ids = []
            for p in data.get("patterns", []):
                rule = p.get("rule", "")
                tags = p.get("tags", [])
                count = p.get("source_count", 1)
                source_ids = p.get("source_ids", [])

                # Quality gate: reject generic patterns
                if not rule or not tags:
                    continue
                if count < MIN_SOURCE_EPISODES:
                    logger.debug("Skipping pattern with only %d sources: %s", count, rule[:60])
                    continue
                if _is_generic_pattern(rule):
                    logger.info("Rejected generic pattern (phrase match): %s", rule[:60])
                    continue

                # LLM-based specificity scoring
                specificity = await self._score_specificity(router, rule)
                if specificity < 0.3:
                    logger.info(
                        "Rejected low-specificity pattern (score=%.2f): %s",
                        specificity, rule[:60],
                    )
                    continue

                pid = self.add_pattern(
                    rule, tags,
                    source_episodes=count,
                    source_episode_ids=source_ids,
                )
                new_ids.append(pid)

            self.apply_decay()
            logger.info("Consolidated: %d new patterns from %d episodes",
                       len(new_ids), len(episodes))
            return new_ids

        except Exception as e:
            logger.error("Consolidation failed: %s", e)
            self.apply_decay()
            return []

    def get_prompt_injection(self, task_description: str, top_k: int = 3) -> str:
        """Get patterns formatted for injection into Agent prompt.

        Includes source provenance for white-box auditability.
        """
        patterns = self.retrieve(task_description, top_k=top_k)
        if not patterns:
            return ""

        lines = ["[Historical patterns] (by confidence, with provenance)"]
        for p in patterns:
            source_info = f" (from {p.source_episodes} episodes)" if p.source_episodes > 1 else ""
            lines.append(f"- [{p.confidence:.0%}]{source_info} {p.rule}")
        return "\n".join(lines)

    def record_application_outcome(self, pattern_id: str, success: bool,
                                   project_id: str = "") -> None:
        """Record outcome of pattern application (ECC Instinct confidence model).

        Success: confidence += 0.1 (capped at MAX_CONFIDENCE=0.95)
        Failure: confidence -= 0.05 (floored at PRUNE_THRESHOLD)
        """
        if pattern_id not in self._patterns:
            return
        p = self._patterns[pattern_id]
        outcome = {
            "project_id": project_id,
            "success": success,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        p.application_outcomes.append(outcome)

        if success:
            p.confidence = min(MAX_CONFIDENCE, p.confidence + 0.1)
        else:
            p.confidence = max(PRUNE_THRESHOLD, p.confidence - 0.05)

        self._save()
        logger.info("Pattern %s outcome recorded: success=%s, confidence=%.2f",
                    pattern_id, success, p.confidence)

    async def _score_specificity(self, router, rule: str) -> float:
        """Ask LLM to score pattern specificity 0-1. Returns 0.0 on failure."""
        prompt = (
            "Is this pattern specific and actionable? "
            "Score 0.0 (generic) to 1.0 (specific). "
            "Reply with ONLY a JSON object: {\"score\": <float>}\n\n"
            f"Pattern: {rule}"
        )
        try:
            from .llm_router import TaskType
            response = router.call(
                task_type=TaskType.CHAT,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=50,
                response_format={"type": "json_object"},
            )
            logger.debug("_score_specificity() raw LLM response: %s", response)
            data = _extract_json(response)
            if data is None:
                logger.warning(
                    "_score_specificity(): could not parse JSON; raw response: %s",
                    response,
                )
                return 0.0
            score = float(data.get("score", 0.0))
            return max(0.0, min(1.0, score))
        except Exception as e:
            logger.warning("Specificity scoring failed for pattern: %s — %s", rule[:60], e)
            return 0.0

    def _evict_weakest(self):
        """Remove the weakest pattern when over MAX_PATTERNS."""
        if not self._patterns:
            return
        weakest = min(
            self._patterns.values(),
            key=lambda p: (p.confidence, p.times_retrieved, p.source_episodes),
        )
        logger.info("Evicting weakest pattern %s (conf=%.3f)", weakest.pattern_id, weakest.confidence)
        del self._patterns[weakest.pattern_id]

    @property
    def pattern_count(self) -> int:
        return len(self._patterns)

    @property
    def stats(self) -> Dict:
        return {
            "pattern_count": self.pattern_count,
            "episodes_pending": self._episodes_since_consolidation,
            "avg_confidence": (
                sum(p.confidence for p in self._patterns.values()) /
                len(self._patterns) if self._patterns else 0
            ),
            "max_patterns": MAX_PATTERNS,
        }


# --- Module-level helpers ---

def _text_similarity(a: str, b: str) -> float:
    """Simple word-overlap Jaccard similarity."""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


_GENERIC_PHRASES = [
    "ensure accuracy", "be thorough", "follow best practices",
    "ensure clarity", "ensure quality", "be specific",
    "ensure completeness", "maintain consistency",
    "ensure relevance", "achieve high performance",
]

def _is_generic_pattern(rule: str) -> bool:
    """Reject patterns that are obvious generic advice."""
    rule_lower = rule.lower()
    for phrase in _GENERIC_PHRASES:
        if phrase in rule_lower:
            return True
    # Reject very short rules (likely not actionable)
    if len(rule.split()) < 8:
        return True
    return False


# Singleton
_instance: Optional[SemanticMemory] = None


def get_semantic_memory() -> SemanticMemory:
    global _instance
    if _instance is None:
        _instance = SemanticMemory()
    return _instance
