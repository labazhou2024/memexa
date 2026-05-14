"""
Session Exit Gate — Capture interactive session outcomes for KAIROS evolution.

When a Claude Code interactive session ends (Stop hook), this module:
1. Queries git for commits made during the session
2. Counts files modified, lines changed
3. Computes a quality estimate from observable signals
4. Writes to data/interactive_sessions.jsonl
5. Logs an event to event_bus for evolution consumption
6. Updates harness_state with session metadata

Usage (Stop hook in settings.json):
  python memex/memex/core/session_exit_gate.py
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA = Path(__file__).parent.parent / "data"
_SESSIONS_FILE = _DATA / "interactive_sessions.jsonl"
_HARNESS = Path(__file__).parent.parent.parent / ".claude" / "config" / "harness_state.json"


def _get_session_start_time() -> str:
    """Read session start time from harness_state."""
    try:
        harness = json.loads(_HARNESS.read_text(encoding="utf-8"))
        return harness.get("current_session", {}).get("start_time", "")
    except Exception:
        return ""


def _get_git_commits_since(since_time: str) -> list:
    """Get git commits made during this session."""
    try:
        cmd = ["git", "log", "--oneline", "--no-merges"]
        if since_time:
            cmd.extend(["--since", since_time])
        else:
            cmd.extend(["-10"])  # Fallback: last 10 commits
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent),
        )
        if result.returncode == 0:
            return [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    except Exception:
        pass
    return []


def _get_git_diff_stat() -> dict:
    """Get uncommitted + staged changes summary."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent),
        )
        lines = result.stdout.strip().splitlines()
        files_changed = 0
        insertions = 0
        deletions = 0
        if lines:
            # Last line: " N files changed, X insertions(+), Y deletions(-)"
            import re
            last = lines[-1]
            m_files = re.search(r'(\d+) files? changed', last)
            m_ins = re.search(r'(\d+) insertion', last)
            m_del = re.search(r'(\d+) deletion', last)
            if m_files:
                files_changed = int(m_files.group(1))
            if m_ins:
                insertions = int(m_ins.group(1))
            if m_del:
                deletions = int(m_del.group(1))
        return {"files_changed": files_changed, "insertions": insertions, "deletions": deletions}
    except Exception:
        return {"files_changed": 0, "insertions": 0, "deletions": 0}


def _estimate_quality(commits: list, diff_stat: dict) -> int:
    """Estimate session quality from observable signals (1-5)."""
    score = 2  # Base: session happened

    if len(commits) >= 1:
        score += 1  # Made at least one commit
    if len(commits) >= 3:
        score += 1  # Productive session (3+ commits)
    if diff_stat.get("files_changed", 0) > 0 and diff_stat.get("insertions", 0) > 0:
        score += 1  # Active code changes

    return min(5, score)


def collect_session_exit() -> dict:
    """Main entry: collect session exit telemetry."""
    start_time = _get_session_start_time()
    end_time = datetime.now(timezone.utc).isoformat()
    commits = _get_git_commits_since(start_time)
    diff_stat = _get_git_diff_stat()
    quality = _estimate_quality(commits, diff_stat)

    session_data = {
        "session_id": f"interactive_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "start_time": start_time or "unknown",
        "end_time": end_time,
        "commits": len(commits),
        "commit_messages": commits[:10],
        "diff_stat": diff_stat,
        "quality_score": quality,
        "source": "interactive",
        "timestamp": end_time,
    }

    # Write to sessions log
    _DATA.mkdir(parents=True, exist_ok=True)
    try:
        with open(_SESSIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(session_data, ensure_ascii=False) + "\n")
        logger.info("Session exit recorded: %d commits, quality=%d", len(commits), quality)
    except Exception as e:
        logger.warning("Failed to write session exit: %s", e)

    # Log to event_bus
    try:
        from .event_bus import log_event
        log_event("interactive_session_end", agent="interactive_cto", details={
            "session_id": session_data["session_id"],
            "commits": len(commits),
            "quality_score": quality,
            "files_changed": diff_stat.get("files_changed", 0),
        })
    except Exception:
        pass

    # Feed into evolution via feedback_collector
    try:
        from .feedback_collector import collect_feedback
        virtual_project = {
            "id": session_data["session_id"],
            "title": f"Interactive session ({len(commits)} commits)",
            "prompt": "",
            "max_budget_usd": 0,
            "max_turns": 0,
        }
        virtual_result = {
            "success": quality >= 3,
            "output": "\n".join(commits[:5]) if commits else "no commits",
            "cost_usd": 0,
            "num_turns": 0,
            "duration_seconds": 0,
        }
        collect_feedback(virtual_project, virtual_result)
        logger.info("Interactive session feedback fed to evolution pipeline")
    except Exception as e:
        logger.debug("Feedback collection for interactive session skipped: %s", e)

    # Update harness_state
    try:
        harness = json.loads(_HARNESS.read_text(encoding="utf-8"))
        harness["last_interactive_session"] = {
            "session_id": session_data["session_id"],
            "end_time": end_time,
            "commits": len(commits),
            "quality": quality,
        }
        # Mark pending feedback for next autoDream
        pending = harness.get("pending_session_feedback", [])
        pending.append(session_data["session_id"])
        harness["pending_session_feedback"] = pending[-10:]  # Keep last 10
        _HARNESS.write_text(json.dumps(harness, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("Harness update skipped: %s", e)

    return session_data


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [SESSION EXIT] %(message)s")
    result = collect_session_exit()
    print(f"[SESSION EXIT] {result['session_id']}: {result['commits']} commits, quality={result['quality_score']}")


if __name__ == "__main__":
    main()
