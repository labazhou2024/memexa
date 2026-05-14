"""userprompt_lesson_recall.py — UserPromptSubmit hook (L2 layer).

Called by Claude Code on every user prompt submission. Extracts topic
keywords from the prompt, queries graph for type=state+opentype:lesson
cards filtered by enforcement_tier (priority: hard > warn > context),
returns ≤3 most relevant lessons as a context-injection block:

    <lesson-from-graph from='2026-05-08' tier='hard'>
    Lesson #1 (sal=0.85 / src=feedback_xxx.md):
      narrative ...
      evidence: "..."
    </lesson-from-graph>

Goals (CEO directive 2026-05-08):
  - Generalization: topic extraction is semantic (BGE-M3 if available,
    keyword fallback otherwise), not hardcoded
  - Speed: ≤3s budget; if hindsight slow, return empty (fail-open)
  - Precision: only inject when confident about relevance (semantic
    similarity ≥ 0.4 OR keyword overlap ≥ 1)

Wire via .claude/config/settings.json hooks.UserPromptSubmit (additive,
doesn't replace existing keyword_router).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_MEMEX_ROOT = Path(__file__).resolve().parents[2]
if str(_MEMEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEMEX_ROOT))


# ────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────

_RECALL_TIMEOUT_S = float(os.environ.get("MEMEX_LESSON_RECALL_TIMEOUT_S", "25.0"))
_MAX_INJECTED = int(os.environ.get("MEMEX_LESSON_RECALL_MAX", "3"))
_MIN_PROMPT_LEN = int(os.environ.get("MEMEX_LESSON_RECALL_MIN_PROMPT", "10"))


# ────────────────────────────────────────────────────────────────────────
# Topic extraction (delegates to lesson_card_v1)
# ────────────────────────────────────────────────────────────────────────

def _extract_topics(prompt: str) -> List[str]:
    try:
        from src.core.lesson_card_v1 import extract_topics
        return extract_topics(prompt, max_topics=5)
    except Exception:
        return []


# ────────────────────────────────────────────────────────────────────────
# Graph query
# ────────────────────────────────────────────────────────────────────────

def _query_lessons(topics: List[str], full_prompt: str = "") -> List[Dict[str, Any]]:
    """Run lesson recall against memory_full_v5.

    Strategy (2026-05-08 v2): use FULL prompt text for BGE-M3 semantic recall
    (not just topic extraction). Empirical (10-prompt benchmark): topic-only
    query → 30% hit rate; full-prompt → expected 50%+ since BGE-M3 handles
    semantic similarity better than keyword overlap.

    Falls back to topics-as-query if full_prompt empty.
    Always exits within _RECALL_TIMEOUT_S budget.
    """
    import concurrent.futures

    def _do_query():
        from src.core.memory_query import _recall_raw
        # Full prompt is best — BGE-M3 understands semantics. Topics
        # extraction was lossy.
        query = full_prompt if full_prompt else " ".join(topics[:5])
        if not query.strip():
            return []
        try:
            raw = _recall_raw(
                query, tags=["opentype:lesson"],
                max_tokens=2048, budget="low",
                timeout=22.0,
            )
            return raw.get("results") or []
        except Exception:
            return []

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(_do_query)
        try:
            return fut.result(timeout=_RECALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            return []
    finally:
        pool.shutdown(wait=False)


# ────────────────────────────────────────────────────────────────────────
# Ranking + formatting
# ────────────────────────────────────────────────────────────────────────

_TIER_PRIORITY = {"hard": 4, "warn": 3, "context": 2, "general": 1}


def _rank_lessons(
    items: List[Dict[str, Any]],
    prompt_topics: List[str],
) -> List[Dict[str, Any]]:
    """Rank by (tier_priority, salience, topic_overlap)."""
    topic_set = {t.lower() for t in prompt_topics}

    def score(it: Dict[str, Any]) -> Tuple[int, float, int]:
        md = it.get("metadata") or {}
        tier = md.get("enforcement_tier", "general")
        tier_p = _TIER_PRIORITY.get(tier, 0)
        try:
            sal = float(md.get("salience", "0"))
        except (ValueError, TypeError):
            sal = 0.0
        text = (it.get("text") or "").lower()
        overlap = sum(1 for t in topic_set if t in text)
        return (tier_p, sal, overlap)

    ranked = sorted(items, key=score, reverse=True)
    return ranked[:_MAX_INJECTED]


def _extract_narrative_from_card_text(text: str) -> str:
    """Cards store full V2 envelope in `text`. Extract just the narrative."""
    if "MEMORYCARD_V2_HEADER_END" in text:
        # Body after the END marker is the narrative
        parts = text.split("】", 2)  # split on 】 of HEADER_END
        if len(parts) >= 3:
            return parts[2].strip()[:400]
    # Fallback: first 200 chars
    return text[:200].replace("\n", " ").strip()


def _format_injection(lessons: List[Dict[str, Any]]) -> str:
    if not lessons:
        return ""

    lines = ["<lesson-from-graph>"]
    for i, it in enumerate(lessons, 1):
        md = it.get("metadata") or {}
        tier = md.get("enforcement_tier", "general")
        sal = md.get("salience", "?")
        when = md.get("when_start", "?")[:10]
        src_md = md.get("source_md_filename", "")
        narrative = _extract_narrative_from_card_text(it.get("text", ""))
        # Truncate
        narrative = narrative.replace("\n", " ")[:300]

        lines.append(f"  Lesson #{i} [{tier}/sal={sal}/{when}"
                     f"{' src=' + src_md if src_md else ''}]:")
        lines.append(f"    {narrative}")

    lines.append("</lesson-from-graph>")
    lines.append("")  # blank line separator
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Main hook entry
# ────────────────────────────────────────────────────────────────────────

def main() -> int:
    """Hook reads JSON from stdin, may inject context via stdout.

    Claude Code UserPromptSubmit hook contract:
      stdin: {"prompt": "user message text", "session_id": "...", ...}
      stdout: any text printed becomes prepended to user's prompt context

    We print a <lesson-from-graph> block when relevant lessons exist.
    """
    t0 = time.time()
    try:
        data = sys.stdin.read()
        if not data:
            return 0
        try:
            j = json.loads(data)
        except json.JSONDecodeError:
            return 0

        prompt = j.get("prompt") or j.get("user_prompt") or ""
        if not isinstance(prompt, str) or len(prompt) < _MIN_PROMPT_LEN:
            return 0

        topics = _extract_topics(prompt)
        # 2026-05-08: pass full prompt for BGE-M3 semantic recall (better than
        # topic-only). Topics still used for downstream ranking.
        items = _query_lessons(topics, full_prompt=prompt)
        if not items:
            return 0

        ranked = _rank_lessons(items, topics)
        injection = _format_injection(ranked)
        if injection:
            print(injection)
            print(f"<!-- lesson_recall: {len(ranked)} cards in "
                   f"{time.time()-t0:.2f}s -->", file=sys.stderr)

    except Exception as e:
        # Fail-open: never block prompt
        print(f"<!-- lesson_recall fail: {type(e).__name__}: {e} -->",
              file=sys.stderr)

    return 0


# Standalone test entry: `python -m src.core.userprompt_lesson_recall test "your prompt here"`
def _cli_test(prompt: str) -> int:
    topics = _extract_topics(prompt)
    print(f"=== topics extracted: {topics} ===\n")
    items = _query_lessons(topics, full_prompt=prompt)
    print(f"=== {len(items)} cards recalled ===\n")
    ranked = _rank_lessons(items, topics)
    injection = _format_injection(ranked)
    print(injection if injection else "(no injection produced)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.exit(_cli_test(" ".join(sys.argv[2:])))
    sys.exit(main())
