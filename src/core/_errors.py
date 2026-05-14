"""Gate infrastructure error types.

GateInfraError surfaces failures in gate-critical infrastructure (flag writes,
spec generation, etc.) that MUST NOT be silenced with a bare ``except Exception: pass``.

Design intent
-------------
- Raised only when a gate-critical write/read fails in a way that would leave
  the system in an unknown or unsafe state (e.g., autopilot_active flag not
  written means fail-closed gates fall back to fail-open).
- Callers MUST write a trace event BEFORE raising this exception so the failure
  is observable even if the raise is later caught somewhere up the stack.
- The ``where`` and ``context`` fields are for operator dashboards; they are
  never user-facing strings and must not contain secret material.
"""
from __future__ import annotations


class GateInfraError(Exception):
    """Raised when a gate-infrastructure operation fails in a way that cannot
    be silenced.

    Unlike a bare ``OSError``, this class signals to callers that the error
    originates specifically inside gate infrastructure and must be surfaced
    rather than swallowed.

    Args:
        message: Human-readable failure description (no PII).
        where: Dotted module.function path for fast triage (e.g.
               ``"_autopilot_flag.set_flag"``).
        context: Optional free-form dict with diagnostic detail (e.g.
                 ``{"error": str(e)[:200]}``).
    """

    def __init__(
        self,
        message: str,
        *,
        where: str = "",
        context: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.where = where
        self.context = context or {}
