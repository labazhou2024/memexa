"""
report_linter_hook.py — PostToolUse wrapper for report_linter (2026-04-21).

Claude Code PostToolUse hook. Reads tool_use JSON from stdin:
  - If tool is Write or Edit to a report file (last_briefing.json,
    last_sync.json, or any under .claude/reports/), runs report_linter.lint()
    on the new content.
  - Emits trace event; exits 0 (allow) or 2 (block) per PostToolUse conventions.

Report file allowlist:
  last_briefing.json, last_sync.json, .claude/reports/*.{md,json}

Fail-open: on any internal error, exit 0 (never block a report write due to
a linter bug — that would be worse than letting a bad report through).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


_REPORT_BASENAMES = {"last_briefing.json", "last_sync.json"}
_REPORT_DIR_MARKER = re.compile(r"[\\/]\.claude[\\/]reports[\\/]")


def _is_report_path(file_path: str) -> bool:
    if not file_path:
        return False
    name = Path(file_path).name
    if name in _REPORT_BASENAMES:
        return True
    if _REPORT_DIR_MARKER.search(file_path):
        return True
    return False


def _extract_new_content(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        # New content is in 'new_string'; for a lint pass this is the
        # post-edit content fragment, which is what we want to check.
        return tool_input.get("new_string", "") or ""
    return ""


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        return 0
    if not raw or not raw.strip():
        return 0
    try:
        data = json.loads(raw)
    except Exception:
        return 0

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or ""

    if tool_name not in ("Write", "Edit"):
        return 0
    if not _is_report_path(file_path):
        return 0

    content = _extract_new_content(tool_name, tool_input)
    if not content:
        return 0

    try:
        from memexa.core.report_linter import lint
        viols = lint(content, file_hint=file_path)
    except Exception as e:
        # Linter blew up — fail-open but emit trace
        _emit_trace("gate_data_source_unhealthy", {
            "gate": "report_linter_hook",
            "reason": f"linter_error_{type(e).__name__}",
        })
        return 0

    if not viols:
        _emit_trace("hook_fired", {"name": "report_linter_hook", "status": "ok",
                                   "file": Path(file_path).name})
        return 0

    # Violations — emit + block
    summary = "; ".join(f"{v.kind}:{v.detail[:60]}" for v in viols[:5])
    _emit_trace("hook_fired", {"name": "report_linter_hook",
                               "status": "block",
                               "file": Path(file_path).name,
                               "violations": len(viols)})
    print(f"[REPORT LINTER] {len(viols)} violation(s) in {Path(file_path).name}: {summary}",
          file=sys.stderr)
    print("[REPORT LINTER] Required: tag claims with [code]/[test]/[LIVE]; "
          "drop banned phrases. Use #lint:ignore to opt out per line.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
