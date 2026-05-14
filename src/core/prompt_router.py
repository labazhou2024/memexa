"""Extraction-prompt router — two-mode dispatch (bundled / byo).

Honors the ``MEMEXA_EXTRACTOR_TIER`` env var:

============  ====================================================
Mode value    Behavior
============  ====================================================
``bundled``   Use the OSS prompts shipped in this repo (default).
              Includes V2 envelope schema, key constraints, and
              worked few-shot examples. Sufficient for demo and
              normal use.
``byo``       Load the user's own prompt file from
              ``MEMEXA_PROMPT_PATH``. The module must expose
              ``PASS2_SYSTEM_PROMPT_BY_SOURCE`` and / or
              ``PASS1_SYSTEM_PROMPT``. Use this when you have
              your own prompt-tuning pipeline.
============  ====================================================

A third option — calling a hosted extraction API — is on the
roadmap (see ROADMAP.md, v0.5 "optional paid API endpoint").
That code path is not part of v0.1.0 and is therefore not
wired up here yet.

Public surface
--------------

.. code-block:: python

    from src.core.prompt_router import get_extraction_prompt

    # Returns ``str`` (the BYO prompt body) or ``None`` if the
    # caller should fall back to the bundled stub.
    prompt = get_extraction_prompt("pass2", source="wechat")
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

ENV_TIER = "MEMEXA_EXTRACTOR_TIER"
ENV_PROMPT_PATH = "MEMEXA_PROMPT_PATH"

_DEFAULT_TIER = "bundled"
# 'basic' kept as a back-compat alias for the old 0.1.0a tier name.
_VALID_TIERS = {"bundled", "basic", "byo"}
_VALID_STAGES = {"pass1", "pass2"}


def _resolve_tier() -> str:
    raw = (os.environ.get(ENV_TIER, "") or _DEFAULT_TIER).strip().lower()
    if raw not in _VALID_TIERS:
        sys.stderr.write(
            f"[prompt_router] unknown {ENV_TIER}={raw!r}; falling back to 'bundled'\n"
        )
        return "bundled"
    # back-compat: 'basic' is the old name for 'bundled'
    if raw == "basic":
        return "bundled"
    return raw


@lru_cache(maxsize=1)
def _load_byo_module():
    """Import the user's BYO prompt module from MEMEXA_PROMPT_PATH."""
    raw = os.environ.get(ENV_PROMPT_PATH, "").strip()
    if not raw:
        raise RuntimeError(
            f"{ENV_TIER}=byo requires {ENV_PROMPT_PATH} to point at a Python file"
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RuntimeError(f"{ENV_PROMPT_PATH} file not found: {path}")
    spec = importlib.util.spec_from_file_location("memexa_byo_prompts", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load BYO prompt module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _byo_prompt(stage: str, source: Optional[str]) -> Optional[str]:
    mod = _load_byo_module()
    if stage == "pass2":
        by_source = getattr(mod, "PASS2_SYSTEM_PROMPT_BY_SOURCE", None)
        if isinstance(by_source, dict) and source in by_source:
            return str(by_source[source])
        generic = getattr(mod, "PASS2_SYSTEM_PROMPT", None)
        return str(generic) if generic else None
    if stage == "pass1":
        generic = getattr(mod, "PASS1_SYSTEM_PROMPT", None)
        return str(generic) if generic else None
    return None


def get_extraction_prompt(
    stage: str,
    source: Optional[str] = None,
) -> Optional[str]:
    """Return the prompt body for ``stage`` (and optionally ``source``).

    Returns ``None`` when the caller should fall back to its bundled
    prompt. The caller is therefore safe to write::

        prompt = get_extraction_prompt("pass2", source="wechat") \\
                 or _BUNDLED_PROMPT
    """
    if stage not in _VALID_STAGES:
        raise ValueError(f"unknown stage {stage!r}; valid: {_VALID_STAGES}")

    tier = _resolve_tier()
    if tier == "bundled":
        return None
    if tier == "byo":
        return _byo_prompt(stage, source)
    return None


def active_tier() -> str:
    """Public accessor for the current tier — used by `memexa config`."""
    return _resolve_tier()


__all__ = [
    "ENV_TIER",
    "ENV_PROMPT_PATH",
    "get_extraction_prompt",
    "active_tier",
]
