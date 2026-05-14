"""User identity helpers — QQ id, email, etc.

Centralises every per-user identifier so callers never embed literal
account numbers in source code.

All getters fall through ``env -> config -> None``.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ._path_resolver import workspace_root


CONFIG_REL = ".memexa/identity.yaml"


def _load_config() -> dict:
    cfg = Path.home() / CONFIG_REL
    if not cfg.is_file():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def qq_id() -> Optional[str]:
    """Return the QQ numeric id for this user.

    Resolution: ``MEMEXA_QQ_ID`` env, then ``identity.yaml:qq_id``,
    then ``None`` if neither is set.
    """
    raw = os.environ.get("MEMEXA_QQ_ID", "").strip()
    if raw:
        return raw
    val = _load_config().get("qq_id")
    return str(val).strip() if val else None


@lru_cache(maxsize=1)
def primary_email() -> Optional[str]:
    raw = os.environ.get("MEMEXA_PRIMARY_EMAIL", "").strip()
    if raw:
        return raw
    val = _load_config().get("primary_email")
    return str(val).strip() if val else None


def qq_db_path() -> Optional[Path]:
    """Standard QQ desktop client DB path -- ``Documents/Tencent Files/<qq>/nt_qq/nt_db/nt_msg.db``.

    Returns ``None`` if ``qq_id()`` is unset.
    """
    qid = qq_id()
    if not qid:
        return None
    return Path.home() / "Documents" / "Tencent Files" / qid / "nt_qq" / "nt_db" / "nt_msg.db"


def reset_cache() -> None:
    qq_id.cache_clear()
    primary_email.cache_clear()


if __name__ == "__main__":
    print(f"qq_id()        -> {qq_id()!r}")
    print(f"primary_email -> {primary_email()!r}")
    print(f"qq_db_path()  -> {qq_db_path()!r}")
