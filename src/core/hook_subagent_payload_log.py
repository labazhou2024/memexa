"""TU-R8 (2026-04-23): SubagentStop payload logger.

Deep-audit S3: `agent_stall_detector.post_check` needs output_bytes +
duration_sec from the PostToolUse(Task) payload, but Claude Code does not
emit those for the Task tool. Rule 9 block has never fired.

This hook logs the RAW SubagentStop payload (allowlisted metadata only)
so a follow-up task can see what CC actually emits and rewire the stall
trigger. This task does NOT touch the current Rule 9 logic.

SECURITY (security-reviewer B1 fix):
  Payload often contains last_assistant_message, stdout, content. Per
  feedback_sanitize_llm_reflected_text.md (HARD RULE), raw LLM text must
  NOT be persisted unsanitized. We write only an allowlist of metadata
  fields: event, ts, task_id, subagent_type, duration_sec, output_bytes,
  tool_use_id, status. ALL other keys dropped even if present.

Log rotation: write to subagent_payload_log.jsonl; cap at 100 entries;
oldest truncated when cap reached.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_MEMEX = Path(__file__).resolve().parents[2]
_LOG = _MEMEX / "memex" / "data" / "subagent_payload_log.jsonl"
_MAX_ENTRIES = 100

# Allowlist (security B1 fix): only these keys are persisted.
_ALLOWLIST = {
    "event", "ts", "task_id", "subagent_type", "duration_sec",
    "output_bytes", "tool_use_id", "status",
}


def _sanitize(payload: dict) -> dict:
    """Keep only allowlisted metadata keys; drop all free-text fields."""
    out = {}
    for k, v in (payload or {}).items():
        if k in _ALLOWLIST:
            # Further: truncate any string to 256 chars, ints pass through
            if isinstance(v, str):
                out[k] = v[:256]
            elif isinstance(v, (int, float, bool)) or v is None:
                out[k] = v
            else:
                out[k] = str(v)[:256]
    return out


def _rotate_if_needed() -> None:
    if not _LOG.exists():
        return
    try:
        lines = _LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    if len(lines) <= _MAX_ENTRIES:
        return
    # Keep tail
    keep = lines[-_MAX_ENTRIES:]
    _LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")


def log_payload(payload: dict) -> None:
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        sanitized = _sanitize(payload)
        # Always include event+ts
        entry = {
            "event": "subagent_stop",
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            **sanitized,
        }
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate_if_needed()
    except Exception:
        pass  # non-blocking


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    log_payload(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
