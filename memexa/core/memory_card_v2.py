"""MemoryCard schema v2 for L0 event layer.

Spec: docs/l0_v5/MASTER_PLAN.md §2

Key v2 deltas vs v1:
1. Entity gains canonical_id + surface_form + resolution_confidence + resolution_evidence
2. New IdentityAssertion class (反哺 Identity Manifest)
3. New TimeResolution class (模糊时间 → 绝对时间, 显式 unresolved)
4. New RelationAssertion class (how_known + shared_contexts 信号源)
5. MemoryCard gains identity_assertions[], time_resolutions[],
   relation_assertions[], unresolved_references[]
6. schema_v = 2
7. Header markers bumped: MEMORYCARD_V2_HEADER_BEGIN/END
8. retain payload all-ASCII metadata (PG SQL_ASCII constraint), CJK b64 encoded

Frozen dataclasses for hash-stable identity. Strict validation in __post_init__.
"""
from __future__ import annotations

import base64 as _b64
import dataclasses
import hashlib
import json as _json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

# ────────────────────────── Type vocabulary ──────────────────────────

CANONICAL_TYPES = frozenset({
    "announcement",
    "commitment",
    "question",
    "decision",
    "correction",
    "opinion",
    "report",
    "share",
    "interaction",
    "state",
})

ROOM_TIERS = frozenset({1, 2, 3})

SPEAKER_ROLES = frozenset({"self", "relay", "third_party", "mixed", "document"})

ATTESTATION_TIERS = frozenset({
    "paired_v2",
    "single_qwen3_v2",
    "single_gemma4_31b_v2",
    "local_arbiter_v2",
    "ds_fallback_v2",
    "rag_assisted_v2",   # phase B (RAG-enabled extraction)
    "probe_v2",
})

ENTITY_ROLES = frozenset({"subject", "object", "mentioned", "audience"})

RESOLUTION_CONFIDENCE = frozenset({"certain", "inferred", "ambiguous", "unresolved"})

ASSERTION_RELATIONS = frozenset({
    "is_self",
    "is_aka",
    "is_in_org",
    "owns",
    "works_with",
    "introduced_by",   # how_known signal
    "met_at",          # how_known signal
    "is_public_figure",
})

RELATION_TYPES = frozenset({
    "introduced",      # A 介绍 B 认识 C  (how_known)
    "co_member",
    "co_event",
    "kinship",
    "romantic",
    "professional",
    "friendship",
})

VALID_SOURCES = frozenset({
    "wechat", "qq", "email", "sms", "doc", "folder", "git",
    "schedule", "probe", "claude_code",
    # 2026-05-12: audio source (recording pen + Mac mic) — 6th v5 source
    "audio",
    # 2026-05-12: browser variants emitted by existing pipelines
    "browser", "browser_session", "browser_search",
})


# ────────────────────────── Validation helpers ──────────────────────────

def _is_iso_dt(s: str) -> bool:
    """Loose ISO 8601 sniff: must contain 'T' and look date-ish."""
    if not isinstance(s, str):
        return False
    return "T" in s and re.match(r"^\d{4}-\d{2}-\d{2}T", s) is not None


def _ensure_ascii(s: str) -> bool:
    return isinstance(s, str) and all(ord(c) < 128 for c in s)


# ────────────────────────── Sub-types ──────────────────────────

@dataclass(frozen=True)
class Entity:
    """A single entity reference within a card.

    surface_form: original text used in chat ('我' / 'Alice' / '@Bob').
    canonical_id: stable ref into identity_manifest (e.g. 'person_alice').
                  None when manifest cannot bind (then resolution=ambiguous/unresolved).
    """
    canonical_name: str
    role_in_card: str  # one of ENTITY_ROLES
    surface_form: str
    canonical_id: Optional[str] = None
    sender_wxid_hash: Optional[str] = None
    resolution_confidence: str = "certain"  # one of RESOLUTION_CONFIDENCE
    resolution_evidence: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.canonical_name:
            raise ValueError("Entity.canonical_name must be non-empty")
        if self.role_in_card not in ENTITY_ROLES:
            raise ValueError(f"Bad role_in_card: {self.role_in_card!r}")
        if not self.surface_form:
            raise ValueError("Entity.surface_form must be non-empty")
        if self.resolution_confidence not in RESOLUTION_CONFIDENCE:
            raise ValueError(
                f"Entity.resolution_confidence must be in "
                f"{sorted(RESOLUTION_CONFIDENCE)}; got {self.resolution_confidence!r}"
            )


