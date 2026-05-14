"""Re-planner for 10h+ autopilot (plan v2, Phase B B1).

Invoked ONLY when task_unit_scheduler.mark_failed() sets trigger_replan=True.
Reads state.json + last 50 trace entries, spawns architect agent via
`claude -p`, validates returned JSON delta, applies to state.json.

Verifier R2 fixes baked in:
  - HIGH-1: path validation uses Path.resolve() on BOTH sides of is_relative_to
  - MED-1: junction-tree safe workspace root resolution
  - B1: JSON injection path traversal defense (unit_id regex, outputs sandbox)

Security model:
  - unit_id must match ^TU-[0-9]+$
  - outputs[] paths must resolve within WORKSPACE_ROOT (no UNC, no drive-crossing)
  - description forbids shell metacharacters (basic)
  - cycle detection before apply
  - cannot mark already-done unit as pending
  - failure → trace event `replan_failed`, return -1 (non-zero)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from memexa.core.task_dir_layout import (
    task_dir, load_state, update_state, append_trace,
)
from memexa.core.task_unit_scheduler import _cycle_check, TaskUnit

logger = logging.getLogger(__name__)

# ASCII-only digit match (SEC MED: \d matches Unicode digits, allowing homograph attacks)
_UNIT_ID_RE = re.compile(r"^TU-[0-9]+$")
# SEC-R2 #1: newline/CR forge phantom markdown rows; %/{/} enable interpolation
_SHELL_META_RE = re.compile(r"[`$;&|<>\n\r%{}]")
# Fields architect is allowed to modify via modify_unit_by_id (SEC-R2 #5 hardened)
_MODIFY_ALLOWED_FIELDS = {"description", "depends_on", "outputs", "status", "phase"}
# Status transitions architect is allowed to make via modify (SEC-R2 #5)
# Cannot set status=done (only mark_done CLI can, after real work)
# Cannot set status=running (only mark_running)
_MODIFY_ALLOWED_STATUS = {"skipped"}

# ArchitectFn: callable with same signature as _call_architect_live,
# injected by tests.
ArchitectFn = Callable[[str], str]


def _workspace_root() -> Path:
    """Resolve workspace (parent of memexa/). Robust to CWD + junctions.

    Verifier R2 MED-1: .resolve() is mandatory on Windows junction trees
    (OneDrive/桌面 is a localized junction on Chinese Win11).
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def _safe_output_path(raw: str, ws_root: Path) -> Optional[Path]:
    """Return resolved path iff inside workspace; else None.

    Rejects UNC paths (`\\\\host\\...`), drive-crossing paths, and `..`
    escapes. Uses .resolve() on both sides for junction-tree safety.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    # Reject UNC
    if raw.startswith("\\\\") or raw.startswith("//"):
        return None
    try:
        candidate = (ws_root / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
    except (OSError, ValueError):
        return None
    # Both sides resolved — junction-tree safe
    try:
        candidate.relative_to(ws_root)
    except ValueError:
        return None
    return candidate


def _validate_delta(delta: Dict[str, Any],
                    current_units: List[Dict[str, Any]],
                    ws_root: Path) -> Tuple[bool, str]:
    """Validate architect-returned JSON delta.

    Schema:
        {
          "add_units": [{"id": "TU-N", "phase": "...", "description": "...",
                         "depends_on": [...], "outputs": [paths]}],
          "modify_unit_by_id": {"TU-N": {field: value, ...}},
          "mark_skipped": ["TU-N"]
        }

    Returns (ok, reason). On ok=False, delta must NOT be applied.
    """
    if not isinstance(delta, dict):
        return False, "delta is not a JSON object"

    existing_ids = {u["id"] for u in current_units}
    done_ids = {u["id"] for u in current_units if u.get("status") in ("done", "skipped")}

    add_units = delta.get("add_units", [])
    modify_map = delta.get("modify_unit_by_id", {})
    skip_list = delta.get("mark_skipped", [])

    if not isinstance(add_units, list) or not isinstance(modify_map, dict) or not isinstance(skip_list, list):
        return False, "malformed delta: add_units/modify_unit_by_id/mark_skipped wrong type"

    # Validate add_units
    new_ids = set()
    for idx, u in enumerate(add_units):
        if not isinstance(u, dict):
            return False, f"add_units[{idx}] not a dict"
        uid = u.get("id", "")
        if not isinstance(uid, str) or not _UNIT_ID_RE.match(uid):
            return False, f"add_units[{idx}].id invalid (must match ^TU-[0-9]+$): {uid!r}"
        if uid in existing_ids or uid in new_ids:
            return False, f"add_units[{idx}].id duplicates existing unit: {uid}"
        new_ids.add(uid)
        desc = u.get("description", "")
        if not isinstance(desc, str) or len(desc) > 500:
            return False, f"{uid}.description invalid or >500 chars"
        if _SHELL_META_RE.search(desc):
            return False, f"{uid}.description contains shell metacharacters"
        phase = u.get("phase", "")
        if not isinstance(phase, str) or _SHELL_META_RE.search(phase):
            return False, f"{uid}.phase invalid"
        deps = u.get("depends_on", [])
        if not isinstance(deps, list):
            return False, f"{uid}.depends_on not a list"
        for d in deps:
            if not _UNIT_ID_RE.match(d or ""):
                return False, f"{uid}.depends_on bad id: {d}"
            if d not in existing_ids and d not in new_ids:
                return False, f"{uid}.depends_on references unknown unit: {d}"
        outputs = u.get("outputs", [])
        if not isinstance(outputs, list):
            return False, f"{uid}.outputs not a list"
        for out in outputs:
            if _safe_output_path(out, ws_root) is None:
                return False, f"{uid}.outputs contains unsafe path: {out!r}"

    # Validate modify_unit_by_id
    for uid, changes in modify_map.items():
        if not _UNIT_ID_RE.match(uid):
            return False, f"modify_unit_by_id bad id: {uid}"
        if uid not in existing_ids and uid not in new_ids:
            return False, f"modify target missing: {uid}"
        if not isinstance(changes, dict):
            return False, f"modify_unit_by_id[{uid}] not a dict"
        # SEC-R2 #5: restrict allowed fields
        bad = set(changes) - _MODIFY_ALLOWED_FIELDS
        if bad:
            return False, f"modify_unit_by_id[{uid}] unknown fields: {bad}"
        # SEC-R2 #5: restrict status transitions - only skipped is allowed via modify
        # (done/running require real CLI mark_done/mark_running calls with artifacts)
        if "status" in changes:
            new_status = changes["status"]
            if new_status not in _MODIFY_ALLOWED_STATUS:
                return False, (
                    f"modify_unit_by_id[{uid}].status={new_status!r} forbidden "
                    f"(only {_MODIFY_ALLOWED_STATUS} via modify; use mark_done CLI for done)"
                )
            if uid in done_ids:
                return False, f"cannot re-status already-done unit: {uid}"
        # SEC-R2 #3: path-check outputs in modify (was only checked for add_units)
        if "outputs" in changes:
            mod_outputs = changes["outputs"]
            if not isinstance(mod_outputs, list):
                return False, f"modify_unit_by_id[{uid}].outputs not a list"
            for out in mod_outputs:
                if _safe_output_path(out, ws_root) is None:
                    return False, (
                        f"modify_unit_by_id[{uid}].outputs contains unsafe path: {out!r}"
                    )
        # Validate description/phase metachars if present
        if "description" in changes:
            d = changes["description"]
            if not isinstance(d, str) or len(d) > 500 or _SHELL_META_RE.search(d):
                return False, f"modify_unit_by_id[{uid}].description invalid"
        if "phase" in changes:
            ph = changes["phase"]
            if not isinstance(ph, str) or _SHELL_META_RE.search(ph):
                return False, f"modify_unit_by_id[{uid}].phase invalid"
        # Validate depends_on references if present
        if "depends_on" in changes:
            deps = changes["depends_on"]
            if not isinstance(deps, list):
                return False, f"modify_unit_by_id[{uid}].depends_on not a list"
            for d in deps:
                if not _UNIT_ID_RE.match(d or ""):
                    return False, f"modify_unit_by_id[{uid}].depends_on bad id: {d}"
                if d not in existing_ids and d not in new_ids:
                    return False, f"modify_unit_by_id[{uid}].depends_on unknown: {d}"

    # Validate mark_skipped
    for uid in skip_list:
        if not _UNIT_ID_RE.match(uid or ""):
            return False, f"mark_skipped bad id: {uid}"
        if uid not in existing_ids:
            return False, f"mark_skipped references unknown unit: {uid}"

    # Cycle detection: build prospective unit list and check
    prospective_units = []
    for u in current_units:
        if u["id"] in modify_map and "depends_on" in modify_map[u["id"]]:
            u2 = {**u, "depends_on": modify_map[u["id"]]["depends_on"]}
        else:
            u2 = u
        prospective_units.append(TaskUnit(
            id=u2["id"], phase=u2.get("phase", ""),
            description=u2.get("description", ""),
            depends_on=list(u2.get("depends_on", [])),
        ))
    for new_u in add_units:
        prospective_units.append(TaskUnit(
            id=new_u["id"], phase=new_u.get("phase", ""),
            description=new_u.get("description", ""),
            depends_on=list(new_u.get("depends_on", [])),
        ))
    try:
        _cycle_check(prospective_units)
    except Exception as e:
        return False, f"delta introduces dependency cycle: {e}"

    return True, "ok"


def _apply_delta(state: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Any]:
    """Mutator for atomic_update_json. Returns new state dict."""
    units = list(state.get("units", []))
    # apply modifications
    modify_map = delta.get("modify_unit_by_id", {})
    for i, u in enumerate(units):
        if u["id"] in modify_map:
            units[i] = {**u, **modify_map[u["id"]]}
    # apply skips
    for skip_id in delta.get("mark_skipped", []):
        for i, u in enumerate(units):
            if u["id"] == skip_id and u.get("status") != "done":
                units[i] = {**u, "status": "skipped"}
    # append new units (defaults)
    for new_u in delta.get("add_units", []):
        full = {
            "id": new_u["id"],
            "phase": new_u.get("phase", ""),
            "description": new_u.get("description", ""),
            "depends_on": list(new_u.get("depends_on", [])),
            "status": "pending",
            "outputs": list(new_u.get("outputs", [])),
            "reason": "",
            "replan_requested": False,
        }
        units.append(full)
    state["units"] = units
    state["last_updated"] = time.time()
    state["replan_requested"] = False  # clear the trigger
    return state


def _call_architect_live(prompt: str, timeout: int = 120) -> str:
    """Subprocess to `claude -p architect` for real JSON delta.

    Returns the raw stdout (expected JSON-in-markdown or pure JSON).
    Callers must still validate via _validate_delta.
    """
    claude_bin = shutil.which("claude") or shutil.which("claude.cmd")
    if not claude_bin:
        raise RuntimeError("`claude` CLI not in PATH — architect invocation impossible")
    proc = subprocess.run(
        [claude_bin, "-p", "--agent", "architect"],
        input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"architect CLI failed (rc={proc.returncode}): {proc.stderr[:400]}")
    return proc.stdout


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """Locate the FIRST syntactically complete JSON object in raw text.

    SEC-R2 #2 + LOG-R2 #3 hardened:
      - Uses json.JSONDecoder.raw_decode (balanced-brace parser built into
        Python's json lib) to stop at the first complete object, instead
        of rfind('}') which concatenates multiple blobs into one.
      - Handles ```json ... ``` fences by extracting fence contents first,
        then parsing via raw_decode on the fence body.
      - Never falls back to greedy first-to-last match (attack surface).
    """
    raw = raw.strip()
    decoder = json.JSONDecoder()

    def _try_parse_at(text: str, start: int) -> Optional[Dict[str, Any]]:
        # Scan forward for '{' and try raw_decode there; return first success
        while True:
            idx = text.find("{", start)
            if idx < 0:
                return None
            try:
                obj, _ = decoder.raw_decode(text, idx)
                if isinstance(obj, dict):
                    return obj
                # Not a dict (e.g., list) - skip
                start = idx + 1
            except (json.JSONDecodeError, ValueError):
                start = idx + 1

    # Try whole-text direct parse first (fastest; fails immediately if prose)
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Try ```json ... ``` fence first (highest precedence in ambiguous output)
    # Greedy fence match, raw_decode stops at first balanced object inside.
    for fm in re.finditer(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL):
        inner = fm.group(1)
        obj = _try_parse_at(inner, 0)
        if obj is not None:
            return obj

    # Scan whole raw text for first balanced JSON object
    return _try_parse_at(raw, 0)


def _build_prompt(state: Dict[str, Any], failure_context: Dict[str, Any],
                  trace_tail: List[Dict]) -> str:
    remaining = [u for u in state.get("units", [])
                 if u.get("status") in ("pending", "failed", "running")]
    return (
        "You are re-planning a stuck autopilot task. Return ONLY a JSON delta "
        "(optionally inside a ```json ... ``` fence).\n\n"
        "Failure context:\n"
        f"{json.dumps(failure_context, ensure_ascii=False)[:1500]}\n\n"
        "Recent trace (last 50 events):\n"
        f"{json.dumps(trace_tail[-50:], ensure_ascii=False)[:3000]}\n\n"
        "Remaining units:\n"
        f"{json.dumps(remaining, ensure_ascii=False)[:2000]}\n\n"
        "Output schema:\n"
        '{\n'
        '  "add_units": [{"id": "TU-N", "phase": "...", '
        '"description": "...", "depends_on": ["TU-M"], "outputs": [...]}],\n'
        '  "modify_unit_by_id": {"TU-N": {"depends_on": [...], "status": "skipped"}},\n'
        '  "mark_skipped": ["TU-N"]\n'
        '}\n\n'
        "Constraints:\n"
        "  - New TU ids must be unused integers > max existing (e.g. if TU-8 exists use TU-9+).\n"
        "  - No shell metachars in descriptions.\n"
        "  - Output paths must be relative to the workspace root.\n"
        "  - Cannot revert done units to pending."
    )


def _read_trace_tail(task_id: str, n: int = 50) -> List[Dict]:
    d = task_dir(task_id)
    trace_file = d / "trace.jsonl"
    if not trace_file.exists():
        return []
    try:
        lines = trace_file.read_text(encoding="utf-8").splitlines()[-n:]
        parsed = []
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
        return parsed
    except OSError:
        return []


def _append_merge_log(task_id: str, delta: Dict[str, Any], failure_context: Dict[str, Any]) -> None:
    """Rollback support: append-only log of applied deltas."""
    d = task_dir(task_id)
    log_file = d / "_merge_log.jsonl"
    entry = {
        "ts": time.time(),
        "delta": delta,
        "failure_context": failure_context,
    }
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # non-critical


def _append_amendment_to_plan_md(task_id: str, delta: Dict[str, Any],
                                  failure_context: Dict[str, Any]) -> bool:
    """B1 (plan v2 2026-04-21): append delta summary to architect's plan_v2.md.

    Reads `state.plan_path` (set by task_unit_scheduler.initialize_units
    when the B1-prep 3rd arg is passed). Appends a human-readable entry
    under ## Amendments section (creates the section if missing). Silent
    no-op (returns True) when plan_path is absent or file unreadable - B1
    is a best-effort traceability feature, not a hard requirement.

    SEC-R1-HIGH-1 (2026-04-21): plan_path from state.json is untrusted
    (a malicious or corrupt state could redirect writes to arbitrary
    files). Before writing, resolve the path and verify it's within
    WORKSPACE_ROOT. Out-of-workspace paths cause silent no-op with a
    trace event — NOT an error return, to preserve best-effort semantics.

    Returns True on success OR intentional no-op; False only on unexpected
    write failure.
    """
    from datetime import datetime, timezone
    state = load_state(task_id)
    if not state:
        return True
    plan_path_str = state.get("plan_path")
    if not plan_path_str:
        return True  # silent no-op: nothing to amend
    plan_file = Path(plan_path_str)
    if not plan_file.exists():
        return True  # plan was moved/deleted; silent no-op
    # SEC-HIGH-1: validate resolved path is inside workspace
    ws_root = _workspace_root()
    try:
        resolved = plan_file.resolve()
        resolved.relative_to(ws_root.resolve())
    except (ValueError, OSError):
        append_trace(task_id, "replan_amendment_rejected",
                     {"reason": "plan_path outside workspace",
                      "plan_path": str(plan_file)[:200]})
        return True  # silent no-op with audit trail

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n_adds = len(delta.get("add_units", []))
    n_mods = len(delta.get("modify_unit_by_id", {}))
    n_skips = len(delta.get("mark_skipped", []))
    failed_unit = failure_context.get("failed_unit", "?")
    reason = str(failure_context.get("reason", ""))[:120]

    entry = (
        f"\n### {ts} — re_planner amendment\n"
        f"- Trigger: unit `{failed_unit}` failed: {reason}\n"
        f"- Delta: +{n_adds} add / ~{n_mods} modify / -{n_skips} skip\n"
    )
    if n_adds:
        ids = [u.get("id", "?") for u in delta.get("add_units", [])[:5]]
        entry += f"- Added units: {', '.join(ids)}\n"
    if n_mods:
        ids = list(delta.get("modify_unit_by_id", {}).keys())[:5]
        entry += f"- Modified: {', '.join(ids)}\n"
    if n_skips:
        entry += f"- Skipped: {', '.join(delta.get('mark_skipped', [])[:5])}\n"

    try:
        existing = plan_file.read_text(encoding="utf-8")
    except OSError:
        return False
    # Create ## Amendments section if absent; else append under it
    if "## Amendments" not in existing:
        new_content = existing.rstrip() + "\n\n## Amendments\n" + entry
    else:
        new_content = existing.rstrip() + entry
    # U7 plan_v2 TU-6: chmod-444-aware retry. plan_versioning.set_latest_plan
    # marks older plan_v<N>.md immutable (chmod 0o444); concurrent re_planner +
    # mini_replan amendment write attempts can fail OSError. Retry once with
    # 0o644 chmod restoration, then emit chmod_failed_post_immutable trace
    # if still failing (best-effort no-raise per existing semantics).
    try:
        plan_file.write_text(new_content, encoding="utf-8")
        return True
    except OSError:
        try:
            os.chmod(plan_file, 0o644)
            plan_file.write_text(new_content, encoding="utf-8")
            return True
        except OSError as e:
            try:
                from memexa.core.trace_sink import write_trace_event
                write_trace_event("chmod_failed_post_immutable", {
                    "task_id": task_id,
                    "plan_path": str(plan_file)[:200],
                    "error_type": type(e).__name__,
                    "error_msg": str(e)[:200],
                })
            except Exception:
                pass
            return False


def re_plan(task_id: str, failure_context: Dict[str, Any],
            architect_fn: Optional[ArchitectFn] = None) -> int:
    """Execute re-planning. Returns number of units added/modified, or -1 on failure.

    Args:
        task_id: target task
        failure_context: dict with at least {"failed_unit": "TU-N", "reason": "..."}
        architect_fn: injectable for tests; default = _call_architect_live
    """
    state = load_state(task_id)
    if not state:
        return -1
    trace_tail = _read_trace_tail(task_id)
    prompt = _build_prompt(state, failure_context, trace_tail)

    caller = architect_fn if architect_fn else _call_architect_live
    try:
        raw = caller(prompt)
    except Exception as e:
        append_trace(task_id, "replan_failed",
                     {"stage": "architect_call", "error": str(e)[:400]})
        return -1

    delta = _extract_json(raw)
    if delta is None:
        append_trace(task_id, "replan_failed",
                     {"stage": "json_parse", "raw_head": raw[:200]})
        return -1

    ws_root = _workspace_root()
    ok, reason = _validate_delta(delta, state.get("units", []), ws_root)
    if not ok:
        append_trace(task_id, "replan_failed",
                     {"stage": "validate", "reason": reason})
        return -1

    def _mut(s):
        return _apply_delta(s, delta)

    if not update_state(task_id, _mut):
        append_trace(task_id, "replan_failed", {"stage": "write"})
        return -1

    _append_merge_log(task_id, delta, failure_context)
    # B1 (plan v2): best-effort append amendment to architect plan_v2.md
    _append_amendment_to_plan_md(task_id, delta, failure_context)
    n_adds = len(delta.get("add_units", []))
    n_mods = len(delta.get("modify_unit_by_id", {}))
    n_skips = len(delta.get("mark_skipped", []))
    total = n_adds + n_mods + n_skips
    append_trace(task_id, "replan_applied",
                 {"add": n_adds, "modify": n_mods, "skip": n_skips})
    return total


def run_replan(task_id: str, failure_ctx: Dict[str, Any] = None,
               ctx: Dict[str, Any] = None) -> int:
    """Canonical replan entry: consult failure_cluster_detector, route to either
    architect_full (cluster) or individual re_plan (otherwise).

    U13 long_term_plan_v2 §3 wrapper. MUST_CALL=run_replan for all autopilot loop
    callers; existing re_plan() stays for direct ctx-locked tests.

    L-3 (Phase 3, 2026-05-04): accept `ctx` keyword arg with mode="live_findings"
    for Stage 6 → Stage 2 auto-loop. live_findings mode skips cluster routing
    and writes a single-AC failure context for re_plan.

    Returns: int from re_plan or trigger_architect_full (n_changes >=0 or -1).
        Cooldown-block path returns 0.
    """
    # L-3: accept ctx kwarg + back-compat with failure_ctx positional
    actual_ctx = ctx if ctx is not None else (failure_ctx or {})
    if actual_ctx.get("mode") == "live_findings":
        # Stage 6 LIVE finding path — skip cluster routing, single-AC replan
        append_trace(task_id, "replan_live_findings_triggered", {
            "task_id": task_id,
            "failed_ac": actual_ctx.get("failed_ac", "?"),
            "root_cause": actual_ctx.get("root_cause", "live_gap"),
            "reason": (actual_ctx.get("reason") or "")[:200],
        })
        # Convert to standard re_plan ctx
        replan_ctx = {
            "failed_unit": actual_ctx.get("failed_ac", "live_finding"),
            "reason": actual_ctx.get("reason", "live_gap"),
            "live_finding": True,
        }
        return re_plan(task_id, replan_ctx)

    from memexa.core.failure_cluster_detector import (
        detect_and_route, trigger_architect_full,
    )
    decision = detect_and_route(task_id)
    action, anchor, count, events = decision
    if action == "cluster_replan" and anchor is not None:
        return trigger_architect_full(task_id, anchor, events, count)
    if action == "cooldown_block":
        append_trace(task_id, "action_item", {
            "level": "L2",
            "reason": "cluster_cooldown_block",
            "axis_anchor": anchor,
            "count": count,
        })
        return 0
    return re_plan(task_id, actual_ctx)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: python -m memexa.core.re_planner <task_id> [--context path.json]"""
    import argparse
    p = argparse.ArgumentParser(prog="re_planner")
    p.add_argument("task_id")
    p.add_argument("--context", help="path to JSON failure_context")
    args = p.parse_args(argv)

    if args.context:
        ctx = json.loads(Path(args.context).read_text(encoding="utf-8"))
    else:
        ctx = {"reason": "unspecified", "failed_unit": None}

    n = re_plan(args.task_id, ctx)
    if n < 0:
        print("re_plan failed (see trace.jsonl)", file=sys.stderr)
        return 2
    print(json.dumps({"units_changed": n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
