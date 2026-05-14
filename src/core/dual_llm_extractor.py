"""
Dual-LLM Fact Extractor — Sonnet primary + Haiku cross-check.

TU3 from 2026-04-19_graphiti_foundation.md. HIGH-risk module.

Core anti-hallucination design (per verifier rounds 2/3):

1. BOTH LLMs must extract facts from the same chunk, each with
   source_span citation (verbatim substring from chunk).
2. Canonicalize entities and predicates BEFORE comparison (alias map).
3. Agreement = 0.5*Jaccard(entities) + 0.5*embedding_cosine(predicates).
4. THIRD SIGNAL (orthogonal to LLMs): source-span byte verification
   AGAINST THE SPECIFIC source file that the fact is attributed to
   (closes coincidental-match loophole).
5. For numeric facts: regex-verify the number appears in source.
6. For file-reference facts: verify file exists.
7. Decision:
   - span_verified AND agreement ≥ 0.85 AND min_conf ≥ 0.7 → auto-write
   - span_verified AND agreement ≥ 0.60                     → pending_review
   - else                                                   → REJECT

Offline/mock path: MEMEXA_DUAL_LLM_MOCK=1 returns deterministic fixtures
for tests.

Contract: extract_from_chunk() never raises; returns ExtractionResult
with status + facts + diagnostics.
"""
from __future__ import annotations


import hashlib
import json
import logging
import os
import re
import threading

# TU-1 (2026-04-23): fail-soft import of json_repair for LLM JSON hardening.
# If the library is absent (supply-chain block, offline bootstrap, etc.), we
# degrade to stdlib-only parsing instead of raising ImportError.
try:
    from json_repair import repair_json as _repair_json  # type: ignore
    _JSON_REPAIR_AVAILABLE = True
except ImportError:  # pragma: no cover
    _repair_json = lambda s: s  # noqa: E731
    _JSON_REPAIR_AVAILABLE = False

# Hard cap for LLM stdout to defeat ReDoS / memory-exhaustion on malformed
# outputs. Real Haiku/Sonnet responses are < 30 KB even for 8-fact arrays.
_MAX_LLM_STDOUT_BYTES = 200_000
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from src.core._path_resolver import memory_dir

logger = logging.getLogger(__name__)

# ---------- Thresholds ----------

AGREE_AUTO = 0.85
AGREE_REVIEW = 0.60
MIN_CONF_AUTO = 0.7
MIN_CONF_REVIEW = 0.4  # [LOG-H2 R2] below this even review queue rejects
SPAN_MIN_CHARS = 8
SPAN_MAX_CHARS = 200
PREDICATE_EMB_COSINE = 0.85
# [HAIKU-H1 R2] Semantic coherence: fact's object (esp. numeric) must
# appear in or immediately adjacent to the cited span, not just anywhere
# in source file. Closes the "span gaming" attack.
SPAN_COHERENCE_WINDOW = 40  # chars around span to check for value

# ---------- Data classes ----------

@dataclass
class Fact:
    subject: str
    predicate: str
    object_: str  # trailing underscore: `object` is builtin
    source_span: str
    confidence: float
    source_episode_id: str  # absolute path to source .md file
    # Derived after canonicalization:
    subject_canon: str = ""
    predicate_canon: str = ""
    object_canon: str = ""
    # TU-2 F2 fix (2026-04-23): repair_tier provenance MUST survive the
    # dict→Fact conversion, otherwise tier-3 demotion in write_fact never
    # fires on the mainline extract_single_llm path. Default 1 = clean parse.
    _repair_tier: int = 1

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["object"] = d.pop("object_")
        return d

    def fingerprint(self) -> str:
        """For dedup and triple matching."""
        s = f"{self.subject_canon}|{self.predicate_canon}|{self.object_canon}"
        return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


@dataclass
class ExtractionResult:
    status: str  # auto_write | pending_review | reject
    facts: List[Fact] = field(default_factory=list)
    reject_reasons: List[str] = field(default_factory=list)
    agreement: float = 0.0
    diagnostics: Dict[str, Any] = field(default_factory=dict)


