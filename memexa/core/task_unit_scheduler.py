"""Task unit scheduler for 10h+ autopilot (plan v2, Phase A A2+A5).

Single-responsibility: decomposition + dependency ordering + progress
bookkeeping. Does NOT classify tasks (task_router's job), does NOT spawn
agents (autopilot skill's job), does NOT review outputs (reviewer's job).

plan.md parsing format (per plan v2 §Phase A):
    ## Phase P-<phase-name>
    | TU | description | depends_on |
    |----|-------------|------------|
    | TU-1 | verify shadow writes | - |
    | TU-2 | enable dual-write    | TU-1 |

state.json schema (extended by A3 in task_router):
    {
      "task_id": "...",
      "current_phase": "<phase_name>",
      "current_unit_idx": int,          # index into units[]
      "units": [
        {"id": "TU-1", "phase": "...", "description": "...",
         "depends_on": [], "status": "pending|running|done|failed|skipped",
         "outputs": [paths], "reason": "failure reason if failed",
         "replan_requested": bool}
      ],
      ...
    }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from memexa.core.task_dir_layout import (
    task_dir, create_task_dir, current_task_id, set_current,
    load_state, update_state, append_trace,
)

_STATUS_VALUES = {"pending", "running", "done", "failed", "skipped"}
# U6 (plan_v2 2026-04-27): widened to depth-3 hierarchical ids
# (TU-1, TU-1.2, TU-1.2.3). Brief P1 caps at depth 3 = max 3 numeric
# segments. {0,2} = 0/1/2 trailing dot-segments after the first \d+.
# Rejects: TU-1.0 (trailing zero, canonicalize to TU-1), TU-01 (zero-pad),
# TU-1.2.3.4 (depth 4 > cap), TU-1.2.3a (suffix). See AC-1.
_UNIT_ID_RE = re.compile(r"^TU-(?:0|[1-9]\d*)(?:\.(?:[1-9]\d*))*$")
_UNIT_ID_DEPTH_CAP = 3
_UNIT_ID_RAW_RE = re.compile(r"^TU-[0-9.a-zA-Z_-]+$")  # for liberal pre-validation


class PlanDecompositionError(ValueError):
    """Raised when plan.md cannot be parsed into a valid TaskUnit list."""


@dataclass
class TaskUnit:
    id: str
    phase: str
    description: str
    depends_on: List[str] = field(default_factory=list)
    status: str = "pending"
    outputs: List[str] = field(default_factory=list)
    reason: str = ""
    replan_requested: bool = False
    # U6 (plan_v2 2026-04-27): hierarchical TU support.
    # parent_tu_id: lexical ancestor (TU-1.2 -> TU-1; TU-1 -> None).
    # phase_id: optional phase grouping key distinct from human-readable
    # `phase` string (e.g. "P1" vs "Phase 1: Foundation Deepening").
    # Both default None for backward compat with pre-U6 state.json files.
    parent_tu_id: Optional[str] = None
    phase_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _validate_unit_id(unit_id: str) -> None:
    """Raise PlanDecompositionError if id violates hierarchical id rules.

    Rules (U6 plan_v2 AC-1):
    - Must match `^TU-(0|[1-9]\\d*)(\\.([1-9]\\d*))*$`
      (rejects TU-01 zero-pad and TU-1.0 trailing-zero by construction).
    - Depth (number of numeric segments) must be 1..3 inclusive.
    - Suffix chars (TU-1.2.3a) rejected.
    - Empty trailing segment (TU-1.) rejected.
    """
    if not _UNIT_ID_RE.match(unit_id):
        raise PlanDecompositionError(
            f"invalid TU id format: {unit_id!r} "
            f"(must match TU-<int>[.<int>[.<int>]] with no zero-pad and "
            f"no trailing .0; see plan_v0 AC-1)"
        )
    segments = unit_id[len("TU-"):].split(".")
    if len(segments) > _UNIT_ID_DEPTH_CAP:
        raise PlanDecompositionError(
            f"TU id {unit_id!r} exceeds depth cap "
            f"(got {len(segments)} segments, max {_UNIT_ID_DEPTH_CAP})"
        )
    # Reject trailing .0 explicitly (regex already does, but message clarity)
    for seg in segments[1:]:  # post-first segments must be >=1
        if seg == "0":
            raise PlanDecompositionError(
                f"TU id {unit_id!r} has trailing-zero segment "
                f"(use canonical form without .0)"
            )


def _infer_parent_tu_id(unit_id: str) -> Optional[str]:
    """Return lexical parent of a hierarchical TU id, or None for top-level.

    TU-1.2.3 -> TU-1.2; TU-1.2 -> TU-1; TU-1 -> None.
    Caller must have already validated via _validate_unit_id.
    """
    segments = unit_id[len("TU-"):].split(".")
    if len(segments) <= 1:
        return None
    return "TU-" + ".".join(segments[:-1])


# --------------------------------------------------------------------
# Decomposition: plan.md → TaskUnit[]
# --------------------------------------------------------------------

_PHASE_HEADER_RE = re.compile(r"^##\s+Phase\s+([^\n]+?)\s*$", re.MULTILINE)
_UNIT_ROW_RE = re.compile(
    # U6 (plan_v2): widened to capture hierarchical ids (TU-1.2.3).
    # Captures liberally; strict canonical-form validation happens in
    # _validate_unit_id (called per-row in decompose()).
    r"^\|\s*(TU-[0-9.a-zA-Z_-]+)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*$",
    re.MULTILINE,
)


def decompose(plan_path: Path, task_id: str) -> List[TaskUnit]:
    """Parse plan.md and return ordered TaskUnit list.

    Raises PlanDecompositionError on missing table, duplicate IDs, or
    dependency cycles.
    """
    if not plan_path.exists():
        raise PlanDecompositionError(f"plan.md not found: {plan_path}")
    text = plan_path.read_text(encoding="utf-8")

    units: List[TaskUnit] = []
    seen_ids: set = set()

    # Walk text section-by-section, associating unit rows with the nearest
    # preceding ## Phase header.
    phase_positions = [(m.start(), m.group(1).strip()) for m in _PHASE_HEADER_RE.finditer(text)]

    # U6: collect raw rows first; validate ids strictly (reject silent-skip
    # for malformed forms like TU-1.0 / TU-01 / TU-1.2.3a per AC-1).
    raw_rows = []  # list of (unit_id, desc, deps_raw, phase, row_pos)
    for row_m in _UNIT_ROW_RE.finditer(text):
        unit_id = row_m.group(1).strip()
        desc = row_m.group(2).strip()
        deps_raw = row_m.group(3).strip()
        # Skip header/separator rows like "| TU | description | depends_on |"
        if unit_id.upper() in ("TU", "ID") or desc.lower() == "description":
            continue
        # Liberal pre-filter: only consider rows whose first column LOOKS
        # like a TU id (TU-something). Anything else is a non-unit row
        # (table header, etc.) and is silently skipped to avoid false
        # rejection of unrelated tables in the plan file.
        if not _UNIT_ID_RAW_RE.match(unit_id):
            continue
        # Strict validation — raises PlanDecompositionError on bad form.
        _validate_unit_id(unit_id)
        if unit_id in seen_ids:
            raise PlanDecompositionError(f"duplicate unit id: {unit_id}")
        seen_ids.add(unit_id)
        # Find enclosing phase
        phase = "unspecified"
        row_pos = row_m.start()
        for pos, name in reversed(phase_positions):
            if pos < row_pos:
                phase = name
                break
        raw_rows.append((unit_id, desc, deps_raw, phase, row_pos))

    if not raw_rows:
        raise PlanDecompositionError(
            f"no TaskUnit rows found in {plan_path} (expected | TU-N | ... | deps | table)"
        )

    # Build TaskUnits with explicit parent_tu_id from id-string ancestry.
    for unit_id, desc, deps_raw, phase, _row_pos in raw_rows:
        # Parse depends_on: comma-separated TU-ids, or "-"/"none"/empty for none
        deps: List[str] = []
        if deps_raw and deps_raw not in ("-", "—", "none", "None", ""):
            for d in re.split(r"[,\s]+", deps_raw):
                d = d.strip()
                if _UNIT_ID_RAW_RE.match(d):
                    _validate_unit_id(d)  # strict — surfaces bad deps too
                    deps.append(d)
        parent_id = _infer_parent_tu_id(unit_id)
        units.append(TaskUnit(
            id=unit_id, phase=phase, description=desc,
            depends_on=deps, parent_tu_id=parent_id,
        ))

    # Validate explicit dependencies exist
    for u in units:
        for d in u.depends_on:
            if d not in seen_ids:
                raise PlanDecompositionError(
                    f"unit {u.id} depends on undefined {d}"
                )

    # U6 (plan_v2 TU-2): inject parent->children as IMPLICIT depends_on
    # edges. Parent only ready when all children done (WBS rollup). Child
    # depending on its parent thereby becomes a natural cycle in Kahn.
    # Build forward index parent_id -> [child_ids] preserving insertion
    # order, then append children to parent's depends_on (deduped).
    children_by_parent: Dict[str, List[str]] = {}
    for u in units:
        if u.parent_tu_id is not None:
            children_by_parent.setdefault(u.parent_tu_id, []).append(u.id)
    # Validate: every parent_tu_id must reference a TU that actually exists.
    for parent_id in children_by_parent:
        if parent_id not in seen_ids:
            raise PlanDecompositionError(
                f"orphan child references parent {parent_id} which "
                f"is not a declared TU"
            )
    for u in units:
        if u.id in children_by_parent:
            existing = set(u.depends_on)
            for child in children_by_parent[u.id]:
                if child not in existing:
                    u.depends_on.append(child)
                    existing.add(child)

    # Cycle detection (Kahn topological sort) — runs over augmented
    # depends_on so child->ancestor cycles surface naturally.
    _cycle_check(units)

    # Emit tu_hierarchy_resolved trace event (U6 AC-6) — registered in
    # trace_sink._ALLOWED_EVENTS during U2 (commit 44b3bf3 line 248).
    try:
        from memexa.core.trace_sink import write_trace_event
        depth_max = max(
            (u.id[len("TU-"):].count(".") + 1 for u in units),
            default=0,
        )
        write_trace_event(
            "tu_hierarchy_resolved",
            {
                "task_id": task_id,
                "depth_max": int(depth_max),
                "parent_count": int(len(children_by_parent)),
                "unit_count": int(len(units)),
            },
        )
    except Exception:
        pass  # fail-soft — telemetry must never break decompose()

    return units


def _cycle_check(units: List[TaskUnit]) -> None:
    in_deg = {u.id: len(u.depends_on) for u in units}
    by_id = {u.id: u for u in units}
    ready = [uid for uid, d in in_deg.items() if d == 0]
    processed = 0
    while ready:
        uid = ready.pop(0)
        processed += 1
        for other in units:
            if uid in other.depends_on:
                in_deg[other.id] -= 1
                if in_deg[other.id] == 0:
                    ready.append(other.id)
    if processed != len(units):
        unresolved = [uid for uid, d in in_deg.items() if d > 0]
        raise PlanDecompositionError(
            f"dependency cycle involves: {unresolved}"
        )


# --------------------------------------------------------------------
# Scheduling: next_unit / mark_*
# --------------------------------------------------------------------

def initialize_units(task_id: str, units: List[TaskUnit],
                     plan_path: Optional[Path] = None) -> bool:
    """Write TaskUnit list into state.json as `units: [...]`.

    Args:
        task_id: task directory id
        units: TaskUnit list from decompose()
        plan_path: optional path to the architect's plan_v2.md; when provided,
            stored into state["plan_path"] so re_planner._append_amendment
            can append delta summaries back into the plan document
            (B1-prep, plan v2 2026-04-21). Legacy 2-arg callers unchanged.
    """
    def _set(state):
        state["units"] = [u.to_dict() for u in units]
        state["current_unit_idx"] = -1
        state["last_updated"] = time.time()
        if plan_path is not None:
            # Store as absolute string for portability; re_planner resolves fresh
            state["plan_path"] = str(Path(plan_path).resolve())
        return state
    ok = update_state(task_id, _set)
    if ok:
        _regenerate_plan_md(task_id)
        append_trace(task_id, "scheduler_init",
                     {"unit_count": len(units),
                      "plan_path": str(plan_path) if plan_path else None})
    return ok


def next_unit(task_id: str) -> Optional[Dict[str, Any]]:
    """First pending unit whose depends_on are all status=='done' or 'skipped'.

    Returns None if all units are done/skipped.
    Returns special sentinel dict {"status": "blocked_by_failure", ...} if there
    are pending units BUT all are blocked by failed dependencies (LOG-R2 #1 fix).
    Caller can distinguish "task done" (None) from "blocked" (dict with
    status=='blocked_by_failure').
    """
    state = load_state(task_id)
    if not state:
        return None
    units = state.get("units", [])
    done_ids = {u["id"] for u in units if u["status"] in ("done", "skipped")}
    failed_ids = {u["id"] for u in units if u["status"] == "failed"}

    pending_exists = False
    blocked_by_failed: List[str] = []
    for u in units:
        if u["status"] != "pending":
            continue
        pending_exists = True
        deps = u.get("depends_on", [])
        if any(dep in failed_ids for dep in deps):
            blocked_by_failed.append(u["id"])
            continue
        if all(dep in done_ids for dep in deps):
            return u

    if pending_exists and blocked_by_failed:
        return {
            "status": "blocked_by_failure",
            "blocked_units": blocked_by_failed,
            "failed_deps": sorted(failed_ids),
        }
    return None


def _find_idx(units: List[Dict], unit_id: str) -> int:
    for i, u in enumerate(units):
        if u["id"] == unit_id:
            return i
    return -1


def mark_running(task_id: str, unit_id: str) -> bool:
    """Mark a unit as running; update current_unit_idx."""
    def _set(state):
        units = state.get("units", [])
        idx = _find_idx(units, unit_id)
        if idx < 0:
            return None  # no-op
        units[idx]["status"] = "running"
        state["current_unit_idx"] = idx
        state["current_phase"] = units[idx].get("phase", "")
        state["last_updated"] = time.time()
        return state
    ok = update_state(task_id, _set)
    if ok:
        append_trace(task_id, "unit_running", {"unit_id": unit_id})
        _regenerate_plan_md(task_id)
    return ok


def mark_done(task_id: str, unit_id: str, outputs: Optional[List[str]] = None) -> bool:
    """Mark unit done, record outputs, regenerate plan.md.

    U6 (plan_v2): propagate-up logic. After the named unit is marked done,
    walk parent_tu_id chain. For each parent in the chain, if EVERY child
    of that parent (i.e. every TU whose parent_tu_id == parent.id) is in
    {done, skipped}, mark the parent done too. Continue upward until a
    parent has any non-done child OR the chain reaches the top (parent_tu_id
    is None). All within ONE update_state callback so concurrent readers
    never observe child-done + parent-pending atomic-boundary violation
    (per HARD RULE feedback_subagent_and_stall_protocol §2 (Mode-B default-
    second-step), single-callback closure rule applied here for state-write
    atomicity).

    `propagated_ids` is collected and trace-emitted post-write so each
    propagation step shows in traces.jsonl using the existing `unit_done`
    event (no new event registered, per logic-iter1 finding HIGH-1).
    """
    outputs = outputs or []
    propagated_ids: List[str] = []  # captured by closure for post-write trace
    # U11 (long_term_plan_v2 Phase 3): mark_done milestone counter is
    # incremented inside _set callback (filelock-protected) and the new
    # value captured into a mutable closure list. logic-iter1-1 fix:
    # post-write read of _count_capture[0] avoids stale-count race that
    # a fresh load_state() would have.
    _count_capture: List[int] = [0]
    # logic-code-iter1-1 fix: capture autopilot mode under the same _set
    # callback (not a fresh _load_state after update_state returns) to
    # eliminate the TOCTOU window where mode could flip between callback
    # return and post-write read.
    _autopilot_mode_capture: List[bool] = [False]

    def _set(state):
        units = state.get("units", [])
        idx = _find_idx(units, unit_id)
        if idx < 0:
            return None
        # Sentinel: filelock + single-update_state callback gives us
        # serialization across concurrent processes. Within this callback
        # we own the whole state; mark the leaf and walk parents under
        # the same write boundary. (Threading-test deferred per AC-4
        # citing filelock atomicity — see test_propagate_up_idempotent.)
        units[idx]["status"] = "done"
        units[idx]["outputs"] = outputs
        # U11: bump milestone counter under same filelock; capture new value
        new_count = int(state.get("mark_done_count", 0)) + 1
        state["mark_done_count"] = new_count
        _count_capture[0] = new_count
        # logic-code-iter1-1 fix: capture autopilot mode under same lock.
        try:
            from memexa.core.persistent_mode import _load_state as _pm_load
            pm = _pm_load() or {}
            _autopilot_mode_capture[0] = (pm.get("mode") == "autopilot")
        except Exception:
            _autopilot_mode_capture[0] = False
        # Walk parent chain
        children_by_parent: Dict[str, List[str]] = {}
        for u in units:
            pid = u.get("parent_tu_id")
            if pid:
                children_by_parent.setdefault(pid, []).append(u["id"])
        cur_id = units[idx].get("parent_tu_id")
        # Bound walk by depth cap to avoid runaway in a malformed state
        for _depth in range(_UNIT_ID_DEPTH_CAP):
            if cur_id is None:
                break
            p_idx = _find_idx(units, cur_id)
            if p_idx < 0:
                break  # parent missing in state — silent stop
            parent_unit = units[p_idx]
            # Idempotency: if parent already done, no propagation needed
            if parent_unit["status"] == "done":
                break
            child_ids = children_by_parent.get(cur_id, [])
            if not child_ids:
                break  # parent has no children in state (shouldn't happen)
            all_children_done = all(
                next((u["status"] for u in units if u["id"] == cid), "")
                in ("done", "skipped")
                for cid in child_ids
            )
            if not all_children_done:
                break
            parent_unit["status"] = "done"
            propagated_ids.append(cur_id)
            cur_id = parent_unit.get("parent_tu_id")
        state["last_updated"] = time.time()
        return state
    ok = update_state(task_id, _set)
    if ok:
        append_trace(task_id, "unit_done", {"unit_id": unit_id, "outputs": outputs})
        for pid in propagated_ids:
            append_trace(task_id, "unit_done",
                         {"unit_id": pid, "outputs": [], "propagated": True})
        _regenerate_plan_md(task_id)
        # U11 (long_term_plan_v2 Phase 3): every 5th mark_done in autopilot
        # mode triggers a tu_milestone checkpoint. Reads the closure-captured
        # count (NOT a fresh load_state) per logic-iter1-1 race fix.
        # Mode gate uses persistent_mode in-memory state per logic-iter1-5.
        cnt = _count_capture[0]
        if cnt > 0 and cnt % 5 == 0 and _autopilot_mode_capture[0]:
            try:
                from memexa.core.autopilot_checkpoint import write_checkpoint
                write_checkpoint(task_id, "tu_milestone",
                                 trigger="tu_milestone")
            except Exception as e:
                try:
                    from memexa.core.trace_sink import write_trace_event
                    write_trace_event("gate_infra_error", {
                        "where": "task_unit_scheduler.mark_done.checkpoint",
                        "reason": str(e)[:200],
                    })
                except Exception:
                    pass
            # I-1: per-cluster integration_test_matrix validate (every 5th)
            _maybe_run_cluster_integration_check(task_id, cnt)
    return ok


def _maybe_run_cluster_integration_check(task_id: str, mark_done_count: int) -> None:
    """I-1 (Phase 4, 2026-05-04): per-cluster integration_test_matrix run.

    Every ceil(N/5) TU mark_done invocation, run integration_test_matrix.
    validate against the latest plan to catch cross-TU contract drift early
    (vs only at Stage 6 commit-prep). fail-soft: never block scheduler on
    observability infra.

    Trigger logic: every 5th mark_done (count % 5 == 0 after counter bump).
    Earlier than ceil(N/5) is fine — small overruns are cheap.
    """
    if mark_done_count <= 0 or mark_done_count % 5 != 0:
        return
    try:
        from memexa.core.integration_test_matrix import validate_matrix
        from memexa.core._plan_path_resolver import resolve_plan_path
        plan_path = resolve_plan_path(task_id)
        if not plan_path or not plan_path.exists():
            return
        plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
        report = validate_matrix(plan_text)
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("integration_matrix_cluster_run", {
                "task_id": task_id,
                "trigger_count": mark_done_count,
                "ok": getattr(report, "ok", True),
                "tu_count": getattr(report, "tu_count", 0),
                "missing_fields": len(getattr(report, "missing_field_tus", [])),
            })
        except Exception:
            pass
    except (ImportError, AttributeError):
        return  # module not yet loaded; skip
    except Exception:
        return  # fail-soft


def mark_failed(task_id: str, unit_id: str, reason: str,
                trigger_replan: bool = False) -> bool:
    """Mark unit failed; optionally flag replan_requested for Phase B.

    B3-prep (plan v2 2026-04-21): when trigger_replan=True, also write
    state["replan_requested_at"] = time.time() so heartbeat idle-branch
    can detect stale (age > 5min) replan requests and emit action_item.
    """
    def _set(state):
        units = state.get("units", [])
        idx = _find_idx(units, unit_id)
        if idx < 0:
            return None
        units[idx]["status"] = "failed"
        units[idx]["reason"] = reason[:500]
        units[idx]["replan_requested"] = trigger_replan
        if trigger_replan:
            state["replan_requested"] = True
            state["replan_requested_at"] = time.time()  # B3-prep
        state["last_updated"] = time.time()
        return state
    ok = update_state(task_id, _set)
    if ok:
        append_trace(task_id, "unit_failed",
                     {"unit_id": unit_id, "reason": reason[:200],
                      "replan": trigger_replan})
        _regenerate_plan_md(task_id)
    return ok


def list_remaining_units(task_id: str) -> List[Dict[str, Any]]:
    """Phase B helper (B3): pending + failed, skip done/skipped."""
    state = load_state(task_id)
    if not state:
        return []
    return [u for u in state.get("units", [])
            if u.get("status") in ("pending", "failed", "running")]


# --------------------------------------------------------------------
# plan.md regeneration (recitation pattern, Manus)
# --------------------------------------------------------------------

_PLAN_BANNER = (
    "<!-- AUTO-GENERATED from state.json by task_unit_scheduler. "
    "Edit state.json instead; this file is regenerated on each mark_*. -->\n"
)


def _regenerate_plan_md(task_id: str) -> bool:
    """Rewrite plan.md from state.json. Preserves banner; refuses if banner absent."""
    state = load_state(task_id)
    if not state:
        return False
    d = task_dir(task_id)
    plan_file = d / "plan.md"
    # Safety: if CEO hand-edited plan.md without the auto-gen marker, don't overwrite
    # Permissive prefix match: "<!-- AUTO-GENERATED" triggers; anything else = refuse
    if plan_file.exists():
        existing = plan_file.read_text(encoding="utf-8")
        if existing and not existing.lstrip().startswith("<!-- AUTO-GENERATED"):
            append_trace(task_id, "plan_md_tampered",
                         {"refused_regenerate": True})
            return False

    units = state.get("units", [])
    # Group by phase preserving order
    from collections import OrderedDict
    by_phase = OrderedDict()
    for u in units:
        by_phase.setdefault(u.get("phase", "unspecified"), []).append(u)

    lines = [_PLAN_BANNER]
    lines.append(f"# Task: {state.get('task_id', '?')}")
    total = len(units)
    done = sum(1 for u in units if u.get("status") in ("done", "skipped"))
    cur = state.get("current_unit_idx", -1)
    lines.append(f"Progress: {done}/{total} units done (current_unit_idx={cur})\n")
    for phase, phase_units in by_phase.items():
        lines.append(f"## Phase {phase}")
        for u in phase_units:
            checkbox = "[x]" if u["status"] in ("done", "skipped") else "[ ]"
            marker = ""
            if u["status"] == "running":
                marker = "  **← RUNNING**"
            elif u["status"] == "failed":
                marker = f"  **FAILED: {u.get('reason', '')[:60]}**"
            lines.append(f"- {checkbox} {u['id']}: {u['description']}{marker}")
        lines.append("")

    plan_file.write_text("\n".join(lines), encoding="utf-8")
    return True


# --------------------------------------------------------------------
# CLI (A5)
# --------------------------------------------------------------------

def _cli_init(args) -> int:
    tid = create_task_dir(args.slug)
    set_current(tid)
    print(tid)
    return 0


def _cli_decompose(args) -> int:
    tid = args.task_id
    d = task_dir(tid)
    if not d.is_dir():
        print(f"ERROR: task dir missing: {tid}", file=sys.stderr)
        return 2
    # If a plan path is given, use it; else use plan.md inside task dir
    plan_path = Path(args.plan) if args.plan else (d / "plan.md")
    try:
        units = decompose(plan_path, tid)
    except PlanDecompositionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    initialize_units(tid, units)
    print(json.dumps({"task_id": tid, "unit_count": len(units),
                      "unit_ids": [u.id for u in units]}))
    return 0


def _cli_resume(args) -> int:
    tid = args.task_id
    u = next_unit(tid)
    if u is None:
        state = load_state(tid)
        if state is None:
            print(f"ERROR: task not found: {tid}", file=sys.stderr)
            return 2
        print(json.dumps({"status": "all_done_or_blocked", "task_id": tid}))
        return 0
    print(json.dumps(u, ensure_ascii=False))
    return 0


def _cli_mark_done(args) -> int:
    outputs = []
    if args.outputs:
        outputs = [s.strip() for s in args.outputs.split(",") if s.strip()]
    ok = mark_done(args.task_id, args.unit_id, outputs)
    return 0 if ok else 2


def _cli_mark_failed(args) -> int:
    ok = mark_failed(args.task_id, args.unit_id, args.reason,
                     trigger_replan=args.replan)
    return 0 if ok else 2


def _cli_status(args) -> int:
    state = load_state(args.task_id)
    if state is None:
        print(f"ERROR: task not found: {args.task_id}", file=sys.stderr)
        return 2
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def _render_tree(units: List[Dict[str, Any]],
                 ascii_only: bool = False) -> str:
    """U6 (plan_v2 TU-4): ASCII box-drawing tree of units by parent_tu_id.

    Status glyphs: [done] [pending] [running] [failed] [skipped].
    Empty hierarchy (zero units) renders "(no units)" without crash.
    `ascii_only=True` emits + - | for cp936 / non-UTF8 stdout (Brief §10).
    """
    if not units:
        return "(no units)\n"
    if ascii_only:
        branch, last, vert, horiz = "+--", "+--", "|  ", "   "
    else:
        branch, last, vert, horiz = "├─", "└─", "│ ", "  "
    # Build children-by-parent map preserving order
    children: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for u in units:
        children.setdefault(u.get("parent_tu_id"), []).append(u)
    # Top-level units = those whose parent_tu_id is None
    roots = children.get(None, [])
    out: List[str] = []

    def _walk(node: Dict[str, Any], prefix: str, is_last: bool) -> None:
        connector = last if is_last else branch
        status = node.get("status", "?")
        out.append(f"{prefix}{connector} {node['id']} [{status}]")
        sub_prefix = prefix + (horiz if is_last else vert)
        kids = children.get(node["id"], [])
        for i, k in enumerate(kids):
            _walk(k, sub_prefix, i == len(kids) - 1)

    for i, root in enumerate(roots):
        # Top-level rendered without a leading connector
        status = root.get("status", "?")
        out.append(f"{root['id']} [{status}]")
        kids = children.get(root["id"], [])
        for j, k in enumerate(kids):
            _walk(k, "", j == len(kids) - 1)
    return "\n".join(out) + "\n"


def _cli_tree(args) -> int:
    state = load_state(args.task_id)
    if state is None:
        print(f"ERROR: task not found: {args.task_id}", file=sys.stderr)
        return 2
    units = state.get("units", [])
    # cp936 fallback: detect non-UTF8 stdout (Windows default codepage)
    enc = (sys.stdout.encoding or "").lower()
    ascii_only = ("utf" not in enc) if enc else False
    sys.stdout.write(_render_tree(units, ascii_only=ascii_only))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="task_unit_scheduler")
    sp = p.add_subparsers(dest="cmd", required=True)

    init_p = sp.add_parser("init")
    init_p.add_argument("slug")
    init_p.set_defaults(func=_cli_init)

    dec_p = sp.add_parser("decompose")
    dec_p.add_argument("task_id")
    dec_p.add_argument("--plan", help="path to plan.md (default: task_dir/plan.md)")
    dec_p.set_defaults(func=_cli_decompose)

    res_p = sp.add_parser("resume")
    res_p.add_argument("task_id")
    res_p.set_defaults(func=_cli_resume)

    done_p = sp.add_parser("mark-done")
    done_p.add_argument("task_id")
    done_p.add_argument("unit_id")
    done_p.add_argument("--outputs", default="")
    done_p.set_defaults(func=_cli_mark_done)

    fail_p = sp.add_parser("mark-failed")
    fail_p.add_argument("task_id")
    fail_p.add_argument("unit_id")
    fail_p.add_argument("--reason", required=True)
    fail_p.add_argument("--replan", action="store_true")
    fail_p.set_defaults(func=_cli_mark_failed)

    stat_p = sp.add_parser("status")
    stat_p.add_argument("task_id")
    stat_p.set_defaults(func=_cli_status)

    tree_p = sp.add_parser("tree", help="render hierarchical TU tree (U6 plan_v2)")
    tree_p.add_argument("task_id")
    tree_p.set_defaults(func=_cli_tree)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
