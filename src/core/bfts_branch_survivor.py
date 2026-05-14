"""BFTS Branch Survivor (long_term_plan_v2 §3 U14).

Sakana v2 Best-First Tree Search 3-stage tree (codegen / tune / research-agenda)
for non-numeric refactor TUs. Lives as Stage-2 dispatch shim (single-TU
sub-execution mechanism), NOT a task_unit_scheduler rewrite.

Key design choices (from plan_v1):
  - tu_class read from plan_v_<latest>.md text (NOT state.units; field has no
    writer per logic-iter1 verifier finding — same shape as U13 axis_anchor).
  - 4-cell fallback matrix on tu_class:
      refactor / schema_migration / doc_update -> run_bfts
      numeric_kernel / mixed                   -> skip_numeric (route to U16)
      unknown (field empty)                    -> skip_unknown + L2 action_item
      missing (field absent / R18 bypassed)    -> skip_missing + L2 action_item
  - Tree state JSON-persisted (NOT pickle); atomic temp+rename.
  - Sequential v1 num_workers=1 (single for loop; assert sum(running)<=1).
  - Strict-AND multi-cmd scoring: ALL verify_cmds rc=0 AND non-empty diff.
  - Tie-break: earliest finish_at -> smallest diff_size -> lex BR-id.
  - Caps: depth=3, branches_per_node=4, total_nodes=12.
  - Resume: orphaned running (dead pid) -> reclassify, NOT re-run.
  - No-winner contract: action="bfts_no_winner" (caller distinguishes).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.task_dir_layout import task_dir, append_trace

logger = logging.getLogger(__name__)

MAX_TREE_DEPTH: int = 3
MAX_BRANCHES_PER_NODE: int = 4
MAX_TOTAL_NODES: int = 12
BRANCH_ID_PREFIX: str = "BR-"

VALID_TU_CLASSES = {
    "refactor", "schema_migration", "doc_update",
    "numeric_kernel", "mixed", "unknown",
}
RUN_BFTS_CLASSES = {"refactor", "schema_migration", "doc_update"}
SKIP_NUMERIC_CLASSES = {"numeric_kernel", "mixed"}

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_TU_ID_RE = re.compile(r"^TU-[0-9.]+$")
_TU_HEADING_RE = re.compile(
    r"### TU-([0-9.]+)[^\n]*\n([\s\S]*?)(?=\n### TU-|\n## |\Z)"
)
_TU_CLASS_LINE_RE = re.compile(
    r"\*\*tu_class\*\*\s*:\s*([a-z_]*)\s*$", re.MULTILINE
)


def _validate_task_id(task_id: str) -> bool:
    if not task_id or len(task_id) > 128:
        return False
    return bool(_TASK_ID_RE.match(task_id))


def _validate_tu_id(tu_id: str) -> bool:
    """Reject path traversal / shell metachars in tu_id (security-iter1-1)."""
    if not tu_id or len(tu_id) > 64:
        return False
    return bool(_TU_ID_RE.match(tu_id))


@dataclass
class BranchNode:
    id: str
    parent_id: Optional[str]
    depth: int
    status: str
    verify_rc: List[int] = field(default_factory=list)
    diff_sha256: str = ""
    diff_size: int = 0
    spawned_at: float = 0.0
    finished_at: float = 0.0
    pid: Optional[int] = None


@dataclass
class RouteResult:
    action: str
    winner_id: Optional[str]
    n_nodes: int
    reason: str


def extract_tu_class_map(task_id: str) -> Dict[str, Optional[str]]:
    """Parse plan_v_<latest>.md for tu_class per TU heading.

    Sentinel policy (logic-iter1-6):
      field absent -> None
      field empty (`tu_class:`) -> "unknown"
      field with valid value -> the value
    """
    if not _validate_task_id(task_id):
        return {}
    d = task_dir(task_id)
    plans = sorted(d.glob("plan_v*.md"))
    plans = [p for p in plans if p.stem != "plan_v_latest"]
    if not plans:
        return {}
    latest_pointer = d / "plan_v_latest.md"
    plan_path = latest_pointer if latest_pointer.is_file() else plans[-1]
    if plan_path.is_symlink():
        return {}
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: Dict[str, Optional[str]] = {}
    for m in _TU_HEADING_RE.finditer(text):
        tu_id = f"TU-{m.group(1)}"
        block = m.group(2)
        cm = _TU_CLASS_LINE_RE.search(block)
        if cm is None:
            out[tu_id] = None
        else:
            val = cm.group(1).strip()
            out[tu_id] = "unknown" if val == "" else val
    return out


def route_tu_class(tu_class: Optional[str]) -> str:
    """4-cell fallback matrix (plan §1)."""
    if tu_class is None:
        return "skip_missing"
    if tu_class in SKIP_NUMERIC_CLASSES:
        return "skip_numeric"
    if tu_class == "unknown":
        return "skip_unknown"
    if tu_class in RUN_BFTS_CLASSES:
        return "run_bfts"
    return "skip_unknown"


def _emit_skip_event(task_id: str, action: str, tu_id: str) -> None:
    event_map = {
        "skip_numeric": "bfts_skipped_numeric",
        "skip_unknown": "bfts_skipped_unknown_class",
        "skip_missing": "bfts_skipped_missing_class",
    }
    event = event_map.get(action)
    if event:
        append_trace(task_id, event, {"tu_id": tu_id, "ts": time.time()})


def score_branch(node: BranchNode) -> bool:
    """Strict-AND: all verify_rc=0 AND diff_size>0 (anti-stub)."""
    if not node.verify_rc:
        return False
    if any(rc != 0 for rc in node.verify_rc):
        return False
    if node.diff_size <= 0:
        return False
    return True


def tie_break(passing: List[BranchNode]) -> Optional[BranchNode]:
    """Earliest finish_at -> smallest diff_size -> lex BR-id."""
    if not passing:
        return None
    return sorted(
        passing,
        key=lambda n: (n.finished_at, n.diff_size, n.id),
    )[0]


def persist_tree(task_id: str, tu_id: str,
                 nodes: List[BranchNode],
                 winner_id: Optional[str]) -> bool:
    """Atomic JSON write: temp + rename."""
    if not _validate_task_id(task_id) or not _validate_tu_id(tu_id):
        return False
    d = task_dir(task_id)
    target = d / f"bfts_tree_{tu_id}.json"
    tmp = d / f".bfts_tree_{tu_id}.json.tmp"
    payload = {
        "tu_id": tu_id,
        "winner_id": winner_id,
        "nodes": [asdict(n) for n in nodes],
        "ts": time.time(),
    }
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(target))
        return True
    except OSError as e:
        logger.warning("persist_tree failed: %s", e)
        return False


def _pid_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def load_tree(task_id: str, tu_id: str) -> Optional[Dict[str, Any]]:
    """Resume helper. Reclassify orphan running -> orphaned + trace."""
    if not _validate_task_id(task_id) or not _validate_tu_id(tu_id):
        return None
    d = task_dir(task_id)
    p = d / f"bfts_tree_{tu_id}.json"
    if not p.is_file() or p.is_symlink():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    nodes = payload.get("nodes", [])
    reclassified = 0
    for n in nodes:
        if n.get("status") == "running":
            pid = n.get("pid")
            if not _pid_alive(pid):
                n["status"] = "orphaned"
                reclassified += 1
                append_trace(task_id, "bfts_orphan_reclassified", {
                    "tu_id": tu_id, "branch_id": n.get("id"),
                    "pid": pid, "ts": time.time(),
                })
    if reclassified:
        persist_tree(
            task_id, tu_id,
            [BranchNode(**{k: v for k, v in n.items()
                           if k in BranchNode.__dataclass_fields__})
             for n in nodes],
            payload.get("winner_id"),
        )
    return payload


def _spawn_branch_stub(task_id: str, tu_id: str,
                       parent_id: Optional[str], depth: int,
                       branch_idx: int,
                       executor_fn: Optional[Any] = None) -> BranchNode:
    """Spawn a branch node. In real flow this would invoke sonnet-executor.

    Test path: executor_fn(BranchNode) -> updates node fields (verify_rc / diff_*).
    Production: caller wires actual sonnet-executor spawn + verify_cmd run.
    """
    branch_id = f"{BRANCH_ID_PREFIX}{tu_id}-{depth}-{branch_idx}"
    node = BranchNode(
        id=branch_id,
        parent_id=parent_id,
        depth=depth,
        status="running",
        spawned_at=time.time(),
        pid=os.getpid(),
    )
    append_trace(task_id, "bfts_branch_spawned", {
        "tu_id": tu_id, "branch_id": branch_id,
        "depth": depth, "parent_id": parent_id,
        "ts": time.time(),
    })
    if executor_fn is not None:
        executor_fn(node)
    node.finished_at = time.time()
    if node.status == "running":
        node.status = "pending"
    return node


def run_bfts(task_id: str, tu_id: str,
             tu_class_override: Optional[str] = None,
             executor_fn: Optional[Any] = None) -> RouteResult:
    """Orchestrate BFTS for one TU. Returns RouteResult."""
    if not _validate_task_id(task_id):
        return RouteResult("skip_missing", None, 0, "invalid_task_id")
    if not _validate_tu_id(tu_id):
        return RouteResult("skip_missing", None, 0, "invalid_tu_id")

    existing = load_tree(task_id, tu_id)
    if existing is not None and existing.get("winner_id"):
        return RouteResult("bfts_winner", existing["winner_id"],
                           len(existing.get("nodes", [])), "resumed_winner")

    if tu_class_override is not None:
        tu_class = tu_class_override if tu_class_override != "__NONE__" else None
    else:
        cmap = extract_tu_class_map(task_id)
        tu_class = cmap.get(tu_id)

    action = route_tu_class(tu_class)
    if action != "run_bfts":
        _emit_skip_event(task_id, action, tu_id)
        return RouteResult(action, None, 0,
                           f"skipped_{action.replace('skip_','')}")

    nodes: List[BranchNode] = []
    root = _spawn_branch_stub(task_id, tu_id, None, 0, 1, executor_fn)
    root.status = "scored" if score_branch(root) else "dead"
    nodes.append(root)
    persist_tree(task_id, tu_id, nodes, None)

    if root.status == "scored":
        winner = root
        append_trace(task_id, "bfts_winner_selected", {
            "tu_id": tu_id, "branch_id": winner.id,
            "ts": time.time(),
        })
        persist_tree(task_id, tu_id, nodes, winner.id)
        return RouteResult("bfts_winner", winner.id, len(nodes), "winner_found")

    queue = [root]
    while queue and len(nodes) < MAX_TOTAL_NODES:
        parent = queue.pop(0)
        if parent.depth >= MAX_TREE_DEPTH:
            append_trace(task_id, "bfts_branch_pruned", {
                "tu_id": tu_id, "branch_id": parent.id,
                "reason": "depth_cap", "ts": time.time(),
            })
            continue
        for i in range(1, MAX_BRANCHES_PER_NODE + 1):
            if len(nodes) >= MAX_TOTAL_NODES:
                append_trace(task_id, "bfts_total_nodes_exceeded", {
                    "tu_id": tu_id, "n_nodes": len(nodes),
                    "ts": time.time(),
                })
                break
            child = _spawn_branch_stub(
                task_id, tu_id, parent.id, parent.depth + 1, i, executor_fn,
            )
            child.status = "scored" if score_branch(child) else "dead"
            nodes.append(child)
            running_n = sum(1 for n in nodes if n.status == "running")
            assert running_n <= 1, f"concurrency invariant broken: {running_n}"
            if child.status == "scored":
                queue.append(child)
            elif child.depth < MAX_TREE_DEPTH:
                queue.append(child)
        if i == MAX_BRANCHES_PER_NODE and len(nodes) < MAX_TOTAL_NODES:
            append_trace(task_id, "bfts_branch_pruned", {
                "tu_id": tu_id, "branch_id": parent.id,
                "reason": "branch_cap", "ts": time.time(),
            })

    passing = [n for n in nodes if n.status == "scored"]
    winner = tie_break(passing)

    if winner is None:
        append_trace(task_id, "bfts_tree_complete", {
            "tu_id": tu_id, "winner_id": None,
            "n_nodes": len(nodes), "ts": time.time(),
        })
        persist_tree(task_id, tu_id, nodes, None)
        return RouteResult("bfts_no_winner", None, len(nodes), "no_winner")

    append_trace(task_id, "bfts_winner_selected", {
        "tu_id": tu_id, "branch_id": winner.id,
        "ts": time.time(),
    })
    append_trace(task_id, "bfts_tree_complete", {
        "tu_id": tu_id, "winner_id": winner.id,
        "n_nodes": len(nodes), "ts": time.time(),
    })
    persist_tree(task_id, tu_id, nodes, winner.id)
    return RouteResult("bfts_winner", winner.id, len(nodes), "winner_found")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="bfts_branch_survivor")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Run BFTS for one TU.")
    r.add_argument("task_id")
    r.add_argument("tu_id")
    r.add_argument("--tu-class", default=None,
                   help="Override tu_class (use __NONE__ to test missing).")
    s = sub.add_parser("status", help="Print tree status JSON.")
    s.add_argument("task_id")
    s.add_argument("tu_id")
    args = p.parse_args(argv)

    if args.cmd == "run":
        if not _validate_task_id(args.task_id):
            print(json.dumps({"error": "invalid task_id"}), file=sys.stderr)
            return 2
        if not _validate_tu_id(args.tu_id):
            print(json.dumps({"error": "invalid tu_id"}), file=sys.stderr)
            return 2
        result = run_bfts(args.task_id, args.tu_id,
                          tu_class_override=args.tu_class)
        print(json.dumps(asdict(result), ensure_ascii=False))
        return 0
    if args.cmd == "status":
        if not _validate_task_id(args.task_id):
            print(json.dumps({"error": "invalid task_id"}), file=sys.stderr)
            return 2
        if not _validate_tu_id(args.tu_id):
            print(json.dumps({"error": "invalid tu_id"}), file=sys.stderr)
            return 2
        payload = load_tree(args.task_id, args.tu_id)
        if payload is None:
            print(json.dumps({"error": "no_tree"}))
            return 0
        print(json.dumps({
            "tu_id": payload.get("tu_id"),
            "winner_id": payload.get("winner_id"),
            "n_nodes": len(payload.get("nodes", [])),
        }, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
