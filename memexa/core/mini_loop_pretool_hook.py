"""PreToolUse hook entrypoint for mini_loop_runner (TU-7b).

Wired into .claude/config/settings.json as a Bash matcher hook. Reads tool input
JSON from stdin; if the Bash command is `git commit ...` AND task complexity is
"complex", invokes mini_loop_runner.run_probes synchronously and blocks the commit
if any probe fails.

For non-git-commit Bash commands or non-complex tasks, exits silently (allow).

Per CEO mandate M3 ("不要写一段代码死一段"): cross-process replay BEFORE commit
lands, so latent bugs surface in this commit, not the next session.

Trace events: pretool_hook_skipped, pretool_hook_invoked, pretool_hook_blocked.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _emit(name: str, payload: dict) -> None:
    """Fail-soft trace emit (no break on trace infra failure)."""
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(name, payload)
    except (ImportError, OSError):
        pass


def _respond(decision: str, reason: str = "") -> None:
    """Output PreToolUse hook decision JSON and exit 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(out))
    sys.exit(0)


_GIT_COMMIT_PATTERN = re.compile(r"\bgit\s+commit\b")


def main() -> None:
    """Entry point — read stdin, filter, conditionally probe, respond."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except OSError:
        _respond("allow")  # fail-soft on stdin error
    if not raw.strip():
        _respond("allow")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _respond("allow")  # fail-soft on malformed input

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    if tool_name != "Bash":
        _emit("pretool_hook_skipped", {"reason": "not_bash"})
        _respond("allow")

    cmd = tool_input.get("command", "")
    if not isinstance(cmd, str) or not _GIT_COMMIT_PATTERN.search(cmd):
        _emit("pretool_hook_skipped", {"reason": "not_git_commit"})
        _respond("allow")

    # Resolve task_id + complexity
    try:
        from memexa.core.task_binding import get_active_task_id
        active_tid = get_active_task_id()
    except (ImportError, OSError):
        active_tid = None

    complexity = "unknown"
    spec_path = Path(__file__).resolve().parent.parent / "data" / "task_spec.json"
    try:
        if spec_path.exists():
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            complexity = str(spec.get("complexity", "unknown"))
    except (OSError, json.JSONDecodeError, ValueError):
        complexity = "unknown"

    if complexity != "complex":
        _emit("pretool_hook_skipped", {
            "reason": "complexity_not_complex",
            "complexity": complexity,
            "active_tid": active_tid or "",
        })
        _respond("allow")

    # Run probes synchronously
    _emit("pretool_hook_invoked", {
        "active_tid": active_tid or "",
        "complexity": complexity,
    })
    try:
        from memexa.core.mini_loop_runner import run_probes
        result = run_probes(active_tid, complexity)
    except (ImportError, OSError, RuntimeError) as e:
        # Fail-soft: probe infra failed → allow commit, emit trace
        _emit("pretool_hook_skipped", {
            "reason": f"probe_infra_error: {type(e).__name__}",
        })
        _respond("allow")

    if result.get("any_failed"):
        failed = [p for p in result.get("probes", [])
                  if p.get("exit_code", 0) != 0]
        reason = (
            f"PRE-COMMIT BLOCK: mini_loop_runner detected {len(failed)} failed "
            f"probe(s) on complex task {active_tid}. Investigate via "
            f"`python -m memexa.core.mini_loop_runner status` then re-commit. "
            f"To override (NOT recommended): mint an HMAC override token via "
            f"`python -m memexa.cli.gates_override mint --reason '...'`. "
            f"Failed probes: {[p.get('cmd', '')[:60] for p in failed]}"
        )
        _emit("pretool_hook_blocked", {
            "active_tid": active_tid or "",
            "failed_count": len(failed),
        })
        _respond("deny", reason)
    else:
        _respond("allow")


if __name__ == "__main__":
    main()
