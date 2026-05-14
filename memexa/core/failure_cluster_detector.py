"""Failure cluster backpressure detector (long_term_plan_v2 §3 U13).

Monitors `unit_failed` events in 30-min sliding window over <task_dir>/trace.jsonl.
When >=3 distinct failed unit IDs share the same axis_anchor, escalates to
architect-level full re-plan instead of N independent re_planner spawns.

Key design choices (from plan_v1):
  - axis_anchor extracted from plan_v_<latest>.md text (NOT state.units; the field
    has no writer per logic-iter1-1 verifier finding).
  - Boundary formula: now - 1800s <= ts <= now + 60s (NTP tolerance applies to
    FUTURE clock skew on right bound only; past widening would break AC-3).
  - Tie-break: when >=2 anchors cluster simultaneously, return decision for the
    anchor whose 3rd-fail timestamp is OLDEST (FIFO).
  - Cooldown source-of-truth: scan trace.jsonl for prior `architect_replan_triggered`
    events (no separate state file -> idempotent under spawn crash).
  - Mock contract: MEMEXA_RE_PLANNER_MOCK=1 returns 0, emits both events with
    result="mocked", does NOT call re_plan().
  - Trace event payloads locked: {axis_anchor, count, coverage, task_id, ts} for
    failure_cluster_detected; {axis_anchor, result, task_id, ts} for architect_replan_triggered.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from memexa.core.task_dir_layout import task_dir, append_trace

logger = logging.getLogger(__name__)

WINDOW_SEC: int = 1800
NTP_TOL_SEC: int = 60
THRESHOLD: int = 3
COOLDOWN_SEC: int = 3600
MAX_PER_TASK_24H: int = 3
SECONDS_PER_DAY: int = 86400

RouteDecision = Tuple[str, Optional[str], int, List[Dict[str, Any]]]

_TU_HEADING_RE = re.compile(r"### TU-([0-9.]+)[^\n]*\n([\s\S]*?)(?=\n### TU-|\n## |\Z)")
_AXIS_ANCHOR_RE = re.compile(
    r"axis_anchor[^\n]*?(\[C:(?:cli|hook|schema|agent):[A-Za-z0-9_\-]+\])"
)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_task_id(task_id: str) -> bool:
    """Reject path traversal / shell metachars / empty (security-iter1-1)."""
    if not task_id or len(task_id) > 128:
        return False
    return bool(_TASK_ID_RE.match(task_id))


def _is_safe_regular_file(path: Path) -> bool:
    """Reject symlinks / NTFS reparse points (security-iter1-2 + HARD RULE
    feedback_ntfs_junction_reparse_point)."""
    if not path.is_file():
        return False
    if path.is_symlink():
        return False
    try:
        st = path.lstat()
        if hasattr(st, "st_file_attributes"):
            if st.st_file_attributes & 0x400:
                return False
    except OSError:
        return False
    return True


def scan_trace_window(
    task_id: str,
    window_sec: int = WINDOW_SEC,
    ntp_tol_sec: int = NTP_TOL_SEC,
    event_type: str = "unit_failed",
) -> List[Dict[str, Any]]:
    """Return trace events of given type with `now-window <= ts <= now+ntp_tol`.

    Malformed JSON lines are skipped with a warning (no exception).
    """
    d = task_dir(task_id)
    trace_file = d / "trace.jsonl"
    if not trace_file.is_file():
        return []
    now = time.time()
    left = now - window_sec
    right = now + ntp_tol_sec
    out: List[Dict[str, Any]] = []
    try:
        text = trace_file.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("scan_trace_window: skip malformed line in %s", trace_file)
            continue
        if entry.get("event") != event_type:
            continue
        ts = entry.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if left <= ts <= right:
            out.append(entry)
    return out


def extract_tu_axis_map(task_id: str) -> Dict[str, Optional[str]]:
    """Parse plan_v_<latest>.md text for {tu_id -> axis_anchor or None}.

    Looks for the canonical [C:cli/hook/schema/agent:NAME] shorthand on metadata
    lines under each H3 TaskUnit heading. Missing -> None ("unknown" bucket).
    """
    d = task_dir(task_id)
    plan_files = sorted(d.glob("plan_v*.md"))
    if not plan_files:
        return {}
    latest_pointer = d / "plan_v_latest.md"
    plan_path = (latest_pointer if _is_safe_regular_file(latest_pointer)
                 else plan_files[-1])
    if not _is_safe_regular_file(plan_path):
        return {}
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: Dict[str, Optional[str]] = {}
    for m in _TU_HEADING_RE.finditer(text):
        tu_id = f"TU-{m.group(1)}"
        block = m.group(2)
        am = _AXIS_ANCHOR_RE.search(block)
        out[tu_id] = am.group(1) if am else None
    return out


def count_clusters(
    events: List[Dict[str, Any]],
    axis_map: Dict[str, Optional[str]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Group failed events by axis_anchor; return {anchor -> [events]} where each
    anchor has list of events from DISTINCT unit_ids only."""
    by_anchor: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for ev in events:
        unit_id = ev.get("payload", {}).get("unit_id")
        if not unit_id:
            continue
        anchor = axis_map.get(unit_id) or "unknown"
        bucket = by_anchor.setdefault(anchor, {})
        if unit_id not in bucket or ev.get("ts", 0) < bucket[unit_id].get("ts", 0):
            bucket[unit_id] = ev
    return {anchor: list(units.values()) for anchor, units in by_anchor.items()}


