"""U7 TU-3: consent_gate — default ON for ALL contacts (CEO directive 全部入图).

Behavior:
  - Default: ALL contacts (1on1 + 群聊) → consent='*_default_on'
  - Manual blocklist: `memexa/data/chat_consent_blocklist.json` lists wxids
    explicitly opted-out by CEO; those become consent='manual_blocked' (drop)
  - Missing blocklist file → empty list (all default ON)
  - Group chats: detected by chat_name ending with '@chatroom'

axis_anchor: [C:schema:consent_default_on]
trace event: chat_consent_recorded
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Set

ConsentDecision = Literal["1on1_default_on", "group_default_on", "manual_blocked"]

_DEFAULT_BLOCKLIST_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "chat_consent_blocklist.json"
)


@dataclass
class ConsentEnvelope:
    """Result of evaluate(); applied to msg envelope."""
    msg: Optional[dict]
    consent: ConsentDecision
    is_group: bool
    chat_room_hash: str  # sha256(chat_name)[:16]


def _load_blocklist(path: Optional[Path] = None) -> Set[str]:
    """Read blocklist json; return set of blocked wxids/chat_names. Missing = empty."""
    p = path or _DEFAULT_BLOCKLIST_PATH
    if not p.exists():
        return set()
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(d, list):
            return {str(x) for x in d}
        if isinstance(d, dict):
            return {str(k) for k in d.get("blocked", [])}
    except (json.JSONDecodeError, OSError):
        return set()
    return set()


def _hash_chat_room(chat_name: str) -> str:
    return hashlib.sha256(str(chat_name).encode("utf-8", errors="replace")).hexdigest()[:16]


def evaluate(msg: dict, blocklist_path: Optional[Path] = None) -> ConsentEnvelope:
    """Evaluate consent for a chat msg.

    msg expected keys: chat_name (str; @chatroom suffix indicates group),
    sender (wxid str). Manual blocklist may list either chat_name or sender wxid.
    """
    chat_name = str(msg.get("chat_name", "") or "")
    sender = str(msg.get("sender", "") or "")
    is_group = chat_name.endswith("@chatroom")
    blocklist = _load_blocklist(blocklist_path)

    # Manual block: chat_name OR sender wxid in blocklist
    if chat_name in blocklist or sender in blocklist:
        env = ConsentEnvelope(
            msg=None, consent="manual_blocked", is_group=is_group,
            chat_room_hash=_hash_chat_room(chat_name),
        )
        _emit_trace(env)
        return env

    consent: ConsentDecision = "group_default_on" if is_group else "1on1_default_on"
    env = ConsentEnvelope(
        msg=msg, consent=consent, is_group=is_group,
        chat_room_hash=_hash_chat_room(chat_name),
    )
    _emit_trace(env)
    return env


def _emit_trace(env: ConsentEnvelope) -> None:
    try:
        from memexa.core.trace_sink import emit  # type: ignore
        emit("chat_consent_recorded", {
            "consent": env.consent,
            "is_group": env.is_group,
            "chat_room_hash": env.chat_room_hash,
        })
    except Exception:
        pass
