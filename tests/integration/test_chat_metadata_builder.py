"""Integration tests for :mod:`memexa.chat.metadata_builder`.

Validates the single-source ``HASH_LEN=32`` invariant and the public
helpers used by all chat-derived ingestion paths.
"""
from __future__ import annotations

import pytest

from memexa.chat.metadata_builder import (
    HASH_LEN,
    EXTRACTED_BY_CANONICAL,
    chat_room_hash,
)

pytestmark = pytest.mark.integration


def test_hash_len_is_32_chars():
    """HASH_LEN is the canonical 128-bit preimage floor (32 hex chars)."""
    assert HASH_LEN == 32


def test_chat_room_hash_returns_32_hex():
    h = chat_room_hash("家庭群")
    assert len(h) == HASH_LEN == 32
    assert all(c in "0123456789abcdef" for c in h)


def test_chat_room_hash_is_deterministic():
    a = chat_room_hash("Test Room")
    b = chat_room_hash("Test Room")
    assert a == b


def test_chat_room_hash_handles_none_and_non_str():
    """Defensive coercion of non-string inputs to a stable hash."""
    h_none = chat_room_hash(None)  # type: ignore[arg-type]
    h_empty = chat_room_hash("")
    h_int = chat_room_hash(12345)  # type: ignore[arg-type]
    for h in (h_none, h_empty, h_int):
        assert isinstance(h, str)
        assert len(h) == 32


def test_extracted_by_canonical_is_frozen_and_complete():
    """Canonical extraction sources are immutable and include the expected set."""
    assert isinstance(EXTRACTED_BY_CANONICAL, frozenset)
    assert "chat-realtime" in EXTRACTED_BY_CANONICAL
    assert "memory_full" in EXTRACTED_BY_CANONICAL
    # Frozen set cannot be mutated
    with pytest.raises(AttributeError):
        EXTRACTED_BY_CANONICAL.add("rogue")  # type: ignore[attr-defined]
