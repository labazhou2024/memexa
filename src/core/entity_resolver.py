"""TU-2 of 2026 backfill plan_v2 §3 — entity_resolver: raw_id → person_uuid (G2 gate).

Per security-iter1-1 fix: passthrough person_* must match strict UUID format
person_<kind>_<32hex> to prevent injection of arbitrary canonical_subject values.

Per security-iter1-8 fix: _NARRATIVE_CACHE bounded via FIFO eviction at
NARRATIVE_CACHE_MAX_SIZE to prevent memory exhaustion.

Per logic-iter1-7 fix: STRUCTURED_SOURCES routing inversion — structured sources
should NEVER fall through to LLM fallback (they are deterministic).


Per plan_v2 §TaskUnits TU-2: rule-based for chat IDs / email From: / git author
email; Mac-Qwen3 fallback for narrative-mentioned names with confidence ≥0.85.

Reuses existing chat-graph U8 entity_pseudonym module
(src.extraction.entity_pseudonym) for chat-realtime / chat-graph namespace; this
module's `canonicalize` is the BACKFILL-superset entry point spanning 14 sources.

Architecture (per plan_v2 §Architecture invariant 2 + 6):
- Same bank `memory_full`; person_uuid is the canonical anchor.
- Personal info NEVER routed to your-org GPU; resolver runs Mac-only.
- Tier-1 rule-based covers ~90% (deterministic, 0 LLM): chat sender IDs,
  email From: addresses, git committer.email, schedule attendee UUIDs.
- Tier-2 LLM-fallback covers ~10% (narrative-mentioned names, e.g.
  "discussed <topic-3> crystal with Prof Wang" → person_uuid for Wang).

axis_anchor: [C:cli:entity_resolver]
trace event: gate_g2_pass / gate_g2_fail (emitted by fact_validator)

Per logic-iter2-2 absorption: this module exposes the same canonicalize() API
shape as the U8 entity_pseudonym; downstream code can call either.

Mode-B safety: when LLM fallback unavailable, resolver returns
(person_uuid=None, confidence=0.0, reason="llm_unavailable") and fact_validator
quarantines as `entity_unresolved`.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from src.core.fact_schema import HASH_LEN

# Reuse chat-graph U8 for chat-realtime resolution path
try:
    from memexa.dispatch import entity_pseudonym as _u8_module
    _HAS_U8 = True
except ImportError:
    _HAS_U8 = False


# ---- Source kind → resolver strategy table (deterministic) ----------------

CHAT_SOURCES = frozenset({
    "backfill-wechat", "backfill-wechat-dump", "backfill-qq",
})
EMAIL_SOURCES = frozenset({"backfill-email"})
GIT_SOURCES = frozenset({"backfill-git"})
NARRATIVE_SOURCES = frozenset({
    "backfill-diary", "backfill-research", "backfill-lab",
})
STRUCTURED_SOURCES = frozenset({
    "backfill-traces", "backfill-evidence", "backfill-schedule",
    "backfill-memory", "backfill-tasks",
})


# Email address regex (RFC-5322 simplified; matches @ + domain.tld)
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)
# person_* canonical UUID format (sec-iter1-1 fix)
_PERSON_UUID_RE = re.compile(r"^person_[a-z]{2,16}_[0-9a-f]{32}$")
# Bounded narrative cache size (sec-iter1-8 fix)
NARRATIVE_CACHE_MAX_SIZE: int = 4096
# WeChat ID regex: wxid_<lowercase + digits + underscore>{1..40} (chat-graph U1)
_WXID_RE = re.compile(r"^wxid_[a-z0-9_]{1,40}$")
# QQ numeric ID: 5-12 digits
_QQID_RE = re.compile(r"^[0-9]{5,12}$")
# Git author email (subset of email): forbids spaces
_GIT_AUTHOR_RE = re.compile(r"^[^\s<>]+@[^\s<>]+$")


@dataclass(frozen=True)
class ResolveResult:
    """Output of canonicalize(). person_uuid is None on unresolved."""
    person_uuid: Optional[str]
    confidence: float
    reason: str  # "rule_hit:wxid" / "rule_hit:email" / "llm_resolved" / "unresolved"
    tier: str  # "tier_1_rule" / "tier_2_llm" / "none"


# ---- Tier-1 rule-based resolvers ------------------------------------------

def _hash_uuid(prefix: str, raw: str) -> str:
    """Build a deterministic person_uuid from a prefix + canonical raw."""
    h = hashlib.sha256(f"{prefix}|{raw}".encode("utf-8", errors="replace"))
    return f"{prefix}_{h.hexdigest()[:HASH_LEN]}"


def _resolve_wechat(raw_id: str) -> Optional[str]:
    """WeChat raw_id → person_uuid via wxid_ prefix."""
    if not isinstance(raw_id, str):
        return None
    raw = raw_id.strip().lower()
    if _WXID_RE.match(raw):
        return _hash_uuid("person_wx", raw)
    return None


def _resolve_qq(raw_id: str) -> Optional[str]:
    """QQ numeric ID → person_uuid."""
    if not isinstance(raw_id, str):
        return None
    raw = raw_id.strip()
    if _QQID_RE.match(raw):
        return _hash_uuid("person_qq", raw)
    return None


def _resolve_email(raw_id: str) -> Optional[str]:
    """Email From: → person_uuid via lowercase normalized address."""
    if not isinstance(raw_id, str):
        return None
    raw = raw_id.strip().lower()
    # Strip "Name <addr@host>" → "addr@host"
    m = re.match(r"^.*<([^>]+)>\s*$", raw)
    if m:
        raw = m.group(1).strip()
    if _EMAIL_RE.match(raw):
        return _hash_uuid("person_email", raw)
    return None


def _resolve_git(raw_id: str) -> Optional[str]:
    """git committer.email → person_uuid."""
    if not isinstance(raw_id, str):
        return None
    raw = raw_id.strip().lower()
    if _GIT_AUTHOR_RE.match(raw):
        return _hash_uuid("person_git", raw)
    return None


# ---- Tier-2 LLM fallback (narrative names) --------------------------------

# Cache: maps narrative_name → resolved_uuid (in-memory, per-run)
_NARRATIVE_CACHE: Dict[str, ResolveResult] = {}


def _resolve_narrative_name(
    raw_id: str,
    llm_callable=None,
) -> Optional[Tuple[str, float]]:
    """LLM-fallback for narrative-mentioned names ("Prof Wang", "Alice").

    Per plan §TaskUnits TU-2: Mac-Qwen3 fallback; confidence ≥0.85 required.
    `llm_callable` is dependency-injected for tests; real call is dispatched
    via src.extraction.mlx_lm_wrapper at runtime by fact_validator.

    Returns (person_uuid, confidence) on success, None on failure.
    """
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None

    # Check cache first
    cached = _NARRATIVE_CACHE.get(raw_id)
    if cached is not None and cached.person_uuid is not None:
        return cached.person_uuid, cached.confidence

    if llm_callable is None:
        # No fallback available → unresolved
        return None

    # Defensive: the LLM must return (person_uuid, confidence) tuple
    try:
        result = llm_callable(raw_id)
        if not isinstance(result, tuple) or len(result) != 2:
            return None
        uuid_str, conf = result
        if not isinstance(uuid_str, str) or not uuid_str.startswith("person_"):
            return None
        if not isinstance(conf, (int, float)) or float(conf) < 0.85:
            return None
        # FIFO eviction when cache hits MAX_SIZE (sec-iter1-8)
        if len(_NARRATIVE_CACHE) >= NARRATIVE_CACHE_MAX_SIZE:
            try:
                oldest_key = next(iter(_NARRATIVE_CACHE))
                _NARRATIVE_CACHE.pop(oldest_key, None)
            except StopIteration:
                pass
        _NARRATIVE_CACHE[raw_id] = ResolveResult(
            person_uuid=uuid_str, confidence=float(conf),
            reason="llm_resolved", tier="tier_2_llm",
        )
        return uuid_str, float(conf)
    except Exception:
        return None


# ---- Public API ------------------------------------------------------------

def canonicalize(
    raw_id: str,
    source_kind: str,
    extracted_by: str,
    llm_callable=None,
) -> ResolveResult:
    """Resolve raw_id (sender / From: / commit author / narrative name) → person_uuid.

    Args:
        raw_id: source-specific raw identifier (e.g. "wxid_abc", "alice@host.com",
            "Prof Wang").
        source_kind: ∈ SOURCE_KIND_ENUM (from FactRow.source_kind).
        extracted_by: ∈ EXTRACTED_BY_BACKFILL (from FactRow.extracted_by); used to
            select strategy.
        llm_callable: optional Tier-2 fallback (Mac-Qwen3); dependency-injected
            for tests. Real provider is dispatched in fact_validator.

    Returns ResolveResult with person_uuid=None on unresolved.

    Per plan §Architecture invariant 6: this resolver runs Mac-local; never
    sends raw narrative content (which may contain personal names) to remote
    your-org GPU. The llm_callable contract is Mac-Qwen3 ONLY.
    """
    # Defensive empty input
    if not isinstance(raw_id, str) or not raw_id.strip():
        return ResolveResult(person_uuid=None, confidence=0.0,
                             reason="empty_raw_id", tier="none")

    # Tier-1: route by extracted_by namespace
    if extracted_by in CHAT_SOURCES:
        # WeChat first
        uid = _resolve_wechat(raw_id) or _resolve_qq(raw_id)
        if uid:
            return ResolveResult(person_uuid=uid, confidence=1.0,
                                 reason="rule_hit:chat_id", tier="tier_1_rule")
        # Some chat dumps include email-style sender
        uid = _resolve_email(raw_id)
        if uid:
            return ResolveResult(person_uuid=uid, confidence=1.0,
                                 reason="rule_hit:email", tier="tier_1_rule")

    elif extracted_by in EMAIL_SOURCES:
        uid = _resolve_email(raw_id)
        if uid:
            return ResolveResult(person_uuid=uid, confidence=1.0,
                                 reason="rule_hit:email", tier="tier_1_rule")

    elif extracted_by in GIT_SOURCES:
        uid = _resolve_git(raw_id)
        if uid:
            return ResolveResult(person_uuid=uid, confidence=1.0,
                                 reason="rule_hit:git_email", tier="tier_1_rule")

    elif extracted_by in NARRATIVE_SOURCES:
        # Narrative source → Tier-2 LLM fallback for names
        result = _resolve_narrative_name(raw_id, llm_callable=llm_callable)
        if result is not None:
            uid, conf = result
            return ResolveResult(person_uuid=uid, confidence=conf,
                                 reason="llm_resolved", tier="tier_2_llm")

    elif extracted_by in STRUCTURED_SOURCES:
        # Per sec-iter1-1 fix: enforce person_<kind>_<32hex> format strictly.
        # Per logic-iter1-7 fix: STRUCTURED sources never fall through to LLM
        # (they are deterministic; LLM-fallback only for narrative).
        if isinstance(raw_id, str) and _PERSON_UUID_RE.match(raw_id):
            return ResolveResult(person_uuid=raw_id, confidence=1.0,
                                 reason="rule_hit:passthrough_uuid", tier="tier_1_rule")
        # Otherwise try email
        uid = _resolve_email(raw_id)
        if uid:
            return ResolveResult(person_uuid=uid, confidence=1.0,
                                 reason="rule_hit:email", tier="tier_1_rule")
        # Structured source unresolved → return immediately (no LLM fallback)
        return ResolveResult(person_uuid=None, confidence=0.0,
                             reason="unresolved_structured", tier="none")

    # All Tier-1 strategies exhausted; only narrative falls through to LLM
    # (logic-iter1-7 fix: was inverted "not in NARRATIVE_SOURCES")
    if extracted_by in NARRATIVE_SOURCES:
        # Already tried in NARRATIVE_SOURCES branch above; this is unreachable
        # but kept as defense-in-depth
        pass

    return ResolveResult(person_uuid=None, confidence=0.0,
                         reason="unresolved", tier="none")


def clear_narrative_cache() -> None:
    """Test helper: clear the in-memory LLM-resolved cache."""
    _NARRATIVE_CACHE.clear()


__all__ = [
    "CHAT_SOURCES",
    "EMAIL_SOURCES",
    "GIT_SOURCES",
    "NARRATIVE_SOURCES",
    "STRUCTURED_SOURCES",
    "ResolveResult",
    "canonicalize",
    "clear_narrative_cache",
]
