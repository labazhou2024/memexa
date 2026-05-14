"""StopFailure hook: log API errors for analysis.

Hook input:
    {
      "hook_event_name": "StopFailure",
      "error_type": "rate_limit|authentication_failed|billing_error|invalid_request|server_error|max_output_tokens|unknown",
      "session_id": "...",
      "transcript_path": "..."
    }

Cannot block (error already happened). Pure observation.

For max_output_tokens errors specifically, extracts as KB pattern
(this signals output cap issues that should inform future agent prompts).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from memexa.core._hook_utils import (  # noqa: E402
    read_hook_input,
    emit_decision,
    log_hook_event,
)


_HOOK_NAME = "stop_failure"


def _extract_pattern_for_token_limit() -> bool:
    """Save max_output_tokens hits as KB pattern."""
    try:
        from memexa.core.pattern_extractor import (
            PatternEntry, Provenance, save_patterns,
        )
        from datetime import datetime
        from dataclasses import asdict

        entry = PatternEntry(
            type="gotcha",
            fact=(
                "[StopFailure max_output_tokens] Claude hit max output token limit "
                "during a turn. This indicates an agent prompt is producing too much "
                "output OR the conversation has accumulated too much context for the model."
            ),
            recommendation=(
                "Review recent agent definitions for output caps. "
                "Consider: stricter findings limits, shorter cross-findings injection, "
                "PreCompact triggered earlier."
            ),
            confidence="high",
            tags=["api_error", "token_limit", "oom"],
            affected_files=[],
            affected_services=["claude_api"],
            provenance=[asdict(Provenance(
                source="stop_failure",
                reference="max_output_tokens",
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
        return 0

    error_type = data.get("error_type", "unknown")
    session_id = data.get("session_id", "")

    pattern_added = False
    if error_type == "max_output_tokens":
        pattern_added = _extract_pattern_for_token_limit()

    log_hook_event(
        event_type="stop_failure",
        hook_name=_HOOK_NAME,
        details={
            "error_type": error_type,
            "session_id": session_id,
            "pattern_extracted": pattern_added,
        },
    )

    # Cannot block, no decision needed
    return 0


if __name__ == "__main__":
    sys.exit(main())
