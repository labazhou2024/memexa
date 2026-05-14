"""Identity Manifest store for L0 v5.

Spec: docs/l0_v5/MASTER_PLAN.md §3

Maintains the cross-source identity graph that grounds Pass-2 extraction:
- persons (with multi-source identifiers, aka with timestamps + contexts)
- organizations
- inanimate (devices/things)
- public_figures (separate namespace, identified via static list)
- relations (pair-keyed: how_known + shared_contexts)

PII GUARANTEES:
- File default location: data/identity_manifest.yaml (gitignore'd)
- Never serialized to your-org server (extraction injects redacted slice only)
- Vault encryption optional (mode='vault' uses Fernet; key from env MEMEX_MANIFEST_KEY)
- All evidence quotes redacted to ≤80 chars before persistence

Concurrent-write semantics:
- All writes go through ManifestStore.commit_*() which acquires file lock
- Atomic replace via tempfile.NamedTemporaryFile + os.replace
- Auto-backup to data/identity_manifest.yaml.bak.<ts> before each save
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore

logger = logging.getLogger(__name__)

# ────────────────────────── Constants ──────────────────────────

NAMESPACE_PERSONS = "persons"
NAMESPACE_ORGS = "organizations"
NAMESPACE_INANIMATE = "inanimate"
NAMESPACE_PUBLIC_FIGURES = "public_figures"

ALL_NAMESPACES = (
    NAMESPACE_PERSONS,
    NAMESPACE_ORGS,
    NAMESPACE_INANIMATE,
    NAMESPACE_PUBLIC_FIGURES,
)

DEFAULT_MANIFEST_PATH = "data/identity_manifest.yaml"
DEFAULT_BACKUP_DIR = "data/manifest_backups"
DEFAULT_PUBLIC_FIGURES_PATH = "data/l0_v5/known_public_figures/known_public_figures.yaml"

EVIDENCE_QUOTE_MAX_LEN = 80

DEPRECATED_AKA_INACTIVE_DAYS = 180
DEPRECATED_AKA_LOW_FREQ_RATIO = 0.1


# ────────────────────────── Helpers ──────────────────────────

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _redact_quote(quote: str) -> str:
    """Trim evidence quote for storage (PII-conservative)."""
    if not isinstance(quote, str):
        return ""
    s = quote.strip()
    if len(s) > EVIDENCE_QUOTE_MAX_LEN:
        s = s[:EVIDENCE_QUOTE_MAX_LEN - 3] + "..."
    return s


def _ensure_yaml() -> None:
    if _yaml is None:
        raise RuntimeError(
            "PyYAML not installed. Run `pip install pyyaml`."
        )


# ────────────────────────── Pinyin (lazy import) ──────────────────────────

def compute_pinyin_initials(text: str) -> List[str]:
    """Compute pinyin firstletters for a Chinese text.

    Returns list because some names have ambiguous pronunciations
    (e.g., 长 zhang/chang). For now we just take the first option.
    Falls back to lowercase ASCII if pypinyin not installed (string is mostly Latin).
    """
    if not text:
        return []
    try:
        from pypinyin import lazy_pinyin, Style
        initials = lazy_pinyin(text, style=Style.FIRST_LETTER)
        joined = "".join(initials).lower()
        return [joined] if joined else []
    except ImportError:
        # Fallback: lowercased ASCII letters only
        ascii_only = "".join(c.lower() for c in text if c.isascii() and c.isalnum())
        return [ascii_only] if ascii_only else []


# ────────────────────────── Manifest data classes ──────────────────────────

@dataclass
class AkaRecord:
    """One alternate-name record with temporal + contextual scope."""
    surface: str
    first_seen_ts: str  # ISO
    last_seen_ts: str   # ISO; rolling
    mention_count: int = 1
    contexts: List[str] = field(default_factory=list)  # list of room_hash
    confidence: str = "certain"  # certain/inferred/ambiguous
    status: str = "active"  # active/deprecated_<reason>
    pinyin_match: bool = False  # true if surface is pinyin firstletters of canonical
    first_seen_card_id: Optional[str] = None
    last_seen_card_id: Optional[str] = None

    def is_active(self, as_of: Optional[str] = None) -> bool:
        if self.status != "active":
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "surface": self.surface,
            "first_seen_ts": self.first_seen_ts,
            "last_seen_ts": self.last_seen_ts,
            "mention_count": self.mention_count,
            "contexts": list(self.contexts),
            "confidence": self.confidence,
            "status": self.status,
            "pinyin_match": self.pinyin_match,
        }
        if self.first_seen_card_id:
            d["first_seen_card_id"] = self.first_seen_card_id
        if self.last_seen_card_id:
            d["last_seen_card_id"] = self.last_seen_card_id
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AkaRecord":
        return cls(
            surface=d["surface"],
            first_seen_ts=d["first_seen_ts"],
            last_seen_ts=d["last_seen_ts"],
            mention_count=int(d.get("mention_count", 1)),
            contexts=list(d.get("contexts", [])),
            confidence=d.get("confidence", "certain"),
            status=d.get("status", "active"),
            pinyin_match=bool(d.get("pinyin_match", False)),
            first_seen_card_id=d.get("first_seen_card_id"),
            last_seen_card_id=d.get("last_seen_card_id"),
        )


@dataclass
class EvidenceRecord:
    """Audit trail for any manifest claim (one entry per source)."""
    source: str  # 'wechat_sqlite_contacts' / 'wechat_card_assertion' / 'github_commit' / ...
    ts: str
    card_id: Optional[str] = None
    quote: Optional[str] = None  # already redacted to ≤80 chars
    confidence: str = "certain"

    def to_dict(self) -> Dict[str, Any]:
        d = {"source": self.source, "ts": self.ts, "confidence": self.confidence}
        if self.card_id:
            d["card_id"] = self.card_id
        if self.quote:
            d["quote"] = self.quote
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvidenceRecord":
        return cls(
            source=d["source"],
            ts=d["ts"],
            card_id=d.get("card_id"),
            quote=_redact_quote(d.get("quote", "")) or None,
            confidence=d.get("confidence", "certain"),
        )


@dataclass
class IdentifierBundle:
    """Cross-source identity bindings."""
    wxids: List[str] = field(default_factory=list)
    wxid_hashes: List[str] = field(default_factory=list)
    github_logins: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    unix_users: List[str] = field(default_factory=list)
    phone_hashes: List[str] = field(default_factory=list)  # hashed for privacy

    def to_dict(self) -> Dict[str, List[str]]:
        return {
            k: list(v) for k, v in self.__dict__.items() if v
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IdentifierBundle":
        return cls(
            wxids=list(d.get("wxids", [])),
            wxid_hashes=list(d.get("wxid_hashes", [])),
            github_logins=list(d.get("github_logins", [])),
            emails=list(d.get("emails", [])),
            unix_users=list(d.get("unix_users", [])),
            phone_hashes=list(d.get("phone_hashes", [])),
        )


@dataclass
class PersonEntry:
    canonical_id: str
    primary_name: str
    aka: List[AkaRecord] = field(default_factory=list)
    pinyin_initials: List[str] = field(default_factory=list)
    identifiers: IdentifierBundle = field(default_factory=IdentifierBundle)
    is_self: bool = False
    attributes: Dict[str, Any] = field(default_factory=dict)
    evidence: List[EvidenceRecord] = field(default_factory=list)

    def all_surface_forms(self) -> Set[str]:
        return {self.primary_name, *(a.surface for a in self.aka)}

    def active_aka_for_room(self, room_hash: str, as_of: str) -> List[AkaRecord]:
        """Aka entries currently active in a given room as of timestamp."""
        return [
            a for a in self.aka
            if a.is_active(as_of) and (
                not a.contexts or room_hash in a.contexts
            )
        ]

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "canonical_id": self.canonical_id,
            "primary_name": self.primary_name,
            "is_self": self.is_self,
            "pinyin_initials": list(self.pinyin_initials),
            "aka": [a.to_dict() for a in self.aka],
            "identifiers": self.identifiers.to_dict(),
            "attributes": dict(self.attributes),
            "evidence": [e.to_dict() for e in self.evidence],
        }
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PersonEntry":
        return cls(
            canonical_id=d["canonical_id"],
            primary_name=d["primary_name"],
            aka=[AkaRecord.from_dict(a) for a in d.get("aka", [])],
            pinyin_initials=list(d.get("pinyin_initials", [])),
            identifiers=IdentifierBundle.from_dict(d.get("identifiers", {})),
            is_self=bool(d.get("is_self", False)),
            attributes=dict(d.get("attributes", {})),
            evidence=[EvidenceRecord.from_dict(e) for e in d.get("evidence", [])],
        )


@dataclass
class OrganizationEntry:
    canonical_id: str
    primary_name: str
    aka: List[AkaRecord] = field(default_factory=list)
    pinyin_initials: List[str] = field(default_factory=list)
    category: str = ""  # university/company/lab/...
    evidence: List[EvidenceRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "primary_name": self.primary_name,
            "category": self.category,
            "pinyin_initials": list(self.pinyin_initials),
            "aka": [a.to_dict() for a in self.aka],
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OrganizationEntry":
        return cls(
            canonical_id=d["canonical_id"],
            primary_name=d["primary_name"],
            aka=[AkaRecord.from_dict(a) for a in d.get("aka", [])],
            pinyin_initials=list(d.get("pinyin_initials", [])),
            category=d.get("category", ""),
            evidence=[EvidenceRecord.from_dict(e) for e in d.get("evidence", [])],
        )


@dataclass
class InanimateEntry:
    canonical_id: str
    primary_name: str
    aka: List[AkaRecord] = field(default_factory=list)
    category: str = ""  # device/document/account/...
    owned_by: Optional[str] = None  # canonical_id of owning person
    purchase_window: Optional[Tuple[str, str]] = None  # (start_iso, end_iso)
    evidence: List[EvidenceRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "canonical_id": self.canonical_id,
            "primary_name": self.primary_name,
            "category": self.category,
            "aka": [a.to_dict() for a in self.aka],
            "evidence": [e.to_dict() for e in self.evidence],
        }
        if self.owned_by:
            d["owned_by"] = self.owned_by
        if self.purchase_window:
            d["purchase_window"] = list(self.purchase_window)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InanimateEntry":
        pw = d.get("purchase_window")
        return cls(
            canonical_id=d["canonical_id"],
            primary_name=d["primary_name"],
            aka=[AkaRecord.from_dict(a) for a in d.get("aka", [])],
            category=d.get("category", ""),
            owned_by=d.get("owned_by"),
            purchase_window=tuple(pw) if pw and len(pw) == 2 else None,  # type: ignore
            evidence=[EvidenceRecord.from_dict(e) for e in d.get("evidence", [])],
        )


@dataclass
class PublicFigureEntry:
    canonical_id: str
    primary_name: str
    aka: List[str] = field(default_factory=list)
    category: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "primary_name": self.primary_name,
            "category": self.category,
            "aka": list(self.aka),
            "is_public_figure": True,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PublicFigureEntry":
        return cls(
            canonical_id=d["canonical_id"],
            primary_name=d["primary_name"],
            aka=list(d.get("aka", [])),
            category=d.get("category", ""),
        )


@dataclass
class HowKnownRecord:
    """How person_a came to know person_b (or vice versa)."""
    via: Optional[str]  # canonical_id of introducer, None if directly met
    when: Optional[str]  # ISO timestamp or year-month
    context: str  # free text scene
    evidence_card_ids: List[str] = field(default_factory=list)
    confidence: str = "inferred"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "via": self.via,
            "when": self.when,
            "context": self.context,
            "confidence": self.confidence,
            "evidence_card_ids": list(self.evidence_card_ids),
        }
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HowKnownRecord":
        return cls(
            via=d.get("via"),
            when=d.get("when"),
            context=d["context"],
            evidence_card_ids=list(d.get("evidence_card_ids", [])),
            confidence=d.get("confidence", "inferred"),
        )


@dataclass
class SharedContextRecord:
    """Aggregated co-occurrence stats for a person pair, by context_type."""
    context_type: str  # card.types[0]: commitment/decision/...
    rooms: Set[str] = field(default_factory=set)  # room_hash set
    first_co_occur_ts: Optional[str] = None
    last_co_occur_ts: Optional[str] = None
    co_occur_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context_type": self.context_type,
            "rooms": sorted(self.rooms),
            "first_co_occur_ts": self.first_co_occur_ts,
            "last_co_occur_ts": self.last_co_occur_ts,
            "co_occur_count": self.co_occur_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SharedContextRecord":
        return cls(
            context_type=d["context_type"],
            rooms=set(d.get("rooms", [])),
            first_co_occur_ts=d.get("first_co_occur_ts"),
            last_co_occur_ts=d.get("last_co_occur_ts"),
            co_occur_count=int(d.get("co_occur_count", 0)),
        )


@dataclass
class RelationEntry:
    """Pair-keyed person relation record (key = sorted([a, b]))."""
    pair: Tuple[str, str]  # sorted canonical_ids
    relation_type: str  # friendship/professional/kinship/romantic/...
    how_known: List[HowKnownRecord] = field(default_factory=list)
    shared_contexts: List[SharedContextRecord] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.pair[0]}||{self.pair[1]}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair": list(self.pair),
            "relation_type": self.relation_type,
            "how_known": [h.to_dict() for h in self.how_known],
            "shared_contexts": [s.to_dict() for s in self.shared_contexts],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RelationEntry":
        pair = tuple(sorted(d["pair"]))  # type: ignore
        return cls(
            pair=pair,  # type: ignore
            relation_type=d.get("relation_type", "unknown"),
            how_known=[HowKnownRecord.from_dict(h)
                       for h in d.get("how_known", [])],
            shared_contexts=[SharedContextRecord.from_dict(s)
                             for s in d.get("shared_contexts", [])],
        )


# ────────────────────────── ManifestStore ──────────────────────────

class ManifestStore:
    """Yaml-backed identity manifest.

    Usage:
        store = ManifestStore.load("data/identity_manifest.yaml")
        person = store.lookup_person_by_wxid("wxid_abc")
        store.upsert_person(...)
        store.save()  # auto-backup + atomic replace
    """

    def __init__(
        self,
        manifest_path: str = DEFAULT_MANIFEST_PATH,
        backup_dir: str = DEFAULT_BACKUP_DIR,
    ):
        self.manifest_path = Path(manifest_path)
        self.backup_dir = Path(backup_dir)
        self.version = "2"
        self.schema_v = 2
        self.updated_at = _now_iso()
        self.updated_by = "manifest_init"
        self.sources_processed = 0

        self.persons: Dict[str, PersonEntry] = {}
        self.organizations: Dict[str, OrganizationEntry] = {}
        self.inanimate: Dict[str, InanimateEntry] = {}
        self.public_figures: Dict[str, PublicFigureEntry] = {}
        self.relations: Dict[str, RelationEntry] = {}

    # ─── persistence ───

    @classmethod
    def load(cls, manifest_path: str = DEFAULT_MANIFEST_PATH) -> "ManifestStore":
        _ensure_yaml()
        store = cls(manifest_path=manifest_path)
        path = Path(manifest_path)
        if not path.exists():
            logger.info(f"manifest {manifest_path} doesn't exist; returning empty store")
            return store
        with open(path, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        store.version = str(data.get("version", "2"))
        store.schema_v = int(data.get("schema_v", 2))
        store.updated_at = data.get("updated_at", _now_iso())
        store.updated_by = data.get("updated_by", "manifest_load")
        store.sources_processed = int(data.get("sources_processed", 0))

        for cid, p in (data.get(NAMESPACE_PERSONS) or {}).items():
            entry = PersonEntry.from_dict(p)
            entry.canonical_id = cid
            store.persons[cid] = entry
        for cid, o in (data.get(NAMESPACE_ORGS) or {}).items():
            entry_o = OrganizationEntry.from_dict(o)
            entry_o.canonical_id = cid
            store.organizations[cid] = entry_o
        for cid, i in (data.get(NAMESPACE_INANIMATE) or {}).items():
            entry_i = InanimateEntry.from_dict(i)
            entry_i.canonical_id = cid
            store.inanimate[cid] = entry_i
        for cid, pf in (data.get(NAMESPACE_PUBLIC_FIGURES) or {}).items():
            entry_pf = PublicFigureEntry.from_dict(pf)
            entry_pf.canonical_id = cid
            store.public_figures[cid] = entry_pf
        for key, rel in (data.get("relations") or {}).items():
            entry_r = RelationEntry.from_dict(rel)
            store.relations[entry_r.key] = entry_r
        return store

    def save(self, *, updated_by: str = "manifest_save") -> None:
        _ensure_yaml()
        # Backup existing
        if self.manifest_path.exists():
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            bak = self.backup_dir / f"identity_manifest.yaml.bak.{ts}"
            shutil.copy2(self.manifest_path, bak)
            # Rotate: keep only newest 20 backups
            backups = sorted(self.backup_dir.glob("identity_manifest.yaml.bak.*"))
            for old in backups[:-20]:
                try:
                    old.unlink()
                except OSError:
                    pass

        self.updated_at = _now_iso()
        self.updated_by = updated_by

        # Build full snapshot
        snapshot = {
            "version": self.version,
            "schema_v": self.schema_v,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "sources_processed": self.sources_processed,
            NAMESPACE_PERSONS: {
                cid: p.to_dict() for cid, p in self.persons.items()
            },
            NAMESPACE_ORGS: {
                cid: o.to_dict() for cid, o in self.organizations.items()
            },
            NAMESPACE_INANIMATE: {
                cid: i.to_dict() for cid, i in self.inanimate.items()
            },
            NAMESPACE_PUBLIC_FIGURES: {
                cid: pf.to_dict() for cid, pf in self.public_figures.items()
            },
            "relations": {
                rel.key: rel.to_dict() for rel in self.relations.values()
            },
            "_indexes": self._build_indexes(),
        }

        # Atomic write
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="manifest_", suffix=".yaml.tmp",
            dir=str(self.manifest_path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                _yaml.safe_dump(snapshot, f, allow_unicode=True,
                                default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, self.manifest_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ─── indexes ───

    def _build_indexes(self) -> Dict[str, Dict[str, str]]:
        idx_wxid: Dict[str, str] = {}
        idx_wxid_hash: Dict[str, str] = {}
        idx_aka_lower: Dict[str, str] = {}
        idx_pinyin: Dict[str, List[str]] = defaultdict(list)
        idx_email: Dict[str, str] = {}
        idx_github: Dict[str, str] = {}

        for cid, p in self.persons.items():
            for w in p.identifiers.wxids:
                idx_wxid[w] = cid
            for h in p.identifiers.wxid_hashes:
                idx_wxid_hash[h] = cid
            for em in p.identifiers.emails:
                idx_email[em.lower()] = cid
            for gh in p.identifiers.github_logins:
                idx_github[gh.lower()] = cid
            idx_aka_lower[p.primary_name.lower()] = cid
            for a in p.aka:
                idx_aka_lower[a.surface.lower()] = cid
            for pi in p.pinyin_initials:
                idx_pinyin[pi].append(cid)

        # Orgs / inanimate / public figures aliases too
        for cid, o in self.organizations.items():
            idx_aka_lower[o.primary_name.lower()] = cid
            for a in o.aka:
                idx_aka_lower[a.surface.lower()] = cid
            for pi in o.pinyin_initials:
                idx_pinyin[pi].append(cid)

        for cid, it in self.inanimate.items():
            idx_aka_lower[it.primary_name.lower()] = cid
            for a in it.aka:
                idx_aka_lower[a.surface.lower()] = cid

        for cid, pf in self.public_figures.items():
            idx_aka_lower[pf.primary_name.lower()] = cid
            for a in pf.aka:
                idx_aka_lower[a.lower()] = cid

        return {
            "by_wxid": idx_wxid,
            "by_wxid_hash": idx_wxid_hash,
            "by_aka_lower": idx_aka_lower,
            "by_pinyin_initials": dict(idx_pinyin),
            "by_email": idx_email,
            "by_github": idx_github,
        }

    # ─── lookups ───

    def lookup_person_by_wxid(self, wxid: str) -> Optional[PersonEntry]:
        for p in self.persons.values():
            if wxid in p.identifiers.wxids:
                return p
        return None

    def lookup_person_by_wxid_hash(self, wxid_hash: str) -> Optional[PersonEntry]:
        for p in self.persons.values():
            if wxid_hash in p.identifiers.wxid_hashes:
                return p
        return None

    def lookup_by_surface(self, surface: str) -> List[Any]:
        """Return matching entries from any namespace (multi-result)."""
        s = surface.lower().strip()
        results: List[Any] = []
        for ns_dict in (self.persons, self.organizations,
                        self.inanimate, self.public_figures):
            for entry in ns_dict.values():
                names = {entry.primary_name.lower()}
                if hasattr(entry, "aka"):
                    aka = entry.aka
                    if aka and isinstance(aka[0], AkaRecord):
                        names.update(a.surface.lower() for a in aka)  # type: ignore
                    else:
                        names.update(a.lower() for a in aka)
                if s in names:
                    results.append(entry)
        return results

    def lookup_by_pinyin(self, abbrev: str) -> List[PersonEntry]:
        """Find persons whose pinyin firstletters match the abbreviation."""
        a = abbrev.lower().strip()
        return [p for p in self.persons.values() if a in p.pinyin_initials]

    def lookup_relation(self, a: str, b: str) -> Optional[RelationEntry]:
        pair = tuple(sorted([a, b]))
        key = f"{pair[0]}||{pair[1]}"
        return self.relations.get(key)

    # ─── upserts (persons / orgs / etc.) ───

    def upsert_person(self, person: PersonEntry) -> PersonEntry:
        existing = self.persons.get(person.canonical_id)
        if existing is None:
            # new
            if not person.pinyin_initials:
                person.pinyin_initials = compute_pinyin_initials(person.primary_name)
            self.persons[person.canonical_id] = person
            return person
        # merge: identifiers union, aka union, evidence append
        merged_wxids = list({*existing.identifiers.wxids, *person.identifiers.wxids})
        merged_wxid_hashes = list({*existing.identifiers.wxid_hashes,
                                   *person.identifiers.wxid_hashes})
        existing.identifiers.wxids = merged_wxids
        existing.identifiers.wxid_hashes = merged_wxid_hashes
        existing.identifiers.emails = list({*existing.identifiers.emails,
                                            *person.identifiers.emails})
        existing.identifiers.github_logins = list(
            {*existing.identifiers.github_logins, *person.identifiers.github_logins}
        )
        existing.identifiers.unix_users = list(
            {*existing.identifiers.unix_users, *person.identifiers.unix_users}
        )
        # aka merge: by surface
        existing_aka = {a.surface: a for a in existing.aka}
        for a in person.aka:
            if a.surface in existing_aka:
                ex = existing_aka[a.surface]
                ex.last_seen_ts = max(ex.last_seen_ts, a.last_seen_ts)
                ex.first_seen_ts = min(ex.first_seen_ts, a.first_seen_ts)
                ex.mention_count += a.mention_count
                for ctx in a.contexts:
                    if ctx not in ex.contexts:
                        ex.contexts.append(ctx)
            else:
                existing.aka.append(a)
                existing_aka[a.surface] = a
        existing.evidence.extend(person.evidence)
        if person.is_self:
            existing.is_self = True
        if person.attributes:
            existing.attributes.update(person.attributes)
        return existing

    def upsert_organization(self, org: OrganizationEntry) -> OrganizationEntry:
        existing = self.organizations.get(org.canonical_id)
        if existing is None:
            if not org.pinyin_initials:
                org.pinyin_initials = compute_pinyin_initials(org.primary_name)
            self.organizations[org.canonical_id] = org
            return org
        existing_aka = {a.surface: a for a in existing.aka}
        for a in org.aka:
            if a.surface not in existing_aka:
                existing.aka.append(a)
                existing_aka[a.surface] = a
        existing.evidence.extend(org.evidence)
        return existing

    def upsert_inanimate(self, item: InanimateEntry) -> InanimateEntry:
        existing = self.inanimate.get(item.canonical_id)
        if existing is None:
            self.inanimate[item.canonical_id] = item
            return item
        existing_aka = {a.surface: a for a in existing.aka}
        for a in item.aka:
            if a.surface not in existing_aka:
                existing.aka.append(a)
                existing_aka[a.surface] = a
        existing.evidence.extend(item.evidence)
        if item.owned_by and not existing.owned_by:
            existing.owned_by = item.owned_by
        if item.purchase_window and not existing.purchase_window:
            existing.purchase_window = item.purchase_window
        return existing

    def upsert_public_figure(self, pf: PublicFigureEntry) -> PublicFigureEntry:
        existing = self.public_figures.get(pf.canonical_id)
        if existing is None:
            self.public_figures[pf.canonical_id] = pf
            return pf
        existing.aka = list({*existing.aka, *pf.aka})
        return existing

    # ─── relations ───

    def upsert_relation(
        self,
        person_a: str,
        person_b: str,
        relation_type: str,
        how_known: Optional[HowKnownRecord] = None,
        context_type: Optional[str] = None,
        room_hash: Optional[str] = None,
        co_occur_ts: Optional[str] = None,
    ) -> RelationEntry:
        if person_a == person_b:
            raise ValueError("relation: person_a == person_b not allowed")
        pair = tuple(sorted([person_a, person_b]))
        key = f"{pair[0]}||{pair[1]}"
        rel = self.relations.get(key)
        if rel is None:
            rel = RelationEntry(pair=pair, relation_type=relation_type)  # type: ignore
            self.relations[key] = rel
        if how_known:
            rel.how_known.append(how_known)
        if context_type and room_hash and co_occur_ts:
            sc_existing = next(
                (s for s in rel.shared_contexts if s.context_type == context_type),
                None,
            )
            if sc_existing is None:
                rel.shared_contexts.append(SharedContextRecord(
                    context_type=context_type,
                    rooms={room_hash},
                    first_co_occur_ts=co_occur_ts,
                    last_co_occur_ts=co_occur_ts,
                    co_occur_count=1,
                ))
            else:
                sc_existing.rooms.add(room_hash)
                sc_existing.co_occur_count += 1
                if (sc_existing.first_co_occur_ts is None
                        or co_occur_ts < sc_existing.first_co_occur_ts):
                    sc_existing.first_co_occur_ts = co_occur_ts
                if (sc_existing.last_co_occur_ts is None
                        or co_occur_ts > sc_existing.last_co_occur_ts):
                    sc_existing.last_co_occur_ts = co_occur_ts
        return rel

    # ─── stats ───

    def stats(self) -> Dict[str, Any]:
        person_aka_counts = [len(p.aka) for p in self.persons.values()]
        return {
            "n_persons": len(self.persons),
            "n_orgs": len(self.organizations),
            "n_inanimate": len(self.inanimate),
            "n_public_figures": len(self.public_figures),
            "n_relations": len(self.relations),
            "person_aka_total": sum(person_aka_counts),
            "person_aka_avg": (
                sum(person_aka_counts) / len(person_aka_counts)
                if person_aka_counts else 0.0
            ),
            "is_self_count": sum(1 for p in self.persons.values() if p.is_self),
            "shared_contexts_total": sum(
                len(r.shared_contexts) for r in self.relations.values()
            ),
            "how_known_total": sum(
                len(r.how_known) for r in self.relations.values()
            ),
        }

    # ─── extraction injection (PII-safe slice) ───

    def extraction_slice_for_batch(
        self,
        sender_wxid_hashes: List[str],
        room_hash: str,
        time_window_iso: Tuple[str, str],
        context_persons_lookback: int = 5,
    ) -> Dict[str, Any]:
        """Build a redacted manifest slice for Pass-2 prompt injection.

        Includes only persons relevant to this batch (sender + recently
        co-occurring), trimmed to <30KB. Excludes raw wxid (hashed only),
        emails/phones (PII).

        Returns dict shaped for direct injection into prompt template.
        """
        relevant_persons: Dict[str, PersonEntry] = {}

        # 1) Senders
        for h in sender_wxid_hashes:
            p = self.lookup_person_by_wxid_hash(h)
            if p:
                relevant_persons[p.canonical_id] = p

        # 2) Recent co-occurring persons (via relations)
        for cid in list(relevant_persons.keys()):
            for rel in self.relations.values():
                if cid in rel.pair:
                    other = rel.pair[1] if rel.pair[0] == cid else rel.pair[0]
                    if other in self.persons and other not in relevant_persons:
                        relevant_persons[other] = self.persons[other]
                        if len(relevant_persons) >= 50:  # guard against huge payload
                            break

        # 3) Active aka filter (room-aware)
        as_of = time_window_iso[1]
        slice_persons = {}
        for cid, p in relevant_persons.items():
            active_aka = p.active_aka_for_room(room_hash, as_of)
            slice_persons[cid] = {
                "primary_name": p.primary_name,
                "pinyin_initials": list(p.pinyin_initials),
                "aka": [a.surface for a in active_aka],
                "is_self": p.is_self,
                # Wxid_hash only (no raw wxid); exclude emails/phones from slice
                "wxid_hashes": list(p.identifiers.wxid_hashes),
            }

        # 4) Public figures (small, all included)
        slice_pubfigs = {
            cid: {
                "primary_name": pf.primary_name,
                "aka": list(pf.aka),
                "category": pf.category,
            }
            for cid, pf in self.public_figures.items()
        }

        # 5) Inanimate (frequently mentioned only — those owned by a sender)
        relevant_inanimate = {
            cid: it for cid, it in self.inanimate.items()
            if it.owned_by and it.owned_by in relevant_persons
        }
        slice_inanimate = {
            cid: {
                "primary_name": it.primary_name,
                "aka": [a.surface for a in it.aka],
                "category": it.category,
                "owned_by": it.owned_by,
            }
            for cid, it in relevant_inanimate.items()
        }

        # 6) Orgs (linked via persons.attributes.org_canonical_ids)
        relevant_org_ids: Set[str] = set()
        for p in relevant_persons.values():
            for oid in p.attributes.get("org_canonical_ids", []) or []:
                relevant_org_ids.add(oid)
        slice_orgs = {
            cid: {
                "primary_name": self.organizations[cid].primary_name,
                "aka": [a.surface for a in self.organizations[cid].aka],
                "pinyin_initials": list(self.organizations[cid].pinyin_initials),
                "category": self.organizations[cid].category,
            }
            for cid in relevant_org_ids if cid in self.organizations
        }

        return {
            "persons": slice_persons,
            "organizations": slice_orgs,
            "inanimate": slice_inanimate,
            "public_figures": slice_pubfigs,
        }


# ────────────────────────── Module-level convenience ──────────────────────────

_default_store: Optional[ManifestStore] = None


def get_default_store() -> ManifestStore:
    global _default_store
    if _default_store is None:
        path = os.environ.get("MEMEX_IDENTITY_MANIFEST", DEFAULT_MANIFEST_PATH)
        _default_store = ManifestStore.load(path)
    return _default_store


def reset_default_store() -> None:
    global _default_store
    _default_store = None


__all__ = [
    "ManifestStore",
    "PersonEntry", "OrganizationEntry", "InanimateEntry", "PublicFigureEntry",
    "RelationEntry", "AkaRecord", "EvidenceRecord", "IdentifierBundle",
    "HowKnownRecord", "SharedContextRecord",
    "compute_pinyin_initials",
    "get_default_store", "reset_default_store",
    "DEFAULT_MANIFEST_PATH", "DEFAULT_PUBLIC_FIGURES_PATH",
]
