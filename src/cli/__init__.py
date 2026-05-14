"""memex top-level CLI entry point.

Exposes :func:`main` which dispatches to subcommands. Installed via
``pyproject.toml`` ``[project.scripts]`` as ``memex`` and ``memex-query``.
"""

from src.cli.main import main

__all__ = ["main"]
