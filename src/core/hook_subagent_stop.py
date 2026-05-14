"""SubagentStop hook: decrement rate-limit counter + monitor output size.

Pairs with hook_pretool_agent.py: when a subagent finishes, release its
slot in the active counter so other agents can spawn.

Also monitors output size and logs OOM warnings.

Hook input:
    {
      "hook_event_name": "SubagentStop",
      "agent_id": "...",
      "agent_type": "chief-researcher|...",
      "agent_transcript_path": "...jsonl",
      "last_assistant_message": "..."
    }

Limitation: Cannot modify the subagent's output that Claude already sees.
This hook is observation + cleanup only.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core._hook_utils import (  # noqa: E402
    read_hook_input,
    emit_decision,
    log_hook_event,
    atomic_json_write,
    safe_load_json,
    get_workspace_paths,
)


_HOOK_NAME = "subagent_stop"
_PATHS = get_workspace_paths()
_COUNTER_FILE = _PATHS["data"] / "agent_active_count.json"
_OUTPUT_STATS_FILE = _PATHS["data"] / "agent_output_stats.json"  # [Item #3 / V-6]

# Output size thresholds (in chars, ~3 chars/token estimate)
_OUTPUT_WARN_CHARS = 30000   # ~10K tokens
_OUTPUT_CRITICAL_CHARS = 100000  # ~33K tokens

# [Item #3 / V-6] Meta-pattern auto-emission
_META_CRITICAL_RATIO = 0.3      # Emit if >30% of runs are critical
_META_MIN_SAMPLES = 5           # Need ≥5 samples to emit
_META_MONTHLY_CAP = 3           # Max 3 meta-patterns per agent_type per 30d
_STATS_PRUNE_AGE_SEC = 7 * 86400  # Prune entries older than 7d

# [SEC-5] Allowlist of known agent_types (from .claude/agents/*.md roster).
# Auto-emitted meta-patterns must target a known agent_type to prevent
# attacker-controlled hook payloads injecting arbitrary high-confidence rules.
_KNOWN_AGENT_TYPES = frozenset({
    "architect", "verifier", "chief-researcher", "briefing",
    "strategic-advisor", "code-reviewer", "security-reviewer",
    "logic-reviewer", "coverage-reviewer", "qa-director",
    "sonnet-executor", "test-runner", "fix-agent", "gate-keeper",
    "release-gate", "knowledge-manager", "research-assistant",
    "reviewer-technical", "reviewer-narrative", "consistency-auditor",
    "chinese-doc-master", "physics-lab-report", "scope-validator",
    "prl-writing-master", "nature-writing-master", "spec-reviewer",
    "investigator", "style-messenger", "compute-runner",
    "statistics-specialist", "formatter", "coverage-reporter",
    "big-loop-orchestrator", "evolution",
})


def _release_slot(agent_id: str, agent_type: str) -> bool:
    """Remove agent from active counter using 3-strategy match.

    Optimistic concurrency (no file lock - locking caused Windows deadlock).
    L-H2/H3 fix retained: 3-strategy match for synthetic-vs-platform agent_id.
    """
    if not _COUNTER_FILE.exists():
        return False

    state = safe_load_json(_COUNTER_FILE, default={"agents": {}})
    agents = state.get("agents", {})
    if not agents:
        return False

    removed = False

    # Strategy 1: Direct key match
    if agent_id and agent_id in agents:
        agents.pop(agent_id, None)
        removed = True
    # Strategy 2: Match by stored tool_use_id field
    elif agent_id:
        matched = None
        for aid, info in agents.items():
            if info.get("tool_use_id") == agent_id:
                matched = aid
                break
        if matched:
            agents.pop(matched, None)
            removed = True
        elif agent_type:
            # Strategy 3: Oldest entry of matching subagent_type
            candidates = [
                (aid, info) for aid, info in agents.items()
                if info.get("subagent_type") == agent_type
            ]
            if candidates:
                candidates.sort(key=lambda x: x[1].get("spawn_time", 0))
                agents.pop(candidates[0][0], None)
                removed = True
    elif agent_type:
        candidates = [
            (aid, info) for aid, info in agents.items()
            if info.get("subagent_type") == agent_type
        ]
        if candidates:
            candidates.sort(key=lambda x: x[1].get("spawn_time", 0))
            agents.pop(candidates[0][0], None)
            removed = True

    if removed:
        atomic_json_write(_COUNTER_FILE, state)
    return removed


def _check_output_size(message: str) -> str:
    """Classify output size. Returns severity string or empty."""
    if not message:
        return ""
    size = len(message)
    if size >= _OUTPUT_CRITICAL_CHARS:
        return "critical"
    if size >= _OUTPUT_WARN_CHARS:
        return "warn"
    return ""


def _update_agent_output_stats(agent_type: str, size: int, severity: str) -> dict:
    """[Item #3] Update rolling stats for agent_type output sizes.

    Returns current stats dict for this agent_type after update.
    Prunes entries older than _STATS_PRUNE_AGE_SEC.
    """
    if not agent_type:
        return {}
    now = time.time()
    stats = safe_load_json(_OUTPUT_STATS_FILE, default={"agents": {}, "last_prune": now})
    # Prune weekly
    if now - stats.get("last_prune", 0) > _STATS_PRUNE_AGE_SEC:
        cutoff = now - _STATS_PRUNE_AGE_SEC
        for a_type, a_data in list(stats.get("agents", {}).items()):
            if a_data.get("last_seen", 0) < cutoff:
                stats["agents"].pop(a_type, None)
        stats["last_prune"] = now
    agent = stats.setdefault("agents", {}).setdefault(agent_type, {
        "total": 0, "warn": 0, "critical": 0, "total_chars": 0,
        "max_chars": 0, "last_seen": 0, "meta_pattern_emitted_count": 0,
    })
    agent["total"] += 1
    if severity == "warn":
        agent["warn"] += 1
    if severity == "critical":
        agent["critical"] += 1
    agent["total_chars"] += size
    agent["max_chars"] = max(agent["max_chars"], size)
    agent["last_seen"] = now
    atomic_json_write(_OUTPUT_STATS_FILE, stats)
    return agent


def _maybe_emit_meta_pattern(agent_type: str, stats: dict) -> bool:
    """[V-6] Emit auto_generated pattern if agent_type exceeds threshold.

    Guards:
      - [SEC-5] agent_type must be in _KNOWN_AGENT_TYPES allowlist
      - Requires ≥5 samples
      - Requires ≥30% critical rate
      - Monthly cap of 3 per agent_type (tracked in stats + KB count)
    """
    if not agent_type or not stats:
        return False
    # [SEC-5] Reject unknown agent_types (prevents hook-payload injection)
    if agent_type not in _KNOWN_AGENT_TYPES:
        return False
    total = stats.get("total", 0)
    critical = stats.get("critical", 0)
    if total < _META_MIN_SAMPLES:
        return False
    if critical / total < _META_CRITICAL_RATIO:
        return False
    # Monthly cap check: count recent auto_generated patterns for this agent_type
    # [LOGIC-H3] Malformed created_at must count AS cap-bearing (fail-closed),
    # not silently skip. Otherwise bad data lets the cap be bypassed indefinitely.
    try:
        from src.core.pattern_extractor import load_all_patterns
        from datetime import datetime as _dt
        recent_meta = 0
        cutoff_30d = _dt.now().timestamp() - 30 * 86400
        for p in load_all_patterns():
            if not getattr(p, "auto_generated", False):
                continue
            if agent_type not in (p.affected_services or []):
                continue
            try:
                created_ts = _dt.fromisoformat(p.created_at.replace("Z", "")).timestamp()
                if created_ts > cutoff_30d:
                    recent_meta += 1
            except (ValueError, AttributeError, TypeError):
                # Fail-closed: count as recent to avoid cap bypass on corrupt data
                recent_meta += 1
        if recent_meta >= _META_MONTHLY_CAP:
            return False
    except Exception:
        return False
    # Emit the meta-pattern
    try:
        from src.core.pattern_extractor import PatternEntry, Provenance, save_patterns
        from datetime import datetime as _dt
        from dataclasses import asdict
        avg_kchars = int(stats.get("total_chars", 0) / max(total, 1) / 1000)
        entry = PatternEntry(
            type="performance",
            fact=(f"agent_type '{agent_type}' returns oversized output: "
                  f"{critical}/{total} runs ≥100k chars, avg {avg_kchars}k, "
                  f"max {stats.get('max_chars', 0) // 1000}k."),
            recommendation=(f"Instruct agent '{agent_type}' to cap output to ≤20k chars; "
                             f"write large artifacts to archive/reports/ and return summary."),
            confidence="high",
            tags=["agent_output", "auto_generated", "subagent"],
            affected_files=[],
            affected_services=[agent_type],
            auto_generated=True,
            provenance=[asdict(Provenance(
                source="subagent_stop_stats",
                reference=f"total={total},critical={critical}",
                date=_dt.now().isoformat(),
            ))],
        )
        added = save_patterns([entry])
        return added > 0
    except Exception:
        return False


def _trigger_extract_from_session() -> int:
    """Run extract_from_session if review artifacts may have been updated.

    OOM-SAFE: throttle via mtime check on a marker file. Only run if last
    extraction was > 60s ago. Prevents 3 parallel reviewers from triggering
    3 simultaneous full reads of last_review*.json.
    """
    import time
    marker = _PATHS["data"] / ".extract_from_session.marker"
    now = time.time()
    try:
        if marker.exists():
            last_run = marker.stat().st_mtime
            if now - last_run < 60:  # Throttle to once per 60s
                return 0
    except OSError:
        pass

    try:
        from src.core.pattern_extractor import extract_from_session
        added = extract_from_session(max_patterns=3)
        # Update marker
        try:
            marker.touch()
        except OSError:
            pass
        return added
    except Exception:
        return 0


def main() -> int:
    data = read_hook_input()
    if not data:
        return 0

    agent_id = data.get("agent_id", "")
    agent_type = data.get("agent_type", "")
    last_msg = data.get("last_assistant_message", "")
    transcript_path = data.get("agent_transcript_path", "")

    # 1. Release rate-limit slot
    released = _release_slot(agent_id, agent_type)

    # 2. Monitor output size + update rolling stats + maybe emit meta-pattern
    size_severity = _check_output_size(last_msg)
    meta_emitted = False
    if agent_type and last_msg:
        stats = _update_agent_output_stats(agent_type, len(last_msg), size_severity)
        if size_severity == "critical":
            meta_emitted = _maybe_emit_meta_pattern(agent_type, stats)

    # 3. Try to extract patterns (review artifacts may have been written)
    patterns_added = 0
    if agent_type in ("logic-reviewer", "security-reviewer", "coverage-reviewer",
                       "code-reviewer", "verifier", "reviewer-technical",
                       "reviewer-narrative", "consistency-auditor"):
        patterns_added = _trigger_extract_from_session()

    # 4. Log everything
    log_hook_event(
        event_type="subagent_complete",
        hook_name=_HOOK_NAME,
        details={
            "agent_id": agent_id,
            "agent_type": agent_type,
            "slot_released": released,
            "output_size_chars": len(last_msg) if last_msg else 0,
            "size_severity": size_severity,
            "patterns_added": patterns_added,
            "meta_pattern_emitted": meta_emitted,
            "has_transcript": bool(transcript_path),
        },
    )

    # 5. Inject context for parent if size warning
    context = ""
    if size_severity == "critical":
        context = (
            f"[hook:subagent_stop] {agent_type} returned {len(last_msg)} chars "
            f"(>= 100K critical threshold). This agent may be ignoring output caps. "
            f"Consider re-prompting with stricter limits."
        )
    elif size_severity == "warn":
        context = (
            f"[hook:subagent_stop] {agent_type} returned {len(last_msg)} chars "
            f"(>= 30K warn threshold). Monitor for context bloat."
        )

    emit_decision(
        decision="allow",
        additional_context=context,
        hook_event_name="SubagentStop",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
