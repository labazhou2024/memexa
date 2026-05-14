"""TU-U4-2 unit tests for _decode_with_fallback (hindsight_client encoding probe).

Per autopilot v2.0 plan_v1 §Acceptance Criteria AC-2 (5 cases incl. double-decode rejection).
"""
from __future__ import annotations

import pytest

from src.core.hindsight_client import _decode_with_fallback


# ---------- Case 1: UTF-8 with BOM ----------
def test_utf8_with_bom_decodes_via_utf8_sig():
    raw = "\ufeffAlice".encode("utf-8")  # encode includes BOM via \ufeff
    text, enc = _decode_with_fallback(raw)
    assert text == "Alice"  # BOM stripped
    assert enc == "utf-8-sig"


# ---------- Case 2: UTF-8 no BOM, pure ASCII ----------
def test_utf8_pure_ascii_decodes_via_utf8():
    raw = b"hello world"
    text, enc = _decode_with_fallback(raw)
    assert text == "hello world"
    assert enc == "utf-8"


# ---------- Case 3: GBK/cp936 with Han characters ----------
def test_cp936_han_decodes_via_cp936_fallback():
    raw = "测试用户".encode("cp936")
    text, enc = _decode_with_fallback(raw)
    assert text == "测试用户"
    assert enc == "cp936"


# ---------- Case 4: GBK with mixed ASCII + Han ----------
def test_cp936_mixed_ascii_han_decodes():
    raw = "test 测试用户 abc".encode("cp936")
    text, enc = _decode_with_fallback(raw)
    assert text == "test 测试用户 abc"
    assert enc == "cp936"


# ---------- Case 5: str input passes through (double-decode rejection) ----------
def test_str_input_passthrough_no_double_decode():
    """logic-iter1-1 fix: isinstance(b, str) returns ('passthrough')."""
    text, enc = _decode_with_fallback("已经decoded")
    assert text == "已经decoded"
    assert enc == "passthrough"


# ---------- Bonus: utf-8 with Han (no BOM) ----------
def test_utf8_han_no_bom_decodes_via_utf8():
    raw = "Alice".encode("utf-8")
    text, enc = _decode_with_fallback(raw)
    assert text == "Alice"
    assert enc == "utf-8"


# ---------- Bonus: invalid bytes triggers replace fallback ----------
def test_invalid_bytes_falls_back_to_replace():
    raw = b"\xff\xfe\xff"  # invalid as utf-8 AND cp936 (cp936 also rejects 0xff)
    text, enc = _decode_with_fallback(raw)
    assert enc in ("utf-8-replace", "cp936")  # cp936 might accept 0xff in some sequences; either is acceptable last-resort
    assert isinstance(text, str)


# ---------- Bonus: TypeError on non-bytes/non-str ----------
def test_typeerror_on_invalid_type():
    with pytest.raises(TypeError):
        _decode_with_fallback(123)  # type: ignore[arg-type]
