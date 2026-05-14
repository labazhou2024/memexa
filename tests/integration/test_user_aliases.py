"""Integration tests for :mod:`src.core._user_aliases`.

Verifies alias loading from env path / default config / defaults, plus the
``is_self`` predicate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core import _user_aliases

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_cache():
    _user_aliases.reset_cache()
    yield
    _user_aliases.reset_cache()


def test_default_when_no_config(monkeypatch):
    """With no env var and no ``~/.memex/aliases.yaml``, defaults apply."""
    monkeypatch.delenv("MEMEX_ALIASES_FILE", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/nonexistent_home_for_test")))
    cfg = _user_aliases.load()
    assert "self" in cfg.self_aliases
    assert "me" in cfg.self_aliases
    assert cfg.timezone == "UTC"


def test_env_var_points_at_explicit_yaml(tmp_path: Path, monkeypatch):
    """``MEMEX_ALIASES_FILE`` overrides the default ``~/.memex/aliases.yaml`` location."""
    yaml = tmp_path / "custom_aliases.yaml"
    yaml.write_text(
        "self_aliases:\n  - alice\n  - me\nself_roles:\n  - student\ntimezone: Asia/Shanghai\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMEX_ALIASES_FILE", str(yaml))
    cfg = _user_aliases.load()
    assert cfg.self_aliases == ["alice", "me"]
    assert cfg.self_roles == ["student"]
    assert cfg.timezone == "Asia/Shanghai"


def test_is_self_case_insensitive(tmp_path: Path, monkeypatch):
    yaml = tmp_path / "aliases.yaml"
    yaml.write_text("self_aliases:\n  - Alice\n  - bob\n", encoding="utf-8")
    monkeypatch.setenv("MEMEX_ALIASES_FILE", str(yaml))
    assert _user_aliases.is_self("alice")
    assert _user_aliases.is_self("BOB")
    assert not _user_aliases.is_self("charlie")


def test_is_self_handles_none():
    assert not _user_aliases.is_self(None)  # type: ignore[arg-type]


def test_malformed_yaml_falls_back_to_default(tmp_path: Path, monkeypatch):
    """A corrupt yaml file should not crash; defaults apply."""
    yaml = tmp_path / "bad.yaml"
    yaml.write_text("self_aliases:\n  - [unclosed list", encoding="utf-8")
    monkeypatch.setenv("MEMEX_ALIASES_FILE", str(yaml))
    cfg = _user_aliases.load()
    # Defaults kick in
    assert "self" in cfg.self_aliases
