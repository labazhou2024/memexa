"""Validate agent tool responses against an expected output shape.

Motivation (2026-04-23): code-reviewer agent twice produced narrate-only output
("Let me check a few things..." then exhausted budget without final JSON),
costing ~60k tokens per spawn. Detect-and-switch prevents repeat cost.

Public API:
  validate_agent_response(subagent_type, response_text, expected_schema)
      -> (is_valid: bool, reason_if_invalid: str | None)
  suggest_alternative_agent(failed_type: str) -> str | None
"""
from __future__ import annotations

import datetime as _dt
import json

from src.core.sanitize import sanitize_for_log
import re
import uuid
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Detection heuristics
# ---------------------------------------------------------------------------

_NARRATE_PREFIX_RE = re.compile(
    r"^\s*(Let me|I'll|I will|Now let me|Let's|I'm going to|Let me check|Now I'll)\b",
    re.IGNORECASE,
)

_JSON_BLOCK_RE = re.compile(r"```json\s*\{", re.IGNORECASE)
_BARE_JSON_OBJ_RE = re.compile(r"^\s*\{[\s\S]*\}\s*$", re.MULTILINE)
_VERDICT_RE = re.compile(r"\bVERDICT\s*:", re.IGNORECASE)
_APPROVED_RE = re.compile(r"\b(APPROVED|REVISE_REQUIRED|CHANGES_REQUIRED)\b", re.IGNORECASE)
_FINDINGS_HEADER_RE = re.compile(r"^\s*#+\s*Findings|^\s*Findings:", re.IGNORECASE | re.MULTILINE)

# Look at the first N chars for signal. Agents that WILL produce real output
# usually start emitting it (or a verdict header) within the first ~200 chars.
_SIGNAL_WINDOW = 200


def _has_json_signal(text: str) -> bool:
    head = text[:_SIGNAL_WINDOW]
    if _JSON_BLOCK_RE.search(text):  # JSON block anywhere is a strong signal
        return True
    # Bare JSON object starting at top of message
    if text.strip().startswith("{") and text.strip().endswith("}"):
        try:
            json.loads(text)
            return True
        except Exception:
            pass
    return False


def _has_verdict_signal(text: str) -> bool:
    return bool(_VERDICT_RE.search(text) or _APPROVED_RE.search(text))


def _has_findings_signal(text: str) -> bool:
    return bool(_FINDINGS_HEADER_RE.search(text))


def validate_agent_response(
    subagent_type: str,
    response_text: str,
    expected_schema: str = "json_only",
) -> Tuple[bool, Optional[str]]:
    """Return (is_valid, reason_if_invalid).

    expected_schema:
      - "json_only"         → must have JSON block OR be bare JSON object
      - "has_verdict"       → must contain VERDICT: or APPROVED/REVISE keyword
      - "markdown_findings" → must have Findings header OR verdict keyword
    """
    if not response_text:
        return False, "empty_response"

    narrate = bool(_NARRATE_PREFIX_RE.match(response_text))

    if expected_schema == "json_only":
        if _has_json_signal(response_text):
            return True, None
        if narrate and not _has_json_signal(response_text):
            return False, "narrate_only_no_json"
        return False, "missing_json_signal"

    if expected_schema == "has_verdict":
        if _has_verdict_signal(response_text):
            return True, None
        return False, "missing_verdict_keyword"

    if expected_schema == "markdown_findings":
        if _has_findings_signal(response_text) or _has_verdict_signal(response_text):
            return True, None
        return False, "missing_findings_section"

    return False, f"unknown_schema:{expected_schema}"


# ---------------------------------------------------------------------------
# Alternative agent suggestion (static angle map)
# ---------------------------------------------------------------------------

AGENT_ANGLE_MAP: dict[str, set[str]] = {
    "architect": {"design", "interface"},
    "verifier": {"assumption", "edge"},
    "chief-researcher": {"literature", "prior_art"},
    "logic-reviewer": {"cross_module", "invariant"},
    "coverage-reviewer": {"test_matrix"},
    "security-reviewer": {"threat_model", "supply_chain"},
    "code-reviewer": {"plan_compliance", "idiomatic"},
    "consistency-auditor": {"numeric", "cross_file"},
    "reviewer-technical": {"derivation", "numeric"},
    "reviewer-narrative": {"story", "accessibility"},
    "spec-reviewer": {"plan_compliance", "compliance"},
}


