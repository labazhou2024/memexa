"""Integration tests for the open-demo ``memexa`` CLI dispatcher.

Each test invokes :func:`memexa.cli.main.main` directly with controlled
``argv``; no subprocesses, no network. The open demo ships only the
``version`` and ``demo`` subcommands — the full engine's commands
(init / ingest / query / backend / doctor) are part of the proprietary
memexa product and are intentionally absent from this package.
"""
from __future__ import annotations

import pytest

from memexa.cli.main import main

pytestmark = pytest.mark.integration


def test_version_flag_returns_zero(capsys):
    """`memexa --version` returns 0 and prints version + interpreter info."""
    rc = main(["--version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "memexa" in captured.out
    assert "python" in captured.out


def test_version_subcommand_matches_flag(capsys):
    """`memexa version` produces equivalent output to the flag."""
    rc = main(["version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "memexa" in captured.out


def test_no_args_prints_help(capsys):
    """`memexa` with no args prints help (usage + subcommands), returns 0."""
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "usage" in captured.out.lower()
    assert "demo" in captured.out


def test_demo_subcommand_runs(capsys):
    """The demo subcommand is the heart of the open package; it must run
    with no backend and no LLM key."""
    rc = main(["demo"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Ingested" in captured.out


def test_unknown_subcommand_returns_argparse_exit():
    """`memexa nonsense_subcmd` raises SystemExit(2) via argparse — expected."""
    with pytest.raises(SystemExit) as excinfo:
        main(["nonsense_subcmd"])
    assert excinfo.value.code == 2