@dataclass(frozen=True)
class IdentityAssertion:
    """A self/other identity assertion extracted from chat (反哺 manifest)."""
    subject_wxid_hash: str
    asserted_relation: str  # one of ASSERTION_RELATIONS
    asserted_value: str
    quote: str
    confidence: str  # certain/inferred/ambiguous
    subject_canonical_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.subject_wxid_hash:
            raise ValueError("IdentityAssertion.subject_wxid_hash required")
        if self.asserted_relation not in ASSERTION_RELATIONS:
            raise ValueError(
                f"asserted_relation must be in {sorted(ASSERTION_RELATIONS)}"
            )
        if not self.asserted_value:
            raise ValueError("asserted_value must be non-empty")
        if not self.quote:
            raise ValueError("quote required (evidence)")
        if self.confidence not in {"certain", "inferred", "ambiguous"}:
            raise ValueError(f"confidence must be certain/inferred/ambiguous")


@dataclass(frozen=True)
class TimeResolution:
    """A fuzzy time expression resolved (or explicitly marked unresolved).

    surface_form: '上周', '过年那阵子', 'this Wednesday'.
    resolved_*: ISO-8601, None when confidence=='unresolved'.
    anchor_message_ts: which message ts was used as anchor.
    resolution_method: 'weekday_offset' / 'calendar:春节' / 'session_calendar' / ...
    """
    surface_form: str
    resolved_start: Optional[str]
    resolved_end: Optional[str]
    anchor_message_ts: str
    confidence: str  # certain/inferred/ambiguous/unresolved
    resolution_method: str

    def __post_init__(self) -> None:
        if not self.surface_form:
            raise ValueError("TimeResolution.surface_form required")
        if self.confidence not in RESOLUTION_CONFIDENCE:
            raise ValueError(
                f"confidence must be in {sorted(RESOLUTION_CONFIDENCE)}"
            )
        if self.confidence != "unresolved":
            if not self.resolved_start or not _is_iso_dt(self.resolved_start):
                raise ValueError(
                    "non-unresolved TimeResolution must have ISO resolved_start; "
                    f"got {self.resolved_start!r}"
                )
            if not self.resolved_end or not _is_iso_dt(self.resolved_end):
                raise ValueError(
                    "non-unresolved TimeResolution must have ISO resolved_end"
                )
        if not self.anchor_message_ts or not _is_iso_dt(self.anchor_message_ts):
            raise ValueError("anchor_message_ts must be ISO 8601")
        if not self.resolution_method:
            raise ValueError("resolution_method required (auditable)")


@dataclass(frozen=True)
class RelationAssertion:
    """A directed person/org relation asserted in chat (反哺 manifest.relations).

    Used for how_known + shared_contexts inference.
    """
    person_a: str  # canonical_id
    person_b: str  # canonical_id
    relation_type: str  # one of RELATION_TYPES
    context: str  # free text scene/occasion (e.g. 'your-org 量子实验室')
    quote: str
    confidence: str  # certain/inferred/ambiguous

    def __post_init__(self) -> None:
        if not self.person_a or not self.person_b:
            raise ValueError("RelationAssertion: both ends required")
        if self.person_a == self.person_b:
            raise ValueError("RelationAssertion: a==b not allowed")
        if self.relation_type not in RELATION_TYPES:
            raise ValueError(
                f"relation_type must be in {sorted(RELATION_TYPES)}"
            )
        if not self.context:
            raise ValueError("RelationAssertion.context required")
        if not self.quote:
            raise ValueError("quote required (evidence)")
        if self.confidence not in {"certain", "inferred", "ambiguous"}:
            raise ValueError("confidence must be certain/inferred/ambiguous")


# ────────────────────────── Memory Card v2 ──────────────────────────

HEADER_BEGIN = "【MEMORYCARD_V2_HEADER_BEGIN】"
HEADER_END = "【MEMORYCARD_V2_HEADER_END】"


