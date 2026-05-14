"""lesson_card_v1.py — Shared lesson-card extraction + classification.

Used by:
  - tools/migrate_md_lessons_to_v5.py    (md → graph migration)
  - memexa/core/stop_session_card_hook.py (session transcript → lesson cards)
  - memexa/core/userprompt_lesson_recall.py (recall + format)

Design goals:
  - Generalization: keyword patterns externalized to data/lesson_keywords.json,
    can be extended without code changes
  - Schema-compatible: emits MemoryCard v2 with types=["state"]+open_type_hint="lesson"
    (CANONICAL_TYPES doesn't include "lesson"; we use the open_type extension)
  - Idempotent: deterministic batch_id from content sha → re-running won't dupe
  - Quality bar: rejects too-short / too-generic candidates
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[2]
_KEYWORDS_PATH = _REPO / "data" / "lesson_keywords.json"


# ────────────────────────────────────────────────────────────────────────
# Keyword library loader (cached)
# ────────────────────────────────────────────────────────────────────────

_KW_CACHE: Optional[Dict[str, Any]] = None


def load_keywords(refresh: bool = False) -> Dict[str, Any]:
    """Load lesson_keywords.json (cached). Caller can pass refresh=True
    after editing the JSON to force reload."""
    global _KW_CACHE
    if _KW_CACHE is None or refresh:
        if _KEYWORDS_PATH.exists():
            _KW_CACHE = json.loads(_KEYWORDS_PATH.read_text(encoding="utf-8"))
        else:
            _KW_CACHE = {
                "tier_keywords": {"hard": {"patterns": []},
                                  "warn": {"patterns": []},
                                  "context": {"patterns": []}},
                "user_lesson_triggers": {"high_signal": [], "diagnostic": [],
                                          "correction": []},
                "user_directive_triggers": {"patterns": []},
                "negative_filters": {"patterns": [],
                                     "min_message_length": 15,
                                     "max_message_length": 500},
            }
    return _KW_CACHE


# ────────────────────────────────────────────────────────────────────────
# Enforcement tier classification (generalizable)
# ────────────────────────────────────────────────────────────────────────

def classify_enforcement_tier(
    text: str,
    filename: Optional[str] = None,
    frontmatter: Optional[Dict[str, Any]] = None,
) -> str:
    """Return one of {hard, warn, context, general}.

    Rules (in priority order, first match wins):
      1. frontmatter['name'] contains 'HARD RULE' → 'hard'
      2. text contains hard-tier patterns → 'hard'
      3. text contains warn-tier patterns → 'warn'
      4. text contains context-tier patterns → 'context'
      5. default → 'general'
    """
    kw = load_keywords()
    fm = frontmatter or {}

    # Priority 1: explicit HARD RULE in frontmatter name
    for key in ("name", "title"):
        v = str(fm.get(key, ""))
        if "HARD RULE" in v or "硬规则" in v:
            return "hard"

    # Priority 2-4: scan text for tier patterns
    for tier in ("hard", "warn", "context"):
        patterns = kw.get("tier_keywords", {}).get(tier, {}).get("patterns", [])
        for p in patterns:
            if p in text:
                return tier

    return "general"


def classify_lesson_subtype(text: str) -> str:
    """Return semantic subtype: lesson | directive | correction | diagnostic."""
    kw = load_keywords()
    triggers = kw.get("user_lesson_triggers", {})
    if any(p in text for p in triggers.get("correction", [])):
        return "correction"
    if any(p in text for p in triggers.get("high_signal", [])):
        return "lesson"
    if any(p in text for p in triggers.get("diagnostic", [])):
        return "diagnostic"
    if any(p in text for p in
           kw.get("user_directive_triggers", {}).get("patterns", [])):
        return "directive"
    return "lesson"  # default


# ────────────────────────────────────────────────────────────────────────
# User-message scanner (Stop hook + ad-hoc transcript parsing)
# ────────────────────────────────────────────────────────────────────────

def is_lesson_candidate(message: str) -> Tuple[bool, str]:
    """Test whether `message` is a lesson candidate.

    Returns (matched, subtype). subtype ∈ {lesson, directive, correction,
    diagnostic, ''}.
    """
    kw = load_keywords()
    nf = kw.get("negative_filters", {})

    # Length gate
    if len(message) < nf.get("min_message_length", 15):
        return False, ""
    if len(message) > nf.get("max_message_length", 500):
        return False, ""

    # Negative patterns (chitchat)
    text_low = message.strip().lower()
    if text_low in [p.lower() for p in nf.get("patterns", [])]:
        return False, ""

    # Positive triggers
    triggers = kw.get("user_lesson_triggers", {})
    if any(p in message for p in triggers.get("high_signal", [])):
        return True, "lesson"
    if any(p in message for p in triggers.get("correction", [])):
        return True, "correction"
    if any(p in message for p in
           kw.get("user_directive_triggers", {}).get("patterns", [])):
        return True, "directive"

    return False, ""


# ────────────────────────────────────────────────────────────────────────
# Semantic enforcement classifier (BGE-M3 prototypes)
# ────────────────────────────────────────────────────────────────────────

_PROTOTYPES_PATH = _REPO / "data" / "lesson_tier_prototypes.json"
_PROTOTYPES_CACHE: Optional[Dict[str, Any]] = None


def _load_prototypes() -> Optional[Dict[str, Any]]:
    """Load tier prototypes JSON. None if not built yet."""
    global _PROTOTYPES_CACHE
    if _PROTOTYPES_CACHE is None:
        if _PROTOTYPES_PATH.exists():
            try:
                _PROTOTYPES_CACHE = json.loads(
                    _PROTOTYPES_PATH.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                _PROTOTYPES_CACHE = {}
        else:
            _PROTOTYPES_CACHE = {}
    return _PROTOTYPES_CACHE if _PROTOTYPES_CACHE.get("prototypes") else None


def _embed_via_mac_bge(text: str, ssh_host: str = "primary-host") -> Optional[List[float]]:
    """Get BGE-M3 embedding by SSH'ing to Mac. ~2s/call.

    Returns L2-normalized embedding or None on failure.
    """
    import subprocess
    truncated = text[:2000].replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    payload = f'{{"inputs":["{truncated}"]}}'
    try:
        out = subprocess.run(
            ["ssh", ssh_host,
             f"curl -s --max-time 10 http://127.0.0.1:18082/embed "
             f"-H 'Content-Type: application/json' "
             f"-d '{payload}'"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        if isinstance(data, list) and data and isinstance(data[0], list):
            emb = data[0]
            # L2-normalize
            s = sum(x * x for x in emb)
            if s <= 0:
                return None
            n = s ** 0.5
            return [x / n for x in emb]
    except (subprocess.TimeoutExpired, subprocess.SubprocessError,
            json.JSONDecodeError, IndexError):
        return None
    return None


def classify_enforcement_tier_hybrid(
    text: str,
    filename: Optional[str] = None,
    frontmatter: Optional[Dict[str, Any]] = None,
) -> str:
    """Hybrid classifier: keyword first (cheap, accurate on explicit markers),
    semantic fallback when keyword returns 'general' (might rescue ambiguous
    cases where text doesn't contain HARD RULE / Why: / 建议 markers but is
    still semantically lesson-like).

    This is the recommended classifier. Use _semantic only for benchmarks.
    """
    kw_tier = classify_enforcement_tier(text, filename, frontmatter)
    if kw_tier != "general":
        return kw_tier  # keyword was confident
    # Try semantic; if that returns hard/warn/context bump up; else stay general
    try:
        sem_tier = classify_enforcement_tier_semantic(text, fallback_to_keyword=False)
        if sem_tier in ("hard", "warn", "context"):
            return sem_tier
    except Exception:
        pass
    return "general"


def classify_enforcement_tier_semantic(
    text: str, fallback_to_keyword: bool = True,
) -> str:
    """Classify enforcement tier via cosine similarity to BGE-M3 prototypes.

    Falls back to keyword classifier if:
      - prototypes JSON not yet built
      - BGE-M3 SSH call fails
      - all cosines below 0.3 floor (not confident)

    Returns one of {hard, warn, context, general}.
    """
    protos = _load_prototypes()
    if not protos:
        if fallback_to_keyword:
            return classify_enforcement_tier(text)
        return "general"

    emb = _embed_via_mac_bge(text)
    if emb is None:
        if fallback_to_keyword:
            return classify_enforcement_tier(text)
        return "general"

    best_tier = None
    best_score = -1.0
    for tier, proto_vec in protos["prototypes"].items():
        if len(proto_vec) != len(emb):
            continue
        # Both already L2-normalized → dot product = cosine
        score = sum(a * b for a, b in zip(emb, proto_vec))
        if score > best_score:
            best_score = score
            best_tier = tier

    # Confidence floor: if best cosine < 0.3, classification is unreliable
    if best_score < 0.3 or best_tier is None:
        if fallback_to_keyword:
            return classify_enforcement_tier(text)
        return "general"

    return best_tier


# ────────────────────────────────────────────────────────────────────────
# Topic extraction (for L2 recall — what to query in graph)
# ────────────────────────────────────────────────────────────────────────

def extract_topics(text: str, max_topics: int = 5) -> List[str]:
    """Extract topic-like noun phrases for graph query.

    Heuristics:
      - 中文连续 2-6 字 (含数字) → 候选
      - 英文 token 长度 >= 3, 不在停用词
      - 排序：长度优先 + 出现次数次之
    """
    # Chinese phrases (2-6 chars, non-punct)
    cn = re.findall(r'[\u4e00-\u9fa5]{2,6}', text)
    # English phrases
    en = re.findall(r'[A-Za-z][A-Za-z0-9_\-\.]{2,30}', text)
    en_stopwords = {"the", "and", "for", "from", "with", "this", "that",
                    "but", "you", "are", "was", "have", "will", "what",
                    "when", "where", "how", "why", "all", "any", "could",
                    "would", "should", "your", "i", "we", "to", "in", "of",
                    "is", "it"}
    en_filtered = [w for w in en if w.lower() not in en_stopwords]

    # Frequency count + sort by (frequency desc, length desc)
    from collections import Counter
    cands = cn + en_filtered
    cnt = Counter(cands)

    ranked = sorted(cnt.items(),
                     key=lambda kv: (-kv[1], -len(kv[0])))
    return [w for w, _ in ranked[:max_topics]]


# ────────────────────────────────────────────────────────────────────────
# MemoryCard v2 builder
# ────────────────────────────────────────────────────────────────────────

def build_lesson_card(
    *,
    narrative: str,
    evidence_quotes: List[str],
    when_start: str,
    when_end: str,
    where_chat_room: str,
    speaker_role: str = "document",
    enforcement_tier: str = "general",
    lesson_subtype: str = "lesson",
    entities: Optional[List[Dict[str, Any]]] = None,
    source: str = "doc",
    salience: Optional[float] = None,
    salience_reason: Optional[str] = None,
    attestation_tier: str = "probe_v2",
    extraction_prompt_sha: str = "lesson_card_v1",
    related_episode: Optional[str] = None,
    extra_metadata: Optional[Dict[str, str]] = None,
    extra_tags: Optional[List[str]] = None,
    open_type_hint: Optional[str] = None,  # default = lesson_subtype
) -> Optional[Dict[str, Any]]:
    """Build a v2 MemoryCard retain payload (JSON-ready) for a lesson.

    Returns None if validation fails (caller should skip + log).

    Salience auto-derived from enforcement_tier if not given:
      hard → 0.85, warn → 0.7, context → 0.55, general → 0.4
    """
    from src.core.memory_card_v2 import MemoryCard, Entity, chat_room_hash

    # Auto-defaults
    if salience is None:
        salience = {"hard": 0.85, "warn": 0.7,
                    "context": 0.55, "general": 0.4}[enforcement_tier]
    if salience_reason is None:
        salience_reason = f"enforcement={enforcement_tier} subtype={lesson_subtype}"[:60]

    # Truncate narrative to schema max
    narrative = (narrative or "").strip()
    if len(narrative) < 30:
        return None
    if len(narrative) > 1200:
        narrative = narrative[:1197] + "..."

    # Truncate evidence quotes
    eq_clean = []
    for q in evidence_quotes[:5]:
        q = (q or "").strip()
        if not q:
            continue
        if len(q) > 200:
            q = q[:197] + "..."
        eq_clean.append(q)
    if not eq_clean:
        eq_clean = [narrative[:120]]

    # Entities
    ents_list: List[Entity] = []
    for e in (entities or []):
        try:
            ents_list.append(Entity(
                canonical_name=e.get("canonical_name", "")[:80],
                role_in_card=e.get("role_in_card", "mentioned"),
                surface_form=e.get("surface_form", e.get("canonical_name", ""))[:80],
                resolution_confidence=e.get("resolution_confidence", "certain"),
            ))
        except Exception:
            continue
    if not ents_list:
        ents_list = [Entity(
            canonical_name="Alice", role_in_card="subject",
            surface_form="Alice", resolution_confidence="certain",
        )]

    # batch_id: deterministic content hash (idempotent re-import)
    content_hash = hashlib.sha256(
        (narrative + "||" + "||".join(eq_clean)).encode("utf-8")
    ).hexdigest()[:16]

    # types — must be from CANONICAL_TYPES
    if enforcement_tier == "hard":
        types_list = ["state", "correction"]
    elif lesson_subtype == "directive":
        types_list = ["state", "commitment"]
    elif lesson_subtype == "correction":
        types_list = ["state", "correction"]
    elif lesson_subtype == "diagnostic":
        types_list = ["state", "question"]
    else:
        types_list = ["state", "report"]

    where_hash = chat_room_hash(where_chat_room)

    # open_type_hint default falls back to subtype if not explicit:
    #   subtype="lesson" / "directive" / "correction" / "diagnostic" → opentype:lesson
    #   subtype="session_summary" → opentype:session_summary (avoid lesson pollution)
    effective_open_type = open_type_hint
    if effective_open_type is None:
        if lesson_subtype == "session_summary":
            effective_open_type = "session_summary"
        else:
            effective_open_type = "lesson"

    try:
        card = MemoryCard(
            narrative=narrative,
            evidence_quotes=eq_clean,
            when_start=when_start,
            when_end=when_end,
            where_chat_room=where_chat_room,
            where_chat_room_hash=where_hash,
            room_tier=1,
            entities=ents_list,
            speaker_role=speaker_role,
            types=types_list,
            salience=salience,
            salience_reason=salience_reason,
            attestation_tier=attestation_tier,
            batch_id=content_hash,
            extraction_prompt_sha=extraction_prompt_sha,
            source=source,
            schema_v=2,
            open_type_hint=effective_open_type,
            related_episode=related_episode,
        )
    except Exception as e:
        print(f"[lesson_card_v1] card build fail: {type(e).__name__}: {e}")
        return None

    payload = card.to_retain_payload()

    # Inject extra metadata (ASCII-only to satisfy hindsight constraint)
    md = payload.setdefault("metadata", {})
    md["enforcement_tier"] = enforcement_tier
    md["lesson_subtype"] = lesson_subtype
    if extra_metadata:
        for k, v in extra_metadata.items():
            if isinstance(v, str) and all(ord(c) < 128 for c in v):
                md[k] = v

    tags = payload.setdefault("tags", [])
    tags.append(f"enforcement:{enforcement_tier}")
    tags.append(f"subtype:{lesson_subtype}")
    if extra_tags:
        for t in extra_tags:
            if t not in tags:
                tags.append(t)

    return payload


# ────────────────────────────────────────────────────────────────────────
# Smoke test
# ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print("=== keyword library loaded ===")
    kw = load_keywords()
    print("tiers:", list(kw["tier_keywords"].keys()))
    print("user lesson triggers:", len(kw["user_lesson_triggers"]["high_signal"]))

    print("\n=== prototypes loaded? ===")
    p = _load_prototypes()
    if p:
        print(f"  exemplars per tier: {p['_meta']['exemplars_per_tier']}")
        print(f"  embedding_dim: {p['_meta']['embedding_dim']}")
    else:
        print("  not built yet (run tools/build_lesson_tier_prototypes.py on Mac)")

    print("\n=== classify_enforcement_tier tests ===")
    for txt in [
        ("HARD RULE: 不要 grep memory/*.md", "hard expected"),
        ("Why: 上次踩了 stdout 编码乱码 → 误判数据坏", "warn expected"),
        ("建议优先用 your-org 而非 Mac", "context expected"),
        ("普通陈述句没有 marker", "general expected"),
    ]:
        print(f"  {txt[1]:30s} → {classify_enforcement_tier(txt[0])}")

    print("\n=== is_lesson_candidate tests ===")
    for msg in [
        "这个查询返回的结果和我预期不一致，需要核对一下",
        "本质上是后台代理没启动",
        "好的",
        "请你彻底解决这个问题",
        "现在的指标比你估计乐观",
    ]:
        m, sub = is_lesson_candidate(msg)
        print(f"  {m} {sub:12s} | {msg[:50]}")

    print("\n=== extract_topics test ===")
    sample = "graph query 单变体仅 1 条 / autologin sysadminctl error 22"
    print(f"  topics: {extract_topics(sample)}")

    print("\n=== build_lesson_card test ===")
    card = build_lesson_card(
        narrative="2026-05-08 端到端验证发现 hindsight-api 启动依赖系统代理。"
                   "代理重启后没自动开 → tiktoken proxy 错 → hindsight-api exit 1。"
                   "修法：plist 加空 HTTP_PROXY/HTTPS_PROXY，直连上游。",
        evidence_quotes=["本质上是代理没开", "ProxyError: Unable to connect"],
        when_start="2026-05-08T19:49:22+08:00",
        when_end="2026-05-08T19:55:00+08:00",
        where_chat_room="claude-code:demo-project",
        speaker_role="self",
        enforcement_tier="warn",
        lesson_subtype="lesson",
        source="claude_code",
    )
    if card:
        print(f"  card built; tags: {card.get('tags', [])[:8]}")
        print(f"  metadata: {list(card.get('metadata', {}).items())[:6]}")
    else:
        print("  card build FAILED")
    sys.exit(0)