# ---------- Agreement scoring ----------

def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _predicate_match_score(preds_a: List[str], preds_b: List[str]) -> float:
    """Fraction of preds in A that have a cosine-similar match in B.

    Uses bge-small-zh via graphiti_client.get_embedder() singleton if
    available; falls back to canonicalized exact-match.
    """
    if not preds_a or not preds_b:
        return 0.0
    # Canonicalize both sides
    try:
        from src.core.canonicalizer import canonicalize_predicate
        ca = [canonicalize_predicate(p) for p in preds_a]
        cb = [canonicalize_predicate(p) for p in preds_b]
    except Exception:
        ca, cb = preds_a, preds_b

    # Exact canonical match is fastest path
    set_b = set(cb)
    exact_hits = sum(1 for p in ca if p in set_b)
    exact_ratio = exact_hits / len(ca)
    if exact_ratio >= 1.0:
        return 1.0

    # Fall back to embedding similarity for non-exact pairs
    try:
        from src.core.graphiti_client import get_embedder
        model = get_embedder()
        if model is None:
            return exact_ratio
        unmatched_a = [p for p in ca if p not in set_b]
        emb_a = model.encode(unmatched_a, convert_to_numpy=True, normalize_embeddings=True)
        emb_b = model.encode(cb, convert_to_numpy=True, normalize_embeddings=True)
        import numpy as np
        hits = 0
        for va in emb_a:
            sims = emb_b @ va  # cosine since both normalized
            if sims.max() >= PREDICATE_EMB_COSINE:
                hits += 1
        return (exact_hits + hits) / len(ca)
    except Exception as e:
        logger.info("predicate emb skip: %s", e)
        return exact_ratio


def score_agreement(facts_a: List[Fact], facts_b: List[Fact]) -> float:
    """0.5 * entity Jaccard + 0.5 * predicate match."""
    ents_a = {f.subject_canon for f in facts_a} | {f.object_canon for f in facts_a}
    ents_b = {f.subject_canon for f in facts_b} | {f.object_canon for f in facts_b}
    j = _jaccard(ents_a, ents_b)
    p = _predicate_match_score(
        [f.predicate_canon for f in facts_a],
        [f.predicate_canon for f in facts_b],
    )
    return round(0.5 * j + 0.5 * p, 3)


# ---------- Third signal: source verification ----------

