"""memexa top-level CLI entry point.

Exposes :func:`main` which dispatches to subcommands. Installed via
``pyproject.toml`` ``[project.scripts]`` as ``memexa`` and ``memexa-query``.
"""

from memexa.cli.main import main

__all__ = ["main"]
