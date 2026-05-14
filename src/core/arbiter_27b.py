"""DEPRECATED — 27B arbiter permanently archived 2026-05-07.

See memex/core/paired_eval.py docstring. CEO directive: archive entire
Mac dual-LLM + 27B arbiter swap stack.

Archived sources: archive/2026_05_07_paired_eval_archived/memex/core/arbiter_27b.py
"""
from __future__ import annotations

import warnings
from typing import Any


class Arbiter27BDeprecatedError(RuntimeError):
    """Raised on any 27B arbiter call post-archival."""


def _warn_once() -> None:
    if not getattr(_warn_once, "_done", False):
        warnings.warn(
            "arbiter_27b archived 2026-05-07; CEO directive — Gemma 4 31B "
            "sole extractor sufficient.",
            DeprecationWarning, stacklevel=3,
        )
        _warn_once._done = True  # type: ignore


def run_arbiter(*args: Any, **kwargs: Any) -> Any:
    _warn_once()
    raise Arbiter27BDeprecatedError("run_arbiter archived")


def swap_to_27b(*args: Any, **kwargs: Any) -> Any:
    _warn_once()
    raise Arbiter27BDeprecatedError("swap_to_27b archived")


def swap_back(*args: Any, **kwargs: Any) -> Any:
    _warn_once()
    raise Arbiter27BDeprecatedError("swap_back archived")


def arbitrate_one(*args: Any, **kwargs: Any) -> Any:
    """Stub: 27B arbiter archived. phase1.stage_C_pair wraps this in
    try/except (line 727-730) — raising lets unarbitrated singles survive
    as `single_*_v1_unarbitrated`. No data loss; just no triple-side
    arbitration on true conflicts (rare per LIVE 2026-05-08 stats).

    TODO future: route to DeepSeek v4 flash via DeepSeek API instead of stub.
    """
    raise Arbiter27BDeprecatedError("arbitrate_one archived — fall through to single_*_unarbitrated")


__all__ = ["Arbiter27BDeprecatedError", "run_arbiter",
           "swap_to_27b", "swap_back", "arbitrate_one"]
