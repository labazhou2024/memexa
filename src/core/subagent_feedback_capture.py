"""B3 (2026-04-18 autopilot) — PostToolUse:Task outcome capture.

Hook entry: Claude Code posts a JSON payload on stdin every time a Task tool
call (subagent) completes. This module parses the payload, derives a quality
score, and appends one record to kairos_feedback.jsonl keyed by agent_role.

Verifier-hardened design:
- NO raw tool_result written to disk (output_hash + output_length only)
- 120-char cap on description/action fields
- PII scrub on description via _redact_pii
- Capture-failure audit log at data/logs/feedback_capture.log
- filelock around jsonl append (multi-process safe)
- Fail-closed: any exception -> exit 0 + log drop_<reason>

Called from: .claude/config/settings.json PostToolUse:Task matcher.
Input: stdin JSON { tool_name, tool_input, tool_result, execution_time_ms, ... }.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# [Security-review MED fix 2026-04-18] agent_role allowlist.
# Agents in .claude/agents/ are 2-32 chars, identifier-safe.
# API keys (sk-..., AIza..., hf_...) are always >= 35 chars, so 32-cap rejects them.
_AGENT_ROLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,31}$")

__all__ = [
    "capture_from_stdin",
    "compute_quality_score",
    "build_record",
    "append_record",
]

_DATA_DIR = Path(__file__).parent.parent / "data"
_FEEDBACK_FILE = _DATA_DIR / "kairos_feedback.jsonl"
_LOGS_DIR = _DATA_DIR / "logs"
_AUDIT_LOG = _LOGS_DIR / "feedback_capture.log"
_LOCK_FILE = _DATA_DIR / ".kairos_feedback.lock"

_MAX_DESC_CHARS = 120
_MIN_OUTPUT_FOR_TRIVIAL = 20  # below this, likely broken/empty
_GOOD_TOKENS = ("passed", "approved", "completed", "success")
_BAD_TOKENS = ("error", "failed", "exception", "traceback", "aborted")


def _audit(status: str, agent_role: str = "", bytes_written: int = 0) -> None:
    """Write one line to feedback_capture.log. Never raises."""
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "agent_role": agent_role,
                "bytes_written": bytes_written,
            },
            ensure_ascii=False,
        )
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # audit log failure must not affect anything


def _redact_pii_safe(text: str) -> str:
    """Thin wrapper: import soft_signal_classifier._redact_pii lazily.

    Falls through to no-op if module missing (hook must not crash).
    """
    if not text:
        return text
    try:
        from src.core.soft_signal_classifier import _redact_pii
        return _redact_pii(text)
    except Exception:
        return text


def compute_quality_score(tool_result: str) -> tuple:
    """Return (score[1..5], verdict, success).

    Pure heuristic, no LLM call. Operates in-memory only; the raw tool_result
    is never written to disk.
    """
    text = (tool_result or "")
    low = text.lower()
    score = 3  # base

    if len(text) > 200:
        score += 1
    if any(tok in low for tok in _GOOD_TOKENS):
        score += 1
    if any(tok in low for tok in _BAD_TOKENS):
        score -= 1
    if len(text) < _MIN_OUTPUT_FOR_TRIVIAL:
        score -= 1

    score = max(1, min(5, score))
    success = score >= 3 and not any(tok in low for tok in _BAD_TOKENS)
    if score >= 4:
        verdict = "good"
    elif score >= 3:
        verdict = "acceptable"
    else:
        verdict = "poor"
    return score, verdict, success


def build_record(payload: dict) -> Optional[dict]:
    """Convert Claude Code hook payload → kairos_feedback record.

    [2026-04-18 bugfix]: Claude Code emits TWO different hook events for
    subagent lifecycle — we accept both:

      SubagentStop schema (primary, fired when Task subagent finishes):
        {hook_event_name: "SubagentStop", agent_type: "...",
         last_assistant_message: "...", agent_id: "...", agent_transcript_path}

      PostToolUse:Task schema (fallback, if Claude Code future emits it):
        {tool_name: "Task", tool_input: {subagent_type, description},
         tool_result: "...", execution_time_ms}

    Returns None if payload is malformed (missing agent identifier).
    """
    # Detect event type
    event = payload.get("hook_event_name") or ""

    if event == "SubagentStop":
        raw_role = str(payload.get("agent_type") or "")
        description = str(payload.get("agent_type") or "")[:_MAX_DESC_CHARS]
        tool_result = payload.get("last_assistant_message") or ""
        # SubagentStop doesn't carry duration — we'd need spawn_time correlation
        exec_ms = None
    else:
        # Fallback: PostToolUse:Task schema
        tool_input = payload.get("tool_input") or {}
        raw_role = str(tool_input.get("subagent_type") or "")
        description = tool_input.get("description") or tool_input.get("prompt", "")[:200]
        tool_result = payload.get("tool_result") or ""
        exec_ms = payload.get("execution_time_ms")

    # [Security-review MED fix 2026-04-18] Allowlist: reject sk-*/control chars
    if not raw_role or not _AGENT_ROLE_RE.match(raw_role):
        return None
    agent_role = raw_role

    description = _redact_pii_safe(str(description))[:_MAX_DESC_CHARS]

    if not isinstance(tool_result, str):
        try:
            tool_result = json.dumps(tool_result, ensure_ascii=False)
        except Exception:
            tool_result = str(tool_result)

    score, verdict, success = compute_quality_score(tool_result)
    output_hash = hashlib.sha256(tool_result.encode("utf-8", errors="replace")).hexdigest()[:16]
    output_length = len(tool_result)

    duration_s = (exec_ms / 1000.0) if isinstance(exec_ms, (int, float)) else 0.0

    ts_now = datetime.now(timezone.utc)
    pid = f"subagent_{int(ts_now.timestamp())}_{output_hash[:8]}"

    # [2026-04-18 bugfix] Source label records which event fired.
    # "postooluse_hook" is retained as alias for dashboard back-compat,
    # "subagent_stop" / "posttool_task" disambiguate in logs.
    source_event = "subagent_stop" if event == "SubagentStop" else "posttool_task"

    return {
        "project_id": pid,
        "agent_role": agent_role,
        "title": description,
        "action": description,
        "quality_score": score,
        "verdict": verdict,
        "success": success,
        "cost_usd": 0.0,
        "num_turns": 0,
        "duration_seconds": round(duration_s, 2),
        "output_hash": output_hash,
        "output_length": output_length,
        "timestamp": ts_now.isoformat(),
        "source": "postooluse_hook",     # alias kept for dashboard back-compat
        "source_event": source_event,     # accurate fine-grained label
    }


def _append_with_lock(record: dict) -> int:
    """Atomically append one JSON line. Returns bytes written, 0 on failure."""
    try:
        from filelock import FileLock
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(_LOCK_FILE), timeout=3.0)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with lock:
            with open(_FEEDBACK_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        return len(line)
    except ImportError:
        # filelock missing → degrade to plain append with atomic write attempt
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with open(_FEEDBACK_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            return len(line)
        except Exception:
            return 0
    except Exception:
        return 0


def append_record(record: dict) -> int:
    """Public wrapper: append + audit. Returns bytes written."""
    n = _append_with_lock(record)
    _audit("ok" if n > 0 else "drop_append_failed", record.get("agent_role", ""), n)
    return n


def capture_from_stdin() -> int:
    """Hook entry point. Always exit 0 (fail-closed).

    [2026-04-18 bugfix] Now supports SubagentStop event (primary) in addition
    to PostToolUse:Task. Entry audit ALWAYS fires so absence of log = hook
    never invoked (distinguishes misconfigured matcher from parse errors).
    """
    # Always audit entry so we can prove the hook was at least called
    _audit("entry")

    try:
        raw = sys.stdin.read() or "{}"
    except Exception:
        _audit("drop_stdin_read_error")
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _audit("drop_malformed_json")
        return 0

    if not isinstance(payload, dict):
        _audit("drop_non_dict_payload")
        return 0

    # Accept either: SubagentStop event OR PostToolUse:Task tool
    event = payload.get("hook_event_name", "")
    tool_name = payload.get("tool_name", "")
    is_subagent_stop = event == "SubagentStop"
    is_task_tool = tool_name == "Task"

    if not (is_subagent_stop or is_task_tool):
        _audit(
            "drop_wrong_event",
            agent_role=f"event={event};tool={tool_name}"[:60],
        )
        return 0

    record = build_record(payload)
    if record is None:
        _audit("drop_missing_agent_type")
        return 0

    append_record(record)
    return 0


def main():
    """CLI entry — always exit 0."""
    sys.exit(capture_from_stdin())


if __name__ == "__main__":
    main()
