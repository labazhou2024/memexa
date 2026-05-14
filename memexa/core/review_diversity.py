"""review_diversity — suggest the next Stage 4 reviewer by angle coverage.

Motivation (plan_v1 CF6): 2026-04-23 Stage 4 hand-picked security-reviewer.
Earlier autopilots used code-reviewer (same angle set as council-architect
`plan_compliance`), producing 2× narrate-middle failures. This module
gives a deterministic pick based on "angle not yet covered".

Shares `AGENT_ANGLE_MAP` with agent_output_validator to avoid drift.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Set

from memexa.core.agent_output_validator import AGENT_ANGLE_MAP


def suggest_next_reviewer(already_used: Iterable[str]) -> Optional[str]:
    """Pick reviewer agent with max new angle coverage vs those already used.

    Returns None if all non-used agents' angles are fully subset of used.
    """
    used = set(already_used or [])
    used_angles: Set[str] = set()
    for a in used:
        used_angles |= AGENT_ANGLE_MAP.get(a, set())

    best: Optional[str] = None
    best_score = 0
    for agent, angles in AGENT_ANGLE_MAP.items():
        if agent in used:
            continue
        new_coverage = len(angles - used_angles)
        if new_coverage > best_score:
            best_score = new_coverage
            best = agent
    return best


def missing_angles(already_used: Iterable[str]) -> Set[str]:
    """Return set of angle labels present in ANGLE_MAP but not yet covered."""
    all_angles = set().union(*AGENT_ANGLE_MAP.values())
    covered = set().union(*(AGENT_ANGLE_MAP.get(a, set()) for a in (already_used or [])))
    return all_angles - covered


# ---------------------------------------------------------------------------
# Drift-prevention test helper: list every .claude/agents/*.md basename.
# ---------------------------------------------------------------------------


def _workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def list_agent_md_basenames() -> Set[str]:
    """Return set of agent names derived from `.claude/agents/*.md`."""
    agents_dir = _workspace_root() / ".claude" / "agents"
    if not agents_dir.exists():
        return set()
    return {p.stem for p in agents_dir.glob("*.md") if p.is_file()}


def drift_report() -> dict:
    """Return {'mapped': [...], 'md_only': [...], 'map_only': [...]} for CI."""
    mapped = set(AGENT_ANGLE_MAP.keys())
    md = list_agent_md_basenames()
    return {
        "mapped": sorted(mapped),
        "md_only": sorted(md - mapped),
        "map_only": sorted(mapped - md),
    }