@dataclass(frozen=True)
class MemoryCard:
    # Required: primary representation
    narrative: str
    evidence_quotes: List[str]

    # Required: spatiotemporal anchor
    when_start: str
    when_end: str
    where_chat_room: str
    where_chat_room_hash: str
    room_tier: int

    # Required: entities + role
    entities: List[Entity]
    speaker_role: str

    # Required: type + salience
    types: List[str]
    salience: float
    salience_reason: str

    # Required: source/audit
    attestation_tier: str
    batch_id: str
    extraction_prompt_sha: str
    source: str = "wechat"
    schema_v: int = 2

    # Optional relation links
    open_type_hint: Optional[str] = None
    supersedes: List[str] = field(default_factory=list)
    answers: Optional[str] = None
    related_episode: Optional[str] = None

    # v2 NEW
    identity_assertions: List[IdentityAssertion] = field(default_factory=list)
    time_resolutions: List[TimeResolution] = field(default_factory=list)
    relation_assertions: List[RelationAssertion] = field(default_factory=list)
    unresolved_references: List[str] = field(default_factory=list)

    # ────────── strict validation ──────────

    def __post_init__(self) -> None:
        # narrative
        if not isinstance(self.narrative, str) or not self.narrative.strip():
            raise ValueError("narrative must be non-empty str")
        n = len(self.narrative)
        if n < 30:
            raise ValueError(f"narrative too short ({n} chars; min 30)")
        if n > 1200:
            raise ValueError(f"narrative too long ({n} chars; max 1200)")

        # evidence_quotes
        if not self.evidence_quotes or not isinstance(self.evidence_quotes, list):
            raise ValueError("evidence_quotes must be non-empty list")
        if len(self.evidence_quotes) > 5:
            raise ValueError("evidence_quotes max 5 items")
        for q in self.evidence_quotes:
            if not isinstance(q, str) or not q.strip():
                raise ValueError("each evidence_quote must be non-empty str")
            if len(q) > 200:
                raise ValueError(f"evidence_quote too long ({len(q)} > 200)")

        # times
        for t_field in ("when_start", "when_end"):
            v = getattr(self, t_field)
            if not _is_iso_dt(v):
                raise ValueError(f"{t_field} must be ISO 8601 with T; got {v!r}")

        # tier
        if self.room_tier not in ROOM_TIERS:
            raise ValueError(f"room_tier must be in {sorted(ROOM_TIERS)}")

        # speaker_role
        if self.speaker_role not in SPEAKER_ROLES:
            raise ValueError(f"speaker_role must be in {sorted(SPEAKER_ROLES)}")

        # types
        if not self.types or not isinstance(self.types, list):
            raise ValueError("types must be non-empty list")
        if len(self.types) > 4:
            raise ValueError("types max 4")
        for t in self.types:
            if t not in CANONICAL_TYPES:
                raise ValueError(
                    f"type {t!r} not in CANONICAL_TYPES; "
                    f"if novel use types=['state'] + open_type_hint"
                )

        # salience
        if not isinstance(self.salience, (int, float)):
            raise ValueError("salience must be numeric")
        s = float(self.salience)
        if s < 0.0 or s > 1.0:
            raise ValueError(f"salience out of range [0,1]: {s}")
        if not self.salience_reason or len(self.salience_reason) > 60:
            raise ValueError("salience_reason required and ≤60 chars")

        # attestation
        if self.attestation_tier not in ATTESTATION_TIERS:
            raise ValueError(
                f"attestation_tier {self.attestation_tier!r} not recognized"
            )

        # entities
        if not isinstance(self.entities, list):
            raise ValueError("entities must be list")

        # where
        if not self.where_chat_room or not self.where_chat_room_hash:
            raise ValueError("where_chat_room AND where_chat_room_hash both required")

        # batch_id / source
        if not self.batch_id:
            raise ValueError("batch_id required for traceability")
        if self.source not in VALID_SOURCES:
            raise ValueError(f"unknown source: {self.source}")

        # schema_v
        if self.schema_v != 2:
            raise ValueError(f"MemoryCard expects schema_v=2; got {self.schema_v}")

        # extraction_prompt_sha
        if not self.extraction_prompt_sha:
            raise ValueError("extraction_prompt_sha required")

    # ────────── helpers ──────────

    def to_retain_payload(self) -> Dict[str, Any]:
        """Convert to Hindsight retain item.

        Constraints (enforced):
        - PG SQL_ASCII: tags + metadata MUST be ASCII-only
        - CJK content goes into `content` field (chunked text, not JSON encoded)
        - CJK metadata values use base64
        - HEADER markers V2 enable round-trip via from_retain_content
        """
        d = dataclasses.asdict(self)
        full_json = _json.dumps(d, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"))

        # Embed full structured card as part of content (CJK-safe)
        content = (
            HEADER_BEGIN + "\n"
            + full_json
            + "\n" + HEADER_END + "\n\n"
            + self.narrative
        )

        # ASCII-only tags
        def _entity_tag_hash(name: str) -> str:
            return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]

        tags = [
            "kind:event",
            f"source:{self.source}",
            f"tier:{self.room_tier}",
            f"room:{self.where_chat_room_hash[:16]}",
            f"speaker:{self.speaker_role}",
            f"attest:{self.attestation_tier}",
            f"schema:v{self.schema_v}",
            *[f"type:{t}" for t in self.types],
            *[f"entity:{_entity_tag_hash(e.canonical_name)}"
              for e in self.entities if e.canonical_name],
            *[f"canon:{e.canonical_id}" for e in self.entities
              if e.canonical_id],
        ]

        if self.related_episode:
            tags.append(f"episode:{self.related_episode}")
        if self.open_type_hint:
            slug = "".join(c if c.isascii() and c.isalnum() else "_"
                           for c in self.open_type_hint)[:40]
            if slug:
                tags.append(f"opentype:{slug}")

        # Has unresolved markers? expose as searchable tag
        if self.unresolved_references:
            tags.append("has:unresolved_anaphora")
        unresolved_time = any(t.confidence == "unresolved"
                              for t in self.time_resolutions)
        if unresolved_time:
            tags.append("has:unresolved_time")

        # Phase B marker
        if self.attestation_tier == "rag_assisted_v2":
            tags.append("rag_assisted")

        # entities_full b64
        entities_full = [
            {
                "canonical_name": e.canonical_name,
                "canonical_id": e.canonical_id,
                "role_in_card": e.role_in_card,
                "surface_form": e.surface_form,
                "resolution_confidence": e.resolution_confidence,
                "sender_wxid_hash": e.sender_wxid_hash,
            }
            for e in self.entities
        ]
        entities_full_b64 = _b64.b64encode(
            _json.dumps(entities_full, ensure_ascii=False,
                        separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

        # entities canonical_id csv (ASCII filter key)
        canonical_ids_csv = ",".join(
            e.canonical_id for e in self.entities if e.canonical_id
        )

        # entities surface→canonical hash csv (ASCII filter)
        entities_csv_hash = ",".join(
            _entity_tag_hash(e.canonical_name)
            for e in self.entities if e.canonical_name
        )

        # salience_reason b64 (CJK-safe ASCII)
        salience_reason_b64 = _b64.b64encode(
            self.salience_reason.encode("utf-8")
        ).decode("ascii") if self.salience_reason else ""

        metadata = {
            "schema_v": str(self.schema_v),
            "salience": f"{self.salience:.3f}",
            "salience_reason_b64": salience_reason_b64,
            "speaker_role": self.speaker_role,
            "where_chat_room_hash": self.where_chat_room_hash,
            "room_tier": str(self.room_tier),
            "when_start": self.when_start,
            "when_end": self.when_end,
            "types_csv": ",".join(self.types),
            "attestation_tier": self.attestation_tier,
            "batch_id": self.batch_id,
            "extraction_prompt_sha": self.extraction_prompt_sha,
            "source": self.source,
            "evidence_quotes_count": str(len(self.evidence_quotes)),
            "n_entities": str(len(self.entities)),
            "entities_hash_csv": entities_csv_hash,
            "entities_canonical_ids_csv": canonical_ids_csv,
            "entities_full_b64": entities_full_b64,
            "n_identity_assertions": str(len(self.identity_assertions)),
            "n_time_resolutions": str(len(self.time_resolutions)),
            "n_relation_assertions": str(len(self.relation_assertions)),
            "n_unresolved": str(len(self.unresolved_references)),
        }
        if self.related_episode:
            metadata["related_episode"] = self.related_episode
        if self.supersedes:
            metadata["supersedes_csv"] = ",".join(self.supersedes)
        if self.answers:
            metadata["answers"] = self.answers

        # Defensive ASCII check
        for k, v in metadata.items():
            if not (_ensure_ascii(str(k)) and _ensure_ascii(str(v))):
                raise ValueError(
                    f"metadata must be ASCII-only (PG SQL_ASCII); "
                    f"offending key={k!r} value={str(v)[:50]!r}"
                )

        for t in tags:
            if not _ensure_ascii(t):
                raise ValueError(f"tag must be ASCII-only; got {t!r}")

        return {
            "content": content,
            "tags": tags,
            "metadata": metadata,
            "document_id": self.related_episode or self.batch_id,
        }

    @classmethod
    def from_retain_content(cls, content: str) -> "MemoryCard":
        """Reverse of to_retain_payload: parse embedded JSON header from
        recalled `content` field. Robust to V2/V1 markers (V1 read-only).
        """
        # Try V2 header first (current schema)
        i0 = content.find(HEADER_BEGIN)
        i1 = content.find(HEADER_END)
        if i0 >= 0 and i1 >= 0 and i1 > i0:
            json_str = content[i0 + len(HEADER_BEGIN):i1].strip()
            full = _json.loads(json_str)

            # Sub-types
            entities = [Entity(**e) for e in full.get("entities", [])]
            full["entities"] = entities
            full["identity_assertions"] = [
                IdentityAssertion(**a) for a in full.get("identity_assertions", [])
            ]
            full["time_resolutions"] = [
                TimeResolution(**t) for t in full.get("time_resolutions", [])
            ]
            full["relation_assertions"] = [
                RelationAssertion(**r) for r in full.get("relation_assertions", [])
            ]
            return cls(**full)

        # V1 fallback (read-only legacy)
        v1_begin = "【MEMORYCARD_V1_HEADER_BEGIN】"
        v1_end = "【MEMORYCARD_V1_HEADER_END】"
        i0 = content.find(v1_begin)
        i1 = content.find(v1_end)
        if i0 >= 0 and i1 >= 0:
            raise ValueError(
                "V1 schema content detected. Run migration v1→v2 separately; "
                "this method only reads v2."
            )

        raise ValueError("MemoryCard header markers not found in content")

    def card_id(self) -> str:
        """Stable ID derived from narrative + evidence_quotes + batch_id."""
        h = hashlib.sha256()
        h.update(self.narrative.encode("utf-8"))
        h.update(b"\x00")
        for q in self.evidence_quotes:
            h.update(q.encode("utf-8"))
            h.update(b"\x00")
        h.update(self.batch_id.encode("utf-8"))
        return h.hexdigest()[:24]


# ────────────────────────── Standalone helpers ──────────────────────────

def chat_room_hash(display_name: str) -> str:
    """Stable 32-char hash for chat_room display_name (mojibake-safe)."""
    safe = display_name.encode("utf-8", errors="replace")
    return hashlib.sha256(safe).hexdigest()[:32]


def wxid_hash(wxid: str) -> str:
    """Stable 16-char wxid hash for use in Entity.sender_wxid_hash."""
    return hashlib.sha256(wxid.encode("utf-8")).hexdigest()[:16]


def entity_tag_hash(canonical_name: str) -> str:
    """Stable 16-char hash for entity:<hash> tag (matches to_retain_payload)."""
    return hashlib.sha256(canonical_name.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "Entity", "IdentityAssertion", "TimeResolution", "RelationAssertion",
    "MemoryCard", "HEADER_BEGIN", "HEADER_END",
    "CANONICAL_TYPES", "ROOM_TIERS", "SPEAKER_ROLES", "ATTESTATION_TIERS",
    "ENTITY_ROLES", "RESOLUTION_CONFIDENCE", "ASSERTION_RELATIONS",
    "RELATION_TYPES", "VALID_SOURCES",
    "chat_room_hash", "wxid_hash", "entity_tag_hash",
]
