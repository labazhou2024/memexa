"""
L2 Soft-Signal Classifier (Phase 2, 2026-04-18)

当 L1 regex 未命中但消息含"软信号"关键词时 (如 "确保/关注/审视/务必/下次"),
调 Claude Haiku 4.5 做 JSON 抽取,判断是否元反馈。

Design (per verifier R2 + R5):
- PII scrub: redact emails/API keys/long numeric sequences before send
- Env whitelist: Popen env= only allows PATH/PYTHONPATH/ANTHROPIC_API_KEY
- Budget cap: daily USD limit via file counter, failure-closed
- ENV flag: MEMEX_L2_SOFT_SIGNAL=1 default on, =0 disables entire module
- Timeout: 5s subprocess hard limit
- Confidence threshold: >= 0.7 to accept result
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = ["classify_soft_signal", "SoftSignalResult", "is_enabled"]


# Soft signal gate: only trigger Haiku if any of these keywords appear
# (avoids spending money on every prompt)
_SOFT_SIGNAL_KEYWORDS = re.compile(
    r"(确保|关注|审视|注意|务必要|务必|下次|以后|时刻|记得|记住|"
    r"ensure|make sure|please|going forward|in the future|keep in mind)",
    re.IGNORECASE,
)

# PII patterns to scrub
# [AC-7 2026-04-20] Expanded coverage: Chinese email domains, CN mobile,
# QQ number (contextual), your-org student ID (PB/SA/PA), URL tokens.
# Baseline regex (kept for compatibility):
_PII_EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-\u4e00-\u9fff]+"  # local part may include CJK via punycode prefix
    r"@"
    r"[A-Za-z0-9.\-\u4e00-\u9fff]+"  # domain may be Chinese (qq.com/163.com/etc.)
    r"\.[A-Za-z]{2,}"
)
_PII_API_KEY = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35,}|"
    r"hf_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
)  # [SEC-LOW] sk-/AIza/hf_/AKIA tokens
_PII_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE)
_PII_LONG_DIGITS = re.compile(r"\b\d{11,}\b")  # phone, CC, etc.
_PII_PATH_USER = re.compile(r"[A-Z]:\\Users\\[^\\]+", re.IGNORECASE)

# [AC-7] New PII patterns for Chinese context:
# CN mobile: 1[3-9] followed by 9 digits = 11 digits total
_PII_CN_MOBILE = re.compile(r"\b1[3-9]\d{9}\b")
# QQ number: 5-11 digits appearing near "qq" keyword (case-insensitive, within 20 chars)
# Pattern: "qq" (optional colon/space/：) then digits, or digits then "qq"
_PII_QQ_NUM = re.compile(
    r"(?:qq\s*[:\uff1a]?\s*)(\d{5,11})"
    r"|(\d{5,11})(?:\s*[:\uff1a]?\s*qq)",
    re.IGNORECASE,
)
# your-org student ID: PB/SA/PA followed by 8 digits
_PII_STUDENT_ID = re.compile(r"\b(?:PB|SA|PA)\d{8}\b", re.IGNORECASE)
# URL with embedded tokens: ?token=..., ?key=..., Authorization=..., access_token=...
_PII_URL_TOKEN = re.compile(
    r"(?:token|key|secret|password|access_token|api_key)"
    r"[=:]\s*[A-Za-z0-9._~+/=%-]{8,}",
    re.IGNORECASE,
)

# [SEC-HIGH] Prompt injection guard: strip delimiter sequences used in template
_INJECTION_DELIMS = re.compile(r"<<<+|>>>+")

# Daily budget counter file
_DATA_DIR = Path(__file__).parent.parent / "data"
_BUDGET_FILE = _DATA_DIR / "haiku_budget_usage.json"

# Haiku 4.5 pricing (2026): ~$0.80/Mtok input, ~$4/Mtok output
# Conservative estimate per call: 500 in + 200 out ≈ $0.0004 + $0.0008 = $0.0012
_EST_COST_PER_CALL_USD = 0.0012

_DEFAULT_DAILY_CAP_USD = float(os.environ.get("MEMEX_HAIKU_DAILY_USD", "2.0"))


@dataclass
class SoftSignalResult:
    is_feedback: bool
    rule_type: str  # "prohibition"/"future_rule"/"order_correction"/"docs_first"/"none"
    rule_text: str
    confidence: float
    source: str = "haiku_soft_signal"


def is_enabled() -> bool:
    """Check ENV flag. Default: on (per R5 user pre-approved)."""
    return os.environ.get("MEMEX_L2_SOFT_SIGNAL", "1") == "1"


def _has_soft_signal(text: str) -> bool:
    """Gate: only call Haiku if text contains soft signals."""
    return bool(_SOFT_SIGNAL_KEYWORDS.search(text))


def _redact_pii(text: str) -> str:
    """Scrub likely PII before sending to Haiku. Returns redacted copy.

    [AC-7 2026-04-20] Order matters: most-specific first to prevent partial
    matches from hiding longer secrets.
    Order: path → api_key/bearer/url_token → student_id → mobile → qq_num
           → email → long digits → injection delims.
    """
    s = _PII_PATH_USER.sub(r"C:\\Users\\[USER]", text)
    s = _PII_API_KEY.sub("[API_KEY_REDACTED]", s)
    s = _PII_BEARER.sub("[BEARER_REDACTED]", s)
    s = _PII_URL_TOKEN.sub("[URL_TOKEN_REDACTED]", s)
    s = _PII_STUDENT_ID.sub("[STUDENT_ID_REDACTED]", s)
    s = _PII_CN_MOBILE.sub("[MOBILE_REDACTED]", s)
    # QQ: replace the captured digit group (group 1 or 2) keeping "qq" label
    def _redact_qq(m: re.Match) -> str:  # type: ignore[type-arg]
        full = m.group(0)
        digits = m.group(1) or m.group(2) or ""
        return full.replace(digits, "[QQ_NUM_REDACTED]")
    s = _PII_QQ_NUM.sub(_redact_qq, s)
    s = _PII_EMAIL.sub("[EMAIL_REDACTED]", s)
    s = _PII_LONG_DIGITS.sub("[DIGITS_REDACTED]", s)
    s = _INJECTION_DELIMS.sub("[DELIM]", s)  # neutralize prompt injection
    return s


_BUDGET_LOCK_FILE = _DATA_DIR / ".haiku_budget.lock"


def _budget_lock():
    """[SEC-HIGH 2026-04-18] Cross-process lock for atomic check-and-increment.

    [SEC-LOW Round2 fix 2026-04-18] Emit a one-time warning if filelock is
    missing — prevents silent loss of atomic guarantee.
    """
    try:
        from filelock import FileLock, Timeout as _FLTimeout  # noqa: F401
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        return FileLock(str(_BUDGET_LOCK_FILE), timeout=0.5)
    except ImportError:
        global _filelock_warn_emitted
        if not _filelock_warn_emitted:
            print(
                "[soft_signal_classifier] WARNING: filelock not installed — "
                "budget check is NOT atomic across processes. "
                "Install with: pip install filelock",
                file=sys.stderr,
            )
            _filelock_warn_emitted = True
        import contextlib
        @contextlib.contextmanager
        def _noop():
            yield
        return _noop()


_filelock_warn_emitted = False


def _check_budget() -> bool:
    """Returns True if daily budget not exhausted. Failure-closed.

    Note: Atomic check-and-increment is _check_and_record_budget(). This
    function is kept for read-only queries (e.g. tests).
    """
    try:
        if not _BUDGET_FILE.exists():
            return True
        data = json.loads(_BUDGET_FILE.read_text(encoding="utf-8"))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_usd = data.get(today, 0.0)
        return today_usd < _DEFAULT_DAILY_CAP_USD
    except Exception:
        return False  # failure-closed


def _check_and_record_budget(cost_usd: float) -> bool:
    """[SEC-HIGH 2026-04-18] Atomic: check budget + increment in one lock.

    Returns True if under cap (and increments). False if at/over cap
    (no increment). Also fail-closed on any exception.
    """
    lock = _budget_lock()
    try:
        with lock:
            data = {}
            if _BUDGET_FILE.exists():
                try:
                    data = json.loads(_BUDGET_FILE.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            current = float(data.get(today, 0.0))
            if current >= _DEFAULT_DAILY_CAP_USD:
                return False
            data[today] = current + cost_usd
            # [SEC-LOW] Calendar-day trim, not entry-count
            from datetime import timedelta as _td
            cutoff = (datetime.now(timezone.utc) - _td(days=7)).strftime("%Y-%m-%d")
            data = {k: v for k, v in data.items() if k >= cutoff}
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _BUDGET_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
    except Exception:
        return False  # failure-closed


def _record_budget(cost_usd: float) -> None:
    """Legacy: increment-only (called AFTER successful call). Best effort.

    Prefer _check_and_record_budget() for atomic usage.
    """
    _check_and_record_budget(cost_usd)


def _safe_env() -> dict:
    """Whitelist env vars for subprocess. Do NOT leak other secrets."""
    whitelist = ["PATH", "PYTHONPATH", "ANTHROPIC_API_KEY", "SYSTEMROOT", "TEMP", "TMP"]
    return {k: v for k, v in os.environ.items() if k in whitelist}


_PROMPT_TEMPLATE = """You are a feedback classifier. Analyze this user message to a coding assistant.

