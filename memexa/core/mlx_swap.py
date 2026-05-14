"""mlx_swap — stub for server backend.

The original mlx_swap.py was archived 2026-05-07 (paired_eval archive).
Phase1 pipeline still imports `load_only` and `is_alive` from this module,
but in server backend mode (your-org vLLM) those operations are no-ops:
the vLLM servers are managed externally (watchdog + screen sessions),
not via mlx_lm launchctl swap.

This stub is safe because:
  - Phase 1 server backend mode (MEMEXA_PIPELINE_BACKEND=server) skips
    actual model swap (vLLM stays loaded across stages)
  - load_only(label) just succeeds — caller assumes vLLM already serving
  - is_alive returns True — caller checks via HTTP probe separately
"""
from __future__ import annotations

from typing import Any


def load_only(label: str, **kwargs: Any) -> bool:
    """No-op for server backend; vLLM lifecycle managed externally."""
    return True


def unload(label: str, **kwargs: Any) -> bool:
    """No-op for server backend."""
    return True


def is_alive(label: str = "", **kwargs: Any) -> bool:
    """Server backend always reports alive; phase1 will probe HTTP separately."""
    return True


def list_running(*args: Any, **kwargs: Any) -> list:
    return []


def stop_all(*args: Any, **kwargs: Any) -> bool:
    return True


def unload_all(*args: Any, **kwargs: Any) -> bool:
    """No-op for server backend — phase1 Stage C uses Mac sidecars (BGE-M3),
    not local mlx_lm. There's nothing to unload."""
    return True


def _ssh(*args: Any, **kwargs: Any) -> Any:
    """No-op SSH stub for server backend. Phase1 server-mode does not need
    to bootout/bootstrap Mac mlx servers; vLLM stays loaded on your-org GPUs."""
    return None