def suggest_alternative_agent(failed_type: str) -> Optional[str]:
    """Given an agent type that produced invalid output, suggest another
    agent whose angle overlaps but is not identical. Returns None if no
    good candidate (caller should fall back to main-session self-review).
    """
    failed_angles = AGENT_ANGLE_MAP.get(failed_type, set())
    if not failed_angles:
        return None
    best: Optional[str] = None
    best_overlap = -1
    for candidate, angles in AGENT_ANGLE_MAP.items():
        if candidate == failed_type:
            continue
        overlap = len(angles & failed_angles)
        # Prefer same-angle coverage but not identical.
        if overlap > best_overlap and angles != failed_angles:
            best_overlap = overlap
            best = candidate
    return best if best_overlap > 0 else None


# ---------------------------------------------------------------------------
# Failure logging (writes full KB schema for prime retrievability, I3 fix)
# ---------------------------------------------------------------------------

def _patterns_path() -> Path:
    # agent_output_validator.py → memex/memex/core/ → data/
    return (
        Path(__file__).resolve().parent.parent
        / "data" / "improvement_patterns.jsonl"
    )


def _log_failure(
    failed_agent: str,
    failed_check: str,
    response_snippet: str,
    expected_schema: str,
) -> str:
    """Append a full-schema KB record so pattern_extractor.prime can retrieve it.

    Returns the generated pattern id (uuid hex).
    """
    # Record shape matches PatternEntry schema (v3), so load_all_patterns can
    # round-trip it. `type="anti_pattern"` earns +1.5 weight under
    # work_type="review" in prime().
    # S2 fix: sanitize snippet via shared sanitize_for_log (imported below).
    # A compromised agent could otherwise embed a full JSONL record or a
    # prompt-injection payload inside response_text[:160] that would be
    # re-surfaced to future LLM sessions via pattern_extractor.prime().
    sanitized_snippet = sanitize_for_log(response_snippet or "", max_len=160)
    record = {
        "id": uuid.uuid4().hex,
        "type": "anti_pattern",
        "fact": (
            f"agent '{failed_agent}' produced invalid output "
            f"(check={failed_check}, schema={expected_schema}). "
            f"snippet: {sanitized_snippet}"
        ),
        "recommendation": (
            f"switch to alternative agent: "
            f"{suggest_alternative_agent(failed_agent) or 'main-session-self-review'}"
        ),
        "confidence": "high",
        "tags": ["agent_output", "validation_failure", failed_agent, failed_check, "agent_failure"],
        "affected_files": [],
        "affected_services": [],
        "created_at": _dt.datetime.utcnow().isoformat() + "Z",
        "updated_at": _dt.datetime.utcnow().isoformat() + "Z",
        "usage_count": 0,
        "helpful_count": 0,
        "outdated_reports": 0,
        "auto_generated": True,
        "provenance": [{
            "source": "agent_output_validator",
            "failed_agent": failed_agent,
            "failed_check": failed_check,
            "expected_schema": expected_schema,
        }],
        "schema_version": 3,
        "source": "auto_extracted",
        "promotion_status": "draft",
        "canonical_tags": [],
        "fingerprint": "",
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    path = _patterns_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Best-effort append; no lock dependency in this lightweight logger.
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
    return record["id"]


def validate_and_log(
    subagent_type: str,
    response_text: str,
    expected_schema: str = "json_only",
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Validate + auto-log if invalid.

    Returns (is_valid, reason, pattern_id).
    pattern_id is None when is_valid=True.
    """
    is_valid, reason = validate_agent_response(subagent_type, response_text, expected_schema)
    if is_valid:
        return True, None, None
    pid = _log_failure(
        failed_agent=subagent_type,
        failed_check=reason or "unknown",
        response_snippet=response_text,
        expected_schema=expected_schema,
    )
    return False, reason, pid
