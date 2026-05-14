"""Integration tests for the ``memex`` top-level CLI dispatcher.

Each test invokes :func:`src.cli.main.main` directly with controlled ``argv``;
no subprocesses, no network. ``memex doctor`` and ``memex quick`` paths that
require a backend are exercised separately under ``tests/integration/test_query_with_mock_backend.py``.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from src.cli.main import main

pytestmark = pytest.mark.integration


def test_version_flag_returns_zero(capsys):
    """`memex --version` returns 0 and prints version + interpreter info."""
    rc = main(["--version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "memex" in captured.out
    assert "python" in captured.out


def test_version_subcommand_matches_flag(capsys):
    """`memex version` and `memex --version` produce equivalent output."""
    rc = main(["version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "memex" in captured.out


def test_no_args_prints_help_and_hint(capsys):
    """`memex` with no args prints help + onboarding hint, returns 0."""
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Hint" in captured.out or "usage" in captured.out.lower()


def test_init_creates_config_files(tmp_path: Path, capsys):
    """`memex init --target X` scaffolds aliases.yaml + identity.yaml + .env."""
    target = tmp_path / ".memex"
    rc = main(["init", "--target", str(target)])
    captured = capsys.readouterr()
    assert rc == 0
    assert target.exists()
    assert (target / "aliases.yaml").exists()
    assert (target / "identity.yaml").exists()
    assert (target / ".env").exists()
    assert "Next steps" in captured.out


def test_init_idempotent_without_force(tmp_path: Path, capsys):
    """Second `memex init` against the same dir reports `exists`, doesn't overwrite."""
    target = tmp_path / ".memex"
    main(["init", "--target", str(target)])

    # Modify aliases.yaml to detect overwrite
    sentinel = "# user-edited content"
    (target / "aliases.yaml").write_text(sentinel, encoding="utf-8")

    rc = main(["init", "--target", str(target)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "exists" in captured.out
    # User edits preserved
    assert (target / "aliases.yaml").read_text(encoding="utf-8") == sentinel


def test_init_force_overwrites(tmp_path: Path):
    """`memex init --force` overwrites existing config files."""
    target = tmp_path / ".memex"
    main(["init", "--target", str(target)])
    (target / "aliases.yaml").write_text("# stale", encoding="utf-8")

    rc = main(["init", "--target", str(target), "--force"])
    assert rc == 0
    assert "stale" not in (target / "aliases.yaml").read_text(encoding="utf-8")


def test_config_subcommand_runs(capsys):
    """`memex config` exits 0 and prints sections (env vars, files, paths)."""
    rc = main(["config"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Environment variables" in captured.out
    assert "Config files" in captured.out


def test_unknown_subcommand_returns_argparse_exit():
    """`memex nonsense_subcmd` raises SystemExit(2) via argparse — expected."""
    with pytest.raises(SystemExit) as excinfo:
        main(["nonsense_subcmd"])
    assert excinfo.value.code == 2
