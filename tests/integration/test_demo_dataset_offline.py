"""Integration test: bundled demo dataset is well-formed.

Does NOT require a running backend — exercises only the stub extractor
path of :mod:`examples.demo_dataset.ingest`. Verifies:

  - all 6 source files are valid JSON / JSONL
  - the stub extractor produces ``≥ 1`` card per source
  - the resulting envelopes carry the required keys
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo_dataset"


@pytest.fixture(scope="module")
def ingest_module():
    return importlib.import_module("examples.demo_dataset.ingest")


def test_demo_data_files_present():
    expected = {
        "wechat_demo.json",
        "qq_demo.json",
        "email_demo.json",
        "browser_demo.json",
        "claude_demo.jsonl",
        "audio_demo_transcript.json",
    }
    actual = {p.name for p in DEMO.iterdir() if p.is_file()}
    missing = expected - actual
    assert not missing, f"missing demo data: {missing}"


def test_demo_json_files_parse():
    for name in ("wechat_demo.json", "qq_demo.json", "email_demo.json",
                 "browser_demo.json", "audio_demo_transcript.json"):
        with (DEMO / name).open(encoding="utf-8") as f:
            json.load(f)


def test_demo_jsonl_lines_parse():
    with (DEMO / "claude_demo.jsonl").open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                json.loads(line)


def test_stub_envelope_has_required_keys(ingest_module):
    env = ingest_module._stub_envelope(
        source="wechat",
        when_iso="2024-01-01T00:00:00Z",
        narrative="sample narrative",
        entities=["Alice"],
    )
    for key in ("source", "when_start", "salience", "narrative", "entities", "evidence_quotes"):
        assert key in env


def test_stub_ingest_produces_cards(ingest_module, capsys):
    """End-to-end stub run: should print 26 cards across 6 sources.

    The open demo is always offline: it builds cards and never attempts
    to POST to a backend.
    """
    sys_mod = importlib.import_module("sys")

    saved_argv = sys_mod.argv
    sys_mod.argv = ["ingest", "--dry-run"]
    try:
        rc = ingest_module.main()  # type: ignore[attr-defined]
    finally:
        sys_mod.argv = saved_argv

    captured = capsys.readouterr()
    assert rc == 0
    assert "total = 26 cards across 6 sources" in captured.out
    assert "offline mode; not POSTing anywhere" in captured.out
