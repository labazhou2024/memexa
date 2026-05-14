"""Integration tests for :mod:`memexa.core._path_resolver`.

Verifies the env → config → default resolution order and that cached
results can be reset between tests.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from memexa.core import _path_resolver

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the lru_cache before/after each test."""
    _path_resolver.reset_cache()
    yield
    _path_resolver.reset_cache()


def test_env_var_overrides_default(tmp_path: Path, monkeypatch):
    """``MEMEXA_WORKSPACE_ROOT`` set to an existing dir wins over ``~/.claude/projects``."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("MEMEXA_WORKSPACE_ROOT", str(workspace))
    assert _path_resolver.workspace_root() == workspace.resolve()


def test_env_var_to_nonexistent_path_falls_through(tmp_path: Path, monkeypatch):
    """A ``MEMEXA_WORKSPACE_ROOT`` pointing at a missing dir is ignored."""
    monkeypatch.setenv("MEMEXA_WORKSPACE_ROOT", str(tmp_path / "nope_does_not_exist"))
    result = _path_resolver.workspace_root()
    assert result != tmp_path / "nope_does_not_exist"


def test_memory_dir_relative_to_workspace_root(tmp_path: Path, monkeypatch):
    """``memory_dir()`` is always ``workspace_root() / memory``."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("MEMEXA_WORKSPACE_ROOT", str(workspace))
    assert _path_resolver.memory_dir() == workspace.resolve() / "memory"


def test_data_dir_relative_to_workspace_root(tmp_path: Path, monkeypatch):
    """``data_dir()`` is always ``workspace_root() / data``."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("MEMEXA_WORKSPACE_ROOT", str(workspace))
    assert _path_resolver.data_dir() == workspace.resolve() / "data"


def test_logs_dir_is_created_on_access(tmp_path: Path, monkeypatch):
    """``logs_dir()`` ensures the directory exists (mkdir -p)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("MEMEXA_WORKSPACE_ROOT", str(workspace))
    logs = _path_resolver.logs_dir()
    assert logs.exists() and logs.is_dir()
