"""DEPRECATED — idle_window_detector permanently archived 2026-05-07.

Used to gate the Mac 27B arbiter swap (only run during idle window).
Archived together with the rest of the paired_eval stack.

Archived source: archive/2026_05_07_paired_eval_archived/memex/core/idle_window_detector.py
"""
from __future__ import annotations

from typing import Any, Dict


def is_idle_window(*args: Any, **kwargs: Any) -> bool:
    """Always returns False post-archival (no swap should ever trigger)."""
    return False


def explain(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return {
        "status": "archived",
        "reason": "paired_eval stack archived 2026-05-07 per CEO directive",
        "is_idle": False,
    }


__all__ = ["is_idle_window", "explain"]
