"""
Error Classifier — Categorizes KAIROS daemon execution errors and recommends retry strategies.

Used by kairos_daemon.py to decide whether a failed project step should be retried,
how long to wait, and whether a fix is actionable.

Categories:
  TRANSIENT  — Temporary failures that should resolve on their own (network, timeout)
  PERMANENT  — Failures that require a code/config fix before retrying
  RESOURCE   — Quota or disk exhaustion; wait until reset time
  UNKNOWN    — Unrecognized error; attempt one cautious retry

Usage:
    from src.core.error_classifier import classify_error, ErrorCategory
    category, strategy = classify_error(error_msg)
    if strategy.should_retry:
        delay = get_delay_for_attempt(strategy, attempt=0)
        time.sleep(delay)
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional, Tuple


class ErrorCategory(Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    RESOURCE = "resource"
    UNKNOWN = "unknown"


@dataclass
class RetryStrategy:
    should_retry: bool
    delay_seconds: float          # Initial delay before first retry
    max_retries: int
    backoff_factor: float         # Exponential backoff multiplier
    strategy_name: str            # Human-readable name
    fix_suggestion: str = ""      # For PERMANENT errors: what to fix
    wait_until: str = ""          # For RESOURCE errors: ISO timestamp to wait until


# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

# Each entry: (compiled regex, strategy_name, RetryStrategy kwargs)
_TRANSIENT_PATTERNS: list = [
    (
        # Budget exhaustion: retry with escalated budget (daemon doubles it)
        re.compile(r"Budget exhausted", re.IGNORECASE),
        "budget_escalate",
        dict(should_retry=True, delay_seconds=2.0, max_retries=1, backoff_factor=1.0),
    ),
    (
        re.compile(r"timeout after \d+s", re.IGNORECASE),
        "timeout_retry",
        dict(should_retry=True, delay_seconds=5.0, max_retries=3, backoff_factor=1.5),
    ),
    (
        re.compile(r"connection refused|ConnectionError|network", re.IGNORECASE),
        "network_retry",
        dict(should_retry=True, delay_seconds=30.0, max_retries=3, backoff_factor=2.0),
    ),
    (
        re.compile(r"ECONNRESET|EPIPE", re.IGNORECASE),
        "socket_reset_retry",
        dict(should_retry=True, delay_seconds=10.0, max_retries=3, backoff_factor=2.0),
    ),
    (
        # "rate limit" but NOT "quota" (quota belongs to RESOURCE)
        re.compile(r"rate limit(?!.*quota)", re.IGNORECASE),
        "rate_limit_retry",
        dict(should_retry=True, delay_seconds=60.0, max_retries=3, backoff_factor=2.0),
    ),
]

_PERMANENT_PATTERNS: list = [
    (
        re.compile(r"option .* argument .* is invalid", re.IGNORECASE),
        "invalid_argument",
        dict(should_retry=False, delay_seconds=0.0, max_retries=0, backoff_factor=1.0),
        lambda m, msg: _extract_valid_choices(msg),  # fix_suggestion extractor
    ),
    (
        re.compile(r"FileNotFoundError", re.IGNORECASE),
        "cli_not_found",
        dict(should_retry=False, delay_seconds=0.0, max_retries=0, backoff_factor=1.0),
        lambda m, msg: "CLI binary not found — verify PATH or install the tool",
    ),
    (
        re.compile(r"ModuleNotFoundError|ImportError", re.IGNORECASE),
        "missing_dependency",
        dict(should_retry=False, delay_seconds=0.0, max_retries=0, backoff_factor=1.0),
        lambda m, msg: "Missing Python dependency — run pip install or update requirements.txt",
    ),
    (
        re.compile(r"SyntaxError", re.IGNORECASE),
        "syntax_error",
        dict(should_retry=False, delay_seconds=0.0, max_retries=0, backoff_factor=1.0),
        lambda m, msg: "Code contains a SyntaxError — rewrite the prompt or fix the generated code",
    ),
    (
        re.compile(r"PermissionError", re.IGNORECASE),
        "permission_error",
        dict(should_retry=False, delay_seconds=0.0, max_retries=0, backoff_factor=1.0),
        lambda m, msg: "OS permission denied — check file/directory permissions",
    ),
]

_RESOURCE_PATTERNS: list = [
    (
        re.compile(r"hit your limit|resets \d+", re.IGNORECASE),
        "quota_limit",
        dict(should_retry=True, delay_seconds=300.0, max_retries=1, backoff_factor=1.0),
    ),
    (
        re.compile(r"quota exceeded|insufficient", re.IGNORECASE),
        "quota_exceeded",
        dict(should_retry=True, delay_seconds=300.0, max_retries=1, backoff_factor=1.0),
    ),
    (
        re.compile(r"disk space|no space left", re.IGNORECASE),
        "disk_full",
        dict(should_retry=False, delay_seconds=0.0, max_retries=0, backoff_factor=1.0),
    ),
]

# ---------------------------------------------------------------------------
# Helper: extract valid choices from argument-error messages
# ---------------------------------------------------------------------------

def _extract_valid_choices(error_msg: str) -> str:
    """Pull out the '(choose from ...)' hint if present, else generic advice."""
    m = re.search(r"choose from ([^)]+)", error_msg, re.IGNORECASE)
    if m:
        return f"Valid choices are: {m.group(1).strip()} — fix the argument value"
    return "Fix the invalid argument value — check CLI --help for valid options"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def parse_reset_time(error_msg: str) -> Optional[str]:
    """Extract quota reset time from error messages like 'resets 2pm (Asia/Shanghai)'.

    Returns an ISO 8601 timestamp string (today's date + parsed hour), or None
    if no recognisable reset time is found.
    """
    # Pattern: resets <time> optionally followed by timezone in parens
    pattern = re.compile(
        r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        re.IGNORECASE,
    )
    m = pattern.search(error_msg)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = (m.group(3) or "").lower()

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    # Use today's date; if the resulting time is already past, roll to tomorrow
    now = datetime.now()
    reset_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset_dt <= now:
        reset_dt += timedelta(days=1)

    return reset_dt.isoformat()


def get_delay_for_attempt(strategy: RetryStrategy, attempt: int) -> float:
    """Calculate delay (seconds) for the nth retry attempt using exponential backoff.

    attempt=0 → strategy.delay_seconds (first retry)
    attempt=1 → delay * backoff_factor
    attempt=2 → delay * backoff_factor^2
    """
    if attempt < 0:
        return strategy.delay_seconds
    return strategy.delay_seconds * (strategy.backoff_factor ** attempt)


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify_error(
    error_msg: str,
    error_type: str = "",
    context: Optional[Dict] = None,
) -> Tuple[ErrorCategory, RetryStrategy]:
    """Classify an error message and return the appropriate retry strategy.

    Args:
        error_msg:  The raw error/exception text.
        error_type: Optional short label (e.g. 'TimeoutError').
        context:    Optional dict with extra metadata (unused for now).

    Returns:
        (ErrorCategory, RetryStrategy)
    """
    combined = f"{error_type} {error_msg}"

    # --- Check RESOURCE first (quota/disk signals take priority over generic rate-limit) ---
    for entry in _RESOURCE_PATTERNS:
        pattern, name, kwargs = entry
        if pattern.search(combined):
            wait_until = parse_reset_time(combined) or ""
            strat = RetryStrategy(strategy_name=name, wait_until=wait_until, **kwargs)
            # Compute a more precise delay if we have a reset time
            if wait_until:
                try:
                    reset_dt = datetime.fromisoformat(wait_until)
                    seconds_until = max(0.0, (reset_dt - datetime.now()).total_seconds())
                    strat.delay_seconds = seconds_until
                except ValueError:
                    pass
            return ErrorCategory.RESOURCE, strat

    # --- Check TRANSIENT ---
    for entry in _TRANSIENT_PATTERNS:
        pattern, name, kwargs = entry
        if pattern.search(combined):
            return ErrorCategory.TRANSIENT, RetryStrategy(
                strategy_name=name, **kwargs
            )

    # --- Check PERMANENT ---
    for entry in _PERMANENT_PATTERNS:
        pattern, name, kwargs, *extras = entry
        if pattern.search(combined):
            fix_fn = extras[0] if extras else None
            fix_suggestion = fix_fn(None, combined) if fix_fn else ""
            return ErrorCategory.PERMANENT, RetryStrategy(
                strategy_name=name, fix_suggestion=fix_suggestion, **kwargs
            )

    # --- UNKNOWN fallback: one cautious retry ---
    return ErrorCategory.UNKNOWN, RetryStrategy(
        should_retry=True,
        delay_seconds=15.0,
        max_retries=1,
        backoff_factor=1.0,
        strategy_name="unknown_cautious",
        fix_suggestion="",
        wait_until="",
    )
