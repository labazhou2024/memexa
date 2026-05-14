"""
CEO Feedback Capture (v1, 2026-04-19)

Bridges CEO's explicit in-chat feedback to the self-evolution reward
signal. Two capture paths:

1. **UserPromptSubmit hook** — scans incoming user prompt for strong
   feedback markers (thumbs / star ratings / Chinese+English sentiment)
   and records to trace_sink.jsonl. Credits currently-active patterns
   via pattern_extractor.credit_session_helpful when positive.

2. **Explicit CLI** — `python -m src.core.ceo_feedback rate <N> [reason]`
   for deliberate post-session rating. N in 1..5.

Design (per feedback_reward_signal_primacy.md):
- Optimizer algorithms cap at signal quality. Mechanical quality_score
  (exit-code + keyword + budget) has avg 4.13 variance ~0.3, which is
  below GEPA/TextGrad's noise floor. Real CEO feedback is the single
  highest-ROI source of variance.
- NON-blocking, NEVER raises from hook path.
- Explicit > implicit: the CLI path is primary; the keyword-detection
  hook is best-effort.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# [LOG-MED R2] Session-level idempotency: prevent double-credit when the
# same session's positive signal arrives via both the UserPromptSubmit
# hook AND the explicit CLI rate.
_CREDITED_LOCK = threading.Lock()
_CREDITED_SESSIONS: set = set()


def _should_credit(session_key: str) -> bool:
    """Returns True if this session_key has not yet been credited."""
    with _CREDITED_LOCK:
        if session_key in _CREDITED_SESSIONS:
            return False
        _CREDITED_SESSIONS.add(session_key)
        return True


def _reset_credit_memo_for_tests() -> None:
    with _CREDITED_LOCK:
        _CREDITED_SESSIONS.clear()

# --- Feedback detection patterns -------------------------------------------

# Positive markers (ordered strongest first)
_POSITIVE_RE = re.compile(
    r"(?:"
    r"(?:👍|👏|💯|🌟|⭐)"             # emoji
    r"|(?<![/\w])5\s*(?:stars?|/5|星)" # "5 stars", "5/5", "5星"
    r"|(?:\bnice\b|\bgreat\b|\bperfect\b|\bexcellent\b|\bawesome\b|\bwell done\b)"
    r"|(?:很好|非常好|完美|赞|厉害|牛|干得漂亮|做得好|棒)"
    r")",
    flags=re.IGNORECASE,
)

# Negative markers
_NEGATIVE_RE = re.compile(
    r"(?:"
    r"(?:👎|💩)"
    r"|(?<![/\w])1\s*(?:stars?|/5|星)"
    r"|(?:\bbad\b|\bwrong\b|\bterrible\b|\bawful\b|\bbroken\b|\bregression\b|\bbug\b)"
    r"|(?:不对|不好|错了|垃圾|糟糕|烂|失败|没做到|做错)"
    r")",
    flags=re.IGNORECASE,
)

# Meta markers: "stop doing X", "don't X" — signals a correction but not
# necessarily a session-level rating.
_CORRECTION_RE = re.compile(
    r"(?:\b(?:stop|don't|do\s+not|never)\b"
    r"|(?:不要|别|停下|停|以后不要))",
    flags=re.IGNORECASE,
)


def detect_feedback(text: str) -> Tuple[str, float]:
    """Analyze a user message. Returns (verdict, confidence).

    verdict ∈ {"positive", "negative", "correction", "neutral"}
    confidence ∈ [0.0, 1.0] — higher = stronger signal

    Heuristic: multi-hit same polarity = high confidence; mixed = lower.
    """
    if not text or not text.strip():
        return ("neutral", 0.0)
    t = text[:4000]  # bound scan

    pos_hits = len(_POSITIVE_RE.findall(t))
    neg_hits = len(_NEGATIVE_RE.findall(t))
    cor_hits = len(_CORRECTION_RE.findall(t))

    if pos_hits and not neg_hits:
        return ("positive", min(1.0, 0.5 + 0.2 * pos_hits))
    if neg_hits and not pos_hits:
        return ("negative", min(1.0, 0.5 + 0.2 * neg_hits))
    if cor_hits and not (pos_hits or neg_hits):
        return ("correction", min(1.0, 0.4 + 0.15 * cor_hits))
    if pos_hits and neg_hits:
        # [LOG-MED R2] Mixed: majority wins, confidence scales with volume
        # so 5-vs-5 is stronger-neutral than 1-vs-1.
        total = pos_hits + neg_hits
        if pos_hits > neg_hits:
            return ("positive", min(0.5, 0.2 + 0.05 * total))
        if neg_hits > pos_hits:
            return ("negative", min(0.5, 0.2 + 0.05 * total))
        # Tie: neutral but confidence scales with volume (higher-volume
        # tie = more genuinely ambiguous signal worth recording)
        return ("neutral", min(0.4, 0.1 + 0.03 * total))
    return ("neutral", 0.0)


# --- Recording -------------------------------------------------------------

def _scrub_reason(text: str) -> str:
    """[SEC-HIGH R2] Scrub secrets from reason before persisting to jsonl.

    Reuses prompt_evolver._sanitize_for_llm (strict 8-pattern + injection
    filter) because reason comes from user prompts and will be read back
    by future LLM-judge paths.
    """
    if not text:
        return ""
    try:
        from src.core.prompt_evolver import _sanitize_for_llm
        return _sanitize_for_llm(text, max_len=400)
    except Exception:
        # Fallback: minimal scrub if prompt_evolver unavailable.
        # Reviewer 2026-04-21 MED-E: cover common non-Anthropic formats
        # since user prompts frequently discuss GitHub/Google/Slack tokens.
        import re as _re
        out = text[:400]
        patterns = [
            r"(?i)\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}",     # Anthropic + OpenAI
            r"\bghp_[A-Za-z0-9]{36}\b",                      # GitHub PAT
            r"\bgho_[A-Za-z0-9]{36}\b",                      # GitHub OAuth
            r"\bAIza[0-9A-Za-z_\-]{35}\b",                   # Google API
            r"\bya29\.[0-9A-Za-z_\-]+",                      # Google OAuth
            r"\bxox[baprs]-[0-9A-Za-z\-]+",                  # Slack
        ]
        for p in patterns:
            out = _re.sub(p, "[REDACTED]", out)
        out = _re.sub(r"(?i)(authorization|bearer)\s*:?\s*\S+",
                      r"\1: [REDACTED]", out)
        return out


def record_feedback(
    verdict: str,
    confidence: float,
    reason: str = "",
    source: str = "hook",
    rating_1_5: Optional[int] = None,
    session_key: Optional[str] = None,
) -> bool:
    """Write to trace_sink + credit active patterns (if positive).

    Returns True ONLY if trace_sink write succeeded (propagates False).

    [COV-HIGH-2 R2 2026-04-19] Previously returned True unconditionally
    after the try block even when write_trace_event returned False —
    callers relying on the return value got false success.
    """
    try:
        from src.core.trace_sink import write_trace_event
        payload = {
            "verdict": verdict,
            "confidence": round(confidence, 3),
            "reason": _scrub_reason(reason),  # [SEC-HIGH R2] scrub
            "source": source,
        }
        if rating_1_5 is not None:
            payload["rating_1_5"] = int(rating_1_5)
        write_ok = write_trace_event("ceo_feedback", payload)
    except Exception as e:
        logger.warning("ceo_feedback trace write failed: %s", e)
        return False

    if not write_ok:
        return False  # [COV-HIGH-2 R2] Propagate write failure

    # Credit positive patterns (reward signal) — session-idempotent.
    if verdict == "positive" and confidence >= 0.5:
        key = session_key or os.environ.get("CLAUDE_SESSION_ID", "default")
        if _should_credit(key):
            try:
                from src.core.pattern_extractor import credit_session_helpful
                credit_session_helpful()
            except Exception as e:
                logger.info("pattern credit skipped: %s", e)

    return True


# --- UserPromptSubmit hook entry point -------------------------------------

def hook_user_prompt_submit(prompt_text: str) -> None:
    """Called from keyword_router or a dedicated UserPromptSubmit handler.

    Fire-and-forget: never raises, returns None.
    """
    try:
        verdict, confidence = detect_feedback(prompt_text)
        if verdict == "neutral" or confidence < 0.4:
            return
        # Only record strong signals to avoid cluttering the trace
        record_feedback(verdict=verdict, confidence=confidence,
                        reason=prompt_text[:200], source="user_prompt")
    except Exception as e:
        logger.info("ceo_feedback hook failed silently: %s", e)


# --- CLI -------------------------------------------------------------------

def _cli() -> int:
    """Usage:
      python -m src.core.ceo_feedback rate <1-5> [reason]
      python -m src.core.ceo_feedback scan "<text>"   (test detection)
      python -m src.core.ceo_feedback stats            (summary)
    """
    if len(sys.argv) < 2:
        print("usage: ceo_feedback <rate|scan|stats> [args]", file=sys.stderr)
        return 1
    cmd = sys.argv[1]

    if cmd == "rate":
        if len(sys.argv) < 3:
            print("rate requires 1..5", file=sys.stderr)
            return 2
        try:
            n = int(sys.argv[2])
            if not 1 <= n <= 5:
                raise ValueError
        except ValueError:
            print("rate must be integer 1..5", file=sys.stderr)
            return 2
        reason = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
        verdict = "positive" if n >= 4 else "negative" if n <= 2 else "neutral"
        conf = 1.0  # explicit CLI is always high-confidence
        ok = record_feedback(verdict=verdict, confidence=conf,
                             reason=reason, source="cli", rating_1_5=n)
        print(f"recorded: {n}/5 ({verdict}) ok={ok}")
        return 0 if ok else 3

    if cmd == "scan":
        text = " ".join(sys.argv[2:])
        v, c = detect_feedback(text)
        print(f"verdict={v} confidence={c:.2f}")
        return 0

    if cmd == "stats":
        try:
            from src.core.trace_sink import read_traces
        except ImportError:
            print("trace_sink unavailable", file=sys.stderr)
            return 3
        events = read_traces(event_filter=["ceo_feedback"])
        counts = {"positive": 0, "negative": 0, "correction": 0, "neutral": 0}
        ratings = []
        for e in events:
            p = e.get("payload", {})
            v = p.get("verdict", "neutral")
            counts[v] = counts.get(v, 0) + 1
            r = p.get("rating_1_5")
            if r is not None:
                ratings.append(r)
        print(f"total ceo_feedback events: {len(events)}")
        for k, v in counts.items():
            print(f"  {k}: {v}")
        if ratings:
            print(f"explicit ratings: n={len(ratings)} mean={sum(ratings)/len(ratings):.2f}")
        return 0

    print(f"unknown command {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
