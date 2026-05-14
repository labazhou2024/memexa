"""TU-1 of Closure A plan_v3 — single-source metadata helper for chat-derived facts.

Per consistency-iter3-2 + RP-7 + RP-24 + RP-27: single NAMED CONSTANT
`HASH_LEN = 32` consumed by both producer (this module) and consumer
(wechat_batch_cursor + wechat_batch_dedup) — eliminates the [:16] / [:32]
silent-split bug.

Per security-iter1-3 + RP-5 + AC-U10-15: 128-bit preimage resistance
floor. Adversary controlling chat content can't preimage 32 hex chars.

Per security-iter1-7 + RP-14: `consent` is a HARDCODED MODULE CONSTANT,
NEVER read from `msg` or `envelope`. Adversary-controlled WeChat content
cannot inject arbitrary consent metadata.

Per consistency-iter3-10 + RP-32 + AC-U10-29: `EXTRACTED_BY_CANONICAL`
declares the canonical extracted_by enum domain. Downstream filters
assert membership.

axis_anchor: [C:cli:chat_metadata_builder]
trace event: chat_metadata_built
"""
from __future__ import annotations

import hashlib
from typing import Any, Final

# Per RP-7: single NAMED CONSTANT for hash truncation length.
# 32 hex chars = 128 bits = preimage-resistance floor (per RP-5).
HASH_LEN: Final[int] = 32

# Per RP-32: canonical Enum-style domain for `extracted_by` field.
# Downstream consumers (decay sweep, query filter, dedup) MUST assert
# the value is a member of this set before mutating.
EXTRACTED_BY_CANONICAL: Final[frozenset[str]] = frozenset({
    "chat-realtime",
    "memory_full",
    "self-evolution",
    "code-review",
    "manual",
})

# Per RP-14: consent value is module-level constant, NEVER per-message.
# All chat-derived facts under WeChat adopter ownership = same consent.
CONSENT_CONSTANT: Final[str] = "user_owner_local_device"


def chat_room_hash(chat_name: str) -> str:
    """Compute PII-free per-chat-room hash. Always sha256(name)[:HASH_LEN]."""
    if not isinstance(chat_name, str):
        chat_name = str(chat_name or "")
    return hashlib.sha256(chat_name.encode("utf-8", errors="replace")).hexdigest()[:HASH_LEN]


def episode_id(ts: str, sender: str, content: str) -> str:
    """Compute per-message stable episode id. Always sha256(...)[:HASH_LEN]."""
    raw = f"{ts}|{sender}|{content}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:HASH_LEN]


def _build_chat_metadata(
    msg: dict,
    envelope: Any,
    confidence: float = 0.7,
) -> dict[str, Any]:
    """Construct the 9-field chat-realtime FactRow metadata.

    Per AC-U9-1..4 + AC-U10-15..29: every chat-derived FactRow MUST have
    these exact fields, no more, no less. `extracted_by` is asserted to
    be a member of EXTRACTED_BY_CANONICAL.

    Args:
        msg: dict with keys `chat_name`, `sender`, `content`, `timestamp`,
            `msg_type` (best-effort).
        envelope: consent envelope object (has `.consent` attribute) — only
            used to validate that an envelope exists; the actual `consent`
            value written is the module constant CONSENT_CONSTANT (per
            security-iter1-7 + RP-14: never adversary-controlled).
        confidence: extractor confidence 0.0-1.0; <0.6 → is_tentative=True.

    Returns:
        dict with 10 fields: extracted_by, valid_at, invalid_at, consent,
            chat_room_hash, episode_id, is_tentative, is_structural,
            msg_type, confidence.
    """
    chat_name = str(msg.get("chat_name", "") or "")
    sender = str(msg.get("sender", "") or "")
    content = str(msg.get("content", "") or "")
    ts = str(msg.get("timestamp", "") or "")

    # Defensive validation: extracted_by MUST be canonical (we set it ourselves
    # so this is a self-check; future readers will assert per AC-U10-29).
    extracted_by = "chat-realtime"
    assert extracted_by in EXTRACTED_BY_CANONICAL, (
        f"extracted_by={extracted_by!r} not in canonical set"
    )

    # Envelope existence check (we don't trust its `consent` attr; we use
    # CONSENT_CONSTANT). Envelope MUST exist (consent_gate guarantees this).
    if envelope is None:
        raise ValueError("consent envelope is None — TU-7 consent_gate must run first")

    # CEO 2026-05-04 directive: human-readable display names + is_group_chat
    # for queryable per-chat-room/per-sender attribution.
    chat_display = str(msg.get("chat_display_name") or chat_name)
    sender_display = str(msg.get("sender_display_name") or sender)
    is_group_chat = bool(msg.get("is_group_chat", False))
    if is_group_chat:
        receiver_display = ""
    else:
        receiver_display = chat_display if sender != chat_name else "我"
    return {
        "extracted_by": extracted_by,
        "valid_at": ts,
        "invalid_at": None,
        "consent": CONSENT_CONSTANT,
        "chat_room_hash": chat_room_hash(chat_name),
        "chat_room_display_name": chat_display,
        "sender_wxid_hash": chat_room_hash(sender),  # reuse hash function
        "sender_display_name": sender_display,
        "receiver_display_name": receiver_display,
        "is_group_chat": is_group_chat,
        "episode_id": episode_id(ts, sender, content),
        "is_tentative": confidence < 0.6,
        "is_structural": False,
        "msg_type": int(msg.get("msg_type", 1)),
        "confidence": confidence,
    }
