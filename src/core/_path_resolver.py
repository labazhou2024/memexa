"""Workspace path resolver.

All modules that previously hard-coded a Windows username path
(``C:\\Users\\<name>\\OneDrive\\...\\claude workspace\\...``) MUST go through
this module instead, so the same code runs on any user's machine
without modification.

Resolution order:

1. ``MEMEXA_WORKSPACE_ROOT`` environment variable -- absolute path,
   ``Path.is_dir()`` must be True.
2. ``~/.memexa/config.yaml`` -- key ``workspace_root``.
3. Fallback: ``~/.claude/projects/`` -- the default Claude Code projects
   directory.

The first hit wins; later resolution paths are not consulted.

The resolver also exposes :func:`memory_dir`, :func:`audit_corpus_path`,
:func:`harness_tasks_dir` and other commonly-referenced subpaths so callers
never spell them out themselves.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional


ENV_VAR = "MEMEXA_WORKSPACE_ROOT"
CONFIG_REL = ".memexa/config.yaml"
DEFAULT_PROJECTS = ".claude/projects"


def _from_env() -> Optional[Path]:
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.is_dir() else None


def _from_config() -> Optional[Path]:
    cfg = Path.home() / CONFIG_REL
    if not cfg.is_file():
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    raw = data.get("workspace_root", "").strip() if isinstance(data, dict) else ""
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.is_dir() else None


def _default() -> Path:
    return Path.home() / DEFAULT_PROJECTS


@lru_cache(maxsize=1)
def workspace_root() -> Path:
    """Return the workspace root directory.

    Caches the answer so repeated calls are free.  Set
    ``MEMEXA_WORKSPACE_ROOT`` to override at runtime.
    """
    for resolver in (_from_env, _from_config):
        candidate = resolver()
        if candidate is not None:
            return candidate
    return _default()


def memory_dir() -> Path:
    return workspace_root() / "memory"


def audit_corpus_path() -> Path:
    return workspace_root() / ".claude" / "data" / "audit_corpus.jsonl"


def harness_tasks_dir() -> Path:
    return workspace_root() / ".claude" / "harness" / "tasks"


def data_dir() -> Path:
    return workspace_root() / "data"


def logs_dir() -> Path:
    p = workspace_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def reset_cache() -> None:
    """Clear the lru_cache.  Useful when tests want to change the env var."""
    workspace_root.cache_clear()


# Convenience: allow `python -m core._path_resolver` to debug-print
if __name__ == "__main__":
    print(f"workspace_root() -> {workspace_root()}")
    print(f"memory_dir()     -> {memory_dir()}")
    print(f"data_dir()       -> {data_dir()}")
    print(f"resolver source  -> env={_from_env()!r} config={_from_config()!r} default={_default()!r}")
    sys.exit(0)
