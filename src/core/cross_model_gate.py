"""TU-2 part 2 (long_term_plan_v2 §3 U16): Cross-model gate.

Enforces:
1. judge_model != claim_producer_model at the **canonical family** level
   (HARD RULE feedback_cross_model_judge_opposite_model.md);
2. Disagreement counter is normalized BEFORE incrementing (numeric / categorical /
   text equivalence), to avoid spurious disagreements (security-iter1-5);
3. Tuple fallback: try first OPPOSITE candidate; on ProviderUnavailable, try
   second; if both fail, raise CrossModelSkipped + emit trace `cross_model_skipped`
   (RP-LOGIC-ITER1-3, logic-iter1-3 HIGH);
4. Circuit-breaker state file at `memexa/data/cross_model_breaker.json`
   (RP-LOGIC-ITER1-5; HARD RULE feedback_state_file_dual_path_discovery.md).

axis_anchor: [C:cli:cross_model_gate]
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Errors


class CrossModelGateError(RuntimeError):
    """Base class for cross_model_gate errors."""


class SameModelJudgeError(CrossModelGateError):
    """Raised when judge_model and claim_producer_model are the same canonical family."""


class CrossModelSkipped(CrossModelGateError):
    """Raised when both OPPOSITE_FAMILY candidates are unavailable.

    RP-LOGIC-ITER1-3: callers may catch this and decide warn-vs-block based on AC criticality.
    """


# Canonical family map per RP-LOGIC-ITER1-2/3 + verifier-2 + architect-1
# When judge_model=None, picks first OPPOSITE family; on unavailable, picks second.
OPPOSITE_FAMILY: Dict[str, Tuple[str, str]] = {
    "claude-sonnet": ("claude-opus", "deepseek"),
    "claude-opus":   ("claude-sonnet", "deepseek"),
    "deepseek":      ("claude-sonnet", "claude-opus"),
    # GLM, OpenAI families can be added later; for now treat as
    # "deepseek-style external" via canonical_family fallback
}

KNOWN_FAMILIES = set(OPPOSITE_FAMILY.keys())


def _canonical_family(model_name: str) -> str:
    """Normalize a model name to its canonical family.

    Examples:
        claude-sonnet-4-6      -> claude-sonnet
        claude-sonnet-4-6-2025 -> claude-sonnet
        claude-opus-4-7        -> claude-opus
        deepseek-v4-pro        -> deepseek
        deepseek-chat          -> deepseek
        unknown-foo            -> unknown-foo (lowercased)
    """
    if not model_name:
        return ""
    name = model_name.strip().lower()
    if name.startswith("claude-sonnet"):
        return "claude-sonnet"
    if name.startswith("claude-opus"):
        return "claude-opus"
    if name.startswith("claude-haiku"):
        return "claude-haiku"
    if name.startswith("deepseek"):
        return "deepseek"
    if name.startswith("gpt-") or name.startswith("openai-"):
        return "openai"
    if name.startswith("glm-") or "zhipu" in name:
        return "glm"
    return name  # unknown / pass-through


# Numeric normalization


_NUMERIC_PATTERN = re.compile(
    r"(?P<sign>[\u2212\u2013\u2014\-+])?\s*"
    r"(?P<num>\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)"
    r"\s*(?P<unit>%|µeV|ueV|eV|meV|μeV|nm|Hz|s|GHz|MHz|kHz|Hartree|Ha)?"
)


def _normalize_numeric(s: str) -> Optional[float]:
    """Extract a single number from a string, handling % / e-notation / unicode minus.

    Per RP-LOGIC-ITER1-2: percent canonicalization is "strip % then divide by 100"
    (NOT "parse number only"). So `5%` → 0.05, `0%` → 0.0.
    Unit-bearing numbers (`5 µeV`) drop the unit and keep the magnitude.

    Returns None if no number present.
    """
    if s is None:
        return None
    # NFC normalize so unicode minus etc. are handled
    s = unicodedata.normalize("NFC", str(s)).strip()
    # Replace unicode minus / en-dash / em-dash with ascii minus
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    m = _NUMERIC_PATTERN.search(s)
    if not m:
        return None
    sign = m.group("sign") or "+"
    num_str = m.group("num")
    unit = m.group("unit") or ""
    try:
        v = float(num_str)
    except ValueError:
        return None
    if sign == "-":
        v = -v
    if unit == "%":
        # RP-LOGIC-ITER1-2: strip % then divide by 100
        v = v / 100.0
    return v


def _compare_with_tolerance(
    a: Optional[float], b: Optional[float],
    rel_tol: float = 1e-3, abs_tol: float = 1e-9,
) -> bool:
    """Numeric equivalence under math.isclose semantics.

    Per RP-LOGIC-ITER1-2: rel_tol=1e-3 + abs_tol=1e-9 covers the 0-baseline
    div-by-zero edge (e.g. `0%` vs `0.0001%` → 0.0 vs 0.000001 → not close).
    """
    if a is None or b is None:
        return False
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


# Categorical normalization


_CATEGORICAL_SYNONYMS: Dict[str, str] = {
    "approved": "APPROVED", "ok": "APPROVED", "pass": "APPROVED",
    "passed": "APPROVED", "yes": "APPROVED", "true": "APPROVED",
    "reject": "REJECT", "rejected": "REJECT", "fail": "REJECT",
    "failed": "REJECT", "no": "REJECT", "false": "REJECT",
    "revise": "REVISE", "revise_required": "REVISE", "revise required": "REVISE",
    "skip": "SKIP", "skipped": "SKIP",
}


def _normalize_categorical(s: str) -> str:
    """Map case-variant or synonym strings to canonical category.

    Returns the input (lowercased) unchanged if no canonical match.
    """
    if not s:
        return ""
    key = unicodedata.normalize("NFC", str(s).strip().lower()).strip("`*\"' ")
    return _CATEGORICAL_SYNONYMS.get(key, key)


# Equivalence


class Equivalence(str, Enum):
    EQUIVALENT = "equivalent"
    DIVERGENT = "divergent"
    UNCLEAR = "unclear"


@dataclass
class CrossModelResult:
    """Result of cross_model_verify."""
    equivalence: Equivalence
    claim_producer: str
    judge_model: str
    canonical_producer: str
    canonical_judge: str
    raw_a: str
    raw_b: str
    normalized_a: Any
    normalized_b: Any
    normalization_kind: str  # "numeric" | "categorical" | "text"
    blocked: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# Circuit breaker


def _workspace_root() -> Path:
    """Resolve workspace root via Path(__file__).resolve().parents[2] (RP-LOGIC-ITER1-5).

    From memexa/core/cross_model_gate.py → memexa/.
    """
    return Path(__file__).resolve().parents[2]


def _breaker_path() -> Path:
    """Circuit-breaker state file path.

    Pinned to memexa/data/cross_model_breaker.json (NOT memexa/memexa/data/)
    per HARD RULE feedback_state_file_dual_path_discovery.md.
    """
    return _workspace_root() / "data" / "cross_model_breaker.json"


def _read_breaker() -> Dict[str, Any]:
    p = _breaker_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_breaker(state: Dict[str, Any]) -> None:
    """Atomic write via temp file + os.replace (security-iter1-2 MED fix).

    Concurrent autopilot sessions may write breaker concurrently; raw
    write_text() can leave a partial JSON file. We write to a temp file
    then atomic-rename via os.replace. Read-modify-write is still racy
    in the sense that two writers may overlap, but no caller will ever
    read a partially-written JSON file.
    """
    import os as _os
    p = _breaker_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        _os.replace(str(tmp), str(p))
    except OSError as e:
        logger.warning("cross_model_gate: breaker write failed: %s", e)


def _is_circuit_open(family: str) -> bool:
    state = _read_breaker()
    fam_state = state.get(family, {})
    consecutive_5xx = fam_state.get("consecutive_5xx", 0)
    last_failure = fam_state.get("last_failure_ts", 0.0)
    if consecutive_5xx >= 3 and (time.time() - last_failure) < 60:
        return True
    return False


def _record_failure(family: str) -> None:
    state = _read_breaker()
    fam_state = state.setdefault(family, {})
    fam_state["consecutive_5xx"] = fam_state.get("consecutive_5xx", 0) + 1
    fam_state["last_failure_ts"] = time.time()
    _write_breaker(state)


def _record_success(family: str) -> None:
    state = _read_breaker()
    fam_state = state.setdefault(family, {})
    fam_state["consecutive_5xx"] = 0
    _write_breaker(state)


# Main public API


def _select_judge_family(
    claim_producer: str,
    judge_model_explicit: Optional[str],
) -> Tuple[str, str]:
    """Pick a judge family. Returns (judge_family_canonical, source_label).

    Rules:
        explicit judge_model     -> use its canonical family
        judge_model=None         -> pick first OPPOSITE_FAMILY[claim_producer] candidate
        unknown claim_producer   -> raise SameModelJudgeError (cannot guarantee opposition)
    """
    canonical_producer = _canonical_family(claim_producer)
    if judge_model_explicit:
        return _canonical_family(judge_model_explicit), "explicit"
    opp = OPPOSITE_FAMILY.get(canonical_producer)
    if not opp:
        raise SameModelJudgeError(
            f"unknown canonical family for claim_producer={claim_producer!r}; "
            f"cannot guarantee opposite-family judge"
        )
    return opp[0], "auto_first"


def _try_alternate_judge(claim_producer: str, primary: str) -> Optional[str]:
    """Pick the second OPPOSITE family if available."""
    canonical_producer = _canonical_family(claim_producer)
    opp = OPPOSITE_FAMILY.get(canonical_producer)
    if not opp:
        return None
    primary_fam = _canonical_family(primary)
    for cand in opp:
        if cand != primary_fam:
            return cand
    return None


def _provider_available(family: str) -> bool:
    """Check whether a model family's provider is available.

    Used by tuple fallback (RP-LOGIC-ITER1-3 + logic-iter1-1): when first
    OPPOSITE_FAMILY candidate is unavailable (no key, circuit-broken),
    try second.
    """
    import os as _os
    if _is_circuit_open(family):
        return False
    if family == "deepseek":
        return bool(_os.environ.get("DEEPSEEK_API_KEY", ""))
    # claude-sonnet, claude-opus, claude-haiku via CLI assumed available
    # (real availability check would be more elaborate)
    return True


def request_tu_cross_model_review(task_id: str, tu_id: str,
                                   tu_description: str = "") -> Dict[str, Any]:
    """I-2 (Phase 4, 2026-05-04): emit a trace marker for TU-level cross-model
    review. Triggered by task_unit_scheduler.mark_done when the TU's plan
    declaration contains `cross_model_required: true`.

    Why an emit-only (not actual run) entry: real LLM cross-model calls cost
    tokens; we wire the trigger now so Phase 6 V-3 dogfood can pick LIVE TUs
    that opted in. Actual cross_model_verify call happens at Stage 4 reviewer
    or via separate dogfood cron, not synchronously per-TU.

    Returns dict with {requested: bool, tu_id, task_id, ts}.
    """
    payload = {
        "task_id": task_id,
        "tu_id": tu_id,
        "description_excerpt": (tu_description or "")[:200],
        "requested": True,
        "wire_status": "trigger_emitted_pending_dogfood_run",
    }
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("cross_model_tu_review_requested", payload)
    except Exception:
        pass
    return payload


def detect_cross_model_tus(plan_text: str) -> list:
    """I-2 helper: scan plan_text for TUs declaring `cross_model_required: true`
    (or `cross_model_required: yes`). Returns list of TU ids.

    Implementation: split into TU blocks (each `### TU-N` boundary), check each
    block independently to avoid greedy cross-block false positives.
    """
    if not plan_text:
        return []
    import re as _re
    out = []
    # Find all TU heading positions
    headings = list(_re.finditer(r"###\s+TU[-_]?([\w.]+)", plan_text))
    for i, m in enumerate(headings):
        block_start = m.end()
        block_end = headings[i + 1].start() if i + 1 < len(headings) else len(plan_text)
        block = plan_text[block_start:block_end]
        if _re.search(r"cross_model_required\s*[:=]\s*(?:true|yes)\b",
                      block, _re.IGNORECASE):
            out.append(f"TU-{m.group(1)}")
    return out


def cross_model_verify(
    claim_a: str,
    claim_b: str,
    claim_producer_model: str,
    judge_model: Optional[str] = None,
    normalize: bool = True,
    rel_tol: float = 1e-3,
    abs_tol: float = 1e-9,
    require_judge_available: bool = False,
) -> CrossModelResult:
    """Verify two claims (a, b) for equivalence under normalization.

    The "claim_producer_model" is the model that produced claim_a (and possibly
    claim_b if same model). The judge_model is what arbitrates if normalization
    is inconclusive — it MUST be from a different canonical family.

    Args:
        claim_a, claim_b: text claims to compare
        claim_producer_model: model that produced claim_a
        judge_model: explicit judge override (opposite family). None → auto-pick
            with tuple fallback (logic-iter1-1: try first, then second OPPOSITE).
        normalize: if True, attempt numeric / categorical normalization first
        rel_tol, abs_tol: numeric tolerance
        require_judge_available: if True, BOTH OPPOSITE candidates must be
            unavailable to raise CrossModelSkipped (full fallback exercised).

    Raises:
        SameModelJudgeError: judge_model same canonical family as claim_producer
        CrossModelSkipped: both OPPOSITE_FAMILY candidates unavailable

    Returns:
        CrossModelResult.
    """
    import hashlib as _hashlib
    canonical_producer = _canonical_family(claim_producer_model)

    # Step 1: pick judge family with tuple fallback (logic-iter1-1 HIGH fix)
    judge_family, source = _select_judge_family(claim_producer_model, judge_model)

    # When auto-picking and require_judge_available, exercise tuple fallback
    if (judge_model is None and require_judge_available
            and not _provider_available(judge_family)):
        alt = _try_alternate_judge(claim_producer_model, judge_family)
        if alt and _provider_available(alt):
            judge_family = alt
            source = "auto_alternate"
        else:
            # Both OPPOSITE candidates unavailable; raise CrossModelSkipped
            try:
                from src.core.trace_sink import write_trace_event
                write_trace_event("cross_model_skipped", {
                    "claim_producer_family": canonical_producer,
                    "tried": [judge_family] + ([alt] if alt else []),
                    "reason": "both_opposites_unavailable",
                })
            except Exception:
                pass
            raise CrossModelSkipped(
                f"both OPPOSITE_FAMILY candidates unavailable for "
                f"claim_producer={canonical_producer!r}; tried={[judge_family, alt]}"
            )

    # Code-level invariant per HARD RULE (architect-1 + verifier-2): canonical-family compare
    if judge_family == canonical_producer:
        raise SameModelJudgeError(
            f"judge_family={judge_family!r} == claim_producer_family={canonical_producer!r}; "
            f"OWASP LLM01 self-attestation hole; pick OPPOSITE family"
        )

    # Step 2: normalize and compare
    norm_kind = "text"
    eq = Equivalence.UNCLEAR
    norm_a: Any = claim_a
    norm_b: Any = claim_b

    if normalize:
        # Try numeric
        a_num = _normalize_numeric(claim_a)
        b_num = _normalize_numeric(claim_b)
        if a_num is not None and b_num is not None:
            norm_kind = "numeric"
            norm_a, norm_b = a_num, b_num
            eq = (Equivalence.EQUIVALENT
                  if _compare_with_tolerance(a_num, b_num, rel_tol, abs_tol)
                  else Equivalence.DIVERGENT)
        else:
            # Try categorical
            a_cat = _normalize_categorical(claim_a)
            b_cat = _normalize_categorical(claim_b)
            if (a_cat in {"APPROVED", "REJECT", "REVISE", "SKIP"}
                    or b_cat in {"APPROVED", "REJECT", "REVISE", "SKIP"}):
                norm_kind = "categorical"
                norm_a, norm_b = a_cat, b_cat
                eq = (Equivalence.EQUIVALENT if a_cat == b_cat
                      else Equivalence.DIVERGENT)
            # else fallback to text path → keep eq=UNCLEAR; LLM judge would resolve
            # but offline tests/mocked-API path leaves UNCLEAR

    # Emit normalized trace — security-iter1-3 + security-iter2-1 MED/LOW fix:
    # scrub raw claim text AND text-path normalized values (may contain API keys
    # / PII). For text fallback, persist sha256 only. For numeric/categorical,
    # persist normalized scalar (already canonical, low-leak).
    try:
        from src.core.trace_sink import write_trace_event
        a_str = str(claim_a)
        b_str = str(claim_b)
        if norm_kind == "text":
            # security-iter2-1 LOW: text fallback may carry sensitive content
            norm_a_safe = _hashlib.sha256(str(norm_a).encode()).hexdigest()[:16]
            norm_b_safe = _hashlib.sha256(str(norm_b).encode()).hexdigest()[:16]
        else:
            # numeric/categorical normalized values are scalars; safe to log
            norm_a_safe = str(norm_a)[:40]
            norm_b_safe = str(norm_b)[:40]
        write_trace_event("cross_model_normalized", {
            "claim_producer_family": canonical_producer,
            "judge_family": judge_family,
            "judge_source": source,
            "normalization_kind": norm_kind,
            "raw_a_sha256": _hashlib.sha256(a_str.encode()).hexdigest()[:16],
            "raw_b_sha256": _hashlib.sha256(b_str.encode()).hexdigest()[:16],
            "raw_a_len": len(a_str),
            "raw_b_len": len(b_str),
            "normalized_a": norm_a_safe,
            "normalized_b": norm_b_safe,
            "equivalence": eq.value,
        })
    except Exception:
        pass

    return CrossModelResult(
        equivalence=eq,
        claim_producer=claim_producer_model,
        judge_model=judge_model or judge_family,
        canonical_producer=canonical_producer,
        canonical_judge=judge_family,
        raw_a=str(claim_a),
        raw_b=str(claim_b),
        normalized_a=norm_a,
        normalized_b=norm_b,
        normalization_kind=norm_kind,
    )


def increment_disagreement_if_divergent(
    result: CrossModelResult,
    iter_state: Dict[str, int],
    threshold: int = 2,
) -> bool:
    """If result.equivalence == DIVERGENT, increment counter; trip block if > threshold.

    Plan TU-2 action-7: ">2 per Stage 4 iter → BLOCK" (strictly greater than).
    Returns True if the gate should BLOCK (count > threshold).
    """
    if result.equivalence != Equivalence.DIVERGENT:
        return False
    iter_state["disagreement_count"] = iter_state.get("disagreement_count", 0) + 1
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("cross_model_disagreement", {
            "claim_producer_family": result.canonical_producer,
            "judge_family": result.canonical_judge,
            "disagreement_count": iter_state["disagreement_count"],
            "threshold": threshold,
            "normalization_kind": result.normalization_kind,
        })
    except Exception:
        pass
    # logic-iter1-3 MED fix: align docstring with plan's "> threshold" semantics.
    # Plan TU-2 action-7 says "disagreements >2 per Stage 4 iter → BLOCK", i.e.
    # strictly greater than threshold. Original code is correct; docstring updated
    # in the function-level comment above to match.
    if iter_state["disagreement_count"] > threshold:
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("cross_model_block", {
                "disagreement_count": iter_state["disagreement_count"],
                "threshold": threshold,
            })
        except Exception:
            pass
        return True
    return False


__all__ = [
    "OPPOSITE_FAMILY",
    "KNOWN_FAMILIES",
    "_canonical_family",
    "_normalize_numeric",
    "_normalize_categorical",
    "_compare_with_tolerance",
    "_breaker_path",
    "_workspace_root",
    "Equivalence",
    "CrossModelResult",
    "SameModelJudgeError",
    "CrossModelSkipped",
    "cross_model_verify",
    "increment_disagreement_if_divergent",
]
