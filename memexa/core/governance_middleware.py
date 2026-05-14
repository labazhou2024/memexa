"""
Governance Middleware (T6, plan v3.1, 2026-04-20)
=================================================

Pre-write guard applied to all auto-write paths for the memory/pattern
knowledge base. Enforces plan v3.1 §4 R1 (poisoning fuse), R12 (taste path
= CEO only), R13 (endpoint + model-family lock on deep consistency check),
and §5 AC-13 (consistency contradiction veto), AC-14 (machine-chain block),
AC-18 partial (budget fail policy aligned with R3 P0-3).

Public entry points
-------------------
    pre_write_check(req: WriteRequest) -> GovernanceDecision
        The single choke point. All callers (autodream extractor,
        reviewer-finding ingester, manual pattern append, memory/*.md
        promotion pipeline) MUST route through this function before
        hitting disk.

    get_governance_depth() -> "shallow" | "deep"
        Reads env MEMEXA_GOVERNANCE_DEPTH (default "shallow"). "shallow"
        uses a local regex consistency check; "deep" shells out to
        consistency-auditor via `claude -p` with hard-pinned endpoint
        + model_family (SEC-R1 S2/C10).

Rule ordering (short-circuit top to bottom)
-------------------------------------------
    1. Path validation      (structural)
    2. Sanitize              (content_sanitizer)
    3. Lineage fuse          (R1 anti-poisoning, machine chains blocked)
    4. Taste-path hard guard (R12, L3 feedback_*.md requires ceo_edit)
    5. Budget check          (R3 P0-3: L1 queue-on-exhaust, L3 fail-closed)
    6. Consistency check     (R13 shallow regex | deep auditor)
    7. Approved              (audit logged to trace_sink)

Env flags
---------
    MEMEXA_GOVERNANCE_ENABLED   "1" (default) | "0" (kill switch → allow-all)
    MEMEXA_GOVERNANCE_DEPTH     "shallow" (default) | "deep"
    MEMEXA_GOVERNANCE_FAIL_OPEN "0" (default) | "1" (cov: testing only)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

# ---------------------------------------------------------------------------
# Soft imports (tolerate module-not-yet-shipped during parallel development)
# ---------------------------------------------------------------------------

try:
    from memexa.core.content_sanitizer import sanitize_for_extraction
except Exception:  # pragma: no cover - sanitizer is a T3 hard dep
    def sanitize_for_extraction(text: str):  # type: ignore[no-redef]
        return (text, [])

try:
    from memexa.core.trace_sink import write_trace_event
except Exception:  # pragma: no cover
    def write_trace_event(event, payload=None, session_id=None):  # type: ignore[no-redef]
        return False

try:
    # T6.5 parallel dev; may not exist yet.
    from memexa.core.llm_budget import check_and_reserve  # type: ignore
except Exception:  # pragma: no cover - fallback when module absent
    # H2 fix (2026-04-20): do NOT silently soft-allow on import failure.
    # When llm_budget is unavailable, _check_budget must fail-closed on L3
    # and queue on L1. Sentinel value None signals "module import failed"
    # to _check_budget.
    check_and_reserve = None  # type: ignore[assignment,no-redef]

try:
    from memexa.core import pattern_extractor as _pe  # type: ignore
except Exception:  # pragma: no cover
    _pe = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Tier = Literal["L1", "L3"]
Source = Literal[
    "ceo_edit", "human_turn", "autodream",
    "reviewer_finding", "auto_extracted", "human_turn_historical",
    # M3 fix (2026-04-20): explicit source for promotion_engine.promote().
    # Semantically distinct from raw 'ceo_edit' so downstream lineage auditors
    # can tell a CEO-approved KB->memory promotion apart from a direct CEO
    # hand-edit of memory/*.md. Both pass the taste-path guard.
    "ceo_approved_promotion",
]
Depth = Literal["shallow", "deep"]


class WriteRequest(TypedDict, total=False):
    tier: Tier
    source: Source
    parent_pattern_id: Optional[str]
    content: str
    why: Optional[str]
    agent_name: Optional[str]
    # Optional: the target filename (relative). For L1 writes the field is
    # implicit (improvement_patterns.jsonl); for L3 writes callers SHOULD
    # provide it so path validation can pin down feedback_* vs project_*.
    target_path: Optional[str]


class GovernanceDecision(TypedDict):
    allowed: bool
    reason: str
    sanitized_content: Optional[str]
    audit_id: str


# ---------------------------------------------------------------------------
# Config & constants
# ---------------------------------------------------------------------------

# Hard-pinned endpoint + model family (SEC-R1 S2/C10). The deep path records
# both the requested and actual response_model in trace_sink so drift
# between what we asked for and what the runtime gave us is auditable.
_DEEP_MODEL_FAMILY: str = "claude-sonnet-4"
_DEEP_ENDPOINT: str = "claude-code-cli"  # subprocess `claude -p`

_L3_FEEDBACK_RE = re.compile(r"^memory/feedback_[a-z0-9_]+\.md$")
_L3_PROJECT_RE = re.compile(r"^memory/project_[a-z0-9_]+\.md$")
_L1_BASENAME = "improvement_patterns.jsonl"

_HUMAN_SOURCES: frozenset = frozenset({
    "ceo_edit", "human_turn", "human_turn_historical",
    # M3: CEO-approved promotion is semantically a human approval act, so
    # for lineage-fuse purposes it counts as a human root just like ceo_edit.
    "ceo_approved_promotion",
})
_MACHINE_SOURCES: frozenset = frozenset({"auto_extracted", "reviewer_finding", "autodream"})

# Taste-path (memory/feedback_*.md) accepts only these sources.
_TASTE_PATH_ALLOWED_SOURCES: frozenset = frozenset({
    "ceo_edit", "ceo_approved_promotion",
})

# Anti-pair vocabulary for shallow regex consistency check. Pairs of tokens
# which, when both appear for the same "name" / "rule" key across records,
# strongly suggest a contradiction (e.g. "always" vs "never").
_ANTI_PAIRS = [
    ("必须", "不要"), ("必须", "禁止"), ("必须", "不得"),
    ("应当", "不应"), ("应当", "禁止"),
    ("always", "never"), ("must", "must not"), ("must", "never"),
    ("do ", "don't "), ("enable", "disable"),
    ("允许", "禁止"), ("要", "不要"),
]

_AUDIT_EVENT = "hook_outcome"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flag_on(name: str, default: str = "1") -> bool:
    val = os.environ.get(name, default)
    return val not in ("", "0", "false", "False", "FALSE", "no", "NO")


def _flag_val(name: str, default: str) -> str:
    return os.environ.get(name, default) or default


def get_governance_depth() -> Depth:
    """Return the currently-selected consistency depth.

    Reads MEMEXA_GOVERNANCE_DEPTH with "shallow" default. Unknown values
    fall back to "shallow" (fail-safe — never silently upgrade to deep
    since deep shells out and costs API calls).
    """
    raw = _flag_val("MEMEXA_GOVERNANCE_DEPTH", "shallow").strip().lower()
    if raw == "deep":
        return "deep"
    return "shallow"


def _audit(audit_id: str, decision: GovernanceDecision, rule_hit: str,
           extra: Optional[Dict[str, Any]] = None) -> None:
    """Record one governance check to trace_sink. Never raises."""
    payload: Dict[str, Any] = {
        "hook": "governance_check",
        "audit_id": audit_id,
        "allowed": decision["allowed"],
        "reason": decision["reason"],
        "rule_hit": rule_hit,
    }
    if extra:
        # Keep it small; trace_sink truncates at 4KB anyway.
        for k, v in extra.items():
            payload[k] = v
    try:
        write_trace_event(_AUDIT_EVENT, payload)
    except Exception:  # pragma: no cover - trace_sink already swallows
        pass


def _decision(allowed: bool, reason: str, audit_id: str,
              sanitized: Optional[str] = None) -> GovernanceDecision:
    return GovernanceDecision(
        allowed=allowed,
        reason=reason,
        sanitized_content=sanitized,
        audit_id=audit_id,
    )


# ---------------------------------------------------------------------------
# Rule 1: Path validation
# ---------------------------------------------------------------------------

def _validate_path(req: WriteRequest) -> Optional[str]:
    """Return None if OK; a failure reason string otherwise."""
    tier = req.get("tier")
    target = req.get("target_path") or ""
    # Normalize path separators for the regex check.
    if target:
        norm = target.replace("\\", "/").strip()
        # Reject parent traversal / absolute paths / NT drive letters.
        if ".." in norm.split("/"):
            return "path_traversal_rejected"
        if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
            return "path_absolute_rejected"
        # Reject symlink-ish or weird characters.
        if "\x00" in norm:
            return "path_invalid_char"

        if tier == "L3":
            if not (_L3_FEEDBACK_RE.match(norm) or _L3_PROJECT_RE.match(norm)):
                return "l3_path_not_whitelisted"
        elif tier == "L1":
            if not norm.endswith(_L1_BASENAME):
                return "l1_path_not_whitelisted"
        else:
            return "unknown_tier"
    else:
        # Implicit paths permitted only for L1 (writes directly to the
        # one JSONL knowledge base). L3 MUST provide target_path so the
        # feedback_*.md vs project_*.md distinction is checkable.
        if tier == "L3":
            return "l3_missing_target_path"
        if tier != "L1":
            return "unknown_tier"
    return None


# ---------------------------------------------------------------------------
# Rule 3: Lineage fuse (R1 anti-poisoning)
# ---------------------------------------------------------------------------

def _load_patterns_map() -> Dict[str, Any]:
    """Return {id -> PatternEntry} snapshot for lineage walk.

    Falls back to empty map if pattern_extractor absent / file missing.
    Each returned object exposes .source and .parent_pattern_id.
    """
    if _pe is None:
        return {}
    try:
        entries = _pe.load_all_patterns()
    except Exception:
        return {}
    out: Dict[str, Any] = {}
    for e in entries:
        pid = getattr(e, "id", None)
        if pid:
            out[pid] = e
    return out


def _lineage_is_human_rooted(
    parent_id: Optional[str],
    index: Dict[str, Any],
    max_depth: int = 2,
) -> bool:
    """Walk up to max_depth ancestors. True if we find a human source
    (ceo_edit / human_turn / human_turn_historical) within that window.
    Returns False if chain is all-machine or parent missing.
    """
    if not parent_id:
        return False
    cur = index.get(parent_id)
    depth = 0
    seen: set = set()
    while cur is not None and depth < max_depth:
        if getattr(cur, "id", None) in seen:
            break  # cycle guard
        seen.add(getattr(cur, "id", None))
        src = getattr(cur, "source", None)
        if src in _HUMAN_SOURCES:
            return True
        next_id = getattr(cur, "parent_pattern_id", None)
        if not next_id:
            return False
        cur = index.get(next_id)
        depth += 1
    return False


# ---------------------------------------------------------------------------
# Rule 6: Consistency check
# ---------------------------------------------------------------------------

_NAME_KEY_RE = re.compile(r"""(?mi)^\s*(?:-\s*)?(?:name|rule|why)\s*:\s*["']?(.+?)["']?\s*$""")
_YAML_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _extract_key_terms(content: str) -> Dict[str, List[str]]:
    """Extract {key -> [values]} from YAML front-matter and bullet patterns.

    Very lightweight; we only look for lines like:
        name: Foo
        rule: Always X
        why: X prevents Y
    Both inside and outside YAML front-matter.
    """
    terms: Dict[str, List[str]] = {}
    # Pull YAML front-matter if present.
    body = content
    m = _YAML_FRONT_RE.match(content)
    if m:
        body = m.group(1) + "\n" + content[m.end():]
    for line in body.splitlines():
        mm = _NAME_KEY_RE.match(line)
        if mm:
            val = mm.group(1).strip()
            # Split into "key" before ":" and the rest as value.
            key_match = re.match(r"""^\s*(?:-\s*)?(name|rule|why)\s*:""", line, re.I)
            if key_match:
                key = key_match.group(1).lower()
                terms.setdefault(key, []).append(val)
    return terms


def _shallow_contradiction(new_terms: Dict[str, List[str]],
                           existing_terms: Dict[str, List[str]]) -> Optional[str]:
    """Check for anti-pair contradictions keyed by same "name".

    Logic: if an existing record and the new record share the same "name"
    value (case-insensitive token overlap >= 1 non-stopword), AND their
    "rule"/"why" fields each contain one half of an anti-pair → flag
    contradiction. Returns a short human-readable explanation.
    """
    new_names = {n.lower().strip() for n in new_terms.get("name", []) if n}
    if not new_names:
        return None
    existing_names = {n.lower().strip() for n in existing_terms.get("name", []) if n}
    overlap = new_names & existing_names
    if not overlap:
        return None

    new_rules = " ".join(new_terms.get("rule", []) + new_terms.get("why", [])).lower()
    old_rules = " ".join(existing_terms.get("rule", []) + existing_terms.get("why", [])).lower()
    if not new_rules or not old_rules:
        return None

    for a, b in _ANTI_PAIRS:
        if (a in new_rules and b in old_rules) or (b in new_rules and a in old_rules):
            return f"anti_pair({a!r},{b!r}) on name={sorted(overlap)}"
    return None


def _shallow_consistency_check(content: str) -> Optional[str]:
    """Shallow regex consistency check.

    Returns None if no contradiction; else a short reason string.
    Compares the proposed content against already-stored patterns by
    extracting "name"/"rule"/"why" terms.
    """
    new_terms = _extract_key_terms(content)
    if not new_terms.get("name"):
        return None  # nothing to compare against
    # Aggregate existing terms across the KB once.
    existing_terms: Dict[str, List[str]] = {}
    if _pe is not None:
        try:
            for e in _pe.load_all_patterns():
                # Each PatternEntry has .fact / .recommendation-style fields;
                # we synthesize a pseudo-{name,rule,why} from its tags+fact.
                tags = getattr(e, "tags", []) or []
                fact = getattr(e, "fact", "") or ""
                rec = getattr(e, "recommendation", "") or ""
                if tags:
                    existing_terms.setdefault("name", []).extend(str(t) for t in tags)
                if fact:
                    existing_terms.setdefault("rule", []).append(str(fact))
                if rec:
                    existing_terms.setdefault("why", []).append(str(rec))
        except Exception:
            pass
    return _shallow_contradiction(new_terms, existing_terms)


def _deep_consistency_check(content: str, audit_id: str) -> Optional[str]:
    """Deep path: shell out to consistency-auditor via `claude -p`.

    SEC-R1 S2/C10: endpoint + model_family are hard-pinned here and
    recorded to trace_sink along with whatever response_model the CLI
    actually served. Never raises (consistency check must degrade to
    "no veto" on subprocess error rather than hard-fail the write).
    """
    # SEC-R1 S1: strip null bytes and bare \r before passing to subprocess.
    # On Windows, \x00 in a cmd arg truncates the argument at the OS level,
    # causing claude -p to receive partial content and return a stale "allowed"
    # verdict without actually checking the full text.
    clean_content = content.replace("\x00", "").replace("\r", "")
    from memexa.core.subprocess_launcher import claude_argv
    cmd = claude_argv([
        "-p",
        "--model", _DEEP_MODEL_FAMILY,
        "consistency-auditor",
        "--content", clean_content[:4000],  # cap input size
    ])
    response_model = ""
    verdict = ""
    try:
        # Subprocess call. Tests monkeypatch subprocess.run.
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        stdout = (proc.stdout or "").strip()
        # Parse optional leading JSON {"response_model":"...","verdict":"..."}
        try:
            j = json.loads(stdout) if stdout.startswith("{") else {}
        except json.JSONDecodeError:
            j = {}
        response_model = str(j.get("response_model", "")) if j else ""
        verdict = str(j.get("verdict", "")) if j else stdout
    except FileNotFoundError:
        # claude CLI missing → degrade gracefully.
        _audit(audit_id,
               _decision(True, "deep_degraded_cli_missing", audit_id),
               rule_hit="consistency_deep",
               extra={"endpoint": _DEEP_ENDPOINT,
                      "model_family_requested": _DEEP_MODEL_FAMILY,
                      "response_model": "",
                      "degraded": True})
        return None
    except Exception:
        return None

    # Trace the endpoint + model family + what we actually got.
    _audit(audit_id,
           _decision(True, "deep_check_completed", audit_id),
           rule_hit="consistency_deep",
           extra={"endpoint": _DEEP_ENDPOINT,
                  "model_family_requested": _DEEP_MODEL_FAMILY,
                  "response_model": response_model})

    if verdict:
        v = verdict.lower()
        if "contradict" in v or "veto" in v or "reject" in v:
            return f"deep_veto({verdict[:120]!r})"
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def pre_write_check(req: WriteRequest) -> GovernanceDecision:
    """Guard every auto-write. See module docstring for rule ordering."""
    audit_id = f"gov_{uuid.uuid4().hex[:12]}"

    # Kill switch.
    if not _flag_on("MEMEXA_GOVERNANCE_ENABLED", "1"):
        d = _decision(True, "governance_disabled", audit_id,
                      sanitized=req.get("content", ""))
        _audit(audit_id, d, rule_hit="kill_switch")
        return d

    tier = req.get("tier")
    source = req.get("source")
    agent_name = req.get("agent_name", "")
    if tier not in ("L1", "L3"):
        d = _decision(False, "unknown_tier", audit_id)
        _audit(audit_id, d, rule_hit="input_validation")
        return d
    if source not in (
        "ceo_edit", "human_turn", "autodream",
        "reviewer_finding", "auto_extracted", "human_turn_historical",
        "ceo_approved_promotion",  # M3 fix (2026-04-20)
    ):
        d = _decision(False, "unknown_source", audit_id)
        _audit(audit_id, d, rule_hit="input_validation")
        return d

    # F7 (SEC-R1-001): source authentication — `ceo_edit` and
    # `ceo_approved_promotion` are taste-path sources that bypass lineage fuse.
    # Only downgrade when agent_name is EXPLICITLY set to a value NOT in the
    # trusted list. agent_name=None/"" is treated as legacy (accepted) — the
    # security benefit is against an attacker SETTING agent_name="evil_bot"
    # while claiming source=ceo_edit; pure omission is allowed for backward
    # compat with existing callers that predate this check.
    _TRUSTED_CALLERS = {
        "ceo_edit": {
            "memory_ingest_watcher",
            "memory_ingest_watcher.scan_and_ingest",
            "memory_ingest_watcher.drain_queue",
        },
        "ceo_approved_promotion": {
            "promotion_engine",
            "promotion_engine.promote",
        },
    }
    if (source in _TRUSTED_CALLERS and agent_name
            and agent_name not in _TRUSTED_CALLERS[source]):
        # Explicit agent_name but not in trusted list → downgrade.
        _audit(audit_id, _decision(True, "source_downgraded_to_auto_extracted",
                                   audit_id),
               rule_hit="source_authentication",
               extra={"claimed_source": source, "agent_name": agent_name})
        source = "auto_extracted"
        req = {**req, "source": source}

    # ---- Rule 1: path ----
    path_fail = _validate_path(req)
    if path_fail:
        d = _decision(False, path_fail, audit_id)
        _audit(audit_id, d, rule_hit="path_validation",
               extra={"target_path": req.get("target_path", "")})
        return d

    # ---- Rule 2: sanitize ----
    raw = req.get("content", "") or ""
    sanitized, removed = sanitize_for_extraction(raw)

    # ---- Rule 3: lineage fuse (R1 anti-poisoning) ----
    if source in _MACHINE_SOURCES and req.get("parent_pattern_id"):
        idx = _load_patterns_map()
        parent = idx.get(req["parent_pattern_id"])
        if parent is None:
            d = _decision(False, "poisoning_fuse_blocked", audit_id,
                          sanitized=sanitized)
            _audit(audit_id, d, rule_hit="lineage_fuse",
                   extra={"detail": "parent_missing"})
            return d
        # Count the direct parent as the first ancestor. If parent is
        # machine-origin, we require a human within the next 2-hop window.
        parent_src = getattr(parent, "source", None)
        if parent_src in _HUMAN_SOURCES:
            pass  # clean root
        else:
            # F7 (LOG-R1-004): previously max_depth=2 gave effective 3-hop
            # window (parent + 2 more). Doc promises 2-hop; change to 1 more
            # hop so window is parent + grandparent = 2 total before human.
            if not _lineage_is_human_rooted(
                    getattr(parent, "parent_pattern_id", None), idx, max_depth=1):
                d = _decision(False, "poisoning_fuse_blocked", audit_id,
                              sanitized=sanitized)
                _audit(audit_id, d, rule_hit="lineage_fuse",
                       extra={"detail": "machine_chain",
                              "parent_source": parent_src})
                return d

    # ---- Rule 4: taste-path hard guard (R12) ----
    if tier == "L3":
        tgt = (req.get("target_path") or "").replace("\\", "/")
        if _L3_FEEDBACK_RE.match(tgt) and source not in _TASTE_PATH_ALLOWED_SOURCES:
            d = _decision(False, "taste_requires_ceo_approval", audit_id,
                          sanitized=sanitized)
            _audit(audit_id, d, rule_hit="taste_path")
            return d

    # ---- Rule 5: budget ----
    budget_ok, budget_reason = _check_budget(tier, source, req)
    if not budget_ok:
        # L1 writes queue; L3 writes fail closed. R3 P0-3: L1 NEVER fail_open.
        if tier == "L1":
            d = _decision(False, "budget_queued", audit_id,
                          sanitized=sanitized)
            _audit(audit_id, d, rule_hit="budget_l1_queue",
                   extra={"budget_reason": budget_reason})
            return d
        # L3
        if _flag_on("MEMEXA_GOVERNANCE_FAIL_OPEN", "0"):
            d = _decision(True, "budget_exhausted_fail_open_testonly",
                          audit_id, sanitized=sanitized)
            _audit(audit_id, d, rule_hit="budget_fail_open_testonly",
                   extra={"budget_reason": budget_reason})
            return d
        d = _decision(False, "budget_exhausted_fail_closed", audit_id,
                      sanitized=sanitized)
        _audit(audit_id, d, rule_hit="budget_l3_closed",
               extra={"budget_reason": budget_reason})
        return d

    # ---- Rule 6: consistency ----
    depth = get_governance_depth()
    veto: Optional[str] = None
    if depth == "shallow":
        veto = _shallow_consistency_check(sanitized)
    else:  # deep
        veto = _deep_consistency_check(sanitized, audit_id)
    if veto:
        d = _decision(False, "consistency_veto", audit_id,
                      sanitized=sanitized)
        _audit(audit_id, d, rule_hit=f"consistency_{depth}",
               extra={"veto_detail": veto})
        return d

    # ---- Rule 7: approved ----
    d = _decision(True, "approved", audit_id, sanitized=sanitized)
    _audit(audit_id, d, rule_hit="approved",
           extra={"sanitized_markers": removed[:8] if removed else []})
    return d


_EST_LLM_COST_USD = 0.003  # Haiku single-call typical (aligned with T6.5)


def _module_for_tier_source(tier: Tier, source: Optional[str],
                             agent_name: Optional[str]) -> str:
    """Map governance (tier, source, agent_name) tuple to llm_budget module name.

    llm_budget._ALLOWED_MODULES = {"ingest", "governance", "promotion"}.
    L3 writes (memory promotions) charge "promotion"; L1 ingest charges
    "ingest"; everything else charges "governance".
    """
    if tier == "L3":
        return "promotion"
    # [LOG-R2-001 2026-04-20] ceo_approved_promotion writes charge the
    # promotion module even if they arrive as L1 (unusual but possible
    # if a caller downgrades the tier).
    if source == "ceo_approved_promotion":
        return "promotion"
    agent = (agent_name or "").lower()
    if "ingest" in agent or source == "ceo_edit":
        return "ingest"
    return "governance"


def _check_budget(tier: Tier, source: Source, req: WriteRequest):
    """Thin wrapper around llm_budget.check_and_reserve.

    H1 (2026-04-20): the live llm_budget API takes positional
    (module, estimated_cost_usd). The previous kw-arg call raised TypeError
    every time, and the except branch silently soft-allowed both L1 and L3,
    turning the BUDGET gate into a no-op. This version:

      - Passes the correct positional signature.
      - Derives `module` from (tier, source, agent_name).
      - On import-failure sentinel (check_and_reserve is None, H2),
        fail-closed on L3, queue on L1.
      - On any other exception, also fail-closed on L3 and queue on L1
        (NEVER silent soft-allow).

    Returns (ok: bool, reason: str).
    """
    # H2: import-failure sentinel. Do NOT soft-allow.
    if check_and_reserve is None:
        if tier == "L3":
            return False, "budget_module_unavailable_fail_closed"
        return False, "budget_module_unavailable_L1_queue"

    module = _module_for_tier_source(tier, source, req.get("agent_name"))
    try:
        ok, reason = check_and_reserve(module, _EST_LLM_COST_USD)
        return bool(ok), str(reason)
    except TypeError as e:
        # Wrong signature means this deployment's llm_budget is stubbed
        # or monkey-patched to a legacy kw-arg shim. Try kw-arg fallback
        # (kept for the existing test suite that uses lambda **kw:),
        # but on failure fail-closed / queue — never silent allow.
        try:
            ok, reason = check_and_reserve(
                tier=tier,
                source=source,
                agent_name=req.get("agent_name"),
            )
            return bool(ok), str(reason)
        except Exception:
            if tier == "L3":
                return False, f"budget_interface_error_fail_closed:{type(e).__name__}"
            return False, f"budget_interface_error_L1_queue:{type(e).__name__}"
    except Exception as e:
        if tier == "L3":
            return False, f"budget_call_error_fail_closed:{type(e).__name__}"
        return False, f"budget_call_error_L1_queue:{type(e).__name__}"


# ---------------------------------------------------------------------------
# Tiny CLI for ad-hoc use (never called by production hooks)
# ---------------------------------------------------------------------------

def _cli():  # pragma: no cover - convenience only
    if len(sys.argv) < 2:
        print("usage: governance_middleware depth | check <json>", file=sys.stderr)
        return 1
    cmd = sys.argv[1]
    if cmd == "depth":
        print(get_governance_depth())
        return 0
    if cmd == "check":
        if len(sys.argv) < 3:
            print("check requires JSON", file=sys.stderr)
            return 2
        try:
            req = json.loads(sys.argv[2])
        except json.JSONDecodeError as e:
            print(f"bad JSON: {e}", file=sys.stderr)
            return 2
        d = pre_write_check(req)
        print(json.dumps(d, ensure_ascii=False))
        return 0 if d["allowed"] else 3
    print(f"unknown command {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli())