def check_cooldown(task_id: str, axis_anchor: str) -> bool:
    """True if any architect_replan_triggered event for this anchor in last 3600s."""
    triggers = scan_trace_window(
        task_id, window_sec=COOLDOWN_SEC, ntp_tol_sec=0,
        event_type="architect_replan_triggered",
    )
    for t in triggers:
        if t.get("payload", {}).get("axis_anchor") == axis_anchor:
            return True
    return False


def check_global_cap(task_id: str) -> bool:
    """True if 24h cluster-replan count has reached MAX_PER_TASK_24H."""
    triggers = scan_trace_window(
        task_id, window_sec=SECONDS_PER_DAY, ntp_tol_sec=0,
        event_type="architect_replan_triggered",
    )
    return len(triggers) >= MAX_PER_TASK_24H


def report_live_finding(task_id: str, ac_id: str, reason: str,
                        root_cause: str = "live_gap") -> bool:
    """L-2 (Phase 3, 2026-05-04): record a Stage 6 LIVE finding for replan.

    Persists a live_findings/<ac_id>.json entry under task_dir, threshold-1
    triggers re_planner.run_replan(mode="live_findings"). Stage 5 commit-gate
    blocks until live_findings is empty (L-5).

    Args:
        task_id: active task_id
        ac_id: AC that failed (e.g. "AC-3")
        reason: short reason from ac_verifier _trace_failed
        root_cause: enum (default live_gap; B-4 added)
    Returns:
        True if recorded; False on infra error (fail-soft).
    """
    if not _validate_task_id(task_id):
        return False
    try:
        from memexa.core.task_dir_layout import task_dir as _td
        from datetime import datetime as _dt
        td = _td(task_id)
        live_dir = td / "live_findings"
        live_dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "task_id": task_id,
            "ac_id": ac_id,
            "reason": reason[:200],
            "root_cause": root_cause,
            "ts": _dt.utcnow().isoformat() + "Z",
        }
        rec_path = live_dir / f"{ac_id}.json"
        rec_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        # Emit trace
        try:
            from memexa.core.trace_sink import write_trace_event
            write_trace_event("live_finding_reported", {
                "task_id": task_id, "ac_id": ac_id, "root_cause": root_cause,
            })
        except Exception:
            pass
        # Trigger re_planner asynchronously (best-effort, fail-soft)
        try:
            from memexa.core.re_planner import run_replan
            run_replan(task_id, ctx={
                "mode": "live_findings",
                "failed_ac": ac_id,
                "reason": reason,
                "root_cause": root_cause,
            })
        except (ImportError, AttributeError, TypeError):
            pass  # re_planner may not yet support live_findings mode
        except Exception:
            pass
        return True
    except Exception:
        return False


def list_live_findings(task_id: str) -> list:
    """L-5 helper: return list of live_findings/<ac>.json records.

    Stage 5 commit-gate calls this to decide if commit is allowed.
    Returns [] when none — gate-pass.
    """
    if not _validate_task_id(task_id):
        return []
    try:
        from memexa.core.task_dir_layout import task_dir as _td
        td = _td(task_id)
        live_dir = td / "live_findings"
        if not live_dir.exists():
            return []
        out = []
        for f in live_dir.glob("*.json"):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out
    except Exception:
        return []


