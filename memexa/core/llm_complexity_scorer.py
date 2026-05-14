"""U6 TU-1: LLM complexity scorer for tier routing.

6-dimensional 0-15 score → tier ∈ {local, flash, pro}.

Dimensions (each 0-3, except privacy_class 0-2; total 0-17 capped at 15):
  - reasoning_depth   : 0=lookup, 1=single-hop, 2=multi-step, 3=novel-synthesis
  - knowledge_breadth : 0=narrow, 1=domain, 2=cross-domain, 3=research-frontier
  - output_structure  : 0=text, 1=structured-json, 2=code, 3=multi-file
  - stakes            : 0=throwaway, 1=draft, 2=production, 3=irreversible
  - context_length    : 0=<2K, 1=2K-16K, 2=16K-64K, 3=>64K (uses flash route override)
  - privacy_class     : 0=public, 1=internal, 2=chat (forces local override)

Threshold (per chat_to_graph plan_v3_FINAL §3 U6 action #2):
  ≤6  → local (Qwen3-14B-MLX)
  7-10 → flash (DeepSeek-V3-chat)
  ≥11 → pro   (DeepSeek-R1)

Chinese-density bonus: text with ≥30% CJK chars → +1 偏 local (Qwen3 Chinese strength).

axis_anchor: [C:hook:complexity_scorer]
trace event: complexity_routed_<tier>
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

Tier = Literal["local", "flash", "pro"]

THRESHOLD_LOCAL_MAX = 6
THRESHOLD_FLASH_MAX = 10
CJK_DENSITY_BONUS_THRESHOLD = 0.30
PRIVACY_CHAT_FORCED_TIER: Tier = "local"


@dataclass
class Score:
    reasoning_depth: int = 0
    knowledge_breadth: int = 0
    output_structure: int = 0
    stakes: int = 0
    context_length: int = 0
    privacy_class: int = 0
    cjk_density: float = 0.0
    cjk_bonus_applied: bool = False

    def total(self) -> int:
        raw = (
            self.reasoning_depth + self.knowledge_breadth + self.output_structure
            + self.stakes + self.context_length + self.privacy_class
        )
        if self.cjk_bonus_applied:
            raw -= 1  # bonus pulls toward local
        return max(0, min(15, raw))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reasoning_depth": self.reasoning_depth,
            "knowledge_breadth": self.knowledge_breadth,
            "output_structure": self.output_structure,
            "stakes": self.stakes,
            "context_length": self.context_length,
            "privacy_class": self.privacy_class,
            "cjk_density": self.cjk_density,
            "cjk_bonus_applied": self.cjk_bonus_applied,
            "total": self.total(),
        }


def cjk_density(text: str) -> float:
    """Fraction of CJK chars in text. 0.0 if empty."""
    if not text:
        return 0.0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return cjk / max(1, len(text))


def score_payload(payload: Dict[str, Any]) -> Score:
    """Score a routing payload.

    Expected payload keys (all optional with defaults):
      - text: str (the prompt content; used for CJK density)
      - reasoning_depth, knowledge_breadth, output_structure, stakes:
        explicit overrides 0-3
      - context_tokens: int (0 → 0; 2K-16K → 1; 16K-64K → 2; >64K → 3)
      - privacy_class: 'public' | 'internal' | 'chat'
    """
    s = Score()
    text = payload.get("text", "") or ""

    s.reasoning_depth = max(0, min(3, int(payload.get("reasoning_depth", 1))))
    s.knowledge_breadth = max(0, min(3, int(payload.get("knowledge_breadth", 1))))
    s.output_structure = max(0, min(3, int(payload.get("output_structure", 0))))
    s.stakes = max(0, min(3, int(payload.get("stakes", 1))))

    ctx = int(payload.get("context_tokens", len(text) // 4))
    if ctx > 64_000:
        s.context_length = 3
    elif ctx > 16_000:
        s.context_length = 2
    elif ctx > 2_000:
        s.context_length = 1
    else:
        s.context_length = 0

    pc = str(payload.get("privacy_class", "public")).lower()
    s.privacy_class = {"public": 0, "internal": 1, "chat": 2}.get(pc, 0)

    s.cjk_density = cjk_density(text)
    if s.cjk_density >= CJK_DENSITY_BONUS_THRESHOLD:
        s.cjk_bonus_applied = True

    return s


def route(payload: Dict[str, Any]) -> Tier:
    """Score payload and return tier.

    Special-case: privacy_class='chat' forces local regardless of score
    (per chat_to_graph plan_v3_FINAL §3 U6 case 5; data-locality invariant).
    Special-case: context_tokens > 64K forces flash (cost vs precision).
    """
    pc = str(payload.get("privacy_class", "public")).lower()
    if pc == "chat":
        _emit_route_trace("local", payload, reason="privacy_class_chat_forced")
        return PRIVACY_CHAT_FORCED_TIER

    s = score_payload(payload)
    if s.context_length == 3:
        _emit_route_trace("flash", payload, reason="context_over_64K", score=s.total())
        return "flash"

    total = s.total()
    if total <= THRESHOLD_LOCAL_MAX:
        tier: Tier = "local"
    elif total <= THRESHOLD_FLASH_MAX:
        tier = "flash"
    else:
        tier = "pro"
    _emit_route_trace(tier, payload, score=total)
    return tier


def score_and_route(payload: Dict[str, Any]) -> tuple[Tier, Score]:
    """Combined: returns (tier, full Score breakdown)."""
    pc = str(payload.get("privacy_class", "public")).lower()
    s = score_payload(payload)
    if pc == "chat":
        return PRIVACY_CHAT_FORCED_TIER, s
    if s.context_length == 3:
        return "flash", s
    total = s.total()
    if total <= THRESHOLD_LOCAL_MAX:
        return "local", s
    if total <= THRESHOLD_FLASH_MAX:
        return "flash", s
    return "pro", s


def _emit_route_trace(tier: Tier, payload: Dict[str, Any], **extra: Any) -> None:
    """Best-effort trace_sink emission."""
    try:
        from memexa.core.trace_sink import emit  # type: ignore
        emit(
            f"complexity_routed_{tier}",
            {
                "tier": tier,
                "privacy_class": payload.get("privacy_class", "public"),
                "context_tokens": payload.get("context_tokens"),
                **extra,
            },
        )
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    """CLI entry: read JSON payload from stdin, print {tier, score} JSON."""
    import sys
    raw = sys.stdin.read().strip() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid_json"}))
        return 1
    tier, s = score_and_route(payload)
    print(json.dumps({"tier": tier, "score": s.to_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
