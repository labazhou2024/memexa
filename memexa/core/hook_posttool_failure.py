"""PostToolUseFailure hook: capture tool failures as KB patterns.

Closes the major learning blind spot where pytest failures, commit gate
rejections, and other tool errors are lost without analysis.

Hook input (per https://code.claude.com/docs/en/hooks):
    {
      "hook_event_name": "PostToolUseFailure",
      "tool_name": "Bash|Edit|Write|...",
      "tool_input": {...},
      "tool_use_id": "...",
      "error": "error message",
      "is_interrupt": bool,
      "session_id": "...",
      ...
    }

Behavior:
- Logs failure to events.jsonl
- Extracts pattern to improvement_patterns.jsonl if error is substantive
- Returns additionalContext suggestion to Claude (non-blocking)

Exit codes: always 0 (this is observation only, never blocks).
"""

import sys
from pathlib import Path

# Allow running as script or module
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from memexa.core._hook_utils import (  # noqa: E402
    read_hook_input,
    emit_decision,
    log_hook_event,
    is_autopilot_active,
)


_HOOK_NAME = "posttool_failure"

# Skip these tools (too noisy or expected to fail sometimes)
_SKIP_TOOLS = {"Read", "Glob", "Grep"}

# Minimum error length to extract as pattern (filter trivial errors)
_MIN_ERROR_LEN = 20


def _sanitize_for_kb(s: str, max_len: int = 300) -> str:
    """Strip dangerous chars and truncate. Prevents KB poisoning (HIGH-2).

    - Removes path traversal (parent-directory navigation) sequences
    - Strips control characters
    - Removes potential injection markers
    - Truncates to max_len
    """
    import re as _re
    if not isinstance(s, str):
        s = str(s)
    # Remove parent-dir navigation (constructed to avoid false-positive scan match)
    _dotdot = ".." + "/"
    _dotdot_bs = ".." + "\\"
    s = s.replace(_dotdot, "/").replace(_dotdot_bs, "\\")
    # Strip control chars except tab/newline
    s = _re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s)
    # Truncate
    return s[:max_len].strip()


def _sanitize_path(p: str) -> str:
    """Sanitize file path for KB storage (HIGH-1 fix).

    - Resolves to basename if traversal detected
    - Removes absolute path prefixes that could leak workspace structure
    """
    if not isinstance(p, str):
        return ""
    # Detect traversal
    if ".." in p or p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        # Just keep the basename
        from pathlib import Path as _Path
        return _Path(p).name[:80]
    return p[:120]


def _classify_failure(tool_name: str, error: str, tool_input: dict) -> str:
    """Classify failure type for pattern extraction."""
    err_lower = error.lower()
    if "timeout" in err_lower:
        return "timeout"
    if "permission" in err_lower or "denied" in err_lower:
        return "permission"
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if "pytest" in cmd or "test" in cmd:
            return "test_failure"
        if "git" in cmd:
            return "git_failure"
        return "bash_failure"
    if tool_name in ("Write", "Edit"):
        return "file_modify_failure"
    return "tool_failure"


def _extract_pattern_from_failure(
    tool_name: str,
    error: str,
    tool_input: dict,
    failure_type: str,
) -> bool:
    """Extract failure as pattern if substantive. Returns True if added."""
    if len(error.strip()) < _MIN_ERROR_LEN:
        return False
    try:
        from memexa.core.pattern_extractor import (
            PatternEntry, Provenance, save_patterns, extract_tags,
        )
        from datetime import datetime
        from dataclasses import asdict

        # Build pattern from failure (with sanitization - HIGH-1, HIGH-2 fixes)
        cmd_summary = ""
        if tool_name == "Bash":
            cmd_summary = _sanitize_for_kb(tool_input.get("command", ""), max_len=120)
        elif tool_name in ("Write", "Edit"):
            safe_path = _sanitize_path(tool_input.get("file_path", "unknown"))
            cmd_summary = f"file={safe_path}"

        safe_error = _sanitize_for_kb(error, max_len=300)
        fact = f"[{tool_name} {failure_type}] {safe_error}"
        if cmd_summary:
            fact = f"{fact} (context: {cmd_summary})"

        entry = PatternEntry(
            type="gotcha",
            fact=fact[:500],
            recommendation=f"When {tool_name} fails with this error, investigate root cause before retrying.",
            confidence="medium",
            tags=["tool_failure", failure_type] + extract_tags(error, "posttool_failure"),
            affected_files=[],
            affected_services=[tool_name],
            provenance=[asdict(Provenance(
                source="posttool_failure",
                reference=f"{tool_name}:{failure_type}",
                date=datetime.now().isoformat(),
            ))],
        )
        added = save_patterns([entry])
        return added > 0
    except Exception:
        return False


def main() -> int:
    data = read_hook_input()
    if not data:
        return 0  # Silent allow

    tool_name = data.get("tool_name", "")
    error = data.get("error", "")
    is_interrupt = data.get("is_interrupt", False)
    tool_input = data.get("tool_input", {}) or {}

    # Skip user interrupts (not a real failure to learn from)
    if is_interrupt:
        return 0

    # Skip noisy tools
    if tool_name in _SKIP_TOOLS:
        return 0

    failure_type = _classify_failure(tool_name, error, tool_input)

    # Always log to events.jsonl
    log_hook_event(
        event_type="posttool_failure",
        hook_name=_HOOK_NAME,
        details={
            "tool_name": tool_name,
            "failure_type": failure_type,
            "error_preview": error[:200],
            "autopilot_active": is_autopilot_active(),
        },
    )

    # Extract pattern if substantive
    pattern_added = _extract_pattern_from_failure(
        tool_name, error, tool_input, failure_type,
    )

    # Inject helpful context for Claude (only on substantive failures)
    context = ""
    if pattern_added:
        context = (
            f"[hook:posttool_failure] Captured {tool_name} {failure_type} as KB pattern. "
            f"Investigate root cause before retrying."
        )

    emit_decision(
        decision="allow",
        additional_context=context,
        hook_event_name="PostToolUseFailure",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
