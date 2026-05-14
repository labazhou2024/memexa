"""
Delete Gate — PreToolUse enforcement for Bash(rm*) operations.

Requires user confirmation before any file deletion command.
Enforces CLAUDE.md §二: "删除文件、强制覆写前必须向用户确认"

Called by: PreToolUse hook in settings.json, matcher "Bash(rm *)"
"""

import json
import sys


def main():
    """Read tool input from stdin, always ask for confirmation on rm commands."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""

    if not raw.strip():
        # No input — allow (hook protocol fallback)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }))
        sys.exit(0)

    try:
        data = json.loads(raw)
        tool_input = data.get("tool_input", {})
        command = tool_input.get("command", "")
    except Exception:
        command = ""

    # Always ask for confirmation on any rm/del command
    reason = (
        f"FILE DELETION: '{command[:120]}' — "
        f"CLAUDE.md §二 requires CEO confirmation before deleting files. "
        f"Approve only if you intended this deletion."
    )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
