"""
Auto-Trigger — Self-triggering evolution conditions.

Checks harness state at session start and returns a list of actions
that should be executed autonomously (no human confirmation needed).

Integrates with: harness_state.json, EventBus, SemanticMemory, config.yaml.

Usage (called by CTO at session start):
    actions = check_triggers()
    for action in actions:
        print(f"Auto-trigger: {action['type']} — {action['reason']}")
"""

import json
import os
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_WORKSPACE = Path(__file__).parent.parent.parent.parent  # claude workspace/
_HARNESS = _WORKSPACE / ".claude" / "config" / "harness_state.json"
_MEMEX = _WORKSPACE / "memex"


def _load_harness() -> Dict:
    if _HARNESS.exists():
        try:
            return json.loads(_HARNESS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _load_config() -> Dict:
    cfg_path = _MEMEX / "config.yaml"
    if cfg_path.exists():
        import yaml
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return {}


def _get_feature_flag(name: str, default: bool = False) -> bool:
    cfg = _load_config()
    return cfg.get("feature_flags", {}).get(name, default)


def _count_commits_since(ref: str) -> int:
    """Count commits since a given ref."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{ref}..HEAD"],
            capture_output=True, text=True, cwd=str(_MEMEX), timeout=10
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return 0


def _run_pytest_quick() -> Dict[str, Any]:
    """Get pytest results using shared cache (no redundant subprocess)."""
    try:
        from .pytest_cache import get_test_results
        return get_test_results()
    except Exception as e:
        return {"passed": 0, "failed": 0, "errors": 1, "success": False,
                "output": str(e)}


def check_triggers() -> List[Dict[str, Any]]:
    """Check all auto-trigger conditions. Returns list of actions to execute.

    Each action: {"type": str, "reason": str, "priority": int, "data": dict}
    Priority: 1=critical, 2=high, 3=normal, 4=low

    Side effect: auto-increments session counter each time this is called,
    so the dream trigger actually fires after enough sessions.
    """
    actions: List[Dict[str, Any]] = []
    harness = _load_harness()
    autonomy = harness.get("autonomy", {})

    if not autonomy.get("enabled", False):
        return actions

    # Rule 18 (2026-05-05) memory_runaway_guard: install daemon watchdog so
    # any long-running orchestrator step inherits commit-charge protection.
    # Idempotent (no-op if already installed). Fail-open if module missing.
    try:
        from src.core.memory_guardrail import MemoryGuardrail
        MemoryGuardrail.start_default()
    except Exception:
        pass

    # Auto-increment session counter — this is the missing link that
    # ensures autoDream eventually triggers. Each check_triggers() call
    # represents a new session start.
    increment_session_counter()

    # TU-A3 self_evolution_reconnect (2026-05-04): wire evolution_trigger.
    # Each session start fires check_and_trigger() to drive prompt evolver
    # when 10-session AND 72h thresholds met. Fail-soft: never blocks.
    try:
        from src.core.evolution_trigger import check_and_trigger as _evo_check
        _evo_result = _evo_check()
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("prompt_evolver_check_fired", {
                "triggered": _evo_result.get("triggered", False),
                "reason": _evo_result.get("reason", ""),
            })
        except Exception:  # pragma: no cover
            pass
    except Exception as _e:  # pragma: no cover
        # never block auto_trigger on evolution check failure
        pass

    # Reload harness to get updated counter
    harness = _load_harness()

    triggers = autonomy.get("auto_triggers", {})

    # 1. autoDream check
    if _get_feature_flag("auto_dream", False):
        dream_state = harness.get("auto_dream", {})
        sessions = dream_state.get("sessions_since_last_dream", 0)
        threshold = triggers.get("auto_dream_threshold", 5)
        if sessions >= threshold:
            actions.append({
                "type": "auto_dream",
                "reason": f"已累计 {sessions} 个会话（阈值 {threshold}），触发 autoDream 记忆整合",
                "priority": 3,
                "data": {"sessions": sessions, "threshold": threshold},
            })

    # 2. mini-loop check (commits since last MINI-LOOP, not last commit)
    # Using mini_loop.last_mini_loop_commit avoids the bug where last_commit
    # gets updated to HEAD on every session, making commits_since always 0.
    mini_loop_state = harness.get("mini_loop", {})
    last_loop_commit = mini_loop_state.get("last_mini_loop_commit", "")
    if not last_loop_commit:
        # Fallback: use last_commit from git_repos
        last_loop_commit = harness.get("git_repos", {}).get("memex", {}).get("last_commit", "")
    if last_loop_commit:
        commits_since = _count_commits_since(last_loop_commit)
        commit_threshold = triggers.get("mini_loop_commit_threshold", 10)
        if commits_since >= commit_threshold:
            actions.append({
                "type": "mini_loop",
                "reason": f"已累计 {commits_since} 个新 commit（阈值 {commit_threshold}），触发 mini-loop (含 big_loop Q1-Q4)",
                "priority": 2,
                "data": {"commits_since": commits_since, "threshold": commit_threshold,
                         "last_loop_commit": last_loop_commit,
                         "big_loop": True},
            })

    # 3. Test health check (always run at session start)
    if triggers.get("auto_fix_on_test_fail", False):
        test_result = _run_pytest_quick()
        if not test_result["success"]:
            actions.append({
                "type": "auto_fix_tests",
                "reason": f"测试失败: {test_result['failed']} failed, {test_result['errors']} errors",
                "priority": 1,
                "data": test_result,
            })
        else:
            actions.append({
                "type": "tests_healthy",
                "reason": f"测试通过: {test_result['passed']} passed",
                "priority": 4,
                "data": test_result,
            })

    # 4. Semantic memory consolidation check
    try:
        from .semantic_memory import get_semantic_memory, CONSOLIDATION_THRESHOLD
        sm = get_semantic_memory()
        if sm._episodes_since_consolidation >= CONSOLIDATION_THRESHOLD:
            actions.append({
                "type": "consolidate_memory",
                "reason": f"语义记忆待整合: {sm._episodes_since_consolidation} episodes 待 consolidation",
                "priority": 3,
                "data": {"pending": sm._episodes_since_consolidation},
            })
    except Exception:
        pass

    # 5. EventBus error pattern check
    if triggers.get("auto_postmortem_error_threshold", 0) > 0:
        try:
            from .event_bus import read_events
            recent = read_events(last_n=100)
            error_events = [e for e in recent if "fail" in e.get("type", "").lower()
                           or "error" in e.get("type", "").lower()]
            threshold = triggers["auto_postmortem_error_threshold"]
            if len(error_events) >= threshold:
                actions.append({
                    "type": "auto_postmortem",
                    "reason": f"最近 100 事件中 {len(error_events)} 个错误（阈值 {threshold}），建议 post-mortem",
                    "priority": 2,
                    "data": {"error_count": len(error_events)},
                })
        except Exception:
            pass

    # 6. Pending CEO approvals
    try:
        from .approval_queue import get_pending
        pending = get_pending()
        if pending:
            l3_count = sum(1 for p in pending if p["level"] == "L3")
            l2_count = sum(1 for p in pending if p["level"] == "L2")
            actions.append({
                "type": "pending_approvals",
                "reason": f"审批队列: {l3_count} 阻塞 + {l2_count} 挂起 待 CEO 审批",
                "priority": 1 if l3_count > 0 else 3,
                "data": {"l3": l3_count, "l2": l2_count, "items": pending},
            })
    except Exception:
        pass

    # 7. Pending agent specs from big_loop (CTO should invoke these)
    specs_file = _MEMEX / "memex" / "data" / "pending_agent_specs.json"
    if specs_file.exists():
        try:
            specs = json.loads(specs_file.read_text(encoding="utf-8"))
            if specs:
                actions.append({
                    "type": "pending_agent_specs",
                    "reason": f"大循环待执行: {len(specs)} 个 Agent 任务 (Q2/Q3/Q5/Q6) 等待 CTO 调度",
                    "priority": 2,
                    "data": {"count": len(specs), "agents": [s.get("agent", "?") for s in specs]},
                })
        except Exception:
            pass

    # 8. Always queue briefing-agent for SessionStart summary
    actions.append({
        "type": "session_briefing",
        "reason": "SessionStart: 调度 briefing-agent 生成状态报告",
        "priority": 5,  # Low priority — runs after all checks
        "data": {"agent": "briefing-agent", "trigger": "session_start"},
    })

    # Sort by priority
    actions.sort(key=lambda a: a["priority"])
    return actions


def format_briefing(actions: List[Dict]) -> str:
    """Format trigger check results as a briefing string."""
    if not actions:
        return "所有系统正常，无自动触发项。"

    lines = ["## 自触发检查结果\n"]
    priority_icons = {1: "🔴", 2: "🟠", 3: "🟡", 4: "🟢"}
    for a in actions:
        icon = priority_icons.get(a["priority"], "⚪")
        lines.append(f"{icon} **{a['type']}** — {a['reason']}")

    critical = [a for a in actions if a["priority"] <= 2]
    if critical:
        lines.append(f"\n需要立即处理的项目: {len(critical)} 个")

    return "\n".join(lines)


def increment_session_counter():
    """Increment sessions_since_last_dream in harness_state.

    Uses a session token (CLAUDE_SESSION_ID or process start time) to prevent
    double-counting when PostCompact re-invokes check_triggers() in the same session.
    """
    # Deduplicate: only increment once per logical session
    session_token = os.environ.get("CLAUDE_SESSION_ID", str(os.getpid()))
    harness = _load_harness()
    if "auto_dream" not in harness:
        harness["auto_dream"] = {"sessions_since_last_dream": 0, "dream_threshold": 5}

    last_token = harness["auto_dream"].get("last_session_token", "")
    if last_token == session_token:
        return  # Already counted this session

    harness["auto_dream"]["sessions_since_last_dream"] = \
        harness["auto_dream"].get("sessions_since_last_dream", 0) + 1
    harness["auto_dream"]["last_session_token"] = session_token
    harness["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _HARNESS.parent.mkdir(parents=True, exist_ok=True)
    _HARNESS.write_text(json.dumps(harness, indent=2, ensure_ascii=False), encoding="utf-8")


def reset_dream_counter():
    """Reset sessions_since_last_dream after autoDream completes."""
    harness = _load_harness()
    if "auto_dream" in harness:
        harness["auto_dream"]["sessions_since_last_dream"] = 0
        harness["auto_dream"]["last_dream_time"] = datetime.utcnow().isoformat() + "Z"
    harness["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _HARNESS.parent.mkdir(parents=True, exist_ok=True)
    _HARNESS.write_text(json.dumps(harness, indent=2, ensure_ascii=False), encoding="utf-8")


def reset_mini_loop_counter():
    """Reset mini-loop commit counter after a mini-loop completes."""
    harness = _load_harness()
    # Record current HEAD as the new baseline
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(_MEMEX), timeout=5,
        )
        current_head = result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        current_head = ""

    if "mini_loop" not in harness:
        harness["mini_loop"] = {}
    harness["mini_loop"]["last_mini_loop_commit"] = current_head
    harness["mini_loop"]["last_mini_loop_time"] = datetime.utcnow().isoformat() + "Z"
    harness["mini_loop"]["total_loops"] = harness["mini_loop"].get("total_loops", 0) + 1
    harness["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _HARNESS.parent.mkdir(parents=True, exist_ok=True)
    _HARNESS.write_text(json.dumps(harness, indent=2, ensure_ascii=False), encoding="utf-8")


def record_autopilot_completion(task_type: str, summary: str, tests_passed: int = 0):
    """Record an autopilot completion in harness_state for learning tracking.

    Called at the end of every autopilot run (Stage 5.4) to ensure
    the self-evolution pipeline has accurate data.
    """
    harness = _load_harness()

    # Update last_session
    harness["last_session"] = {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "status": "complete",
        "task_type": task_type,
        "summary": summary[:300],
    }

    # Update test count
    if tests_passed > 0:
        harness.setdefault("infrastructure", {})["tests"] = tests_passed

    # Update git HEAD
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(_MEMEX), timeout=5,
        )
        if result.returncode == 0:
            harness.setdefault("git_repos", {}).setdefault("memex", {})["last_commit"] = result.stdout.strip()
    except Exception:
        pass

    harness["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _HARNESS.parent.mkdir(parents=True, exist_ok=True)
    _HARNESS.write_text(json.dumps(harness, indent=2, ensure_ascii=False), encoding="utf-8")
