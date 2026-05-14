"""U7 (long_term_plan_v2.md, 2026-04-27) — mini-replan progress trigger.

Sister module to `re_planner.py`: where re_planner is the *failure-trigger*
recovery path (mark_failed → trigger_replan=True → architect re-plan), this
module is the *progress-trigger* fanout path. Phase-N completion OR ≥10-TU
mark_done events drive `check_and_trigger`, which optionally calls an architect
agent to APPEND new TUs to the plan (no rewrites, no done-revert).

Architecture (per long_term_plan_v2 §480 + plan_v2 BLOCKERs):

  reuses verbatim from re_planner:
    _validate_delta, _extract_json, _safe_output_path, _call_architect_live,
    _apply_delta, _append_merge_log, _append_amendment_to_plan_md

  new in this module:
    check_and_trigger(task_id) -> Optional[str]
        Returns a trigger reason ("phase_complete:N" / "ten_tu") or None.
        Idempotency: two-tier — permanent marker (durable) + 60s TTL token
        (in-flight architect-call dedupe).
    mini_replan(task_id, phase_id, architect_fn=None) -> int
        n>=0 = applied units count; -1 = architect/parse failure;
        -2 = blocked by failure-replan in progress;
        -3 = stale-context (concurrent re_planner added units mid-call).
    _validate_delta_for_progress(...)
        Wrapper that calls re_planner._validate_delta then enforces:
          - REQUIRED phase per add_units (B6)
          - phase ∈ known_phases ∪ {max+1}  (logic-iter1-1: fanout extension)

Security:
  - Reuses re_planner _validate_delta + _safe_output_path verbatim (Forbidden
    Approach #1: no fork). Mini_replan adds a wrapper layer for phase semantics.
  - _replan_triggers.json corruption → fail-safe UPWARD (refuse to fire) +
    `mini_replan_skipped_corrupt_triggers` trace (security-iter2-3 MED fix).
  - update_state mutator re-reads fresh state (B1 race-safety) AND detects
    stale-context (logic-iter1-6 MED fix) when fresh_unit_count > snapshot.

Public API:
  check_and_trigger(task_id) -> Optional[str]
  mini_replan(task_id, phase_id, architect_fn=None) -> int
  main(argv) -> int    CLI entry
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core.task_dir_layout import (
    task_dir, load_state, update_state, append_trace,
)
from src.core.re_planner import (
    _validate_delta,
    _extract_json,
    _safe_output_path,
    _call_architect_live,
    _apply_delta,
    _append_merge_log,
    _append_amendment_to_plan_md,
    _workspace_root,
    ArchitectFn,
)

logger = logging.getLogger(__name__)

# In-flight architect-call dedupe TTL. Permanent completion is tracked by
# phase_complete:<id> markers (logic-iter1-4 MED fix); TTL is only for
# the window between check_and_trigger return and mini_replan apply.
_TRIGGER_TTL_SEC = 60
_TRIGGERS_FILENAME = "_replan_triggers.json"
_TEN_TU_THRESHOLD = 10

# Return-code sentinels (negative; see module docstring for semantics)
RC_OK_BASE = 0          # >=0 means N units applied
RC_ARCHITECT_FAIL = -1
RC_FAILURE_REPLAN_IN_FLIGHT = -2
RC_STALE_CONTEXT = -3


def _trigger_token(reason: str, phase_id: Optional[int]) -> str:
    """Stable token for in-flight dedupe. SHA-256 short hash."""
    raw = f"{reason}|phase={phase_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _read_triggers(task_id: str) -> Dict[str, Any]:
    """Read _replan_triggers.json. Fail-safe UPWARD on corruption.

    Returns:
      Dict with keys "permanent" (list of marker strings) and "in_flight"
      (dict token -> ts).
      On JSONDecodeError / partial write → returns sentinel dict
      `{"_corrupt": True}`; caller MUST treat as "refuse to fire" and emit
      `mini_replan_skipped_corrupt_triggers` trace (security-iter2-3 MED fix).
    """
    d = task_dir(task_id)
    f = d / _TRIGGERS_FILENAME
    if not f.exists():
        return {"permanent": [], "in_flight": {}}
    try:
        raw = f.read_text(encoding="utf-8")
    except OSError:
        return {"_corrupt": True, "_reason": "OSError"}
    if not raw.strip():
        # empty file: equivalent to no markers (allowed)
        return {"permanent": [], "in_flight": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_corrupt": True, "_reason": "json_decode",
                "_size": len(raw), "_head": raw[:50]}
    if not isinstance(data, dict):
        return {"_corrupt": True, "_reason": "not_a_dict"}
    # Schema soft-check
    perm = data.get("permanent", [])
    flight = data.get("in_flight", {})
    if not isinstance(perm, list) or not isinstance(flight, dict):
        return {"_corrupt": True, "_reason": "schema_mismatch"}
    return {"permanent": perm, "in_flight": flight}


def _write_triggers(task_id: str, triggers: Dict[str, Any]) -> bool:
    """Atomic write of _replan_triggers.json (best-effort)."""
    d = task_dir(task_id)
    f = d / _TRIGGERS_FILENAME
    try:
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text(
            json.dumps(triggers, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(f)
        return True
    except OSError as e:
        logger.warning("triggers write failed: %s", e)
        return False


def _is_permanent_marked(triggers: Dict[str, Any], phase_id: int) -> bool:
    """Check permanent completion marker phase_complete:<id>."""
    marker = f"phase_complete:{phase_id}"
    return marker in triggers.get("permanent", [])


def _is_in_flight(triggers: Dict[str, Any], token: str) -> bool:
    """Check TTL-based in-flight dedupe."""
    flight = triggers.get("in_flight", {})
    ts = flight.get(token)
    if ts is None:
        return False
    return (time.time() - float(ts)) < _TRIGGER_TTL_SEC


def _record_in_flight(task_id: str, token: str) -> None:
    """Mark token as in-flight (TTL-based dedupe)."""
    triggers = _read_triggers(task_id)
    if triggers.get("_corrupt"):
        # Caller already handled corruption; just abandon this side-effect.
        return
    triggers.setdefault("in_flight", {})[token] = time.time()
    # Garbage-collect expired tokens
    now = time.time()
    triggers["in_flight"] = {
        k: v for k, v in triggers["in_flight"].items()
        if (now - float(v)) < _TRIGGER_TTL_SEC * 2
    }
    _write_triggers(task_id, triggers)


def _record_permanent(task_id: str, phase_id: int, reason: str = "phase_complete") -> None:
    """Mark trigger as permanently fired; never expires.

    For reason == "phase_complete" (or any phase-specific reason), records
    `phase_complete:<phase_id>`. For reason == "ten_tu", records `ten_tu`
    (count-based trigger applies once per task_id, not per phase).
    """
    triggers = _read_triggers(task_id)
    if triggers.get("_corrupt"):
        return
    perm = triggers.setdefault("permanent", [])
    if reason == "ten_tu":
        marker = "ten_tu"
    else:
        marker = f"phase_complete:{phase_id}"
    if marker not in perm:
        perm.append(marker)
    _write_triggers(task_id, triggers)


def _phase_units(units: List[Dict[str, Any]], phase: Any) -> List[Dict[str, Any]]:
    """Return units belonging to the given phase (string-comparable)."""
    return [u for u in units if str(u.get("phase", "")) == str(phase)]


def _known_phases(units: List[Dict[str, Any]]) -> List[str]:
    """Distinct non-empty phase strings present in current units."""
    seen = []
    for u in units:
        ph = u.get("phase", "")
        if ph and str(ph) not in seen:
            seen.append(str(ph))
    return seen


def check_and_trigger(task_id: str) -> Optional[str]:
    """Return a trigger reason if mini_replan should fire, None otherwise.

    Reasons:
      - "phase_complete:<phase_id>" — all TUs in some phase have status=done
        and that phase is not yet permanent-marked.
      - "ten_tu" — total done >= _TEN_TU_THRESHOLD and "ten_tu" token not
        in-flight.

    Returns None on:
      - state not loaded
      - corrupt triggers file (fail-safe UPWARD; emits trace)
      - already-marked permanent
      - in-flight TTL active
    """
    state = load_state(task_id)
    if not state:
        return None
    units = state.get("units", [])
    if not units:
        return None

    triggers = _read_triggers(task_id)
    if triggers.get("_corrupt"):
        # security-iter2-3 fail-safe upward
        append_trace(task_id, "mini_replan_skipped_corrupt_triggers", {
            "reason": triggers.get("_reason", "unknown"),
            "size": triggers.get("_size", -1),
            "head_preview": str(triggers.get("_head", ""))[:100],
        })
        return None

    # Phase-complete trigger (priority over ten_tu)
    for phase in _known_phases(units):
        ph_units = _phase_units(units, phase)
        if not ph_units:
            continue
        if all(u.get("status") in ("done", "skipped") for u in ph_units):
            if _is_permanent_marked(triggers, phase):
                continue  # already fanned out
            token = _trigger_token("phase_complete", phase)
            if _is_in_flight(triggers, token):
                continue
            return f"phase_complete:{phase}"

    # Ten-TU trigger (separate idempotency token)
    n_done = sum(1 for u in units if u.get("status") in ("done", "skipped"))
    if n_done >= _TEN_TU_THRESHOLD:
        token = _trigger_token("ten_tu", None)
        if _is_in_flight(triggers, token):
            return None
        # Permanent marker for ten-tu is also "ten_tu" key; once fired,
        # it does not re-fire even on cycle.
        if "ten_tu" in triggers.get("permanent", []):
            return None
        return "ten_tu"

    return None


def _validate_delta_for_progress(
    delta: Dict[str, Any],
    current_units: List[Dict[str, Any]],
    ws_root: Path,
    known_phases: List[str],
) -> Tuple[bool, str]:
    """Wrapper around re_planner._validate_delta with progress-trigger rules.

    Additional rules (logic-iter1-1 + B6):
      - every add_units entry MUST have non-empty `phase`
      - phase ∈ known_phases OR int(phase) == max(int(p))+1 (fanout extension)
      - non-contiguous phase (skipping next integer) → reject
    """
    # First: re_planner's 14-rule validation (cycle, sandbox, metachars, ...).
    ok, reason = _validate_delta(delta, current_units, ws_root)
    if not ok:
        return ok, reason

    # Empty delta is permitted (B10: noop).
    add_units = delta.get("add_units", [])
    if not add_units:
        return True, "ok_empty"

    # Compute max known phase as int (best-effort).
    int_phases = []
    for p in known_phases:
        try:
            int_phases.append(int(p))
        except (ValueError, TypeError):
            continue
    max_known = max(int_phases) if int_phases else 0

    for idx, u in enumerate(add_units):
        ph = u.get("phase", "")
        if not isinstance(ph, str) or not ph.strip():
            return False, f"add_units[{idx}].phase missing/empty (REQUIRED for mini_replan)"
        if ph in known_phases:
            continue
        # Fanout extension allowed iff int(ph) == max_known + 1
        try:
            ph_int = int(ph)
        except (ValueError, TypeError):
            return False, f"add_units[{idx}].phase not an integer string: {ph!r}"
        if ph_int != max_known + 1:
            return False, (
                f"add_units[{idx}].phase={ph!r} is non-contiguous "
                f"(known={known_phases}, allowed fanout={max_known+1})"
            )
    return True, "ok"


def _build_progress_prompt(
    state: Dict[str, Any], phase_id: int, reason: str,
    trace_tail: List[Dict],
) -> str:
    """Prompt for progress-trigger architect call. Distinct from re_planner._build_prompt."""
    units = state.get("units", [])
    phases = _known_phases(units)
    return (
        "You are FANNING OUT a successful autopilot phase. Return ONLY a JSON "
        "delta (optionally inside a ```json ... ``` fence).\n\n"
        f"Trigger reason: {reason}\n"
        f"Current phase: {phase_id}\n"
        f"Known phases: {phases}\n\n"
        "Recent trace (last 30 events):\n"
        f"{json.dumps(trace_tail[-30:], ensure_ascii=False)[:1500]}\n\n"
        "Current units:\n"
        f"{json.dumps(units, ensure_ascii=False)[:2000]}\n\n"
        "Output schema (same as re_planner; PLUS phase REQUIRED for add_units):\n"
        '{\n'
        '  "add_units": [{"id": "TU-N", "phase": "<int_string>", '
        '"description": "...", "depends_on": [], "outputs": []}],\n'
        '  "modify_unit_by_id": {},\n'
        '  "mark_skipped": []\n'
        '}\n\n'
        "Constraints:\n"
        "  - REQUIRED: every add_units[*] has non-empty 'phase'.\n"
        "  - phase MUST be an existing phase OR exactly max(known)+1 (fanout).\n"
        "  - New TU ids must be unused integers > max existing.\n"
        "  - No shell metachars. Output paths within workspace root.\n"
        "  - Cannot revert done units to pending. Cannot rewrite existing TUs."
    )


def _read_trace_tail(task_id: str, n: int = 30) -> List[Dict]:
    d = task_dir(task_id)
    trace_file = d / "trace.jsonl"
    if not trace_file.exists():
        return []
    try:
        lines = trace_file.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return parsed


def mini_replan(
    task_id: str,
    phase_id: int,
    architect_fn: Optional[ArchitectFn] = None,
    *,
    reason: str = "phase_complete",
) -> int:
    """Execute progress-trigger replan. See module docstring for return codes."""
    state = load_state(task_id)
    if not state:
        return RC_ARCHITECT_FAIL

    # Failure-replan precedence (B7): if a failure recovery is in flight,
    # progress-trigger defers (-2). re_planner sets state["replan_requested"]=True
    # via mark_failed. _apply_delta clears it on apply.
    if state.get("replan_requested"):
        append_trace(task_id, "mini_replan_skipped_due_to_failure_replan",
                     {"phase_id": phase_id, "reason": reason})
        return RC_FAILURE_REPLAN_IN_FLIGHT

    snapshot_unit_count = len(state.get("units", []))
    triggers = _read_triggers(task_id)
    if triggers.get("_corrupt"):
        append_trace(task_id, "mini_replan_skipped_corrupt_triggers",
                     {"reason": triggers.get("_reason"), "phase_id": phase_id})
        return RC_ARCHITECT_FAIL

    token = _trigger_token(reason, phase_id)
    _record_in_flight(task_id, token)

    append_trace(task_id, "mini_replan_triggered", {
        "phase_id": phase_id, "reason": reason,
        "snapshot_unit_count": snapshot_unit_count,
        "trigger_token": token,
    })

    trace_tail = _read_trace_tail(task_id)
    prompt = _build_progress_prompt(state, phase_id, reason, trace_tail)

    caller = architect_fn if architect_fn else _call_architect_live
    try:
        raw = caller(prompt)
    except Exception as e:
        append_trace(task_id, "replan_failed",
                     {"stage": "architect_call_progress",
                      "error": str(e)[:400]})
        return RC_ARCHITECT_FAIL

    delta = _extract_json(raw)
    if delta is None:
        append_trace(task_id, "replan_failed",
                     {"stage": "json_parse_progress", "raw_head": raw[:200]})
        return RC_ARCHITECT_FAIL

    # Empty delta → no-op (B10)
    add_units = delta.get("add_units", []) or []
    if not add_units and not delta.get("modify_unit_by_id") and not delta.get("mark_skipped"):
        append_trace(task_id, "mini_replan_done", {
            "phase_id": phase_id, "n_added": 0,
            "trigger_token": token, "reason": reason,
            "noop": True,
        })
        _record_permanent(task_id, phase_id, reason)
        return 0

    ws_root = _workspace_root()

    # Closure-based outcome propagation: update_state's atomic_update_json
    # catches mutator exceptions and returns False; we cannot raise through
    # it. Instead, mutator returns the unchanged state (no-op write) and
    # writes its outcome to a closure dict that the caller inspects.
    outcome: Dict[str, Any] = {
        "stale": False, "snapshot": snapshot_unit_count, "fresh": -1,
        "validation_error": None, "applied": False,
    }

    def _mut(s: Dict[str, Any]) -> Dict[str, Any]:
        fresh_units = s.get("units", [])
        outcome["fresh"] = len(fresh_units)
        if len(fresh_units) > snapshot_unit_count:
            outcome["stale"] = True
            return s  # no-op (return identical state); update_state still records ok=True
        kn_phases = _known_phases(fresh_units)
        ok, why = _validate_delta_for_progress(delta, fresh_units, ws_root, kn_phases)
        if not ok:
            outcome["validation_error"] = why
            return s  # no-op
        outcome["applied"] = True
        return _apply_delta(s, delta)

    update_ok = update_state(task_id, _mut)

    if outcome["stale"]:
        append_trace(task_id, "mini_replan_stale_context", {
            "phase_id": phase_id, "snapshot": outcome["snapshot"],
            "fresh": outcome["fresh"], "trigger_token": token,
        })
        return RC_STALE_CONTEXT

    if outcome["validation_error"] is not None:
        append_trace(task_id, "replan_failed",
                     {"stage": "validate_progress",
                      "reason": outcome["validation_error"]})
        return RC_ARCHITECT_FAIL

    if not update_ok or not outcome["applied"]:
        append_trace(task_id, "replan_failed",
                     {"stage": "write_progress"})
        return RC_ARCHITECT_FAIL

    _append_merge_log(task_id, delta, {"reason": reason, "phase_id": phase_id})
    _append_amendment_to_plan_md(
        task_id, delta, {"reason": reason, "phase_id": phase_id})
    _record_permanent(task_id, phase_id, reason)

    n_adds = len(delta.get("add_units", []))
    n_mods = len(delta.get("modify_unit_by_id", {}))
    n_skips = len(delta.get("mark_skipped", []))
    append_trace(task_id, "mini_replan_done", {
        "phase_id": phase_id, "n_added": n_adds, "n_modified": n_mods,
        "n_skipped": n_skips, "trigger_token": token, "reason": reason,
    })
    return n_adds + n_mods + n_skips


class _StaleContextError(RuntimeError):
    def __init__(self, fresh: int, snapshot: int):
        self.fresh = fresh
        self.snapshot = snapshot
        super().__init__(f"stale context: fresh={fresh} > snapshot={snapshot}")


class _DeltaInvalidError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: python -m src.core.mini_replan <task_id> [--phase N] [--check]"""
    import argparse
    p = argparse.ArgumentParser(prog="mini_replan")
    p.add_argument("task_id")
    p.add_argument("--phase", type=int, default=None,
                   help="phase id to fan out (required unless --check)")
    p.add_argument("--check", action="store_true",
                   help="only check_and_trigger; do not call architect")
    p.add_argument("--reason", default="phase_complete",
                   help="trigger reason label")
    args = p.parse_args(argv)

    if args.check:
        reason = check_and_trigger(args.task_id)
        print(json.dumps({"trigger_reason": reason}))
        return 0 if reason else 1

    if args.phase is None:
        print("--phase required (or use --check)", file=__import__("sys").stderr)
        return 64

    n = mini_replan(args.task_id, args.phase, reason=args.reason)
    if n < 0:
        print(json.dumps({"rc": n}), file=__import__("sys").stderr)
        return abs(n) + 1  # rc=-1 → exit 2; rc=-2 → exit 3; rc=-3 → exit 4
    print(json.dumps({"units_changed": n}))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
