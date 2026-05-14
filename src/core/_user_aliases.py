"""User alias / self-match logic.

Replaces hard-coded ``("self", "myself", "<real name>", ...)`` lists
scattered across calendar_daemon / entity_kind / canonicalizer.

Configuration source order:

1. ``MEMEX_ALIASES_FILE`` environment variable pointing to a YAML file.
2. ``~/.memex/aliases.yaml``.
3. Built-in safe defaults (English ``self / me / myself`` only, no real names).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional


ENV_VAR = "MEMEX_ALIASES_FILE"
DEFAULT_CONFIG = ".memex/aliases.yaml"


@dataclass(frozen=True)
class AliasConfig:
    """Resolved alias configuration.

    Attributes
    ----------
    self_aliases :
        Every string that should match "the user themselves" when speaking
        in chat / email / voice.  Used in speaker_role classification.
    self_roles :
        Roles the user plays in the social graph (student / employee / etc.)
        Used to bucket events where the user is the implicit subject.
    timezone :
        IANA timezone string.  Used by audio session classifier and
        cron orchestrator for local-time decisions.
    """
    self_aliases: List[str] = field(default_factory=lambda: ["self", "me", "myself"])
    self_roles: List[str] = field(default_factory=list)
    timezone: str = "UTC"


def _load_yaml(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None


def _config_path() -> Path:
    raw = os.environ.get(ENV_VAR, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / DEFAULT_CONFIG


@lru_cache(maxsize=1)
def load() -> AliasConfig:
    """Return the resolved AliasConfig (cached)."""
    data = _load_yaml(_config_path())
    if not data:
        return AliasConfig()
    self_aliases = list(data.get("self_aliases") or AliasConfig().self_aliases)
    self_roles = list(data.get("self_roles") or [])
    timezone = str(data.get("timezone") or "UTC")
    return AliasConfig(
        self_aliases=self_aliases,
        self_roles=self_roles,
        timezone=timezone,
    )


def self_aliases() -> List[str]:
    return load().self_aliases


def is_self(text: str) -> bool:
    """Return True if ``text`` is one of the user's self-aliases (case-insensitive)."""
    if text is None:
        return False
    needle = text.strip().lower()
    return any(needle == a.strip().lower() for a in self_aliases())


def reset_cache() -> None:
    load.cache_clear()


if __name__ == "__main__":
    cfg = load()
    print(f"self_aliases = {cfg.self_aliases}")
    print(f"self_roles   = {cfg.self_roles}")
    print(f"timezone     = {cfg.timezone}")
