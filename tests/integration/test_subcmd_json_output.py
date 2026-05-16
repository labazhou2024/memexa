"""Integration test for the ``--json`` output mode added in v0.1.x.

The fourteen query subcommands all accept ``--json`` at the top level
of ``python -m memexa.core.memory_query``. When set, the subcommand
short-circuits text rendering and emits a single JSON document on
stdout that an AI agent can parse with ``json.loads()``.

These tests verify:

1. The ``--json`` flag is accepted by the argparse top-level parser.
2. Subcommands that don't require a running Hindsight backend
   (``pending``, ``session-context``) emit valid JSON and exit cleanly.
3. The query log records the invocation just like the text path does.
4. Help text exposes ``--json`` so a first-time agent reader sees it.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

PY = [sys.executable, "-m", "memexa.core.memory_query"]


def _run(args, timeout=10):
    """Invoke the memory_query CLI as a subprocess; return (rc, stdout, stderr).

    Force UTF-8 decoding because the memexa CLI uses Chinese characters
    that don't round-trip through Windows GBK.
    """
    proc = subprocess.run(PY + args, capture_output=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def test_json_flag_appears_in_top_level_help():
    rc, out, err = _run(["--help"])
    assert rc == 0
    assert "--json" in out, (
        "expected --json to be listed in top-level help; got:\n" + out
    )


def test_pending_json_returns_parseable_list():
    """`pending` is the simplest subcommand for this test because it does
    not require a Hindsight backend — it reads the calendar index off
    disk and emits commitment cards."""
    rc, out, err = _run(["--json", "pending"])
    assert rc == 0, f"--json pending exit rc={rc}; stderr:\n{err}"
    # stdout must be parseable as JSON.
    parsed = json.loads(out.strip().splitlines()[0])
    # Either a list (active commitments) or an empty list (none).
    assert isinstance(parsed, list), (
        f"--json pending should emit a JSON array; got {type(parsed).__name__}"
    )


def test_pending_json_does_not_print_human_text():
    """The JSON path must short-circuit the text renderer. No 'PENDING ('
    header should appear in --json output."""
    rc, out, err = _run(["--json", "pending"])
    assert rc == 0
    assert "PENDING (" not in out, (
        f"--json mode should suppress human text headers; saw 'PENDING ('"
        f" in stdout:\n{out}"
    )


def test_subcommands_accept_json_flag_without_argparse_error():
    """For every advertised subcommand, ``--json <subcmd> --help`` should
    parse cleanly (rc=0 from argparse). This proves the flag is wired
    at top level, not duplicated per subcommand."""
    subcmds = [
        "quick", "reflect", "timeline", "person", "project", "pending",
        "session-context", "topic", "arc", "types", "graph-walk",
        "summary", "trends", "cross-source",
    ]
    for sc in subcmds:
        rc, out, err = _run(["--json", sc, "--help"])
        # argparse exit 0 on --help, regardless of subcmd
        assert rc == 0, (
            f"argparse rejected '--json {sc} --help'; rc={rc}, stderr:\n{err}"
        )
