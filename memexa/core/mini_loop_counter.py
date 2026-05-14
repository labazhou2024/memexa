"""TU-R6 (2026-04-23): mini-loop commit counter.

Runs as PostToolUse hook on Bash(git commit*). Increments
harness_state.json's `mini_loop.commits_since_last_mini_loop`. Once the
counter reaches threshold (default 10), writes an action_item for the CEO
to trigger the mini-loop.

Closes deep-audit S1: "56 commits/7d → 2 loops" (PostToolUse hook was
never bound to git commit, so counter never advanced despite CLAUDE.md
§二b promising auto-trigger at commits>=10).

Called as:
  python -m memexa.core.mini_loop_counter [--reset|--status]
In PostToolUse hook matcher "Bash" with command starting "git commit".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parents[3]
_HARNESS = _WORKSPACE / ".claude" / "config" / "harness_state.json"

_DEFAULT_THRESHOLD = 10


def _load() -> dict:
    try:
        return json.loads(_HARNESS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        import tempfile
        # atomic write
        tmp = _HARNESS.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, _HARNESS)
    except Exception:
        pass  # non-blocking


def _is_git_commit(cmd: str) -> bool:
    """Heuristic: cmd starts with 'git commit' (ignoring wrappers like rtk)."""
    if not cmd:
        return False
    s = cmd.strip()
    # strip common wrappers
    for wrap in ("rtk ",):
        if s.startswith(wrap):
            s = s[len(wrap):]
    return s.startswith("git commit")


def bump(threshold: int = _DEFAULT_THRESHOLD) -> dict:
    """Increment commits counter. Return {counter, threshold, action_item_added}."""
    d = _load()
    ml = d.setdefault("mini_loop", {})
    counter = int(ml.get("commits_since_last_mini_loop", 0)) + 1
    ml["commits_since_last_mini_loop"] = counter
    added_action = False
    if counter >= threshold:
        # Add action_item if not already present
        items = d.setdefault("action_items_for_user", [])
        marker = f"[MiniLoop] {counter} commits since last loop. Run quality cycle"
        if not any(marker in (it or "") for it in items if isinstance(it, str)):
            items.append(
                f"[MiniLoop] {counter} commits since last loop. "
                f"Run quality cycle: pytest -> fix -> commit."
            )
            added_action = True
    _save(d)
    # Also emit event
    try:
        from memexa.core.event_bus import log_event
        log_event("mini_loop_commit", agent="hook:mini_loop_counter",
                  details={"counter": counter, "threshold": threshold,
                           "action_added": added_action})
    except Exception:
        pass
    return {"counter": counter, "threshold": threshold,
            "action_item_added": added_action}


def reset() -> dict:
    d = _load()
    ml = d.setdefault("mini_loop", {})
    prev = int(ml.get("commits_since_last_mini_loop", 0))
    ml["commits_since_last_mini_loop"] = 0
    ml["last_reset_ts"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    ml["total_loops"] = int(ml.get("total_loops", 0)) + 1
    _save(d)
    return {"prev_counter": prev, "reset_to": 0,
            "total_loops": ml["total_loops"]}


def status() -> dict:
    d = _load()
    ml = d.get("mini_loop", {})
    return {
        "commits_since_last_mini_loop": int(
            ml.get("commits_since_last_mini_loop", 0)
        ),
        "threshold": _DEFAULT_THRESHOLD,
        "total_loops": int(ml.get("total_loops", 0)),
        "last_reset_ts": ml.get("last_reset_ts", ""),
    }


def _hook_main() -> int:
    """PostToolUse entry: read stdin JSON; if Bash git commit, bump."""
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0  # non-blocking
    tool_name = data.get("tool_name", "")
    if tool_name != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command", "")
    if _is_git_commit(cmd):
        bump()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mini_loop_counter")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("reset")
    sub.add_parser("bump")
    sub.add_parser("hook")  # PostToolUse path
    args = p.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "reset":
        print(json.dumps(reset(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "bump":
        print(json.dumps(bump(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "hook":
        return _hook_main()
    # Default: hook (for PostToolUse direct invocation without subcommand)
    return _hook_main()


if __name__ == "__main__":
    sys.exit(main())
