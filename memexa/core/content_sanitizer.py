"""
Content Sanitizer — pre-extraction prompt-injection defense (R1 / S1).

Used by the future memory_ingest_watcher: before CEO-edited memory/*.md
content is sent to Haiku for fact extraction, this module strips / marks
content that could hijack the extraction prompt.

Public API
----------
    sanitize_for_extraction(text: str) -> tuple[str, list[str]]
        Returns (sanitized_text, removed_markers).

Design rules (AC-T3 2026-04-20):
1. Prompt-injection prefixes (EN + CN): "Ignore prior", "Disregard above",
   "Forget instructions" and Chinese equivalents → replaced with [SANITIZED].
2. Imperative commands at line-start: "Run X", "Execute Y", "Delete Z"
   and Chinese equivalents → [SANITIZED_IMPERATIVE].
3. Base64 blobs: ≥100 consecutive [A-Za-z0-9+/=] chars → [SANITIZED_B64].
4. Deeply nested Markdown blockquotes (depth ≥ 2): ">> " or "> > " prefix
   lines → replaced with [SANITIZED_NESTED_QUOTE].
5. Unicode homograph attack chars (Cyrillic/Greek look-alikes for ASCII)
   → normalized/marked.
6. Clean text passes through unchanged.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

__all__ = ["sanitize_for_extraction"]

# ---------------------------------------------------------------------------
# Pattern catalogue
# ---------------------------------------------------------------------------

# 1. Prompt-injection prefix patterns (case-insensitive, anchored to
#    word/line boundaries so mid-sentence occurrences don't false-positive).
_INJECTION_PREFIX_EN = re.compile(
    r"(?i)\b("
    r"ignore\s+(?:all\s+)?(?:prior|previous|above|earlier)\s+(?:instructions?|rules?|prompts?|context)"
    r"|disregard\s+(?:all\s+)?(?:prior|previous|above|earlier)\s+(?:instructions?|rules?|prompts?|context)"
    r"|forget\s+(?:all\s+)?(?:prior|previous|above|earlier)\s+(?:instructions?|rules?|prompts?|context)"
    r"|you\s+are\s+now\s+(?:a\s+)?(?:different|new)\s+(?:ai|assistant|model)"
    r"|act\s+as\s+(?:a\s+)?(?:different|new|unrestricted)\s+(?:ai|assistant|model)"
    r"|new\s+(?:system\s+)?(?:prompt|instruction|directive)\s*:"
    r")"
)

_INJECTION_PREFIX_CN = re.compile(
    r"(?:"
    r"忽略(?:所有)?(?:之前|上面|前面|先前)的?(?:指令|规则|提示|上下文)"
    r"|忘记(?:所有)?(?:之前|上面|前面|先前)的?(?:指令|规则|提示|上下文)"
    r"|不要理会(?:之前|上面|前面|先前)的?(?:指令|规则|提示)"
    r"|你现在是(?:一个)?(?:不同的|新的|不受限制的)?(?:AI|助手|模型)"
    r"|新的?(?:系统)?(?:提示词?|指令)\s*[：:]"
    r")"
)

# 2. Imperative commands at the start of a line (not inside code blocks)
_IMPERATIVE_EN = re.compile(
    r"(?im)^(?:>+\s*)?("
    r"run\s+\S"
    r"|execute\s+\S"
    r"|delete\s+\S"
    r"|remove\s+\S"
    r"|drop\s+(?:table|database|collection|index)\s"
    r"|rm\s+-[rf]"
    r"|format\s+(?:disk|drive|c:)"
    r")"
)

_IMPERATIVE_CN = re.compile(
    r"(?im)^(?:>+\s*)?("
    r"运行\s*\S"
    r"|执行\s*\S"
    r"|删除\s*\S"
    r"|格式化\s*\S"
    r"|清空\s*\S"
    r")"
)

# 3. Base64 blobs: ≥100 consecutive base64 chars
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/=]{100,}")

# 4. Nested blockquotes: lines starting with ">>" or "> >" (depth ≥ 2)
#    Match both ">>text" and "> > text" forms.
_NESTED_QUOTE = re.compile(r"(?m)^((?:>\s*){2,})(.*)")

# 5. Unicode homograph: Cyrillic/Greek characters that look like Latin ASCII
#    Detected by checking if NFKC normalization + category differs from
#    expected ASCII. We mark the whole token containing such chars.
_SUSPICIOUS_UNICODE = re.compile(
    r"[\u0400-\u04FF"   # Cyrillic block
    r"\u0370-\u03FF"    # Greek block (most are legitimate but flag in context)
    r"\uFF00-\uFFEF"    # Fullwidth/halfwidth forms
    r"\u2100-\u214F"    # Letterlike symbols
    r"]+"
)

# ---------------------------------------------------------------------------
# Sanitizer implementation
# ---------------------------------------------------------------------------

_MAX_SANITIZE_LEN = 10_000  # [SEC-R1-003 2026-04-20] hard cap to prevent ReDoS


def sanitize_for_extraction(text: str) -> Tuple[str, List[str]]:
    """Remove / neutralize content that could hijack Haiku fact extraction.

    Parameters
    ----------
    text : str
        Raw memory file content (or any string) before LLM extraction.

    Returns
    -------
    (sanitized, removed_markers)
        sanitized      : cleaned text safe to pass to Haiku.
        removed_markers: list of descriptive strings naming what was removed.
                         Empty if text was clean (clean_passthrough case).

    [SEC-R1-003 2026-04-20] ReDoS mitigation for CJK-containing regex patterns:
    - Input is truncated to _MAX_SANITIZE_LEN before any regex is applied.
    - _INJECTION_PREFIX_CN is skipped when no CJK code-points are present.
    """
    if not text:
        return ("", [])

    # Hard cap input length before applying any regex to prevent catastrophic
    # backtracking on long inputs (especially long CJK strings without "@").
    if len(text) > _MAX_SANITIZE_LEN:
        text = text[:_MAX_SANITIZE_LEN]

    removed: List[str] = []
    s = text

    # --- Step 1: EN injection prefix ---
    def _repl_inj_en(m: re.Match) -> str:  # type: ignore[type-arg]
        removed.append(f"injection_prefix_en:{m.group(0)[:60]!r}")
        return "[SANITIZED]"

    s_new = _INJECTION_PREFIX_EN.sub(_repl_inj_en, s)
    if s_new != s:
        s = s_new

    # --- Step 2: CN injection prefix ---
    # [SEC-R1-003 2026-04-20] Skip CJK regex when no CJK code-points present —
    # avoids triggering the alternation-heavy pattern on pure-ASCII/emoji input.
    _HAS_CJK = re.compile(r'[\u4E00-\u9FFF\u3400-\u4DBF]')
    if _HAS_CJK.search(s):
        def _repl_inj_cn(m: re.Match) -> str:  # type: ignore[type-arg]
            removed.append(f"injection_prefix_cn:{m.group(0)[:60]!r}")
            return "[SANITIZED]"

        s_new = _INJECTION_PREFIX_CN.sub(_repl_inj_cn, s)
        if s_new != s:
            s = s_new

    # --- Step 3: EN imperative ---
    def _repl_imp_en(m: re.Match) -> str:  # type: ignore[type-arg]
        removed.append(f"imperative_en:{m.group(0)[:60]!r}")
        return "[SANITIZED_IMPERATIVE]"

    s_new = _IMPERATIVE_EN.sub(_repl_imp_en, s)
    if s_new != s:
        s = s_new

    # --- Step 4: CN imperative ---
    def _repl_imp_cn(m: re.Match) -> str:  # type: ignore[type-arg]
        removed.append(f"imperative_cn:{m.group(0)[:60]!r}")
        return "[SANITIZED_IMPERATIVE]"

    s_new = _IMPERATIVE_CN.sub(_repl_imp_cn, s)
    if s_new != s:
        s = s_new

    # --- Step 5: Base64 blobs ---
    def _repl_b64(m: re.Match) -> str:  # type: ignore[type-arg]
        removed.append(f"base64_blob:len={len(m.group(0))}")
        return "[SANITIZED_B64]"

    s_new = _BASE64_BLOB.sub(_repl_b64, s)
    if s_new != s:
        s = s_new

    # --- Step 6: Nested blockquotes ---
    def _repl_nested(m: re.Match) -> str:  # type: ignore[type-arg]
        depth = m.group(1).count(">")
        removed.append(f"nested_quote:depth={depth}")
        return "[SANITIZED_NESTED_QUOTE]"

    s_new = _NESTED_QUOTE.sub(_repl_nested, s)
    if s_new != s:
        s = s_new

    # --- Step 7: Unicode homograph characters ---
    def _repl_unicode(m: re.Match) -> str:  # type: ignore[type-arg]
        token = m.group(0)
        # NFKC normalize — if it maps to ASCII, it's definitely a homograph
        normalized = unicodedata.normalize("NFKC", token)
        # Only flag if the original != normalized (i.e., it changed)
        if normalized != token:
            removed.append(f"unicode_homograph:{token!r}->{normalized!r}")
            return normalized  # replace with the canonical form, not blank
        # Otherwise it's legitimate (e.g. real Greek or Cyrillic text)
        return token

    s_new = _SUSPICIOUS_UNICODE.sub(_repl_unicode, s)
    if s_new != s:
        s = s_new

    return (s, removed)
