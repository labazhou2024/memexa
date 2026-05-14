"""Shared sanitizer for LLM-reflected strings.

Any text that flows from user/cmd/agent data INTO
`hookSpecificOutput.permissionDecisionReason`, `.additionalContext`, or a
`improvement_patterns.jsonl` / memory record's fact/snippet field MUST go
through `sanitize_for_log`. See `memory/feedback_sanitize_llm_reflected_text.md`.

Single source of truth — earlier duplicates in cmd_retry_tracker.py /
agent_output_validator.py / pretool_gate.py migrated to this module.
"""
from __future__ import annotations


def sanitize_for_log(s: str, max_len: int = 500) -> str:
    """Strip control chars (keep space + printable) + cap length.

    Blocks prompt-injection via embedded newlines/CR/NUL/ANSI CSI etc. and
    KB poisoning via crafted snippets that would become a valid JSONL row.

    Examples that are stripped:
      - \\n \\r \\t \\x00-\\x1f (control)
      - ANSI escape CSI sequences (ESC [ ... )
      - Unicode format chars when not printable
    Preserved:
      - ASCII letters/digits/punct
      - Non-ASCII printables (CJK, 4-byte emoji bodies, RTL scripts)
      - Plain space (so wording stays readable)
    """
    if not s:
        return ""
    cleaned = "".join(c for c in s if c.isprintable() or c == " ")
    return cleaned[:max_len]


# Backward-compat alias (ONLY inside this module — call sites import the
# new name `sanitize_for_log`; shim lets a caller that still says
# `_sanitize_for_log` work without breaking). Remove next autopilot per
# FUTURE_WORK.md entry.
_sanitize_for_log = sanitize_for_log
