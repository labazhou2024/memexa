"""
Shared workspace path resolution for all gate modules.

Problem: Path(__file__).parent.parent.parent.parent.resolve() fails on Windows
with CJK characters in path (e.g., non-ASCII directory names) due to encoding
mismatch. relative_to() throws ValueError, caught by bare except, silently
returns allow.

Solution: Multi-strategy resolution with marker file verification.

Used by: pretool_gate.py, session_gate.py, session_start_gate.py

Resolution order:
  1. ``os.getcwd()`` — Claude Code sets cwd to workspace root on hook invocation.
  2. Walk up from ``__file__`` using string ops (CJK-safe).
  3. Defer to :func:`src.core._path_resolver.workspace_root` (env / config / default).
"""

import os
from pathlib import Path
from src.core._path_resolver import workspace_root as _resolver_workspace_root

_MARKER = Path(".claude") / "config" / "settings.json"


def find_workspace() -> Path:
    """Find the workspace root directory robustly on Windows with CJK paths."""
    # Strategy 1: cwd (most reliable -- Claude Code sets cwd to workspace root)
    try:
        cwd = Path(os.getcwd())
        if (cwd / _MARKER).exists():
            return cwd
    except (OSError, ValueError):
        pass

    # Strategy 2: walk up from this file's directory
    try:
        here = Path(__file__).parent  # src/core/
        candidate = here.parent.parent  # src/core -> src -> repo root
        if (candidate / _MARKER).exists():
            return candidate
    except (OSError, ValueError):
        pass

    # Strategy 3: defer to _path_resolver (env / config / default)
    try:
        return _resolver_workspace_root()
    except Exception:
        return Path(os.getcwd())


def find_jarvis_root() -> Path:
    """Find the memex project root."""
    return find_workspace() / "memex"


def find_data_dir() -> Path:
    """Find the memex data directory."""
    return find_jarvis_root() / "memex" / "data"


# Pre-computed for import convenience
WORKSPACE = find_workspace()
MEMEX_ROOT = find_jarvis_root()
DATA_DIR = find_data_dir()
