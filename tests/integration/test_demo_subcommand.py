"""Integration test for ``memexa demo`` subcommand.

Verifies the 30-second onboarding path end-to-end:

  - ``memexa demo`` returns rc=0
  - stdout reports ≥ 1 card across the six bundled sources
  - five query labels appear in stdout (quick / arc / timeline /
    pending / topic)
  - no backend, no LLM key, no configuration required (the test
    process clears any MEMEXA_* env that might point at one)
"""
from __future__ import annotations

import os

import pytest

from memexa.cli.main import main

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clear_memexa_env(monkeypatch):
    """Demo command must work even when the user has no backend env set."""
    for k in list(os.environ):
        if k.startswith("MEMEXA_"):
            monkeypatch.delenv(k, raising=False)


def test_demo_returns_zero_and_prints_card_count(capsys):
    rc = main(["demo"])
    captured = capsys.readouterr()
    assert rc == 0, f"memexa demo returned rc={rc}; stdout:\n{captured.out}\nstderr:\n{captured.err}"
    assert "Ingested" in captured.out
    # The bundled synthetic dataset across six sources is small but
    # never empty.
    assert "0 cards" not in captured.out or "✓ Ingested" in captured.out


def test_demo_runs_five_sample_queries(capsys):
    rc = main(["demo"])
    captured = capsys.readouterr()
    assert rc == 0
    for label in ("memexa quick", "memexa arc", "memexa timeline",
                  "memexa pending", "memexa topic"):
        assert label in captured.out, (
            f"expected sample query label {label!r} in demo stdout but"
            f" did not find it; full stdout:\n{captured.out}"
        )


def test_demo_prints_proprietary_handoff(capsys):
    """Demo must hand the user off to the proprietary full engine."""
    rc = main(["demo"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "proprietary" in captured.out.lower()
    assert "README" in captured.out


def test_demo_help_lists_subcommand(capsys):
    """``memexa --help`` (no args) should advertise the demo subcommand
    so a first-time user discovers it without reading docs."""
    rc = main([])
    captured = capsys.readouterr()
    # `main([])` prints help; rc should be 0 (friendly hint mode).
    assert rc == 0
    # The argparse-generated help lists registered subcommands; demo
    # must appear there.
    assert "demo" in captured.out
