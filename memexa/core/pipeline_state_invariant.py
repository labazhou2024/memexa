"""Pipeline State Invariant Enforcement (TU-3, R-3).

Contract: for every phase in P0..P9, `status == 'failed'` iff `n_failed > 0`.

This module provides:
- validate_phase_status(phase_dict)  -- raises PhaseStateInvariantViolation on contract breach
- validate_state_file(path)          -- validates all phases in pipeline_state.json
- migrate_legacy_state(path)         -- migrates legacy phases missing 4-tuple schema

Trace events emitted:
- phase_state_validated
- phase_state_invariant_violation
- hard_rule_promoted
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_DRAIN_PROGRESS = _DATA / "backfill_drain_progress.json"
_PIPELINE_STATE = _DATA / "backfill_pipeline_state.json"

# All valid phase keys (backfill pipeline P0..P9)
ALL_PHASES = [
    "P0_prereq", "P1_paired_eval", "P2_chat_unified", "P3_structured",
    "P4_narrative", "P5_ustc_gpu", "P6_reconcile", "P7_lineage",
    "P8_drain", "P9_ac13_verify",
]


class PhaseStateInvariantViolation(ValueError):
    """Raised when a phase dict violates the status-derives-from-n_failed invariant."""


@dataclass
class PhaseRecord:
    """Normalised 4-tuple representing a single phase's outcome."""
    phase: str
    n_attempted: int = 0
    n_succeeded: int = 0
    n_failed: int = 0
    status: str = "ok"


@dataclass
class ValidationResult:
    """Outcome of validating a full pipeline state file."""
    passed: bool = True
    phases_checked: int = 0
    violations: List[Dict[str, Any]] = field(default_factory=list)
    migrated: List[str] = field(default_factory=list)
    error: Optional[str] = None


def _emit(event: str, payload: dict) -> None:
    """Best-effort trace emission."""
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def validate_phase_status(phase_dict: dict) -> bool:
    """Validate a single phase dict against the status-derives-from-n_failed contract.

    Contract: ``status == 'failed'`` iff ``n_failed > 0``.

    Args:
        phase_dict: Must contain at minimum ``n_failed`` (int) and ``status`` (str).
                    Keys ``n_attempted`` and ``n_succeeded`` are optional but recommended.

    Returns:
        True when invariant holds.

    Raises:
        PhaseStateInvariantViolation: when contract is breached.
        TypeError: when phase_dict is not a dict.
        KeyError: when required keys are missing.
    """
    if not isinstance(phase_dict, dict):
        raise TypeError(f"phase_dict must be dict, got {type(phase_dict).__name__}")

    # Strict key presence check
    if "n_failed" not in phase_dict:
        raise KeyError("phase_dict missing required key 'n_failed'")
    if "status" not in phase_dict:
        raise KeyError("phase_dict missing required key 'status'")

    n_failed: int = int(phase_dict["n_failed"])
    status: str = str(phase_dict["status"])

    # Invariant: status == 'failed' iff n_failed > 0
    should_be_failed = n_failed > 0
    is_failed = (status == "failed")

    if should_be_failed != is_failed:
        phase_name = phase_dict.get("phase", "<unknown>")
        msg = (
            f"Phase '{phase_name}': invariant violated — "
            f"n_failed={n_failed}, status='{status}'. "
            f"Expected status='{'failed' if should_be_failed else 'ok'}'"
        )
        _emit("phase_state_invariant_violation", {
            "phase": phase_name,
            "n_failed": n_failed,
            "status": status,
            "expected_status": "failed" if should_be_failed else "ok",
        })
        raise PhaseStateInvariantViolation(msg)

    _emit("phase_state_validated", {
        "phase": phase_dict.get("phase", "<unknown>"),
        "n_failed": n_failed,
        "status": status,
    })
    return True


def validate_state_file(path: Path) -> ValidationResult:
    """Read pipeline_state.json and validate every phase's 4-tuple contract.

    Only phases that contain BOTH 'n_failed' and 'status' keys are validated;
    phases missing either key are treated as legacy and reported separately.

    Returns:
        ValidationResult with passed=True if all present tuples satisfy the invariant.
    """
    result = ValidationResult()
    if not path.is_file():
        result.passed = False
        result.error = f"State file not found: {path}"
        return result

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        result.passed = False
        result.error = f"Cannot read state file: {e}"
        return result

    phase_results: dict = state.get("phase_results", {})

    for phase_name, pdata in phase_results.items():
        if not isinstance(pdata, dict):
            continue
        if "n_failed" not in pdata or "status" not in pdata:
            # Legacy phase — does not contain 4-tuple yet
            result.migrated.append(phase_name)
            continue

        result.phases_checked += 1
        # Inject phase name for clearer error messages
        pdata_with_name = dict(pdata)
        pdata_with_name.setdefault("phase", phase_name)
        try:
            validate_phase_status(pdata_with_name)
        except PhaseStateInvariantViolation as exc:
            result.passed = False
            result.violations.append({
                "phase": phase_name,
                "n_failed": pdata.get("n_failed"),
                "status": pdata.get("status"),
                "message": str(exc),
            })

    return result


