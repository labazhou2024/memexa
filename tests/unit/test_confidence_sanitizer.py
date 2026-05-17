"""Unit tests for _normalize_confidence + _normalize_llm_card confidence
sanitization (v0.1.1 — closes the WeChat 12/18 enum-mismatch dead-letter).

Verifies:
  1. Every canonical enum value passes through unchanged.
  2. Numeric LLM emissions (0.85, 0.5, 0.2) map to enum strictly.
  3. English/Chinese free-text variants map sensibly.
  4. allow_unresolved=False (used by IdentityAssertion + RelationAssertion)
     re-maps "unresolved"/"unknown"/None → "ambiguous"/"inferred".
  5. _normalize_llm_card produces 4 valid confidence fields end-to-end.
"""
from __future__ import annotations

import pytest

from memexa.extraction.run_e2e_pipeline import (
    _normalize_confidence,
    _normalize_llm_card,
)


class TestNormalizeConfidencePassthrough:
    @pytest.mark.parametrize("val", ["certain", "inferred", "ambiguous",
                                     "unresolved"])
    def test_canonical_4_passthrough(self, val: str) -> None:
        assert _normalize_confidence(val, allow_unresolved=True) == val

    @pytest.mark.parametrize("val", ["certain", "inferred", "ambiguous"])
    def test_canonical_3_passthrough(self, val: str) -> None:
        assert _normalize_confidence(val, allow_unresolved=False) == val

    def test_unresolved_blocked_when_disallowed(self) -> None:
        assert _normalize_confidence("unresolved",
                                     allow_unresolved=False) == "ambiguous"


class TestNumericMapping:
    @pytest.mark.parametrize("val,expected", [
        (1.0, "certain"), (0.95, "certain"), (0.9, "certain"),
        (0.85, "inferred"), (0.7, "inferred"), (0.6, "inferred"),
        (0.5, "ambiguous"), (0.4, "ambiguous"), (0.3, "ambiguous"),
        (0.2, "unresolved"), (0.1, "unresolved"), (0.0, "unresolved"),
    ])
    def test_numeric_to_enum(self, val: float, expected: str) -> None:
        assert _normalize_confidence(val, allow_unresolved=True) == expected

    def test_int_high(self) -> None:
        assert _normalize_confidence(1, allow_unresolved=True) == "certain"

    def test_numeric_disallow_unresolved(self) -> None:
        # 0.1 would be "unresolved" but assertion fields don't allow it
        assert _normalize_confidence(0.1, allow_unresolved=False) == "ambiguous"


class TestFreeTextMapping:
    @pytest.mark.parametrize("val,expected", [
        ("high", "certain"), ("HIGH", "certain"), ("Very_High", "certain"),
        ("definite", "certain"), ("sure", "certain"),
        ("medium", "inferred"), ("moderate", "inferred"),
        ("probable", "inferred"),
        ("low", "ambiguous"), ("unclear", "ambiguous"),
        ("tentative", "ambiguous"), ("weak", "ambiguous"),
        ("unknown", "unresolved"), ("null", "unresolved"),
        ("n/a", "unresolved"), ("NA", "unresolved"),
    ])
    def test_english_words(self, val: str, expected: str) -> None:
        assert _normalize_confidence(val, allow_unresolved=True) == expected

    @pytest.mark.parametrize("val,expected", [
        ("确定", "certain"),
        ("明确", "certain"),
        ("推断", "inferred"),
        ("推测", "inferred"),
        ("模糊", "ambiguous"),
        ("不确定", "ambiguous"),
    ])
    def test_chinese_words(self, val: str, expected: str) -> None:
        assert _normalize_confidence(val, allow_unresolved=True) == expected

    def test_substring_match_certain(self) -> None:
        assert _normalize_confidence("very_certain_indeed",
                                     allow_unresolved=True) == "certain"

    def test_substring_match_ambig(self) -> None:
        assert _normalize_confidence("kind_of_ambiguous",
                                     allow_unresolved=True) == "ambiguous"

    def test_default_fallback(self) -> None:
        # Unknown free text → safe default "inferred"
        assert _normalize_confidence("nonsensical_value",
                                     allow_unresolved=True) == "inferred"


class TestBooleanAndNone:
    def test_none(self) -> None:
        assert _normalize_confidence(None,
                                     allow_unresolved=True) == "inferred"

    def test_true(self) -> None:
        assert _normalize_confidence(True,
                                     allow_unresolved=True) == "certain"

    def test_false(self) -> None:
        assert _normalize_confidence(False,
                                     allow_unresolved=True) == "ambiguous"


