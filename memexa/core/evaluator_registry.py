"""TU-2 part 1 (long_term_plan_v2 §3 U16): Numeric-AC resolver.

Picks the *best* deterministic evaluator for a claim. LLM judgement on numeric
claims is forbidden when a deterministic evaluator exists (per BL-4 / Stage 4
enforcement rule).

Tie-break order (priority high → low): pytest > ac_verifier > physics_gate > numerical_diff.

axis_anchor: [C:cli:cross_model_gate]  (shared with cross_model_gate.py)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class EvaluatorKind(str, Enum):
    PYTEST = "pytest"
    AC_VERIFIER = "ac_verifier"
    PHYSICS_GATE = "physics_gate"
    NUMERICAL_DIFF = "numerical_diff"


# Lower number = higher priority
_PRIORITY: Dict[EvaluatorKind, int] = {
    EvaluatorKind.PYTEST: 1,
    EvaluatorKind.AC_VERIFIER: 2,
    EvaluatorKind.PHYSICS_GATE: 3,
    EvaluatorKind.NUMERICAL_DIFF: 4,
}


@dataclass
class EvaluatorResult:
    """Result of an evaluator invocation."""
    evaluator_name: str
    kind: EvaluatorKind
    success: bool
    message: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Evaluator:
    """A registered deterministic evaluator."""
    name: str
    kind: EvaluatorKind
    invoke: Callable[..., EvaluatorResult]
    priority: int = 99

    def __post_init__(self) -> None:
        if self.priority == 99 and self.kind in _PRIORITY:
            self.priority = _PRIORITY[self.kind]


_REGISTRY: Dict[str, Evaluator] = {}


def register(evaluator: Evaluator) -> None:
    """Register an evaluator. Replaces any prior registration with the same name."""
    _REGISTRY[evaluator.name] = evaluator
    logger.info(
        "evaluator_registry: registered %s (kind=%s priority=%d)",
        evaluator.name, evaluator.kind.value, evaluator.priority,
    )


def get(name: str) -> Optional[Evaluator]:
    return _REGISTRY.get(name)


def list_evaluators() -> List[Evaluator]:
    return list(_REGISTRY.values())


def resolve_best(
    claim: str,
    ac_id: Optional[str] = None,
    candidates: Optional[List[str]] = None,
) -> Optional[Evaluator]:
    """Select the best evaluator for a claim.

    Tie-break: lowest priority number first (pytest=1 wins over ac_verifier=2).
    If candidates is given, restrict to those names. Otherwise consider all.

    Args:
        claim: The claim text being verified (used for trace, not selection).
        ac_id: Optional AC id for trace context.
        candidates: Optional list of evaluator names to restrict choice to.

    Returns:
        Best Evaluator (lowest priority number) or None if registry empty.
    """
    pool = list(_REGISTRY.values())
    if candidates is not None:
        pool = [e for e in pool if e.name in candidates]
    if not pool:
        # Emit trace event so reader-writer schema-contract is preserved
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("evaluator_pair_resolution", {
                "ac_id": ac_id or "n/a",
                "selected": None,
                "pool_size": 0,
                "reason": "empty_pool",
            })
        except Exception:
            pass
        return None
    best = min(pool, key=lambda e: e.priority)
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("evaluator_pair_resolution", {
            "ac_id": ac_id or "n/a",
            "selected": best.name,
            "selected_kind": best.kind.value,
            "selected_priority": best.priority,
            "pool_size": len(pool),
            "pool": [e.name for e in pool],
        })
    except Exception:
        pass
    return best


def clear() -> None:
    """Clear the registry (for tests only)."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Default evaluators (registered lazily on first import)
# ---------------------------------------------------------------------------


def _default_pytest_invoke(claim: str, **kwargs: Any) -> EvaluatorResult:
    """Stub-only default evaluator. Real invocation lives at higher layer."""
    return EvaluatorResult(
        evaluator_name="pytest_exit_code",
        kind=EvaluatorKind.PYTEST,
        success=False,
        message="default-stub: real pytest invocation lives in ac_verifier.run",
    )


def _default_ac_verifier_invoke(claim: str, **kwargs: Any) -> EvaluatorResult:
    return EvaluatorResult(
        evaluator_name="ac_verifier_run",
        kind=EvaluatorKind.AC_VERIFIER,
        success=False,
        message="default-stub: real call wires through ac_verifier.run",
    )


def _default_physics_gate_invoke(claim: str, **kwargs: Any) -> EvaluatorResult:
    return EvaluatorResult(
        evaluator_name="physics_gate_check",
        kind=EvaluatorKind.PHYSICS_GATE,
        success=False,
        message="default-stub: real call wires through physics_gate.check_toy_present",
    )


def _default_numerical_diff_invoke(claim: str, **kwargs: Any) -> EvaluatorResult:
    return EvaluatorResult(
        evaluator_name="numerical_diff",
        kind=EvaluatorKind.NUMERICAL_DIFF,
        success=False,
        message="default-stub: real call uses numpy.allclose on two values",
    )


def _register_defaults() -> None:
    if "pytest_exit_code" in _REGISTRY:
        return  # idempotent
    register(Evaluator(name="pytest_exit_code", kind=EvaluatorKind.PYTEST,
                       invoke=_default_pytest_invoke))
    register(Evaluator(name="ac_verifier_run", kind=EvaluatorKind.AC_VERIFIER,
                       invoke=_default_ac_verifier_invoke))
    register(Evaluator(name="physics_gate_check", kind=EvaluatorKind.PHYSICS_GATE,
                       invoke=_default_physics_gate_invoke))
    register(Evaluator(name="numerical_diff", kind=EvaluatorKind.NUMERICAL_DIFF,
                       invoke=_default_numerical_diff_invoke))


_register_defaults()


__all__ = [
    "EvaluatorKind",
    "EvaluatorResult",
    "Evaluator",
    "register",
    "get",
    "list_evaluators",
    "resolve_best",
    "clear",
]
