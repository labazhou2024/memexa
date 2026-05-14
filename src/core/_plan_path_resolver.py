"""TU-2 (U1, plan_v1, 2026-04-26) — plan_v<N>.md path resolver.

V1 BLOCKER-V1 fix: in-scope canonical lookup order; not deferred to U4.
Lookup order:
  1. <workspace>/.claude/harness/tasks/<task_id>/plan_v<latest>.md  (autopilot canonical)
  2. memex/memex/data/plans/<task_id>/plan_v<latest>.md           (legacy programmatic)
  3. memex/.claude/plans/<task_id>.md                              (architect plan)

Returns Path to highest-version plan_v<N>.md found.
Raises FileNotFoundError if no plan in any location.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

_VERSION_RE = re.compile(r"plan_v(\d+)\.md$")


def _workspace_root() -> Path:
    """Resolve workspace root (parent of memex/)."""
    return Path(__file__).resolve().parent.parent.parent.parent


def _jarvis_root() -> Path:
    """Resolve memex package root."""
    return Path(__file__).resolve().parent.parent.parent


def _highest_version(paths: List[Path]) -> Optional[Path]:
    """Pick plan_v<N>.md with highest N. None if list empty."""
    versioned = []
    for p in paths:
        m = _VERSION_RE.search(p.name)
        if m:
            versioned.append((int(m.group(1)), p))
    if not versioned:
        return None
    return max(versioned, key=lambda x: x[0])[1]


def list_plan_versions(task_id: str) -> List[Path]:
    """Return all plan_v<N>.md files (any location), sorted by version desc."""
    out: List[Path] = []
    workspace = _workspace_root()
    memex = _jarvis_root()
    candidates = [
        workspace / ".claude" / "harness" / "tasks" / task_id,
        memex / "memex" / "data" / "plans" / task_id,
    ]
    for d in candidates:
        if d.is_dir():
            out.extend(sorted(d.glob("plan_v*.md")))
    # Architect plan dir uses single .md per task_id (no version suffix)
    arch = memex / ".claude" / "plans" / f"{task_id}.md"
    if arch.exists():
        out.append(arch)
    # Sort by version desc
    def _vk(p: Path) -> int:
        m = _VERSION_RE.search(p.name)
        return int(m.group(1)) if m else -1
    return sorted(out, key=_vk, reverse=True)


def resolve_plan_path(task_id: str) -> Path:
    """Locate latest plan_v<N>.md for task_id.

    Priority order: workspace task_dir > memex/memex/data > memex/.claude/plans

    Raises FileNotFoundError if no plan found.
    """
    workspace = _workspace_root()
    memex = _jarvis_root()

    # 1. workspace task_dir (autopilot canonical)
    ws_dir = workspace / ".claude" / "harness" / "tasks" / task_id
    if ws_dir.is_dir():
        latest = _highest_version(list(ws_dir.glob("plan_v*.md")))
        if latest:
            return latest

    # 2. memex/memex/data/plans/<task_id>/
    jd_dir = memex / "memex" / "data" / "plans" / task_id
    if jd_dir.is_dir():
        latest = _highest_version(list(jd_dir.glob("plan_v*.md")))
        if latest:
            return latest

    # 3. memex/.claude/plans/<task_id>.md (architect single-file)
    arch = memex / ".claude" / "plans" / f"{task_id}.md"
    if arch.exists():
        return arch

    raise FileNotFoundError(
        f"No plan found for task_id={task_id} in any of:\n"
        f"  - {ws_dir}/plan_v*.md\n"
        f"  - {jd_dir}/plan_v*.md\n"
        f"  - {arch}"
    )
