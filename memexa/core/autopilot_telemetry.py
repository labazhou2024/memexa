"""
Autopilot telemetry wrappers (U2 plan_v2, 2026-04-26).

Thin convenience layer over `trace_sink.write_trace_event` for the seven
spec events that every autopilot run is required to emit, plus a
`@refresh_on_exit(stage_name)` decorator that fuses the
`persistent_mode.refresh_stage` call into the natural function-exit path
of each Stage block (so SKILL.md needs only one MUST-emit line per stage
instead of two boilerplate calls).

Design choices:

- All 7 emit_* helpers funnel through `_emit`, which honours the
  fail-soft contract of `trace_sink.write_trace_event` (never raises).
- The decorator emits `stage_refresh_decorator_fired` BEFORE delegating
  to `persistent_mode.refresh_stage`, so the trace witnesses the
  decorator firing even if `refresh_stage` is no-op (persistent mode off).
- Exceptions raised inside the wrapped function are NOT swallowed;
  refresh_stage still runs in `finally`. That matches the original SKILL
  contract (a raising stage should still tick its bookkeeping).
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Dict, Optional

from memexa.core.trace_sink import write_trace_event

logger = logging.getLogger(__name__)


def _emit(event: str, payload: Optional[Dict[str, Any]] = None) -> bool:
    return write_trace_event(event, payload or {})


def emit_council_spawn_batch(experts: list, mode: str, task_id: str = "") -> bool:
    """Stage 1.2: planning council launched N experts in parallel."""
    return _emit(
        "council_spawn_batch",
        {"experts": list(experts), "mode": mode, "task_id": task_id},
    )


def emit_position_submitted(role: str, task_id: str = "", verdict: str = "") -> bool:
    """Stage 1.2: one council expert wrote their position paper."""
    return _emit(
        "council_position_submitted",
        {"role": role, "task_id": task_id, "verdict": verdict},
    )


def emit_synthesis_complete(task_id: str, conflicts: int) -> bool:
    """Stage 1.2: synthesis.md + conflicts.md written."""
    return _emit(
        "council_synthesis_complete",
        {"task_id": task_id, "conflicts": int(conflicts)},
    )


def emit_plan_revision_approved(task_id: str, plan_version: int, reviewer: str) -> bool:
    """Stage 1.4: independent reviewer APPROVED plan_v<N>."""
    return _emit(
        "plan_revision_approved",
        {
            "task_id": task_id,
            "plan_version": int(plan_version),
            "reviewer": reviewer,
        },
    )


def emit_agent_spawned_for_task(role: str, task_id: str, axis_anchor: str = "") -> bool:
    """Stage 2 / 4: a sub-agent (executor or reviewer) was spawned."""
    return _emit(
        "agent_spawned_for_task",
        {"role": role, "task_id": task_id, "axis_anchor": axis_anchor},
    )


def emit_reviewer_fallback_triggered(role: str, reason: str, task_id: str = "") -> bool:
    """Stage 4: Mode-A→Mode-B fallback (auth_fail / stall / governance)."""
    return _emit(
        "reviewer_fallback_triggered",
        {"role": role, "reason": reason, "task_id": task_id},
    )


def emit_ac_verified(ac_id: str, exit_code: int, task_id: str = "") -> bool:
    """Stage 6: ac_verifier ran verify_cmd for one AC."""
    return _emit(
        "ac_verified",
        {"ac_id": ac_id, "exit_code": int(exit_code), "task_id": task_id},
    )


def refresh_on_exit(stage_name: str) -> Callable:
    """
    Decorator: call `persistent_mode.refresh_stage(stage_name)` after the
    wrapped function returns (or raises). Always emits a
    `stage_refresh_decorator_fired` trace event so SKILL.md MUST-emit
    coverage can be audited statically.

    Usage:
        @refresh_on_exit("stage_3_qc")
        def run_stage_3():
            ...
    """
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            finally:
                _emit("stage_refresh_decorator_fired", {"stage": stage_name})
                try:
                    from memexa.core.persistent_mode import refresh_stage
                    refresh_stage(stage_name)
                except Exception as exc:
                    # fail-soft: telemetry must never break the wrapped
                    # stage's exit path. We log at WARNING so the failure
                    # is observable in stderr / logs even though no new
                    # trace event is registered for this path (security-
                    # iter1-1 finding 2026-04-26: emitting a new event
                    # type would itself need atomic _ALLOWED_EVENTS
                    # registration; deferred to U18 super-gate audit).
                    logger.warning(
                        "refresh_on_exit(%s): refresh_stage raised %s",
                        stage_name, exc,
                    )
        return wrapper
    return deco


__all__ = [
    "emit_council_spawn_batch",
    "emit_position_submitted",
    "emit_synthesis_complete",
    "emit_plan_revision_approved",
    "emit_agent_spawned_for_task",
    "emit_reviewer_fallback_triggered",
    "emit_ac_verified",
    "refresh_on_exit",
]