def clear_live_finding(task_id: str, ac_id: str) -> bool:
    """When ac_verifier re-verifies an AC successfully (L-4 path), clear
    its live_finding record so Stage 5 commit-gate can pass.
    """
    if not _validate_task_id(task_id):
        return False
    try:
        from memexa.core.task_dir_layout import task_dir as _td
        td = _td(task_id)
        rec_path = td / "live_findings" / f"{ac_id}.json"
        if rec_path.exists():
            rec_path.unlink()
            try:
                from memexa.core.trace_sink import write_trace_event
                write_trace_event("live_finding_cleared", {
                    "task_id": task_id, "ac_id": ac_id,
                })
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def detect_and_route(task_id: str) -> RouteDecision:
    if not _validate_task_id(task_id):
        logger.warning("detect_and_route: invalid task_id %r", task_id)
        return ("individual_replan", None, 0, [])
    events = scan_trace_window(task_id)
    axis_map = extract_tu_axis_map(task_id)
    clusters = count_clusters(events, axis_map)

    cluster_anchors: List[Tuple[str, List[Dict[str, Any]], float]] = []
    for anchor, evs in clusters.items():
        if len(evs) >= THRESHOLD:
            sorted_evs = sorted(evs, key=lambda e: e.get("ts", 0))
            third_fail_ts = sorted_evs[THRESHOLD - 1].get("ts", 0.0)
            cluster_anchors.append((anchor, sorted_evs, third_fail_ts))

    if not cluster_anchors:
        return ("individual_replan", None, len(events), [])

    cluster_anchors.sort(key=lambda x: x[2])

    if check_global_cap(task_id):
        anchor, evs, _ = cluster_anchors[0]
        return ("cooldown_block", anchor, len(evs), evs)

    blocked: List[Tuple[str, List[Dict[str, Any]]]] = []
    for anchor, evs, _ in cluster_anchors:
        if check_cooldown(task_id, anchor):
            blocked.append((anchor, evs))
            continue
        return ("cluster_replan", anchor, len(evs), evs)

    anchor, evs = blocked[0]
    return ("cooldown_block", anchor, len(evs), evs)


def trigger_architect_full(
    task_id: str,
    anchor_id: str,
    failed_units: List[Dict[str, Any]],
    prior_count: int,
) -> int:
    """Build 7-field architect_full ctx; emit both events with locked payloads."""
    coverage = "degraded" if anchor_id == "unknown" else "full"
    now = time.time()

    append_trace(task_id, "failure_cluster_detected", {
        "axis_anchor": anchor_id,
        "count": len(failed_units),
        "coverage": coverage,
        "task_id": task_id,
        "ts": now,
    })

    plan_v_current, plan_path = _find_latest_plan(task_id)
    ctx = {
        "replan_mode": "architect_full",
        "axis_anchor": anchor_id,
        "failed_units": [
            {
                "id": e.get("payload", {}).get("unit_id"),
                "reason": e.get("payload", {}).get("reason", ""),
                "ts": e.get("ts"),
            }
            for e in failed_units
        ],
        "cluster_window_sec": WINDOW_SEC,
        "prior_replan_count": prior_count,
        "plan_v_current": plan_v_current,
        "plan_path": plan_path,
    }

    if os.environ.get("MEMEXA_RE_PLANNER_MOCK") == "1":
        append_trace(task_id, "architect_replan_triggered", {
            "axis_anchor": anchor_id,
            "result": "mocked",
            "task_id": task_id,
            "ts": time.time(),
        })
        return 0

    try:
        from memexa.core.re_planner import re_plan
        n = re_plan(task_id, ctx)
    except Exception as e:
        logger.warning("trigger_architect_full: re_plan raised %s", e)
        n = -1

    append_trace(task_id, "architect_replan_triggered", {
        "axis_anchor": anchor_id,
        "result": "ok" if n >= 0 else "failed",
        "task_id": task_id,
        "ts": time.time(),
    })
    return n


def _find_latest_plan(task_id: str) -> Tuple[int, str]:
    d = task_dir(task_id)
    plans = sorted(d.glob("plan_v*.md"))
    plans = [p for p in plans if p.stem != "plan_v_latest"]
    if not plans:
        return (0, "")
    versions: List[int] = []
    for p in plans:
        m = re.match(r"plan_v(\d+)$", p.stem)
        if m:
            versions.append(int(m.group(1)))
    if not versions:
        return (0, str(plans[-1]))
    latest = max(versions)
    return (latest, str(d / f"plan_v{latest}.md"))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="failure_cluster_detector")
    sub = p.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="Run detect_and_route and print JSON.")
    chk.add_argument("task_id")
    args = p.parse_args(argv)

    if args.cmd == "check":
        if not _validate_task_id(args.task_id):
            print(json.dumps({"error": "invalid task_id"}), file=sys.stderr)
            return 2
        action, anchor, count, events = detect_and_route(args.task_id)
        out = {
            "action": action,
            "axis_anchor": anchor,
            "count": count,
            "events_count": len(events),
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
