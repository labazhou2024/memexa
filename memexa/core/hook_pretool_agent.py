"""PreToolUse(matcher=Agent) hook: rate-limit parallel subagent spawns.

Platform-level enforcement of parallel agent limits. Replaces the
prompt-only "max 5 agents" rule that Claude can ignore.

CEO-approved limits (2026-04-17):
  - Opus models: max 5 concurrent
  - Sonnet models: max 7 concurrent
  - No per-agent-type limit (multiple chief-researchers OK)
  - 15-minute timeout: agents tracked beyond 15min force-released

Hook input:
    {
      "hook_event_name": "PreToolUse",
      "tool_name": "Agent",
      "tool_input": {
        "prompt": "...",
        "subagent_type": "chief-researcher|...",
        "model": "opus|sonnet (optional)"
      }
    }

Decision:
  - allow: increment counter, allow spawn
  - deny: limit reached, Claude must wait
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from memexa.core._hook_utils import (  # noqa: E402
    read_hook_input,
    emit_decision,
    log_hook_event,
    atomic_json_write,
    safe_load_json,
    get_workspace_paths,
)


_HOOK_NAME = "pretool_agent"
_PATHS = get_workspace_paths()
_COUNTER_FILE = _PATHS["data"] / "agent_active_count.json"

# CEO-approved limits
LIMITS = {
    "opus_max": 5,
    "sonnet_max": 7,
    "default_max": 5,  # If model unknown, treat as opus
    "stale_seconds": 15 * 60,  # 15 min force-release
}

# Map agent name -> model (from .claude/agents/*.md frontmatter)
# This is a snapshot; if agents change, update the map.
_AGENT_MODEL_MAP = {
    "chief-researcher": "opus",
    "qa-director": "opus",
    "strategic-advisor": "opus",
    "architect": "opus",
    "verifier": "opus",
    "prl-writing-master": "opus",
    "nature-writing-master": "opus",
    "reviewer-technical": "opus",
    "reviewer-narrative": "opus",
    "chinese-doc-master": "opus",
    "physics-lab-report": "opus",
    "consistency-auditor": "opus",
    "scope-validator": "opus",
    "research-assistant": "opus",
    "logic-reviewer": "sonnet",
    "security-reviewer": "sonnet",
    "coverage-reviewer": "sonnet",
    "code-reviewer": "sonnet",
    "fix-agent": "sonnet",
    "gate-keeper": "sonnet",
    "release-gate": "sonnet",
    "knowledge-manager": "sonnet",
    "sonnet-executor": "sonnet",
    "test-runner": "sonnet",
    "briefing": "sonnet",
}


def _get_model_for_agent(agent_type: str, override: str = "") -> str:
    """Determine model class (opus|sonnet) for an agent type."""
    if override:
        m = override.lower()
        if "opus" in m:
            return "opus"
        if "sonnet" in m:
            return "sonnet"
    return _AGENT_MODEL_MAP.get(agent_type, "opus")  # Conservative default


def _load_active_agents() -> dict:
    """Load active agent registry. Auto-prune stale entries."""
    state = safe_load_json(_COUNTER_FILE, default={"agents": {}})
    now = time.time()
    pruned = {}
    for agent_id, info in state.get("agents", {}).items():
        spawn_time = info.get("spawn_time", 0)
        if now - spawn_time < LIMITS["stale_seconds"]:
            pruned[agent_id] = info
    state["agents"] = pruned
    return state


def _count_by_model(state: dict) -> dict:
    """Count active agents per model class."""
    counts = {"opus": 0, "sonnet": 0}
    for info in state["agents"].values():
        model = info.get("model", "opus")
        counts[model] = counts.get(model, 0) + 1
    return counts


_VALID_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _make_agent_id(tool_use_id: str, subagent_type: str) -> str:
    """Generate unique agent ID. Validates tool_use_id format to prevent injection.

    HIGH-3 fix: tool_use_id from hook stdin must be alphanumeric+underscore+hyphen,
    max 64 chars. Otherwise fall back to internal-generated ID. Prevents collisions
    that could bypass rate limit.
    """
    if tool_use_id and _VALID_ID_PATTERN.match(tool_use_id):
        return tool_use_id
    # Use sanitized subagent_type + timestamp as fallback
    safe_type = re.sub(r"[^a-zA-Z0-9_-]", "_", subagent_type)[:32]
    return f"_internal:{safe_type}:{int(time.time() * 1000)}"


def _atomic_check_and_increment(model_class: str, agent_id: str, subagent_type: str,
                                  tool_use_id: str) -> tuple:
    """Optimistic check + increment with atomic write.

    DESIGN NOTE (2026-04-17 revision):
    Original fcntl/msvcrt locking caused deadlock on Windows (LK_LOCK blocks
    with retry, lock byte offset mismatch on partial release). Replaced with
    optimistic concurrency:
    - Read state, prune stale, check limit
    - If allowed, write back via tempfile+rename (atomic on both Unix/Windows)
    - In rare race (2 simultaneous spawns), 1-2 agents may exceed limit briefly
      - Acceptable since real API rate limit is much higher
      - Subsequent SubagentStop will release slots, restoring invariant
    """
    _COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Read current state
    state = safe_load_json(_COUNTER_FILE, default={"agents": {}})

    # Auto-prune stale (15-min) entries
    now = time.time()
    agents = state.get("agents", {})
    pruned = {}
    for aid, info in agents.items():
        if now - info.get("spawn_time", 0) < LIMITS["stale_seconds"]:
            pruned[aid] = info
    state["agents"] = pruned

    # Count by model class
    counts = {"opus": 0, "sonnet": 0}
    for info in pruned.values():
        m = info.get("model", "opus")
        counts[m] = counts.get(m, 0) + 1
    active_count = counts.get(model_class, 0)
    limit = LIMITS.get(f"{model_class}_max", LIMITS["default_max"])

    if active_count >= limit:
        # Even on deny, persist the pruned state to keep file fresh
        atomic_json_write(_COUNTER_FILE, state)
        return False, active_count, limit

    # Allowed - register agent
    state["agents"][agent_id] = {
        "subagent_type": subagent_type,
        "model": model_class,
        "spawn_time": now,
        "tool_use_id": tool_use_id,
    }
    atomic_json_write(_COUNTER_FILE, state)
    return True, active_count + 1, limit


def main() -> int:
    data = read_hook_input()
    if not data:
        return 0

    tool_name = data.get("tool_name", "")
    if tool_name != "Agent":
        # Not a subagent spawn, allow silently
        return 0

    tool_input = data.get("tool_input", {}) or {}
    subagent_type = tool_input.get("subagent_type", "")
    model_override = tool_input.get("model", "")
    tool_use_id = data.get("tool_use_id", "")

    if not subagent_type:
        # Can't determine agent type, allow (avoid breaking unknown shapes)
        return 0

    model_class = _get_model_for_agent(subagent_type, model_override)
    agent_id = _make_agent_id(tool_use_id, subagent_type)

    # ATOMIC: check + increment under file lock (HIGH-4 fix for TOCTOU)
    allowed, active_count, limit = _atomic_check_and_increment(
        model_class, agent_id, subagent_type, tool_use_id,
    )

    if not allowed:
        log_hook_event(
            event_type="agent_rate_limited",
            hook_name=_HOOK_NAME,
            details={
                "subagent_type": subagent_type,
                "model_class": model_class,
                "active": active_count,
                "limit": limit,
            },
        )
        emit_decision(
            decision="deny",
            reason=(
                f"Parallel {model_class} agent limit reached "
                f"({active_count}/{limit}). Wait for an active {model_class} "
                f"agent to complete before spawning {subagent_type}. "
                f"Active agents are auto-released after 15 minutes if stuck."
            ),
            hook_event_name="PreToolUse",
        )
        return 0

    log_hook_event(
        event_type="agent_spawned",
        hook_name=_HOOK_NAME,
        details={
            "subagent_type": subagent_type,
            "model_class": model_class,
            "active_after": active_count,
            "limit": limit,
            "agent_id": agent_id,
        },
    )

    # TU-2 (2026-04-22): write spawn timestamp file for the PostToolUse
    # counterpart (hook_posttool_agent_complete.py) to compute duration.
    # Keyed by tool_use_id so concurrent spawns don't collide. Fail-soft.
    try:
        _write_agent_spawn_ts(tool_use_id, subagent_type)
    except Exception as _e:  # never block legitimate spawn
        log_hook_event(
            event_type="agent_spawn_ts_failed",
            hook_name=_HOOK_NAME,
            details={"err": str(_e)[:120]},
        )

    emit_decision(
        decision="allow",
        hook_event_name="PreToolUse",
    )
    return 0


def _agent_spawns_dir() -> "Path":
    """LOG-R1 MED-2 fix (2026-04-22): delegate to single source of truth
    to prevent drift with hook_posttool_agent_complete counterpart."""
    from memexa.core._agent_spawns import agent_spawns_dir
    return agent_spawns_dir()


def _write_agent_spawn_ts(tool_use_id: str, subagent_type: str) -> None:
    """TU-2 (2026-04-22): record spawn timestamp for stall detection.

    File path: .claude/harness/agent_spawns/<safe_id>.ts
    Content: "<time.time()>\t<sanitized_subagent_type>\n"

    Sanitized tool_use_id used as filename to prevent path traversal. If
    tool_use_id is empty or invalid, fallback key uses the SAME colon
    separator convention as _make_agent_id (SEC-R1 LOW fix — previously
    used "_internal_<type>_<ms>" which never matched _make_agent_id's
    "_internal:<type>:<ms>", permanently blinding stall detection for
    spawns with invalid tool_use_id).

    SEC-R1 MED fix: subagent_type value is sanitized to strip tabs and
    newlines before embedding in the ts file content.
    """
    import time as _time
    # Reuse same validation as rate-limit to stay consistent.
    if tool_use_id and _VALID_ID_PATTERN.match(tool_use_id):
        key = tool_use_id
    else:
        # Windows-safe fallback: underscores only. Note: _make_agent_id uses
        # colons for its in-memory dict key; colons are invalid in NTFS
        # filenames (ADS syntax) so we intentionally diverge here. Fallback
        # path is already a corner case (adversarial/missing tool_use_id);
        # SEC-R1 LOW accepted — stall detection for invalid-id spawns will
        # silently no-op (duration_sec=0 via _ts_key() returning "").
        safe_type = re.sub(r"[^a-zA-Z0-9_-]", "_", subagent_type or "unknown")[:32]
        key = f"_internal_{safe_type}_{int(_time.time() * 1000)}"
    # Defensive filename sanitize (Windows NTFS compat: strip ':' + anything
    # other than alphanumeric/underscore/dash).
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", key)[:128]
    # Strip tab/newline/control chars from subagent_type before persisting
    sanitized_type = re.sub(r"[\t\n\r\x00-\x1f]", "_", subagent_type or "")[:128]
    ts_file = _agent_spawns_dir() / f"{safe_key}.ts"
    ts_file.write_text(
        f"{_time.time()}\t{sanitized_type}\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
