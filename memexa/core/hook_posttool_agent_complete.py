"""TU-2 (2026-04-22): PostToolUse(matcher=Agent) hook that auto-detects
agent stalls after Agent tool completion.

Wires together:
  - PreToolUse hook_pretool_agent.py wrote .claude/harness/agent_spawns/<id>.ts
    on spawn.
  - This hook reads the ts + tool_response size, computes duration + bytes,
    invokes agent_stall_detector.post_check() which writes a stall flag
    when output-per-minute falls below threshold.
  - Next spawn of same subagent_type hits pretool_gate.py Rule 9 which
    reads the flag and blocks until TTL clears (or operator runs
    `agent_stall_detector clear <subagent>`).

Hook input shape (Claude Code PostToolUse for Agent tool):
    {
      "hook_event_name": "PostToolUse",
      "tool_name": "Agent",
      "tool_input": {
        "subagent_type": "chief-researcher|...",
        "prompt": "...",
        "model": "opus|sonnet (optional)"
      },
      "tool_use_id": "toolu_...",
      "tool_response": "..." OR "tool_result": "..."  (platform variance)
    }

Contract:
  - ALWAYS exits 0 (fail-open). Never blocks legitimate tool completion.
  - GC side-effect: opportunistically removes orphan spawn ts files >1h.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


_HOOK_NAME = "hook_posttool_agent_complete"
_VALID_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Orphan ts cleanup threshold. Agent_stall_detector uses 30-min TTL for
# its own flags; 1h here gives safe margin against legitimate long-running
# agents (though those usually aren't ours).
_ORPHAN_TS_TTL_SEC = 3600.0


def _agent_spawns_dir() -> Path:
    """LOG-R1 MED-2 fix (2026-04-22): delegate to single source of truth."""
    from memexa.core._agent_spawns import agent_spawns_dir
    return agent_spawns_dir()


def _ts_key(tool_use_id: str, subagent_type: str) -> str:
    """Mirror the same key derivation as hook_pretool_agent._write_agent_spawn_ts."""
    if tool_use_id and _VALID_ID_PATTERN.match(tool_use_id):
        return tool_use_id
    # Without the original timestamp we cannot reconstruct the
    # _internal_<type>_<ms> suffix; duration will fall back to 0.
    return ""


# TU-3 (2026-04-22): local _is_safe_spawn_file removed — migrated to
# memexa.core._safe_fs.is_safe_child (see _safe_fs.py module docstring).

from memexa.core._safe_fs import is_safe_child as _is_safe_spawn_file  # backwards-compat alias
from memexa.core._safe_fs import safe_unlink as _safe_unlink_helper
from memexa.core._safe_fs import safe_unlink_symlink as _safe_unlink_link


def _read_ts_file(key: str) -> Optional[float]:
    """Read the spawn timestamp written by PreToolUse. Return None on
    missing/invalid. Delete the file on successful read (cleanup).

    TU-3 (2026-04-22): unlink goes through _safe_fs.safe_unlink which
    enforces realpath+containment+reject-symlinks.
    """
    if not key:
        return None
    spawns_dir = _agent_spawns_dir()
    ts_file = spawns_dir / f"{key}.ts"
    if not _is_safe_spawn_file(ts_file, spawns_dir):
        return None
    try:
        raw = ts_file.read_text(encoding="utf-8").strip()
        # Format: "<float>\t<subagent_type>"
        ts_str = raw.split("\t", 1)[0] if "\t" in raw else raw
        spawn_ts = float(ts_str)
    except Exception:
        _safe_unlink_helper(ts_file, spawns_dir)  # best-effort cleanup
        return None
    _safe_unlink_helper(ts_file, spawns_dir)  # successful-read cleanup
    return spawn_ts


_MAX_OUTPUT_PROBE_BYTES = 1024 * 1024  # SEC-R1 LOW: 1 MB cap before json.dumps


def _compute_output_bytes(resp) -> int:
    """Derive byte count from tool_response / tool_result. Accepts str or
    any JSON-serializable.

    SEC-R1 LOW fix: cap probe size at 1 MB. For deeply nested platform
    responses, we only need enough data to drive the stall threshold
    (50 B/min over 300s = 250 B min signal); 1 MB cap is 4000x that.
    """
    if resp is None:
        return 0
    if isinstance(resp, str):
        # Bound str length before encode to avoid allocating 2x for wide chars
        return len(resp[:_MAX_OUTPUT_PROBE_BYTES].encode("utf-8", errors="replace"))
    try:
        # Use separators to skip whitespace, cap length during conversion
        s = json.dumps(resp, ensure_ascii=False, separators=(",", ":"))
        return len(s[:_MAX_OUTPUT_PROBE_BYTES].encode("utf-8", errors="replace"))
    except Exception:
        return 0


def _gc_orphan_spawn_files() -> int:
    """Remove spawn ts files older than _ORPHAN_TS_TTL_SEC. Returns count
    removed. Called opportunistically; failures are silent (fail-open).

    TU-3 (2026-04-22): delegates to _safe_fs.safe_unlink / safe_unlink_symlink.
    """
    d = _agent_spawns_dir()
    now = time.time()
    count = 0
    try:
        for p in d.glob("*.ts"):
            try:
                age = now - p.lstat().st_mtime  # lstat: mtime without following symlink
                if age <= _ORPHAN_TS_TTL_SEC:
                    continue
                if p.is_symlink():
                    # Remove the dangling/old symlink itself, not its target
                    if _safe_unlink_link(p, d):
                        count += 1
                    continue
                if _safe_unlink_helper(p, d):
                    count += 1
            except Exception:
                continue
    except Exception:
        pass
    return count


def main() -> int:
    """Hook entry. Reads stdin JSON; ALWAYS exits 0 (fail-open contract)."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        return 0
    if not raw or not raw.strip():
        return 0

    try:
        payload = json.loads(raw)
    except Exception:
        return 0  # malformed JSON — silent no-op

    # Basic payload sanity: must be Agent tool
    if payload.get("tool_name") not in ("Agent", "Task"):
        return 0

    tool_input = payload.get("tool_input") or {}
    subagent_type = tool_input.get("subagent_type") or ""
    if not subagent_type:
        return 0  # malformed — nothing to do

    tool_use_id = payload.get("tool_use_id") or ""
    key = _ts_key(tool_use_id, subagent_type)

    spawn_ts = _read_ts_file(key)
    if spawn_ts is None:
        duration_sec = 0.0
    else:
        duration_sec = max(0.0, time.time() - spawn_ts)

    resp = payload.get("tool_response")
    if resp is None:
        resp = payload.get("tool_result")
    output_bytes = _compute_output_bytes(resp)

    # Core call: agent_stall_detector decides whether to write a flag.
    try:
        from memexa.core.agent_stall_detector import post_check
        post_check(subagent_type, duration_sec, output_bytes)
    except Exception:
        pass  # fail-soft

    # W1-1 (2026-05-04): emit subagent_complete event so W6 callback rate
    # tracks all PostToolUse:Agent firings, not only the subset that triggers
    # SubagentStop (CC does not fire SubagentStop for every Agent tool call —
    # LIVE-witnessed 14.5% completion rate vs 100% spawn rate).
    try:
        from memexa.core._hook_utils import log_hook_event
        log_hook_event(
            event_type="subagent_complete",
            hook_name=_HOOK_NAME,
            details={
                "agent_type": subagent_type,
                "tool_use_id": tool_use_id,
                "duration_sec": round(duration_sec, 1),
                "output_bytes": int(output_bytes),
                "source": "posttool_agent_complete",  # distinguish from SubagentStop path
            },
        )
    except Exception:
        pass  # fail-soft; W6 metric is observability, not gate

    # Opportunistic GC on every call — cheap, keeps dir from growing.
    try:
        _gc_orphan_spawn_files()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