class TestNormalizeLLMCardEndToEnd:
    """Verify _normalize_llm_card produces enum-valid output even with
    LLM-emitted garbage confidence values across all 4 fields."""

    def test_time_resolution_numeric_confidence(self) -> None:
        card = {
            "narrative": "x" * 50,
            "evidence_quotes": ["q"],
            "when_start": "2026-01-01T00:00:00+08:00",
            "time_resolutions": [{
                "surface_form": "今天",
                "resolved_start": "2026-01-01T00:00:00+08:00",
                "resolved_end": "2026-01-01T23:59:59+08:00",
                "anchor_message_ts": "2026-01-01T00:00:00+08:00",
                "resolution_method": "llm_inferred",
                "confidence": 0.85,
            }],
        }
        out = _normalize_llm_card(card)
        assert out["time_resolutions"][0]["confidence"] == "inferred"

    def test_entity_high_confidence(self) -> None:
        card = {
            "entities": [{
                "canonical_name": "Alice",
                "role_in_card": "subject",
                "surface_form": "Alice",
                "resolution_confidence": "high",
            }],
        }
        out = _normalize_llm_card(card)
        assert out["entities"][0]["resolution_confidence"] == "certain"

    def test_identity_assertion_unresolved_to_ambiguous(self) -> None:
        # IdentityAssertion rejects "unresolved" — must remap.
        card = {
            "identity_assertions": [{
                "quote": "q",
                "subject_wxid_hash": "h",
                "asserted_relation": "is_self",
                "asserted_value": "v",
                "confidence": "unresolved",
            }],
        }
        out = _normalize_llm_card(card)
        assert out["identity_assertions"][0]["confidence"] == "ambiguous"

    def test_relation_assertion_numeric_low(self) -> None:
        card = {
            "relation_assertions": [{
                "quote": "q",
                "context": "c",
                "person_a": "p1",
                "person_b": "p2",
                "confidence": 0.2,  # would be "unresolved", but field disallows
            }],
        }
        out = _normalize_llm_card(card)
        # 0.2 → "unresolved" → remapped to "ambiguous" because
        # allow_unresolved=False on RelationAssertion
        assert out["relation_assertions"][0]["confidence"] == "ambiguous"

    def test_anchor_message_ts_bare_date_normalized(self) -> None:
        """anchor_message_ts must be ISO 8601 -- v0.1.1 closes the case
        where LLM emits a bare date ("2024-01-08") and TimeResolution
        validator rejects with 'anchor_message_ts must be ISO 8601'."""
        card = {
            "narrative": "x" * 50,
            "time_resolutions": [{
                "surface_form": "周三",
                "resolved_start": "2024-01-08T00:00:00+08:00",
                "resolved_end": "2024-01-08T23:59:59+08:00",
                "anchor_message_ts": "2024-01-08",  # bare date
                "resolution_method": "calendar",
                "confidence": "certain",
            }],
        }
        out = _normalize_llm_card(card)
        anchor = out["time_resolutions"][0]["anchor_message_ts"]
        assert "T" in anchor and ("+" in anchor or "Z" in anchor), (
            f"anchor_message_ts should be ISO; got {anchor!r}"
        )

    def test_anchor_message_ts_empty_string_falls_back(self) -> None:
        card = {
            "time_resolutions": [{
                "surface_form": "now",
                "resolved_start": "2024-01-08T00:00:00+08:00",
                "resolved_end": "2024-01-08T00:00:00+08:00",
                "anchor_message_ts": "",
                "resolution_method": "now",
                "confidence": "certain",
            }],
        }
        out = _normalize_llm_card(card)
        anchor = out["time_resolutions"][0]["anchor_message_ts"]
        # Empty input falls back to the card-level when_start_default
        assert "T" in anchor

    def test_18_diverse_confidence_values_all_valid_enum(self) -> None:
        """Simulate the 18 demo cards' worth of diverse LLM emissions —
        all 18 must map to a valid TimeResolution.confidence enum value
        (this is the §C.-41.6 reproduction)."""
        valid_tr = {"certain", "inferred", "ambiguous", "unresolved"}
        test_values = [
            "certain", "inferred", "ambiguous", "unresolved",  # canonical 4
            "high", "low", "medium", "unclear",               # English 4
            "确定", "推断", "模糊", "不确定",
            0.95, 0.7, 0.4, 0.1,                              # numeric 4
            None, "wat_is_this",                              # garbage 2
        ]
        assert len(test_values) == 18
        for v in test_values:
            card = {
                "time_resolutions": [{
                    "surface_form": "x",
                    "resolved_start": "2026-01-01T00:00:00+08:00",
                    "resolved_end": "2026-01-01T23:59:59+08:00",
                    "anchor_message_ts": "2026-01-01T00:00:00+08:00",
                    "resolution_method": "llm_inferred",
                    "confidence": v,
                }],
            }
            out = _normalize_llm_card(card)
            assert out["time_resolutions"][0]["confidence"] in valid_tr, (
                f"input {v!r} → {out['time_resolutions'][0]['confidence']!r}"
            )
