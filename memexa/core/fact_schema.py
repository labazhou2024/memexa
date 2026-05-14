"""TU-1 of 2026 backfill plan_v2 §3 — single-source FactRow schema (G1 gate input).

Per plan_v2 §Architecture invariant 1 + 2: 100% reuses 9 existing modules public API
(graph_memory_v2 / hindsight_outbox / chat.metadata_builder / dispatch.entity_pseudonym /
dispatch.keystone_outbox.scrub_pii); same bank `memory_full`; `extracted_by` enum
isolates namespace from chat-realtime.

This module is the **superset schema** for all 14 backfill sources (WeChat / WeChat
dump / .claude/data/traces.jsonl / evidence.jsonl / task trace.jsonl /
桌面/日志 2026.docx / schedule_data.json / research/ / lab_reports/ / memexa git /
your-org GPU / QQ NapCat / Email / memory frontmatter / task_dirs synthesis).

Existing chat.metadata_builder._build_chat_metadata (10 fields) is the CHAT-REALTIME
variant; this module's `FactRow` is the GENERAL backfill variant with 15 fields
covering provenance (source_offset / extraction_prompt_sha), governance (tentative /
corroborated_by / contradicted_by), and bi-temporal (valid_at / invalidated_at).

Schema invariants (per plan §TaskUnits TU-1):
- 15 fields strict; missing any → G1 reject
- `extracted_by` ∈ EXTRACTED_BY_BACKFILL (separate namespace from chat-realtime)
- `source_offset` is replay-anchor for G3 attestation against raw source bytes
- `valid_at` ∈ [2026-01-01, now+1d]; G4 enforces
- `corroborated_by` and `contradicted_by` are sets of (other_fact_id) tuples

axis_anchor: [C:cli:fact_schema] [C:schema:fact_row_v1]
trace event: gate_g1_pass / gate_g1_fail (emitted by fact_validator)

Per security-iter1 absorption: HMAC-related fields (extraction_prompt_sha) are
sha256(prompt_template_text)[:32] — providing replay-able prompt-anchor
without requiring secret-key access at validation time.

Per logic-iter2-3 absorption: G3 attestation is computed against
sha256(source_file_bytes_at_offset)[:32] — NOT against `content` (which G8 will
mutate). The `source_offset` field is the anchor.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, FrozenSet, Optional, Tuple

# Reuse Closure A unified hash length (per memory feedback_hash_len_unified +
# chat.metadata_builder HASH_LEN=32 + parallel autopilot Closure A plan_v3)
from memexa.chat.metadata_builder import HASH_LEN as _CHAT_HASH_LEN

HASH_LEN: int = _CHAT_HASH_LEN  # 32 hex chars = 128 bit preimage floor

# Backfill `extracted_by` enum — DISTINCT from chat-realtime's
# EXTRACTED_BY_CANONICAL (which contains "chat-realtime", "memory_full",
# "self-evolution", "code-review", "manual"). This namespace isolation lets
# downstream filters distinguish chat-realtime from backfill ingestion.
EXTRACTED_BY_BACKFILL: FrozenSet[str] = frozenset({
    "backfill-wechat",       # TU-7 WeChat Q1+Q2 driver
    "backfill-wechat-dump",  # TU-7 historical dump
    "backfill-qq",           # TU-8 QQ NapCat
    "backfill-email",        # TU-8 email
    "backfill-traces",       # TU-9 .claude/data/traces.jsonl + task trace.jsonl
    "backfill-evidence",     # TU-9 evidence.jsonl
    "backfill-schedule",     # TU-9 schedule_data.json
    "backfill-git",          # TU-9 memexa git history
    "backfill-memory",       # TU-9 memory/*.md frontmatter
    "backfill-tasks",        # TU-9 .claude/harness/tasks/ synthesis
    "backfill-diary",        # TU-10 桌面/日志 2026.docx
    "backfill-research",     # TU-10 research/
    "backfill-lab",          # TU-10 lab_reports/
    "backfill-ustc-gpu",     # TU-10 your-org GPU academic-only
})

# Source kind enum — distinguishes raw source format for G3 attestation replay
SOURCE_KIND_ENUM: FrozenSet[str] = frozenset({
    "sqlite",      # WeChat DB
    "jsonl",       # traces / evidence / outbox
    "docx",        # diary
    "json",        # schedule_data
    "git_log",     # commits
    "md_frontmatter",  # memory/*.md
    "txt",         # research notes / lab reports
    "imap",        # email
    "napcat_http", # QQ
    "ssh_remote",  # your-org GPU
    "filesystem",  # task_dirs walk
})

# Predicate vocabulary (G7 gate; closed enum per logic-iter2 + sec-iter1)
PREDICATE_VOCAB: FrozenSet[str] = frozenset({
    # Identity / kinship
    "is_a", "knows", "works_with", "advised_by", "advises",
    # Communication
    "sent_message_to", "received_message_from", "discussed",
    # Calendar / events
    "attended", "scheduled_for", "deadline_at", "completed",
    # Research / academic
    "authored", "cited", "studies", "experiments_on", "computed",
    # Git / code
    "committed", "modified_file", "fixed_bug", "added_feature",
    # State / belief
    "believes", "decided", "preferred",
    # Backfill-specific
    "ingested_from",
    "validated_by",
    "withdrew",
})

# Bi-temporal bounds (G4 gate input)
VALID_AT_MIN_ISO: str = "2026-01-01T00:00:00+00:00"
VALID_AT_MAX_FUTURE_DAYS: int = 1  # now + 1 day grace

# TU-Phase2-1: paired_eval attestation field valid values.
# "paired_v1"        — dual-model (Qwen3+Gemma) agreement quorum passed.
# "single_qwen_v1"   — single-model (Qwen3) extract; legacy / grandfather path.
# "arbiter_27b"      — TU-E1: Gemma-3-27B sole arbiter resolved a disagreement.
# "legacy_unverified"— TU-F2: historical row pre-paired_eval; retroactive flag.
# "none"             — attestation not performed; triggers gate fail when required.
PAIRED_EVAL_ATTESTED_VALID: frozenset = frozenset({
    "paired_v1",
    "single_qwen_v1",
    "arbiter_27b",
    "legacy_unverified",
    "none",
})


# ----------------------------------------------------------------------------
# Helpers (small, reused)
# ----------------------------------------------------------------------------

def _is_hex_str(s: Any, min_len: int = HASH_LEN) -> bool:
    """True if s is a lowercase hex string of length >= min_len."""
    return (
        isinstance(s, str)
        and len(s) >= min_len
        and bool(re.fullmatch(r"[0-9a-f]+", s))
    )


def _is_iso_datetime(s: Any) -> bool:
    """True if s is parseable as ISO-8601 datetime (timezone optional)."""
    if not isinstance(s, str):
        return False
    try:
        # Accept Z suffix per ISO-8601
        normalized = s.replace("Z", "+00:00")
        datetime.fromisoformat(normalized)
        return True
    except (ValueError, TypeError):
        return False


def fact_id(
    source_kind: str,
    source_offset: str,
    canonical_subject: str,
    predicate: str,
    object_: str,
) -> str:
    """Deterministic fact id from 5 anchor fields. sha256(...)[:HASH_LEN].

    Per security-iter1-3 fix: use length-prefixed encoding to prevent pipe-delimiter
    collision attacks. (a|b, c) and (a, b|c) used to produce same hash; now they
    produce distinct hashes via per-field length prefix.
    """
    parts = [str(x or "") for x in (source_kind, source_offset, canonical_subject, predicate, object_)]
    raw = "".join(f"{len(p)}:{p}\x1f" for p in parts)  # \x1f = ASCII Unit Separator
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:HASH_LEN]


def prompt_sha(prompt_text: str) -> str:
    """sha256(prompt_template)[:HASH_LEN] for extraction_prompt_sha field."""
    if not isinstance(prompt_text, str):
        prompt_text = str(prompt_text or "")
    return hashlib.sha256(prompt_text.encode("utf-8", errors="replace")).hexdigest()[:HASH_LEN]


# ----------------------------------------------------------------------------
# FactRow dataclass — 15-field strict schema
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class FactRow:
    """Backfill superset FactRow. 15 fields, frozen (hashable, immutable post-init).

    Per plan_v2 §TaskUnits TU-1: 14-字段 (the docstring count was off-by-one;
    actual list is 15). The 15th comes from explicit listing of corroborated_by
    AND contradicted_by AND tentative.

    Field semantics:
      id: deterministic sha256[:32] of (source_kind, source_offset, subject,
          predicate, object) — replay-able from raw source.
      source_kind: ∈ SOURCE_KIND_ENUM; identifies parser to use.
      source_offset: replay anchor — opaque string interpreted by parser
          (e.g. for sqlite: "table:msg,row_id:12345"; for jsonl: byte offset).
      extracted_by: ∈ EXTRACTED_BY_BACKFILL; namespace isolator.
      extraction_prompt_sha: sha256[:32] of extraction prompt template text.
      confidence: float ∈ [0.0, 1.0]; <0.6 → tentative=True (G5 input).
      valid_at: ISO-8601 datetime; fact-time when assertion holds (G4 input).
      invalidated_at: ISO-8601 datetime OR None; bi-temporal withdrawal.
      canonical_subject: G2 entity_resolver output (person_uuid OR room_hash).
      predicate: ∈ PREDICATE_VOCAB (G7 enforces).
      object: free-form scrubbed string (G8 has scrubbed; ≤500 chars).
      episode_id: chat episode anchor for chain-building (G5 corroboration via
          adjacency in TU-4 L1 event_chain).
      tentative: bool; True if confidence<0.6 OR <2 source corroboration.
      corroborated_by: tuple of fact_id (other facts that confirm this).
      contradicted_by: tuple of fact_id (G6 contradiction queue input).
    """
    id: str
    source_kind: str
    source_offset: str
    extracted_by: str
    extraction_prompt_sha: str
    confidence: float
    valid_at: str
    invalidated_at: Optional[str]
    canonical_subject: str
    predicate: str
    object: str
    episode_id: str
    tentative: bool
    corroborated_by: Tuple[str, ...] = field(default_factory=tuple)
    contradicted_by: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        """Plain-dict serialization (asdict + tuple→list)."""
        d = asdict(self)
        d["corroborated_by"] = list(self.corroborated_by)
        d["contradicted_by"] = list(self.contradicted_by)
        return d


# ----------------------------------------------------------------------------
# G1 gate: schema validator (the public function fact_validator.py imports)
# ----------------------------------------------------------------------------

# Strict 15-field set for G1 schema check
_REQUIRED_FIELDS: FrozenSet[str] = frozenset({
    "id", "source_kind", "source_offset", "extracted_by",
    "extraction_prompt_sha", "confidence", "valid_at", "invalidated_at",
    "canonical_subject", "predicate", "object", "episode_id",
    "tentative", "corroborated_by", "contradicted_by",
})


def is_valid_fact(d: Any) -> Tuple[bool, str]:
    """G1 gate: strict 15-field schema + per-field type/value check.

    Returns (ok, reason_or_empty). Used by fact_validator.run_gates step 1.

    Per logic-iter2-1: all reasons are "schema_*" namespaced for G1 quarantine.

    LIVE-tested by tests/test_fact_validation_gates.py with 1 pass + 1 negative
    per gate (G1 reject test = 8 paths covered here).
    """
    if not isinstance(d, dict):
        return False, "schema_not_dict"

    # 1. Field-set exact match (no missing, no extra)
    actual = set(d.keys())
    missing = _REQUIRED_FIELDS - actual
    extra = actual - _REQUIRED_FIELDS
    if missing:
        return False, f"schema_missing_fields:{','.join(sorted(missing))}"
    if extra:
        return False, f"schema_extra_fields:{','.join(sorted(extra))}"

    # 2. id field: sha256[:HASH_LEN] hex
    if not _is_hex_str(d["id"], min_len=HASH_LEN):
        return False, "schema_id_not_hex"

    # 3. source_kind enum
    if d["source_kind"] not in SOURCE_KIND_ENUM:
        return False, f"schema_source_kind_unknown:{d['source_kind']!r}"

    # 4. source_offset non-empty string
    if not isinstance(d["source_offset"], str) or not d["source_offset"]:
        return False, "schema_source_offset_empty"

    # 5. extracted_by enum (BACKFILL namespace isolation)
    if d["extracted_by"] not in EXTRACTED_BY_BACKFILL:
        return False, f"schema_extracted_by_not_backfill:{d['extracted_by']!r}"

    # 6. extraction_prompt_sha: hex
    if not _is_hex_str(d["extraction_prompt_sha"], min_len=HASH_LEN):
        return False, "schema_extraction_prompt_sha_not_hex"

    # 7. confidence: float ∈ [0.0, 1.0]
    conf = d["confidence"]
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        return False, "schema_confidence_not_numeric"
    if not (0.0 <= float(conf) <= 1.0):
        return False, f"schema_confidence_out_of_range:{conf}"

    # 8. valid_at: ISO-8601
    if not _is_iso_datetime(d["valid_at"]):
        return False, "schema_valid_at_not_iso"

    # 9. invalidated_at: None OR ISO-8601
    inv = d["invalidated_at"]
    if inv is not None and not _is_iso_datetime(inv):
        return False, "schema_invalidated_at_not_iso_or_none"

    # 10. canonical_subject: non-empty string
    if not isinstance(d["canonical_subject"], str) or not d["canonical_subject"]:
        return False, "schema_canonical_subject_empty"

    # 11. predicate: enum
    if d["predicate"] not in PREDICATE_VOCAB:
        return False, f"schema_predicate_not_in_vocab:{d['predicate']!r}"

    # 12. object: string, len ≤ 500
    obj = d["object"]
    if not isinstance(obj, str):
        return False, "schema_object_not_string"
    if len(obj) > 500:
        return False, f"schema_object_too_long:{len(obj)}"

    # 13. episode_id: hex
    if not _is_hex_str(d["episode_id"], min_len=HASH_LEN):
        return False, "schema_episode_id_not_hex"

    # 14. tentative: bool
    if not isinstance(d["tentative"], bool):
        return False, "schema_tentative_not_bool"

    # 15. corroborated_by / contradicted_by: list/tuple of strings
    for k in ("corroborated_by", "contradicted_by"):
        v = d[k]
        if not isinstance(v, (list, tuple)):
            return False, f"schema_{k}_not_seq"
        for item in v:
            if not isinstance(item, str):
                return False, f"schema_{k}_item_not_string"

    # TU-Phase2-1: optional field paired_eval_attested — value must be in valid set if present.
    if "paired_eval_attested" in d:
        val = d["paired_eval_attested"]
        if val not in PAIRED_EVAL_ATTESTED_VALID:
            return False, f"schema_paired_eval_attested_invalid:{val!r}"

    return True, ""


# ----------------------------------------------------------------------------
# G4 helper (referenced by fact_validator; centralized here for schema cohesion)
# ----------------------------------------------------------------------------

def is_valid_at_in_bounds(valid_at_iso: str, now: Optional[datetime] = None) -> bool:
    """G4 gate helper: valid_at ∈ [2026-01-01, now + 1 day]."""
    if not _is_iso_datetime(valid_at_iso):
        return False
    try:
        ts = datetime.fromisoformat(valid_at_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Per logic-iter1-8 fix: drop dead code (was double-assigning upper)
    from datetime import timedelta
    lower = datetime.fromisoformat(VALID_AT_MIN_ISO)
    upper = now + timedelta(days=VALID_AT_MAX_FUTURE_DAYS)
    return lower <= ts <= upper


# ----------------------------------------------------------------------------
# Public API surface
# ----------------------------------------------------------------------------

__all__ = [
    "HASH_LEN",
    "EXTRACTED_BY_BACKFILL",
    "SOURCE_KIND_ENUM",
    "PREDICATE_VOCAB",
    "VALID_AT_MIN_ISO",
    "VALID_AT_MAX_FUTURE_DAYS",
    "PAIRED_EVAL_ATTESTED_VALID",
    "FactRow",
    "fact_id",
    "prompt_sha",
    "is_valid_fact",
    "is_valid_at_in_bounds",
]
