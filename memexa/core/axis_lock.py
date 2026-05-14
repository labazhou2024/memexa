"""
axis_lock.py — K8s ValidatingAdmissionPolicy-style rules over PlanSpec (2026-04-21).

Enforces which fields of a PlanSpec can change across revisions:
  - anchors_no_removal: axis_anchor IDs may only grow (append-only)
  - anchors_immutable_text: existing anchor.text cannot be modified
    (except via revise_anchor L3 approval path)
  - acs_no_removal: AC IDs may only grow
  - version_monotonic: version integer strictly increases
  - revision_reason_required: new spec must declare a reason

Violations are returned as a list of Violation dataclass instances.
Gate callers decide block/allow based on the list.

Declarative rules (Rule instances) so new constraints can be added
without touching check_revision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


@dataclass
class Violation:
    rule_name: str
    kind: str          # "anchor_removed" | "anchor_modified" | "ac_removed" | ...
    detail: str
    severity: str = "block"  # "block" | "warn"


@dataclass
class Rule:
    name: str
    description: str
    check_fn: Callable[[Any, Any], List[Violation]]


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------


def _anchors_no_removal(new_spec, old_spec) -> List[Violation]:
    old_ids = {a.id for a in old_spec.axis_anchors}
    new_ids = {a.id for a in new_spec.axis_anchors}
    removed = old_ids - new_ids
    return [
        Violation("anchors_no_removal", "anchor_removed",
                  f"anchor {aid} removed from v{old_spec.version} -> v{new_spec.version}")
        for aid in sorted(removed)
    ]


def _anchors_immutable_text(new_spec, old_spec) -> List[Violation]:
    old_by_id = {a.id: a for a in old_spec.axis_anchors}
    new_by_id = {a.id: a for a in new_spec.axis_anchors}
    viols = []
    for aid, new_a in new_by_id.items():
        if aid not in old_by_id:
            continue  # newly added; allowed
        old_a = old_by_id[aid]
        if new_a.text.strip() != old_a.text.strip():
            viols.append(Violation(
                "anchors_immutable_text", "anchor_modified",
                f"anchor {aid} text changed: {_truncate(old_a.text)} -> {_truncate(new_a.text)}",
            ))
    return viols


def _acs_no_removal(new_spec, old_spec) -> List[Violation]:
    old_ids = {a.id for a in old_spec.acceptance_criteria}
    new_ids = {a.id for a in new_spec.acceptance_criteria}
    removed = old_ids - new_ids
    return [
        Violation("acs_no_removal", "ac_removed",
                  f"AC {aid} removed from v{old_spec.version} -> v{new_spec.version}")
        for aid in sorted(removed)
    ]


def _version_monotonic(new_spec, old_spec) -> List[Violation]:
    if new_spec.version <= old_spec.version:
        return [Violation(
            "version_monotonic", "version_regression",
            f"version {new_spec.version} <= {old_spec.version}",
        )]
    return []


def _revision_reason_required(new_spec, old_spec) -> List[Violation]:
    if not new_spec.revision_reason or not new_spec.revision_reason.strip():
        return [Violation(
            "revision_reason_required", "empty_reason",
            "revision_reason is empty",
        )]
    return []


def _truncate(s: str, n: int = 60) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "..."


# ---------------------------------------------------------------------------
# Default rule set
# ---------------------------------------------------------------------------


DEFAULT_RULES: List[Rule] = [
    Rule("anchors_no_removal",
         "Axis anchors may only be appended, never removed",
         _anchors_no_removal),
    Rule("anchors_immutable_text",
         "Existing anchor.text cannot be modified (use revise_anchor L3 path)",
         _anchors_immutable_text),
    Rule("acs_no_removal",
         "Acceptance criteria may only be appended; IDs are permanent",
         _acs_no_removal),
    Rule("version_monotonic",
         "Version integer must strictly increase across revisions",
         _version_monotonic),
    Rule("revision_reason_required",
         "Revision must declare non-empty reason",
         _revision_reason_required),
]


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def check_revision(new_spec, old_spec,
                   rules: Optional[List[Rule]] = None,
                   ignore_revise_anchor_approvals: bool = False) -> List[Violation]:
    """Evaluate all rules; return aggregated violations.

    If `ignore_revise_anchor_approvals=False` (default), anchor_removed
    and anchor_modified violations are filtered out when an approved
    `revise_anchor_requested` item exists in approval_queue for that
    task. This is the CEO-approved anchor-edit escape hatch
    (break BC-3 deadlock).
    """
    rules = rules if rules is not None else DEFAULT_RULES
    all_viols: List[Violation] = []
    for r in rules:
        try:
            all_viols.extend(r.check_fn(new_spec, old_spec))
        except Exception as e:
            all_viols.append(Violation(r.name, "rule_check_error", str(e)[:200]))

    if not ignore_revise_anchor_approvals:
        approved = _approved_revise_anchor_items(new_spec.task_id)
        if approved:
            # Filter anchor_* violations whose anchor_id was explicitly approved
            filtered = []
            for v in all_viols:
                if v.kind in ("anchor_removed", "anchor_modified"):
                    # extract anchor id from detail
                    ok = any(aid in v.detail for aid in approved)
                    if ok:
                        continue  # CEO-approved, skip
                filtered.append(v)
            all_viols = filtered
    return all_viols


def _approved_revise_anchor_items(task_id: str) -> List[str]:
    """Return anchor IDs that have APPROVED revise_anchor items in queue.

    Fail-open: on any error, returns empty list (so violations stand).
    """
    try:
        from memexa.core.approval_queue import get_all
        items = get_all()
    except Exception:
        return []
    out = []
    for it in items or []:
        if it.get("category") != "revise_anchor":
            continue
        if it.get("status") != "approved":
            continue
        ctx = it.get("context", "") + " " + it.get("title", "")
        if task_id not in ctx:
            continue
        # Parse "anchor=ANCHOR-N" marker
        import re
        m = re.search(r"anchor[=: ]?\s*(ANCHOR-\w+)", ctx)
        if m:
            out.append(m.group(1))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv) -> int:
    import argparse
    import json
    import sys
    p = argparse.ArgumentParser(prog="axis_lock")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_c = sub.add_parser("check", help="check revision violations")
    p_c.add_argument("task_id")
    p_c.add_argument("old_version", type=int)
    p_c.add_argument("new_version", type=int)
    args = p.parse_args(argv[1:])

    if args.cmd == "check":
        from memexa.core.plan_spec import load_plan
        try:
            old_spec = load_plan(args.task_id, version=args.old_version)
            new_spec = load_plan(args.task_id, version=args.new_version)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        viols = check_revision(new_spec, old_spec)
        out = {
            "task_id": args.task_id,
            "old_version": args.old_version,
            "new_version": args.new_version,
            "violations": [v.__dict__ for v in viols],
            "block": any(v.severity == "block" for v in viols),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if not out["block"] else 1
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv))