Task: decide if the message contains a corrective rule the assistant should remember \
for future sessions. Output ONLY a single JSON object, no prose.

User message (PII redacted):
<<<
{message}
>>>

Output schema:
{{"is_feedback": true|false,
  "rule_type": "prohibition"|"future_rule"|"order_correction"|"docs_first"|"clarification"|"none",
  "rule_text": "<concise rule in user's language, <=120 chars>",
  "confidence": <0.0-1.0>}}

Rules:
- is_feedback=true ONLY if message contains a persistent rule (e.g., "ensure X", "always Y", "don't Z in future").
- is_feedback=false for conversational messages, questions, or single-task instructions.
- confidence<0.7 means uncertain; caller will discard.
- rule_text should be the NORMALIZED rule, not the raw message.

Examples:
- "请你确保以后不再犯" → {{"is_feedback":true,"rule_type":"future_rule","rule_text":"避免重复之前犯过的错误","confidence":0.85}}
- "帮我写代码" → {{"is_feedback":false,"rule_type":"none","rule_text":"","confidence":0.95}}
"""


def _call_haiku(prompt: str) -> Optional[dict]:
    """Call claude -p with Haiku model. Returns parsed JSON or None.

    [SEC-HIGH 2026-04-18] Atomic budget check-and-increment before call.
    """
    if not _check_and_record_budget(_EST_COST_PER_CALL_USD):
        return None
    try:
        # claude CLI: -p for programmatic (no interactive), --model for model selection
        from src.core.subprocess_launcher import claude_argv
        cmd = claude_argv([
            "-p",
            "--model", "claude-haiku-4-5",
            "--output-format", "text",
            prompt,
        ])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            env=_safe_env(),
        )
        # Budget already recorded atomically via _check_and_record_budget
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        # Extract JSON (LLM may wrap in markdown fence)
        m = re.search(r"\{[^{}]*\"is_feedback\"[^{}]*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    except Exception:
        return None


def classify_soft_signal(message: str) -> Optional[SoftSignalResult]:
    """
    Main entry: classify a user message as feedback/non-feedback using Haiku.

    Returns SoftSignalResult if confidence >= 0.7, else None.
    Returns None if module disabled, budget exhausted, or no soft signals.
    """
    if not is_enabled():
        return None
    if not message or len(message.strip()) < 4:
        return None
    if not _has_soft_signal(message):
        return None  # save money: regex-only gate

    redacted = _redact_pii(message)
    # [SEC-LOW Round2 fix 2026-04-18] Escape braces before .format() — user
    # message containing {rule_type} etc. would raise KeyError otherwise.
    safe_redacted = redacted.replace("{", "{{").replace("}", "}}")[:800]
    try:
        prompt = _PROMPT_TEMPLATE.format(message=safe_redacted)
    except (KeyError, ValueError):
        return None  # malformed template / encoding issue → silent skip

    parsed = _call_haiku(prompt)
    if not parsed:
        return None

    confidence = float(parsed.get("confidence", 0.0))
    if confidence < 0.7:
        return None

    is_feedback = bool(parsed.get("is_feedback", False))
    if not is_feedback:
        return None

    rule_type = str(parsed.get("rule_type", "none"))
    rule_text = str(parsed.get("rule_text", ""))[:200]

    return SoftSignalResult(
        is_feedback=True,
        rule_type=rule_type,
        rule_text=rule_text,
        confidence=confidence,
    )
