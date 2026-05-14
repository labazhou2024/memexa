"""cross_check_helper.py — TU-7 Closure B (2026-05-01).

Tier-based comparison helper for reviewer cross-checking of user-stated rules.

Tier weights:
  HARD_RULE_BLOCKING: 3
  HARD_RULE_WARN:     2
  chat_forbids:       1
  chat_dislikes:      0

Usage:
    from src.core.cross_check_helper import tier_compare, TIER_WEIGHTS

    result = tier_compare(tier_a, tier_b)
    # returns positive if a > b, negative if a < b, 0 if equal
"""
from __future__ import annotations

from typing import Literal, Optional

TierName = Literal[
    "HARD_RULE_BLOCKING",
    "HARD_RULE_WARN",
    "chat_forbids",
    "chat_dislikes",
]

TIER_WEIGHTS: dict[str, int] = {
    "HARD_RULE_BLOCKING": 3,
    "HARD_RULE_WARN": 2,
    "chat_forbids": 1,
    "chat_dislikes": 0,
}


def tier_weight(tier: str) -> int:
    """Return numeric weight for a tier name.

    Returns -1 for unknown tier names (treated as lower than any known tier).
    """
    return TIER_WEIGHTS.get(tier, -1)


def tier_compare(tier_a: str, tier_b: str) -> int:
    """Compare two tier names by their weights.

    Returns:
        positive int if tier_a > tier_b (a has higher priority)
        negative int if tier_a < tier_b (b has higher priority)
        0 if equal weight
    """
    return tier_weight(tier_a) - tier_weight(tier_b)


def highest_tier(tiers: list[str]) -> Optional[str]:
    """Return the highest-weight tier from a list.

    Returns None if the list is empty.
    """
    if not tiers:
        return None
    return max(tiers, key=tier_weight)


def is_blocking(tier: str) -> bool:
    """Return True if the tier is HARD_RULE_BLOCKING."""
    return tier == "HARD_RULE_BLOCKING"


def is_enforced(tier: str) -> bool:
    """Return True if the tier has any enforcement weight (>= 1)."""
    return tier_weight(tier) >= 1


def explain_tier(tier: str) -> str:
    """Return human-readable explanation for a tier."""
    explanations = {
        "HARD_RULE_BLOCKING": (
            "Machine-enforced gate; violation is blocked automatically. "
            "Weight=3 (highest priority)."
        ),
        "HARD_RULE_WARN": (
            "Warns on violation; agent must acknowledge but can proceed. "
            "Weight=2."
        ),
        "chat_forbids": (
            "User explicitly stated this is forbidden in chat. "
            "Weight=1; agent must respect."
        ),
        "chat_dislikes": (
            "User expressed dislike or preference against in chat. "
            "Weight=0; soft guidance only."
        ),
    }
    return explanations.get(tier, f"Unknown tier '{tier}' (weight={tier_weight(tier)})")
