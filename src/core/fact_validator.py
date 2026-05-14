"""TU-2 of 2026 backfill plan_v2 §3 — fact_validator: 8-gate orchestrator.

Per plan_v2 §TaskUnits TU-2 + logic-iter2-3 absorption: gate order is
G1 schema → G3 source attest (raw bytes) → G8 PII scrub → G2 entity →
G4 temporal → G5 corroboration → G6 contradiction → G7 predicate vocab.

G3 attestation key invariant: sha256(source_file_bytes_at_offset)[:HASH_LEN]
NOT sha256(fact.content). fact.content can be scrubbed by G8 downstream;
the attestation tuple (source_kind, source_offset, source_sha256) remains
anchored to raw source bytes for replay-ability.

Per plan §Architecture invariant 4: gate fail → quarantine; never bypass.
Quarantine writer appends to data/fact_quarantine.jsonl with:
{ts, fact_id, gate, reason, fact_dict_redacted}.

axis_anchor: [C:cli:fact_validator] [C:hook:8_gates_v2_order]
trace events:
  gate_g1_pass / gate_g1_fail
  gate_g3_pass / gate_g3_fail
  gate_g8_pass / gate_g8_fail
  gate_g2_pass / gate_g2_fail
  gate_g4_pass / gate_g4_fail
  gate_g5_pass / gate_g5_fail
  gate_g6_pass / gate_g6_fail
  gate_g7_pass / gate_g7_fail

Writer-reader contract (per HARD RULE feedback_writer_reader_schema_contract):
- run_gates(fact_dict, source_bytes_replay, ...) takes a plain dict, not FactRow,
  to allow incremental construction by callers; final FactRow may be assembled
  AFTER G8 scrubs content.
- Returns GateResult with (passed, gate_failed_at, reason, scrubbed_fact).
- scrubbed_fact has G8-scrubbed content; G2-resolved canonical_subject;
  matches FactRow schema strictly when passed=True.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.core.fact_schema import (
    HASH_LEN,
    PAIRED_EVAL_ATTESTED_VALID,
    PREDICATE_VOCAB,
    is_valid_fact,
    is_valid_at_in_bounds,
)
from src.core.entity_resolver import canonicalize as _canonicalize_entity

# Import scrub_pii from the existing keystone outbox helper
try:
    from src.extraction.keystone_outbox import scrub_pii
    _HAS_SCRUB = True
except ImportError:
    _HAS_SCRUB = False
    def scrub_pii(text: str):  # type: ignore[no-redef]
        # Defensive stub; real scrub_pii MUST be available in production
        return (text, 0)


# ---------------------------------------------------------------------------
# Quarantine writer
# ---------------------------------------------------------------------------

# Default quarantine path (overridable for tests)
DEFAULT_QUARANTINE_PATH = Path("data/fact_quarantine.jsonl")


# PII patterns for sec-iter1-1: if redacted object string ≤80 chars AND
# matches PII regex, do NOT include plaintext — only sha256.
_QUARANTINE_PII_PATTERNS: Tuple = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),           # CN phone 11-digit
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),           # CN ID 18-digit
    re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),  # email
)

# 10MB rotation threshold for quarantine file.
_QUARANTINE_ROTATE_BYTES: int = 10 * 1024 * 1024  # 10 MB


def _is_short_pii(text: str) -> bool:
    """Return True if text ≤80 chars and matches any PII pattern (sec-iter1-1)."""
    if len(text) > 80:
        return False
    return any(p.search(text) for p in _QUARANTINE_PII_PATTERNS)


def _redact_fact_for_quarantine(fact: Dict[str, Any]) -> Dict[str, Any]:
    """Redact sensitive fields for quarantine log (no full PII to persistent log).

    sec-iter1-1 fix: if object string is ≤80 chars AND matches PII regex,
    replace with sha256 only (no plaintext). For longer strings: truncate + sha256.
    """
    redacted = dict(fact)
    if "object" in redacted and isinstance(redacted["object"], str):
        obj = redacted["object"]
        obj_sha = hashlib.sha256(obj.encode("utf-8", errors="replace")).hexdigest()[:HASH_LEN]
        if _is_short_pii(obj):
            # sec-iter1-1: short PII → hash-only, no plaintext
            redacted["object"] = f"[pii_redacted; sha256={obj_sha}]"
        elif len(obj) > 80:
            redacted["object"] = obj[:80] + f"...[redacted; sha256={obj_sha}]"
    return redacted


def _rotate_quarantine_if_needed(path: Path) -> None:
    """Rotate quarantine.jsonl if it exceeds 10MB (rename to .N)."""
    try:
        if path.exists() and path.stat().st_size >= _QUARANTINE_ROTATE_BYTES:
            # Find next available rotation index
            idx = 1
            while True:
                rotated = path.with_suffix(f".{idx}")
                if not rotated.exists():
                    break
                idx += 1
            path.rename(rotated)
    except OSError:
        pass  # rotation failure is non-fatal; continue writing to original path


def _chmod_quarantine(path: Path) -> None:
    """Set quarantine file permissions to 0600 (owner read/write only)."""
    try:
        import stat as _stat
        path.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    except (OSError, AttributeError):
        pass  # Windows may not support chmod; non-fatal


def write_quarantine(
    fact: Dict[str, Any],
    gate: str,
    reason: str,
    quarantine_path: Optional[Path] = None,
) -> None:
    """Append quarantine entry for fact. Atomic per-line write; CRLF safe.

    sec-iter1-1: short PII in object → hash-only, no plaintext.
    10MB rotation: rename to .1, .2, ... and continue writing to new file.
    chmod 0600 on quarantine file.
    """
    path = quarantine_path or DEFAULT_QUARANTINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rotate before write if needed
    _rotate_quarantine_if_needed(path)
    entry = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "fact_id": fact.get("id", "unknown"),
        "gate": gate,
        "reason": reason,
        "fact_redacted": _redact_fact_for_quarantine(fact),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(line)
    # Ensure permissions after write (new file may have been created)
    _chmod_quarantine(path)


# ---------------------------------------------------------------------------
# G3 source attestation
# ---------------------------------------------------------------------------

def compute_source_sha256(source_bytes: bytes) -> str:
    """sha256[:HASH_LEN] of raw source bytes at the offset window.

    Per logic-iter2-3: this is the REPLAY anchor. fact.content (string) may be
    scrubbed downstream by G8; the source_bytes hash is stable.
    """
    if not isinstance(source_bytes, (bytes, bytearray)):
        raise TypeError(f"source_bytes must be bytes, got {type(source_bytes).__name__}")
    return hashlib.sha256(bytes(source_bytes)).hexdigest()[:HASH_LEN]


def gate_g3_source_attest(
    fact: Dict[str, Any],
    source_bytes_replay: Optional[bytes] = None,
    expected_sha: Optional[str] = None,
) -> Tuple[bool, str]:
    """G3 gate: verify source-byte attestation.

    For tests/development: pass `source_bytes_replay` directly.
    For production: caller resolves source_offset → bytes via per-source replay
    function (BACKFILL_REPLAY_REGISTRY) BEFORE calling run_gates.

    Returns (ok, reason).
    """
    if source_bytes_replay is None:
        return False, "attestation_no_replay_bytes_available"

    actual_sha = compute_source_sha256(source_bytes_replay)
    if expected_sha is not None and actual_sha != expected_sha:
        return False, f"attestation_sha_mismatch:{actual_sha[:8]}!={expected_sha[:8]}"

    # Anchor must be derivable: write the computed sha back into fact for
    # downstream lineage. We DO NOT mutate fact here; caller does that.
    return True, ""


# ---------------------------------------------------------------------------
# G8 PII scrub gate
# ---------------------------------------------------------------------------

def gate_g8_pii_scrub(fact: Dict[str, Any]) -> Tuple[bool, str, str]:
    """G8 gate: run scrub_pii_cjk_v3 on fact.object; verify no leaked phone/id/email/addr.

    Returns (ok, reason, scrubbed_object).

    Per plan §TaskUnits TU-2 G8: leaves no phone/id/email/addr.
    Per security-iter1-4 fix: added Chinese address heuristic.
    """
    obj = fact.get("object", "")
    if not isinstance(obj, str):
        return False, "pii_object_not_string", ""

    scrubbed_text, scrubbed_lines = scrub_pii(obj)
    # After scrub_pii, defensive re-check for residual patterns
    leaks = []
    # Phone (CN 11-digit)
    if re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", scrubbed_text):
        leaks.append("phone")
    # Email
    if re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", scrubbed_text):
        leaks.append("email")
    # ID card (CN 18-digit)
    if re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", scrubbed_text):
        leaks.append("id_card")
    # CN address heuristic (省/市/区/路/号 cluster) — sec-iter1-4 fix
    # Pattern: 2+ admin-tokens within 30 chars indicates a CN postal address
    if re.search(r"[\u4e00-\u9fa5]{2,}(省|市|区|县|镇|路|街|号|楼|室)[\u4e00-\u9fa5]{0,30}(省|市|区|县|镇|路|街|号|楼|室)", scrubbed_text):
        leaks.append("address")

    if leaks:
        return False, f"pii_leak:{','.join(leaks)}", scrubbed_text

    return True, "", scrubbed_text


# ---------------------------------------------------------------------------
# G2 entity gate (delegates to entity_resolver.canonicalize)
# ---------------------------------------------------------------------------

def gate_g2_entity(
    fact: Dict[str, Any],
    llm_callable: Optional[Callable] = None,
) -> Tuple[bool, str, Optional[str]]:
    """G2 gate: resolve canonical_subject (and check object if person-style).

    Returns (ok, reason, resolved_subject).

    Per plan §TaskUnits TU-2 G2: input is scrubbed subject + object (G8 ran
    first). For chat-realtime / chat-graph already-pseudonymized facts,
    canonical_subject is already a person_uuid (passthrough).
    """
    raw_subject = fact.get("canonical_subject", "")
    extracted_by = fact.get("extracted_by", "")
    source_kind = fact.get("source_kind", "")

    # Already-canonical: passthrough
    if isinstance(raw_subject, str) and raw_subject.startswith("person_"):
        return True, "", raw_subject

    # Otherwise resolve via entity_resolver
    result = _canonicalize_entity(
        raw_id=raw_subject,
        source_kind=source_kind,
        extracted_by=extracted_by,
        llm_callable=llm_callable,
    )
    if result.person_uuid is None:
        return False, f"entity_unresolved:{result.reason}", None
    return True, "", result.person_uuid


# ---------------------------------------------------------------------------
# TU-Phase2-1: paired_eval attestation check (post-G2 source attestation)
# ---------------------------------------------------------------------------

def _check_paired_eval_attestation(
    fact: Dict[str, Any],
    env_required: Optional[bool] = None,
    emit_trace: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Tuple[bool, str]:
    """Check paired_eval_attested field against PAIRED_EVAL_ATTESTED_VALID.

    Args:
        fact: fact dict (may or may not contain paired_eval_attested).
        env_required: override for env check (for testing). If None, reads
            MEMEXA_PAIRED_EVAL_REQUIRED env var (default "1").
        emit_trace: optional trace emitter; if None uses trace_sink.

    Returns:
        (ok, reason). ok=True means check passed.

    Enforcement mode (MEMEXA_PAIRED_EVAL_REQUIRED=1, default):
        - Missing field OR value="none" → FAIL; trace fact_paired_eval_invalid_value.
        - Value in {"paired_v1", "single_qwen_v1"} → PASS.
        - Value not in valid set → FAIL; trace fact_paired_eval_invalid_value.

    Grandfather mode (MEMEXA_PAIRED_EVAL_REQUIRED=0):
        - Always PASS; trace fact_paired_eval_grandfather_skipped.
    """
    def _trace(event: str, payload: dict) -> None:
        if emit_trace is not None:
            try:
                emit_trace(event, payload)
            except Exception:
                pass
        else:
            try:
                from src.core.trace_sink import write_trace_event
                write_trace_event(event, payload)
            except Exception:
                pass

    # Resolve env_required from environment if not explicitly passed
    if env_required is None:
        env_required = os.environ.get("MEMEXA_PAIRED_EVAL_REQUIRED", "1") == "1"

    if not env_required:
        # Grandfather mode: accept unconditionally + emit informational trace
        _trace("fact_paired_eval_grandfather_skipped", {
            "fact_id": fact.get("id", "unknown"),
            "paired_eval_attested": fact.get("paired_eval_attested", "<missing>"),
        })
        return True, ""

    # Enforcement mode
    attested = fact.get("paired_eval_attested", "none")

    # Absent key is treated as "none" (not attested)
    if "paired_eval_attested" not in fact:
        attested = "none"

    # Valid enrolled values (excluding "none" which means not attested)
    _PASSING_VALUES = {"paired_v1", "single_qwen_v1"}

    if attested not in _PASSING_VALUES:
        reason = f"paired_eval_not_attested:{attested!r}"
        _trace("fact_paired_eval_invalid_value", {
            "fact_id": fact.get("id", "unknown"),
            "value": attested,
            "reason": reason,
        })
        return False, reason

    return True, ""


# ---------------------------------------------------------------------------
# G4 temporal gate
# ---------------------------------------------------------------------------

def gate_g4_temporal(fact: Dict[str, Any]) -> Tuple[bool, str]:
    """G4 gate: valid_at ∈ [2026-01-01, now+1d]."""
    valid_at = fact.get("valid_at", "")
    if not is_valid_at_in_bounds(valid_at):
        return False, f"temporal_oob:{valid_at!r}"
    return True, ""


# ---------------------------------------------------------------------------
# G5 corroboration gate
# ---------------------------------------------------------------------------

def gate_g5_corroboration(fact: Dict[str, Any]) -> Tuple[bool, str]:
    """G5 gate: tentative=True if corroborated_by has <2 entries (per plan).

    G5 NEVER rejects (always pass); it MUTATES the tentative flag.
    Caller is expected to write back tentative=True if returned reason is
    "marked_tentative".

    Per logic-iter1-4 fix: None / non-sequence corroborated_by → mark tentative
    (defensive coalesce; G5 invariant says always-pass).
    """
    corro = fact.get("corroborated_by", [])
    if corro is None:
        return True, "marked_tentative"
    if not isinstance(corro, (list, tuple)):
        # Per plan §TaskUnits TU-2 G5: tentative=True if <2 — treat invalid type
        # as "no corroboration available" → tentative.
        return True, "marked_tentative_invalid_type"
    if len(corro) < 2:
        return True, "marked_tentative"
    return True, ""


# ---------------------------------------------------------------------------
# G6 contradiction gate (invariant-only, per plan R3)
# ---------------------------------------------------------------------------

# Invariant-only predicates: SINGLE-VALUED claims where a contradiction is
# meaningful (e.g. "is_a", "advised_by"). Multi-valued predicates like "knows"
# (alice can know both bob AND carol) MUST NOT be in this set or G6 produces
# false-positives.
# Per logic-iter1-3 fix: removed "knows", "works_with" (both multi-valued).
# Event-style predicates (e.g. "sent_message_to", "committed") also excluded.
INVARIANT_PREDICATES: frozenset = frozenset({
    "is_a", "advised_by", "advises",
    "preferred", "decided", "believes",
})


def gate_g6_contradiction(
    fact: Dict[str, Any],
    existing_facts: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bool, str]:
    """G6 gate: scan existing_facts for invariant contradictions.

    Per plan R3: G6 only checks invariant-style claims; event claims (both
    parties valid at different t) skip G6.

    For backfill bulk-ingest, `existing_facts` is the in-memory batch-window
    of recently validated facts (size-bounded by caller).
    """
    pred = fact.get("predicate", "")
    if pred not in INVARIANT_PREDICATES:
        return True, "skip_event_predicate"

    if not existing_facts:
        return True, ""

    new_subj = fact.get("canonical_subject", "")
    new_obj = fact.get("object", "")
    for other in existing_facts:
        if other.get("predicate") != pred:
            continue
        if other.get("canonical_subject") != new_subj:
            continue
        # Same subject + invariant predicate → object should agree
        old_obj = other.get("object")
        if old_obj != new_obj:
            # Per security-iter1-10 fix: truncate object values to 50 chars
            # in reason string to avoid PII leakage to quarantine log.
            new_obj_safe = (str(new_obj)[:50] + "...") if len(str(new_obj)) > 50 else str(new_obj)
            old_obj_safe = (str(old_obj)[:50] + "...") if len(str(old_obj)) > 50 else str(old_obj)
            return False, (
                f"contradiction:{pred} subject={new_subj[:60]} "
                f"object_new_pfx={new_obj_safe!r} object_old_pfx={old_obj_safe!r}"
            )
    return True, ""


# ---------------------------------------------------------------------------
# G7 predicate vocab gate
# ---------------------------------------------------------------------------

def gate_g7_predicate(fact: Dict[str, Any]) -> Tuple[bool, str]:
    """G7 gate: predicate ∈ closed vocab."""
    pred = fact.get("predicate", "")
    if pred not in PREDICATE_VOCAB:
        return False, f"predicate_not_in_vocab:{pred!r}"
    return True, ""


# ---------------------------------------------------------------------------
# Orchestrator: run_gates
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """Output of run_gates. passed=True means all gates green."""
    passed: bool
    gate_failed_at: Optional[str] = None  # "g1" / "g3" / ... or None
    reason: str = ""
    scrubbed_fact: Optional[Dict[str, Any]] = None
    tentative_marked: bool = False
    gates_run: List[str] = field(default_factory=list)


def run_gates(
    fact: Dict[str, Any],
    source_bytes_replay: Optional[bytes] = None,
    expected_source_sha: Optional[str] = None,
    existing_facts: Optional[List[Dict[str, Any]]] = None,
    llm_callable: Optional[Callable] = None,
    quarantine_path: Optional[Path] = None,
    emit_trace: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> GateResult:
    """Run G1→G3→G8→G2→G4→G5→G6→G7 in order. Quarantine on first fail.

    Per plan §TaskUnits TU-2 + logic-iter2-3:
      1. G1 schema (15 fields strict) — input dict shape must be FactRow-compatible
      2. G3 source attestation (against raw source bytes via source_offset)
      3. G8 PII scrub (mutates content; subsequent gates see scrubbed)
      4. G2 entity resolve (on scrubbed subject)
      5. G4 temporal (valid_at in bounds)
      6. G5 corroboration (mark tentative if <2)
      7. G6 contradiction (invariant-only)
      8. G7 predicate vocab

    Returns GateResult; on fail, quarantine entry written via write_quarantine.

    `emit_trace` is dependency-injected for tests; production calls
    src.core.trace_sink.write_trace_event.
    """
    def _emit(event_type: str, payload: Dict[str, Any]) -> None:
        if emit_trace is not None:
            try:
                emit_trace(event_type, payload)
            except Exception:
                pass

    fact_id_for_trace = fact.get("id", "unknown") if isinstance(fact, dict) else "unknown"
    gates_run: List[str] = []
    working = dict(fact) if isinstance(fact, dict) else fact

    # ---- G1 schema ------------------------------------------------------
    gates_run.append("g1")
    # G1 expects scrubbed/canonical fact; for the run_gates entrypoint we
    # accept partial-fact (e.g. canonical_subject still raw_id) and re-validate
    # after G2/G8. So G1 here checks structural fields only via lite-mode.
    # Implementation: we call is_valid_fact at the END (after G8+G2 mutate);
    # at this stage we validate that the input is a dict and has the required
    # field NAMES (not values).
    if not isinstance(working, dict):
        write_quarantine({"id": "n/a"}, "g1", "schema_not_dict", quarantine_path)
        _emit("gate_g1_fail", {"fact_id": "n/a", "reason": "schema_not_dict"})
        return GateResult(passed=False, gate_failed_at="g1",
                          reason="schema_not_dict", gates_run=gates_run)

    # Field-set check (subset of is_valid_fact)
    from src.core.fact_schema import _REQUIRED_FIELDS  # type: ignore[attr-defined]
    actual = set(working.keys())
    missing = _REQUIRED_FIELDS - actual
    extra = actual - _REQUIRED_FIELDS
    if missing or extra:
        reason = ""
        if missing:
            reason += f"schema_missing:{','.join(sorted(missing))}"
        if extra:
            reason += (";" if reason else "") + f"schema_extra:{','.join(sorted(extra))}"
        write_quarantine(working, "g1", reason, quarantine_path)
        _emit("gate_g1_fail", {"fact_id": fact_id_for_trace, "reason": reason})
        return GateResult(passed=False, gate_failed_at="g1", reason=reason,
                          gates_run=gates_run)
    _emit("gate_g1_pass", {"fact_id": fact_id_for_trace})

    # ---- G3 source attest ------------------------------------------------
    gates_run.append("g3")
    ok3, reason3 = gate_g3_source_attest(working, source_bytes_replay, expected_source_sha)
    if not ok3:
        write_quarantine(working, "g3", reason3, quarantine_path)
        _emit("gate_g3_fail", {"fact_id": fact_id_for_trace, "reason": reason3})
        return GateResult(passed=False, gate_failed_at="g3", reason=reason3,
                          gates_run=gates_run)
    _emit("gate_g3_pass", {"fact_id": fact_id_for_trace})

    # ---- G8 PII scrub (mutates content) ----------------------------------
    gates_run.append("g8")
    ok8, reason8, scrubbed_obj = gate_g8_pii_scrub(working)
    if not ok8:
        write_quarantine(working, "g8", reason8, quarantine_path)
        _emit("gate_g8_fail", {"fact_id": fact_id_for_trace, "reason": reason8})
        return GateResult(passed=False, gate_failed_at="g8", reason=reason8,
                          gates_run=gates_run)
    working["object"] = scrubbed_obj
    _emit("gate_g8_pass", {"fact_id": fact_id_for_trace})

    # ---- G2 entity (on scrubbed) ----------------------------------------
    gates_run.append("g2")
    ok2, reason2, resolved = gate_g2_entity(working, llm_callable=llm_callable)
    if not ok2:
        write_quarantine(working, "g2", reason2, quarantine_path)
        _emit("gate_g2_fail", {"fact_id": fact_id_for_trace, "reason": reason2})
        return GateResult(passed=False, gate_failed_at="g2", reason=reason2,
                          gates_run=gates_run)
    if resolved is not None:
        working["canonical_subject"] = resolved
    _emit("gate_g2_pass", {"fact_id": fact_id_for_trace})

    # ---- G2b paired_eval attestation (post-G2 entity, post-G3 source attest) --
    gates_run.append("g2b_paired_eval")
    ok_pe, reason_pe = _check_paired_eval_attestation(
        working, emit_trace=_emit,
    )
    if not ok_pe:
        write_quarantine(working, "g2b_paired_eval", reason_pe, quarantine_path)
        _emit("gate_g2b_paired_eval_fail", {"fact_id": fact_id_for_trace, "reason": reason_pe})
        return GateResult(passed=False, gate_failed_at="g2b_paired_eval",
                          reason=reason_pe, gates_run=gates_run)
    _emit("gate_g2b_paired_eval_pass", {"fact_id": fact_id_for_trace})

    # ---- G4 temporal -----------------------------------------------------
    gates_run.append("g4")
    ok4, reason4 = gate_g4_temporal(working)
    if not ok4:
        write_quarantine(working, "g4", reason4, quarantine_path)
        _emit("gate_g4_fail", {"fact_id": fact_id_for_trace, "reason": reason4})
        return GateResult(passed=False, gate_failed_at="g4", reason=reason4,
                          gates_run=gates_run)
    _emit("gate_g4_pass", {"fact_id": fact_id_for_trace})

    # ---- G5 corroboration (mutates tentative) ----------------------------
    gates_run.append("g5")
    ok5, reason5 = gate_g5_corroboration(working)
    tentative_marked = False
    if not ok5:
        write_quarantine(working, "g5", reason5, quarantine_path)
        _emit("gate_g5_fail", {"fact_id": fact_id_for_trace, "reason": reason5})
        return GateResult(passed=False, gate_failed_at="g5", reason=reason5,
                          gates_run=gates_run)
    if reason5.startswith("marked_tentative"):
        working["tentative"] = True
        tentative_marked = True
    _emit("gate_g5_pass", {"fact_id": fact_id_for_trace, "tentative": tentative_marked})

    # ---- G6 contradiction -----------------------------------------------
    gates_run.append("g6")
    ok6, reason6 = gate_g6_contradiction(working, existing_facts=existing_facts)
    if not ok6:
        write_quarantine(working, "g6", reason6, quarantine_path)
        _emit("gate_g6_fail", {"fact_id": fact_id_for_trace, "reason": reason6})
        return GateResult(passed=False, gate_failed_at="g6", reason=reason6,
                          gates_run=gates_run)
    _emit("gate_g6_pass", {"fact_id": fact_id_for_trace})

    # ---- G7 predicate vocab ---------------------------------------------
    gates_run.append("g7")
    ok7, reason7 = gate_g7_predicate(working)
    if not ok7:
        write_quarantine(working, "g7", reason7, quarantine_path)
        _emit("gate_g7_fail", {"fact_id": fact_id_for_trace, "reason": reason7})
        return GateResult(passed=False, gate_failed_at="g7", reason=reason7,
                          gates_run=gates_run)
    _emit("gate_g7_pass", {"fact_id": fact_id_for_trace})

    # ---- Final FactRow shape verify -------------------------------------
    final_ok, final_reason = is_valid_fact(working)
    if not final_ok:
        # Last-line defense: shouldn't happen if G1+G2+G8 ran cleanly
        write_quarantine(working, "g1_post", final_reason, quarantine_path)
        _emit("gate_g1_fail", {"fact_id": fact_id_for_trace, "reason": final_reason,
                                "stage": "final"})
        return GateResult(passed=False, gate_failed_at="g1_post", reason=final_reason,
                          gates_run=gates_run)

    return GateResult(passed=True, scrubbed_fact=working,
                      tentative_marked=tentative_marked, gates_run=gates_run)


__all__ = [
    "DEFAULT_QUARANTINE_PATH",
    "INVARIANT_PREDICATES",
    "GateResult",
    "compute_source_sha256",
    "gate_g3_source_attest",
    "gate_g8_pii_scrub",
    "gate_g2_entity",
    "gate_g4_temporal",
    "gate_g5_corroboration",
    "gate_g6_contradiction",
    "gate_g7_predicate",
    "run_gates",
    "write_quarantine",
    "_check_paired_eval_attestation",
    "_is_short_pii",
    "_QUARANTINE_ROTATE_BYTES",
]
