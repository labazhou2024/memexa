"""Integration tests for :mod:`src.core._user_identity`.

Verifies env-first resolution + yaml fallback + None-on-missing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core import _user_identity

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_cache():
    _user_identity.reset_cache()
    yield
    _user_identity.reset_cache()


def test_qq_id_from_env(monkeypatch):
    monkeypatch.setenv("MEMEXA_QQ_ID", "12345678")
    assert _user_identity.qq_id() == "12345678"


def test_qq_id_none_when_unset(monkeypatch):
    monkeypatch.delenv("MEMEXA_QQ_ID", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/nonexistent_home_for_test")))
    # No env, no yaml -> None
    assert _user_identity.qq_id() is None


def test_primary_email_from_env(monkeypatch):
    monkeypatch.setenv("MEMEXA_PRIMARY_EMAIL", "demo@example.com")
    assert _user_identity.primary_email() == "demo@example.com"


def test_qq_db_path_returns_none_when_qq_id_unset(monkeypatch):
    monkeypatch.delenv("MEMEXA_QQ_ID", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/nonexistent_home_for_test")))
    assert _user_identity.qq_db_path() is None


def test_qq_db_path_constructed_when_qq_id_set(monkeypatch):
    monkeypatch.setenv("MEMEXA_QQ_ID", "987654321")
    p = _user_identity.qq_db_path()
    assert p is not None
    assert "Tencent Files" in str(p)
    assert "987654321" in str(p)
    assert p.name == "nt_msg.db"
