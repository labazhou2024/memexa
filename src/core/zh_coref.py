"""TU-U4-3 (2026-04-26): deterministic zh coreference injector.

Replaces 3rd-person pronouns (他/她/它/其) with the FIRST proper-name candidate
from the immediately preceding sentence within the same paragraph.

Per autopilot v2.0 plan_v1 TU-U4-3 + logic-iter1 + council BLOCKERs:
- Rule (a): antecedent is the first Han substring in preceding sentence whose
  characters are ALL plausible name chars (NOT in NON_NAME_CHAR set: verbs/
  particles/numbers/calendar terms). Length must be 2 OR 3 chars.
- Rule (b): pronoun in {他, 她, 它, 其}
- Rule (c): antecedent is in IMMEDIATELY PRECEDING sentence in same paragraph
- Rule (d): cross-paragraph rejected (paragraph break breaks binding chain)
- Rule (e): pronoun inside 「」 quote skipped (dialog excluded)
- Rule (f): if multiple distinct pronouns appear in same paragraph, skip
  (avoid binding ambiguity with multiple subjects)

NOT a learned model; rule-based heuristic only. Trade-off: only works for
2-3 char Han names (most Chinese given+surname pairs); ASCII/longer names
not supported by design.

Hooked from graph_memory_v2.write_fact when zh content detected (Han ratio >=0.3).
Emits trace coref_injected per substitution.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import List

_PRONOUNS = ("他", "她", "它", "其")
_PRONOUN_SET = set(_PRONOUNS)
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_HAN_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_QUOTE_OPEN = "「"
_QUOTE_CLOSE = "」"

# NON_NAME_CHARS: a SMALL set of single Han chars that are clearly NOT part
# of a personal name. When walking a Han run, antecedent extraction terminates
# at the first NON_NAME_CHAR encountered.
#
# Design constraint: keep this list MINIMAL. Many Chinese names contain rare
# chars; over-blocking causes false negatives. Numbers and seasonal terms are
# NOT included because of names like 王一 / 张三 / 李冬.
_NON_NAME_CHARS = set(
    # high-frequency particles + auxiliary verbs
    "的了着过得地"
    # high-frequency verbs (single-char) that almost never appear in names
    "是在开做说看写发启动来去给用让把被听想取"
    # high-frequency time prefixes (still permissive on 春/夏/秋/冬/年/月 because
    # names like 春兰 / 秋月 exist)
    "今昨已经"
    # quote markers
    "「」"
)

_TRACE_LOG_PATH = os.environ.get("MEMEX_COREF_TRACE_LOG", "")


def _emit_trace(event: str, payload: dict) -> None:
    if not _TRACE_LOG_PATH:
        return
    try:
        rec = {"event": event, "ts": time.time(), **payload}
        with open(_TRACE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def detect_zh_dense(text: str, threshold: float = 0.3) -> bool:
    """Return True if Han character ratio >= threshold."""
    if not text:
        return False
    han_count = len(_HAN_RE.findall(text))
    return han_count / max(1, len(text)) >= threshold


def _split_paragraphs_with_seps(text: str) -> List[str]:
    """Split text on blank-line boundaries; preserve paragraph order.

    Returns list of paragraphs (separator strings stripped). To rebuild,
    use _join_paragraphs_with_seps with original separators tracked separately.
    For our use: rebuild via re.sub-based replacement so separators are preserved.
    """
    return re.split(r"\n\s*\n", text)


def _split_sentences(paragraph: str) -> List[str]:
    """Split paragraph on sentence terminators (keep terminators with sentence)."""
    parts = re.split(r"([。！？!?])", paragraph)
    sents = []
    buf = ""
    for p in parts:
        buf += p
        if p in ("。", "！", "？", "!", "?"):
            sents.append(buf)
            buf = ""
    if buf:
        sents.append(buf)
    return [s for s in sents if s.strip()]


def _is_in_quote(paragraph: str, pos: int) -> bool:
    return paragraph[:pos].count(_QUOTE_OPEN) > paragraph[:pos].count(_QUOTE_CLOSE)


def _extract_name_from_han_run(han_run: str) -> str:
    """Walk Han run from start, accumulate while chars are plausible name chars.

    Stop at first NON_NAME_CHAR. Return the prefix if it is 2 or 3 chars
    long; otherwise return empty (too ambiguous).
    """
    buf = ""
    for ch in han_run:
        if ch in _NON_NAME_CHARS or ch in _PRONOUN_SET:
            break
        buf += ch
        if len(buf) == 3:
            break
    if 2 <= len(buf) <= 3:
        return buf
    return ""


def _first_name_candidate(sentence: str) -> str:
    """Return the FIRST plausible name extracted from any Han run in the sentence."""
    for m in _HAN_RUN_RE.finditer(sentence):
        cand = _extract_name_from_han_run(m.group(0))
        if cand:
            return cand
    return ""


def inject_antecedents(text: str) -> str:
    """Inject antecedents into 3rd-person pronouns under 6-rule deterministic logic.

    Returns modified text. Single-paragraph processing only (paragraph breaks
    reset binding). Original paragraph separators are preserved.
    """
    if not text:
        return text
    paragraphs = _split_paragraphs_with_seps(text)
    if len(paragraphs) == 1:
        return _process_paragraph(paragraphs[0])
    new_paragraphs = [_process_paragraph(p) for p in paragraphs]
    # Preserve original separators by walking text
    out = []
    last = 0
    p_idx = 0
    out.append(new_paragraphs[0])
    for m in re.finditer(r"\n\s*\n", text):
        out.append(m.group(0))
        p_idx += 1
        if p_idx < len(new_paragraphs):
            out.append(new_paragraphs[p_idx])
    return "".join(out)


def _process_paragraph(paragraph: str) -> str:
    sentences = _split_sentences(paragraph)
    if len(sentences) < 2:
        return paragraph

    # Rule (f): if MULTIPLE distinct pronouns appear in this paragraph, skip
    pronouns_in_para = {p for p in _PRONOUNS if p in paragraph}
    if len(pronouns_in_para) > 1:
        return paragraph

    out_sentences = list(sentences)
    # FIX logic-iter1-1: use ORIGINAL sentences[:i] for prefix_len so quote
    # offset stays aligned to the original `paragraph` string after multiple
    # antecedent expansions in earlier sentences.
    original_prefix_lens = []
    cum = 0
    for s in sentences:
        original_prefix_lens.append(cum)
        cum += len(s)

    for i in range(1, len(out_sentences)):
        prev = out_sentences[i - 1]
        curr = out_sentences[i]
        antecedent = _first_name_candidate(prev)
        if not antecedent:
            continue
        new_curr = []
        prefix_len = original_prefix_lens[i]
        # Walk ORIGINAL curr (sentences[i]) to use original offsets;
        # build new_curr from out_sentences[i] (which == sentences[i] here
        # since we only modify out_sentences AFTER the for-loop iteration).
        for j, ch in enumerate(curr):
            if ch in _PRONOUN_SET:
                global_pos = prefix_len + j
                if _is_in_quote(paragraph, global_pos):
                    new_curr.append(ch)
                    continue
                _emit_trace("coref_injected",
                            {"pronoun": ch, "antecedent": antecedent,
                             "sentence_index": i})
                new_curr.append(antecedent)
            else:
                new_curr.append(ch)
        out_sentences[i] = "".join(new_curr)
    return "".join(out_sentences)