_NUM_RE = re.compile(r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")


# [SEC-H2 R2 + R3 2026-04-19] Restrict source_episode_id to whitelisted roots
# so an adversarial LLM-produced path can't read arbitrary files.
# Derive workspace from __file__ so rename/deployment doesn't silently
# disable traversal protection (R3 fix: was hardcoded).
# Path layout: WORKSPACE/memexa/memexa/core/dual_llm_extractor.py → 4 parents up
_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_MEMORY_ROOT = (
    memory_dir()
).resolve()


def _is_allowed_source_path(p: Path) -> bool:
    """Only memory/*.md under user memory dir OR workspace tree is readable."""
    try:
        resolved = p.resolve()
    except Exception:
        return False
    try:
        for root in (_MEMORY_ROOT, _WORKSPACE_ROOT):
            if resolved == root or resolved.is_relative_to(root):
                return True
    except Exception:
        pass
    return False


def _source_bytes(source_episode_id: str) -> Optional[bytes]:
    """Read the source file. None if unreadable OR outside allowed roots."""
    try:
        p = Path(source_episode_id)
        if not _is_allowed_source_path(p):
            logger.warning(
                "source path rejected (outside allowed roots): %s",
                source_episode_id[:120],
            )
            return None
        if not p.is_file():
            return None
        return p.read_bytes()
    except Exception:
        return None


def verify_source_span(fact: Fact) -> Tuple[bool, str]:
    """Verify source_span appears byte-for-byte in fact.source_episode_id.

    [HAIKU-M2 R2] NFC-normalize both span and source before byte compare
    to handle composed/decomposed Unicode equivalents.
    [HAIKU-H1 R2] Semantic coherence: numeric facts require the number to
    appear in the cited span (or its 40-char window), not "anywhere in
    the file" which lets an LLM pick an unrelated span and still pass.

    Returns (ok, reason).
    """
    import unicodedata
    span = (fact.source_span or "").strip()
    if not span:
        return (False, "empty_source_span")
    if len(span) < SPAN_MIN_CHARS:
        return (False, f"span_too_short ({len(span)}<{SPAN_MIN_CHARS})")
    if len(span) > SPAN_MAX_CHARS:
        return (False, f"span_too_long ({len(span)}>{SPAN_MAX_CHARS})")
    if not fact.source_episode_id:
        return (False, "no_source_episode_id")

    src = _source_bytes(fact.source_episode_id)
    if src is None:
        return (False, f"source_unreadable:{fact.source_episode_id}")

    # Normalize both sides to NFC to collapse decomposed ≡ composed forms
    src_text = unicodedata.normalize("NFC", src.decode("utf-8", errors="replace"))
    span_norm = unicodedata.normalize("NFC", span)

    span_start = src_text.find(span_norm)
    if span_start < 0:
        return (False, "span_not_in_source_file")
    span_end = span_start + len(span_norm)

    # [HAIKU-H1 R2 + R3 + AC5] Coherence: numeric tokens from fact fields must
    # appear with strict word-boundary matching in span or ±window context.
    # AC-5 hardening: the coherence window check CANNOT be bypassed by partial
    # numeric matches (e.g. fact says "192" but source only has "1920").
    # Rule: number m extracted from fact must match \bm\b in the source context.
    # This is NOT bypassable — we use _num_wb_in_text() which enforces \b on
    # both sides, so "192" never matches inside "1920" or "11920".
    window_start = max(0, span_start - SPAN_COHERENCE_WINDOW)
    window_end = min(len(src_text), span_end + SPAN_COHERENCE_WINDOW)
    coherence_ctx = src_text[window_start:window_end]
    for ft in (fact.subject, fact.predicate, fact.object_):
        for m in _NUM_RE.findall(ft or ""):
            # [AC-5] Always use word-boundary needle. "192" must NOT match
            # inside "1920" or "11920" — the \b before and after the digits
            # ensures the number stands alone as a lexical token.
            needle = r"\b" + re.escape(m) + r"\b"
            # Check span first (span is verified to be in source), then window.
            # Both checks use the SAME word-boundary needle — no fallback to
            # plain substring matching anywhere in this path.
            if re.search(needle, span_norm):
                continue
            if not re.search(needle, coherence_ctx):
                return (False, f"number_not_in_span_window:{m}")

    # File-reference sub-check (Windows-aware separator detection)
    for ft in (fact.subject, fact.object_):
        if not ft:
            continue
        looks_like_path = (
            ft.endswith(".md") or ft.endswith(".py")
            or "/" in ft or "\\" in ft
        )
        if looks_like_path:
            candidates = [Path(ft), _WORKSPACE_ROOT / ft, _MEMORY_ROOT / ft]
            if not any(c.exists() for c in candidates):
                return (False, f"referenced_file_missing:{ft}")

    return (True, "ok")


# ---------- Canonicalization wrapper ----------

def _fallback_canon(s: str) -> str:
    """[LOG-M6 R2] Match canonicalizer's normalization: lowercase, strip,
    collapse whitespace/hyphens/underscores — so fallback fingerprints
    match real-path fingerprints for the same input."""
    if not s:
        return ""
    out = s.strip().lower()
    out = re.sub(r"[\s_\-]+", "", out)
    return out


def _canonicalize_facts(facts: List[Fact]) -> List[Fact]:
    try:
        from src.core.canonicalizer import canonicalize_fact
    except Exception:
        for f in facts:
            f.subject_canon = _fallback_canon(f.subject)
            f.predicate_canon = _fallback_canon(f.predicate)
            f.object_canon = _fallback_canon(f.object_)
        return facts
    for f in facts:
        s_c, p_c, o_c, _unknown = canonicalize_fact(f.subject, f.predicate, f.object_)
        f.subject_canon = s_c
        f.predicate_canon = p_c
        f.object_canon = o_c
    return facts


# ---------- LLM callers (pluggable for tests) ----------

LLMFn = Callable[[str, str], List[Dict[str, Any]]]
"""Signature: (model_name, chunk) -> list of raw fact dicts."""

_llm_lock = threading.Lock()
_llm_cache: Dict[str, LLMFn] = {}


def _mock_llm_call(model: str, chunk: str) -> List[Dict[str, Any]]:
    """Deterministic mock — returns one fact if chunk contains 'SIGNAL:FACT'."""
    if "SIGNAL:FACT" not in chunk:
        return []
    # Extract a simple span
    i = chunk.index("SIGNAL:FACT")
    span = chunk[i:i + 30]
    return [{
        "subject": "mock_entity",
        "predicate": "has_value",
        "object": "42",
        "source_span": span,
        "confidence": 0.9,
    }]


def _real_llm_call(model: str, chunk: str) -> List[Dict[str, Any]]:
    """Call LLM via llm_provider (supports claude/openai/glm swap).

    [2026-04-22 Track C migration] Previously called `claude -p` directly;
    now routes through `llm_provider.call_llm()` respecting MEMEXA_LLM_PROVIDER
    env. `model` argument kept for backward compat but mapped to tier:
      claude-opus-* → premium
      claude-sonnet-* → smart
      claude-haiku-* → cheap_fast
    """
    # Map legacy claude model string → provider-agnostic tier
    if "opus" in model:
        tier = "premium"
    elif "sonnet" in model:
        tier = "smart"
    else:
        tier = "cheap_fast"  # haiku or unknown → cheap
    system = (
        "You output ONLY a JSON array. No preamble. No markdown. No backticks. "
        "No explanation. Start your response with [ and end with ]. "
        "Format: "
        '[{"subject":"...","predicate":"...","object":"...","source_span":"...","confidence":0.0-1.0}]. '
        "source_span is a VERBATIM 8-200 char substring from the input chunk "
        "(no paraphrase). "
        "Return AT MOST 8 most-important facts — fewer is better. "
        "Prefer concrete facts with proper names, tools, projects. "
        "Skip narrative prose. If none extractable, return []."
    )
    try:
        from src.core.llm_provider import call_llm, LLMError, ProviderUnavailable
    except Exception as e:
        logger.info("llm_provider unavailable: %s", e)
        return []
    try:
        out = call_llm(user=chunk, system=system, tier=tier, timeout=60)
    except ProviderUnavailable as e:
        logger.info("LLM provider unavailable: %s", e)
        return []
    except LLMError as e:
        logger.warning("LLM call (%s/%s) failed: %s", model, tier, e)
        return []
    try:
        out = (out or "").strip()

        # TU-1 (2026-04-23): oversize short-circuit. Oversized stdout is either
        # a LLM runaway generation or adversarial injection; bounded parsers
        # are still O(n), but stop well before we hit them.
        if len(out) > _MAX_LLM_STDOUT_BYTES:
            logger.warning("LLM (%s) stdout oversize: %d bytes > %d; dropping",
                           model, len(out), _MAX_LLM_STDOUT_BYTES)
            return []

        # Tolerate markdown fences (``` or ```json).
        if out.startswith("```"):
            out = re.sub(r"^```(?:json)?\s*|\s*```$", "",
                         out, flags=re.MULTILINE).strip()

        # Three-tier parse pipeline (TU-1 v3):
        #   Tier 1: stdlib json.loads on the whole output
        #   Tier 2: json_repair then json.loads on the whole output
        #   Tier 3: locate first '[' / last ']' with str.find/rfind
        #           (NO regex, O(n) linear, ReDoS-free), then repair+parse
        data: Any = None
        tier_used: int = 0

        # Tier 1
        try:
            data = json.loads(out)
            tier_used = 1
        except (json.JSONDecodeError, ValueError):
            pass

        # Tier 2: aggressive repair on the whole string
        if tier_used == 0:
            try:
                repaired = _repair_json(out)
                data = json.loads(repaired) if isinstance(repaired, str) else repaired
                tier_used = 2
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        # Tier 3: substring extract by linear scan (ReDoS-proof), then repair
        if tier_used == 0:
            start = out.find("[")
            end = out.rfind("]")
            if start == -1 or end == -1 or end <= start:
                logger.warning("LLM (%s) no JSON array brackets in %d-byte stdout",
                               model, len(out))
                return []
            candidate = out[start:end + 1]
            try:
                repaired = _repair_json(candidate)
                data = json.loads(repaired) if isinstance(repaired, str) else repaired
                tier_used = 3
            except (json.JSONDecodeError, ValueError, TypeError) as e3:
                logger.warning("LLM (%s) tier-3 repair failed: %s", model, e3)
                return []

        if not isinstance(data, list):
            return []

        # TU-2 (2026-04-23): tag each fact dict with _repair_tier so
        # downstream (write_fact) can demote Tier-3 facts to pending_review.
        for fact in data:
            if isinstance(fact, dict):
                fact["_repair_tier"] = tier_used

        if tier_used > 1:
            logger.info("LLM (%s) json parse via Tier-%d (repaired)", model, tier_used)
        return data
    except Exception as e:
        logger.warning("LLM call (%s) failed: %s", model, e)
        return []


def _get_llm_fn() -> LLMFn:
    """[SEC-MED R2] Mock-in-production guard: if graph is ACTIVE
    (flag=1, not shadow) AND mock is set, refuse — mocks writing
    synthetic facts to a real graph is always wrong.
    """
    if os.environ.get("MEMEXA_DUAL_LLM_MOCK", "0") == "1":
        if os.environ.get("MEMEXA_GRAPHITI_ENABLED", "0") == "1":
            logger.error("refusing mock LLM in ACTIVE graph mode; returning no-op")
            return lambda m, c: []  # no-op; extraction will reject on empty
        return _mock_llm_call
    return _real_llm_call


def _nfkc_clean(s: str) -> str:
    """TU-P4 (2026-04-21): NFKC-normalize text from LLM JSON before storage.

    Three jobs:
      1. Apply Unicode NFKC so full-width forms collapse to ASCII
         (Ｗｉｇｎｅｒ -> <topic-3>) and CJK compat ideographs canonicalize.
      2. Strip ASCII control chars (excluding tab/newline/CR).
      3. Leave U+FFFD (replacement char) AS-IS so downstream write_fact
         can detect mojibake residue and reject the whole fact. Do NOT
         silently strip U+FFFD here — that would let corrupt data slip
         through to the graph.
    """
    import unicodedata
    if not s:
        return s
    # Already-string contract enforced upstream (str(...) cast).
    s2 = unicodedata.normalize("NFKC", s)
    # Drop control chars C0/C1 except tab(0x09)/newline(0x0a)/CR(0x0d).
    # L-04 R1 fix (2026-04-21): previously missed DEL (U+007F) and the
    # C1 range (U+0080-U+009F), contrary to docstring claim.
    def _is_printable(ch: str) -> bool:
        if ch in ("\t", "\n", "\r"):
            return True
        cp = ord(ch)
        if cp < 0x20:          # C0 controls
            return False
        if 0x7F <= cp <= 0x9F:  # DEL + C1 controls
            return False
        return True
    s2 = "".join(ch for ch in s2 if _is_printable(ch))
    return s2


def _dicts_to_facts(raw: List[Dict[str, Any]], source_episode_id: str) -> List[Fact]:
    """Convert LLM JSON output into Fact instances.

    [LOG-L10 R2] SECURITY: source_episode_id is FIXED from the caller's
    argument, never from LLM output. Do NOT modify this — a future dev
    adding `d.get("source_episode_id", caller_id)` would silently enable
    attacker-influenceable provenance bypassing verify_source_span.

    [TU-P4 2026-04-21] All string fields pass through _nfkc_clean to
    normalize full-width chars + strip control bytes. U+FFFD passes
    through deliberately so write_fact can reject mojibake.
    """
    out = []
    for d in raw:
        try:
            # F2 fix 2026-04-23: preserve _repair_tier from raw dict.
            # Accept 1/2/3, coerce invalid/missing to 1 (clean-parse default).
            rt_raw = d.get("_repair_tier")
            rt = rt_raw if isinstance(rt_raw, int) and 1 <= rt_raw <= 3 else 1
            out.append(Fact(
                subject=_nfkc_clean(str(d.get("subject", ""))),
                predicate=_nfkc_clean(str(d.get("predicate", ""))),
                object_=_nfkc_clean(str(d.get("object", ""))),
                source_span=_nfkc_clean(str(d.get("source_span", ""))),
                confidence=float(d.get("confidence", 0.0)),
                source_episode_id=source_episode_id,  # <-- FIXED, not from d
                _repair_tier=rt,
            ))
        except Exception as e:
            logger.info("skip malformed raw fact: %s", e)
    return out


# ---------- Public entry point ----------

def extract_from_chunk(chunk: str, source_episode_id: str,
                       sonnet_model: str = "claude-sonnet-4-6",
                       haiku_model: str = "claude-haiku-4-5-20251001"
                       ) -> ExtractionResult:
    """Dual-LLM extraction with third-signal verify + agreement scoring.

    NEVER raises. Result.status is always one of
    {"auto_write","pending_review","reject"}.
    """
    result = ExtractionResult(status="reject")
    if not chunk or not chunk.strip():
        result.reject_reasons.append("empty_chunk")
        return result
    if not source_episode_id:
        result.reject_reasons.append("no_source_episode_id")
        return result

    llm = _get_llm_fn()

    # Parallel-ish calls (sequential is fine for SessionStart; user uptime
    # matters more than 2x-speed here)
    raw_a = llm(sonnet_model, chunk)
    raw_b = llm(haiku_model, chunk)

    facts_a = _canonicalize_facts(_dicts_to_facts(raw_a, source_episode_id))
    facts_b = _canonicalize_facts(_dicts_to_facts(raw_b, source_episode_id))

    result.diagnostics.update({
        "sonnet_count": len(facts_a),
        "haiku_count": len(facts_b),
    })

    # [Phase B 2026-04-21] Single-LLM degraded mode. Haiku on Windows is
    # flaky (STACK_BUFFER_OVERRUN / pagefile pressure observed on the CEO
    # laptop during back-to-back calls). When one LLM produces facts and
    # the other is empty, OPT-IN fallback to single-LLM Sonnet-verified
    # path. Default OFF preserves historical reject semantics — callers
    # who know their environment is flaky opt in explicitly.
    # Safety: still requires source_span verification; only skips the
    # cross-LLM agreement requirement. Tier=pending_review so CEO sees.
    single_fallback = os.environ.get(
        "MEMEXA_DUAL_LLM_SINGLE_FALLBACK", "0"
    ) == "1"
    if single_fallback and (facts_a and not facts_b):
        # Sonnet-only path: verify spans, mark pending_review.
        verified_single: List[Fact] = []
        for fa in facts_a:
            ok, reason = verify_source_span(fa)
            if ok:
                verified_single.append(fa)
            else:
                result.reject_reasons.append(f"single:{reason}")
        if verified_single:
            result.facts = verified_single
            result.agreement = 0.5  # neutral; no cross-LLM signal
            result.status = "pending_review"
            result.diagnostics.update({
                "mode": "sonnet_only_fallback",
                "verified": len(verified_single),
                "haiku_missing_reason": "empty_return",
            })
            return result

    # [LOG-H1 R2] Preserve duplicate-fingerprint facts (same canonical
    # triple, different source_span). Any one of them may pass span
    # verification. Bucket them per fingerprint instead of overwriting.
    by_fp_a: Dict[str, List[Fact]] = {}
    for f in facts_a:
        by_fp_a.setdefault(f.fingerprint(), []).append(f)
    by_fp_b: Dict[str, List[Fact]] = {}
    for f in facts_b:
        by_fp_b.setdefault(f.fingerprint(), []).append(f)
    shared_fps = set(by_fp_a) & set(by_fp_b)

    agree = score_agreement(facts_a, facts_b)
    result.agreement = agree

    if not shared_fps:
        # [LOG-M5 R2] Distinguish "one LLM empty" from "both non-empty but disagree"
        if not facts_a or not facts_b:
            result.reject_reasons.append("one_llm_returned_empty")
        else:
            result.reject_reasons.append("no_shared_triples")
        result.status = "reject"
        return result

    # Third signal: source verification on each shared fact.
    # [LOG-H3 R2 + LOG-H1 R3] Try BOTH sides' spans, not just the higher-conf
    # side. If either LLM emitted a valid span, accept.
    import dataclasses as _dc
    verified: List[Fact] = []
    for fp in shared_fps:
        passed = False
        for fa in by_fp_a[fp]:
            if passed:
                break
            for fb in by_fp_b[fp]:
                # Try fa.span first, then fb.span — take whichever verifies.
                # This is strictly more permissive than "max_conf only".
                candidates = sorted(
                    [fa, fb], key=lambda f: -f.confidence
                )
                picked = None
                for cand in candidates:
                    ok, reason = verify_source_span(cand)
                    if ok:
                        picked = cand
                        break
                    result.reject_reasons.append(f"{fp}:{reason}")
                if picked is None:
                    continue
                avg_conf = round((fa.confidence + fb.confidence) / 2.0, 3)
                fresh = _dc.replace(picked, confidence=avg_conf)
                verified.append(fresh)
                passed = True
                break

    if not verified:
        result.status = "reject"
        result.reject_reasons.append("no_verified_facts")
        return result

    min_conf = min(f.confidence for f in verified)

    # [LOG-H2 R2] Decision tree with min_conf gate on BOTH branches
    if agree >= AGREE_AUTO and min_conf >= MIN_CONF_AUTO:
        result.status = "auto_write"
    elif agree >= AGREE_REVIEW and min_conf >= MIN_CONF_REVIEW:
        result.status = "pending_review"
    else:
        result.status = "reject"
        if agree < AGREE_REVIEW:
            result.reject_reasons.append(f"agreement_too_low:{agree}")
        if min_conf < MIN_CONF_REVIEW:
            result.reject_reasons.append(f"min_conf_too_low:{min_conf}")
        return result

    result.facts = verified
    result.diagnostics.update({
        "agreement": agree, "min_conf": min_conf,
        "shared_fps": len(shared_fps), "verified": len(verified),
    })
    return result


# ---------- Single-LLM mode (Phase B degraded path) ----------

def extract_single_llm(
    chunk: str,
    source_episode_id: str,
    model: str = "claude-sonnet-4-6",
) -> ExtractionResult:
    """Single-LLM extraction with source_span verification.

    Phase B (2026-04-21): when dual-LLM is impractical (Haiku flaky on
    Windows, pagefile pressure, CLI crashes on back-to-back calls), this
    single-call path still goes through span_verify but sets agreement=0.5
    (no cross-check available) and tier=pending_review so the CEO can
    promote via weekly review.

    Use when your machine cannot afford 2x sequential claude -p calls.
    """
    result = ExtractionResult(status="reject")
    if not chunk or not chunk.strip():
        result.reject_reasons.append("empty_chunk")
        return result
    if not source_episode_id:
        result.reject_reasons.append("no_source_episode_id")
        return result

    llm = _get_llm_fn()
    # AC-G3 (2026-04-25): retry-on-empty. LLM stochasticity LIVE-confirmed
    # 2026-04-25: same input returned 0 facts then 8 facts on consecutive
    # runs. Without retry, every file that hits a 0-facts run silently
    # never reaches write_fact. This was the root cause of the 23h
    # write→graph blackout (last-fact 2026-04-24T15:44Z despite 725 daily
    # haiku_extract_done events; pattern_extractor pipeline kept running
    # in parallel masking the graph silence).
    facts: List[Fact] = []
    attempts_used = 0
    for attempt in range(2):  # 1 initial + 1 retry
        attempts_used = attempt + 1
        raw = llm(model, chunk)
        facts = _canonicalize_facts(_dicts_to_facts(raw, source_episode_id))
        if facts:
            break
    result.diagnostics["single_count"] = len(facts)
    result.diagnostics["mode"] = "single_llm"
    result.diagnostics["model"] = model
    result.diagnostics["attempts"] = attempts_used
    if not facts:
        result.reject_reasons.append("single_llm_empty")
        # Emit observable telemetry so future blackouts surface immediately.
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("extract_rejected", {
                "reason": "single_llm_empty",
                "attempts": attempts_used,
                "source_episode_id": (source_episode_id or "")[:120],
                "model": model,
            })
        except (ImportError, OSError):
            pass  # fail-soft
        return result

    # Source-span verification (third signal)
    verified: List[Fact] = []
    for f in facts:
        ok, reason = verify_source_span(f)
        if ok:
            verified.append(f)
        else:
            result.reject_reasons.append(f"span:{reason}")
    if not verified:
        result.reject_reasons.append("no_verified_facts")
        return result

    min_conf = min(f.confidence for f in verified)
    result.facts = verified
    result.agreement = 0.5  # no cross-LLM signal
    # Single-LLM never gets auto_write tier; needs CEO promotion.
    result.status = "pending_review" if min_conf >= MIN_CONF_REVIEW else "reject"
    result.diagnostics["verified"] = len(verified)
    result.diagnostics["min_conf"] = min_conf
    return result


# ---------- Pending review queue ----------

_REVIEW_FILE = Path(__file__).parent.parent / "data" / "pending_review.jsonl"


def _scrub_chunk_preview(text: str, max_len: int = 200) -> str:
    """[SEC-MED R2] Strip secrets before persisting chunk_preview to jsonl."""
    if not text:
        return ""
    try:
        from src.core.prompt_evolver import _sanitize_for_llm
        return _sanitize_for_llm(text, max_len=max_len)
    except Exception:
        return text[:max_len]


def queue_for_review(result: ExtractionResult, chunk: str,
                     source_episode_id: str) -> int:
    """Append each fact as one JSONL row. Returns rows written."""
    if not result.facts:
        return 0
    _REVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().isoformat()
    n = 0
    with open(_REVIEW_FILE, "a", encoding="utf-8") as f:
        for fact in result.facts:
            rec = {
                "id": f"pr_{fact.fingerprint()}_{int(datetime.utcnow().timestamp()*1000)}",
                "status": "pending",
                "ts": ts,
                "source_episode_id": source_episode_id,
                "chunk_preview": _scrub_chunk_preview(chunk, 200),
                "agreement": result.agreement,
                "fact": fact.to_dict(),
                "reject_reasons": result.reject_reasons[:5],
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


# ---------- CLI ----------

def _cli() -> int:
    import sys
    if len(sys.argv) < 2:
        print("usage: dual_llm_extractor <extract|score> ...", file=sys.stderr)
        return 1
    if sys.argv[1] == "extract":
        if len(sys.argv) < 4:
            print("extract <chunk> <source_file>", file=sys.stderr)
            return 2
        r = extract_from_chunk(sys.argv[2], sys.argv[3])
        print(json.dumps({
            "status": r.status, "agreement": r.agreement,
            "facts": [f.to_dict() for f in r.facts],
            "reject_reasons": r.reject_reasons,
            "diagnostics": r.diagnostics,
        }, ensure_ascii=False, indent=2))
        return 0
    print("unknown command", file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
