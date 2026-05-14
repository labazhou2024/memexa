"""
Departure Protocol v3 — Delegate to TaskBrain for project generation.

When the user leaves, TaskBrain generates smart projects based on current
system state (test failures, bugs, untested modules). KAIROS executes them.

Usage:
    from memexa.core.departure import plan_departure
    plan = plan_departure(duration_minutes=180)
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_DATA = Path(__file__).parent.parent / "data"


def plan_departure(duration_minutes: int = 180) -> Dict:
    """Generate and queue projects for autonomous execution while user is away.

    Uses TaskBrain for intelligent project generation based on current system state.
    Falls back to a single generic maintenance project if TaskBrain fails.
    """
    from .kairos_daemon import submit_project

    # Try TaskBrain for intelligent project generation
    project_specs = _generate_via_brain(duration_minutes)

    # Fallback: single generic maintenance project
    if not project_specs:
        project_specs = [_fallback_project()]

    # Submit projects to KAIROS queue
    queued: List[Dict] = []
    total_estimate = 0
    for spec in project_specs:
        proj_id = submit_project(
            title=spec["title"],
            prompt=spec["prompt"],
            priority=spec.get("priority", 3),
            model=spec.get("model", "sonnet"),
            max_budget_usd=spec.get("max_budget_usd", 1.00),
            max_turns=spec.get("max_turns", 25),
        )
        if proj_id:
            estimate = spec.get("estimate_min", 15)
            queued.append({"id": proj_id, "title": spec["title"], "estimate_min": estimate})
            total_estimate += estimate

    plan = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "duration_minutes": duration_minutes,
        "projects_queued": len(queued),
        "estimated_minutes": total_estimate,
        "buffer_minutes": max(0, duration_minutes - total_estimate),
        "projects": queued,
    }

    # Save plan
    _DATA.mkdir(parents=True, exist_ok=True)
    (_DATA / "departure_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("Departure plan: %d projects, ~%d min, %d min buffer",
                len(queued), total_estimate, plan["buffer_minutes"])
    return plan


def _generate_via_brain(duration_minutes: int) -> List[Dict]:
    """Use TaskBrain for intelligent project generation."""
    try:
        from .task_brain import get_task_brain
        brain = get_task_brain()
        return brain.generate_projects(duration_minutes=duration_minutes)
    except Exception as e:
        logger.warning("TaskBrain unavailable: %s", e)
        return []


def _fallback_project() -> Dict:
    """Minimal fallback: run tests + fix failures."""
    return {
        "title": "Maintenance: run tests and fix failures",
        "prompt": (
            "Run the full test suite with: python -m pytest tests/ -q --tb=short\n"
            "If any tests fail, investigate and fix them.\n"
            "If all tests pass, review recent git changes for any issues.\n"
            "Commit any fixes with descriptive messages."
        ),
        "priority": 3,
        "model": "sonnet",
        "max_budget_usd": 1.00,
        "max_turns": 25,
        "estimate_min": 15,
    }
