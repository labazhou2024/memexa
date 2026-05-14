"""TU-4 (plan_v1, 2026-04-25) — shared staleness thresholds.

Two distinct constants because consumers have distinct semantics
(documented per `feedback_priority_inverted_fallback_2x2_matrix.md`
discipline — different consumers, different thresholds, NOT collapsed).

Matrix:
                   consumer = REGEN              consumer = PROBE
    spec age <30m  spec ok                       spec ok (probe runs)
    30m..24h       spec ok                       STALE → BLOCK probe
    age >24h       STALE → REGEN spec            STALE → BLOCK probe

REGEN consumer: ``persistent_mode._is_spec_stale`` — when true,
    ``activate(autopilot)`` regenerates a fresh task_spec.json.
    Long horizon (24h) because regenerating mid-task wastes work.

PROBE consumer: ``mini_loop_runner._detect_stale_spec`` — when true,
    pre-commit probe is blocked (caller must re-bind/re-activate).
    Short horizon (30min) because probes need fresh state to attest.
"""
from __future__ import annotations

# Long horizon: triggers spec regeneration. Used by persistent_mode.
SPEC_STALE_REGEN_SEC: int = 24 * 3600

# Short horizon: blocks pre-commit probe. Used by mini_loop_runner.
SPEC_STALE_PROBE_SEC: int = 30 * 60

__all__ = ["SPEC_STALE_REGEN_SEC", "SPEC_STALE_PROBE_SEC"]