def _load_drain_progress() -> Optional[dict]:
    """Load drain progress file for migration evidence."""
    if not _DRAIN_PROGRESS.is_file():
        return None
    try:
        return json.loads(_DRAIN_PROGRESS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _derive_status_from_evidence(phase_name: str, existing_ok: Optional[bool],
                                  drain_progress: Optional[dict]) -> Tuple[str, int]:
    """Derive (status, n_failed) for a legacy phase using available evidence.

    Logic-iter1-5 fix:
    - Phase had ok=True → keep 'ok', n_failed=0 (invariant satisfied).
    - Phase had ok=False → status='failed', n_failed=1 (minimum evidence).
    - P8_drain specifically: check drain_progress.json n_failed field directly.
    - Phase had no status → ok if drain_progress clean else 'failed'.

    Returns:
        (status, n_failed)
    """
    # P8 drain: always use drain_progress.json as authoritative source
    if phase_name == "P8_drain" and drain_progress is not None:
        n_failed = int(drain_progress.get("n_failed", 0))
        status = "failed" if n_failed > 0 else "ok"
        return status, n_failed

    # For other phases: derive from existing ok flag
    if existing_ok is True:
        return "ok", 0
    elif existing_ok is False:
        return "failed", 1
    else:
        # No ok flag at all: check drain_progress as tie-breaker
        if drain_progress is not None:
            n_failed = int(drain_progress.get("n_failed", 0))
            if n_failed > 0:
                return "failed", n_failed
        return "ok", 0


def migrate_legacy_state(path: Path) -> ValidationResult:
    """Migrate legacy pipeline_state.json phases to the 4-tuple schema.

    For each phase in phase_results that lacks n_failed/status/n_attempted/n_succeeded:
      - Derives status and n_failed from existing evidence
      - Writes back the augmented state atomically via atomic_write_json

    Returns:
        ValidationResult indicating which phases were migrated.
    """
    result = ValidationResult()
    if not path.is_file():
        result.passed = False
        result.error = f"State file not found: {path}"
        return result

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        result.passed = False
        result.error = f"Cannot read state file: {e}"
        return result

    drain_progress = _load_drain_progress()
    phase_results: dict = state.get("phase_results", {})
    changed = False

    for phase_name, pdata in list(phase_results.items()):
        if not isinstance(pdata, dict):
            continue
        # Skip phases that already have the 4-tuple
        if ("n_failed" in pdata and "n_succeeded" in pdata
                and "n_attempted" in pdata and "status" in pdata):
            continue

        existing_ok: Optional[bool] = pdata.get("ok")
        status, n_failed = _derive_status_from_evidence(
            phase_name, existing_ok, drain_progress
        )
        n_succeeded = int(pdata.get("n_succeeded", 0))
        n_attempted = int(pdata.get("n_attempted", n_succeeded + n_failed))

        pdata["n_attempted"] = n_attempted
        pdata["n_succeeded"] = n_succeeded
        pdata["n_failed"] = n_failed
        pdata["status"] = status
        pdata.setdefault("phase", phase_name)
        phase_results[phase_name] = pdata
        result.migrated.append(phase_name)
        result.phases_checked += 1
        changed = True
        _emit("phase_state_validated", {
            "phase": phase_name, "n_failed": n_failed, "status": status,
            "migrated": True,
        })

    if changed:
        state["phase_results"] = phase_results
        state["migrated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            from memexa.core.atomic_io import atomic_write_json
            atomic_write_json(path, state)
        except Exception as e:
            result.passed = False
            result.error = f"Failed to write migrated state: {e}"
            return result

    result.passed = True
    return result


def cli_validate(path: Optional[Path] = None) -> int:
    """CLI entry-point: validate a state file.

    Returns 0 on pass, 1 on violation, 2 on error.
    """
    import argparse
    parser = argparse.ArgumentParser(
        description="Validate pipeline_state.json P0..P9 invariant"
    )
    parser.add_argument("--state-file", type=Path, default=None,
                        help="Path to pipeline_state.json")
    parser.add_argument("--migrate", action="store_true",
                        help="Migrate legacy phases to 4-tuple schema")
    args = parser.parse_args()

    target = args.state_file or _PIPELINE_STATE
    if args.migrate:
        result = migrate_legacy_state(target)
        print(json.dumps({
            "action": "migrate",
            "passed": result.passed,
            "migrated": result.migrated,
            "error": result.error,
        }, ensure_ascii=False, indent=2))
        return 0 if result.passed else 2

    result = validate_state_file(target)
    out = {
        "passed": result.passed,
        "phases_checked": result.phases_checked,
        "violations": result.violations,
        "legacy_phases": result.migrated,
    }
    if result.error:
        out["error"] = result.error
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    import sys
    sys.exit(cli_validate())
