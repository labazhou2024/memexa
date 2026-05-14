"""
KAIROS Daemon v3 — Parallel project execution via async worker pool.

v1: 20 isolated one-shot tasks.
v2: Projects with multi-step sessions using --resume.
v3: Async worker pool for parallel project execution; CC reads CLAUDE.md
    natively so workflow suffix injection is removed.

Usage:
  python -m src.core.kairos_daemon              # Run daemon (async, 2 workers)
  python -m src.core.kairos_daemon --duration 60 # Auto-stop after 60min
  python -m src.core.kairos_daemon --once        # One project and exit
  python -m src.core.kairos_daemon --workers 3   # 3 parallel workers
"""

import json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .error_classifier import (
    ErrorCategory,
    classify_error,
    get_delay_for_attempt,
)

logger = logging.getLogger(__name__)

_MEMEXA_ROOT = Path(__file__).parent.parent.parent
_WORKSPACE = _MEMEXA_ROOT.parent
_DATA = Path(__file__).parent.parent / "data"
_PROJECTS_FILE = _DATA / "pending_projects.json"
_REPORTS_DIR = _DATA / "project_reports"
_LOG_FILE = _DATA / "kairos_daemon.log"

# Also keep backward compat with pending_tasks.json
_TASKS_FILE = _DATA / "pending_tasks.json"

# Claude CLI path — cached for subprocess reliability
_CLAUDE_PATH_CACHE = _DATA / ".claude_path"
_CLAUDE_CMD = shutil.which("claude") or str(Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd")
if _CLAUDE_CMD and Path(_CLAUDE_CMD).exists():
    _DATA.mkdir(parents=True, exist_ok=True)
    _CLAUDE_PATH_CACHE.write_text(_CLAUDE_CMD, encoding="utf-8")
elif _CLAUDE_PATH_CACHE.exists():
    _CLAUDE_CMD = _CLAUDE_PATH_CACHE.read_text(encoding="utf-8").strip()

# Config
POLL_INTERVAL = 30
DEFAULT_MODEL = "opus"
DEFAULT_BUDGET = 100.00  # Max subscription — no budget-related failures
MAX_FAILURES = 2

# Two execution modes:
#   "workflow" (default): Full multi-agent pipeline. CC reads CLAUDE.md natively
#                         which contains workflow rules — no suffix injection needed.
#   "quick": Single-shot execution for reporting, metrics, read-only tasks. Low cost.
DEFAULT_MODE = "workflow"

# Thread lock for file I/O safety (multiple workers may call load/save concurrently)
_file_lock = threading.Lock()


def _load_projects() -> List[Dict]:
    with _file_lock:
        if _PROJECTS_FILE.exists():
            try:
                return json.loads(_PROJECTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []


def _save_projects(projects: List[Dict]):
    with _file_lock:
        _DATA.mkdir(parents=True, exist_ok=True)
        _PROJECTS_FILE.write_text(
            json.dumps(projects, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def submit_project(
    title: str,
    prompt: str,
    priority: int = 3,
    model: str = DEFAULT_MODEL,
    max_budget_usd: float = DEFAULT_BUDGET,
    max_turns: int = 50,
    mode: str = DEFAULT_MODE,
) -> str:
    """Submit a project to KAIROS.

    Args:
        mode: "workflow" (default) = full multi-agent pipeline (A→B→C→D),
              "quick" = single-shot read-only task.

    Workflow mode: Opus decomposes, sub-agents execute, review+test+gate enforced.
    Quick mode: Sonnet single-shot, low budget, no code changes expected.
    """
    projects = _load_projects()

    # Dedup: skip if title already exists
    if any(p.get("title") == title for p in projects):
        logger.info("Skipping duplicate project: %s", title[:50])
        return ""

    proj_id = f"proj_{int(time.time())}_{len(projects):03d}"

    # Apply mode defaults (CC reads CLAUDE.md natively — no suffix injection)
    if mode == "quick":
        model = model if model != DEFAULT_MODEL else "sonnet"
        max_budget_usd = min(max_budget_usd, 10.00)
        max_turns = min(max_turns, 15)
    full_prompt = prompt

    project = {
        "id": proj_id,
        "title": title,
        "prompt": full_prompt,
        "priority": min(5, max(1, priority)),
        "status": "pending",
        "submitted_at": datetime.utcnow().isoformat() + "Z",
        "model": model,
        "mode": mode,
        "max_budget_usd": max_budget_usd,
        "max_turns": max_turns,
        "session_id": None,
        "steps_completed": 0,
        "total_cost_usd": 0,
        "result": None,
        "completed_at": None,
        "failure_count": 0,
    }

    projects.append(project)
    _save_projects(projects)

    try:
        from .event_bus import log_event
        log_event("project_submitted", agent="kairos", details={
            "project_id": proj_id, "title": title[:60], "priority": priority,
        })
    except Exception:
        pass

    logger.info("Project submitted: %s — %s", proj_id, title[:60])
    return proj_id


def _has_pending_projects() -> bool:
    """Non-destructive check: are there any pending projects?

    Unlike get_next_project(), this does NOT mark anything as dispatched.
    """
    with _file_lock:
        if _PROJECTS_FILE.exists():
            try:
                projects = json.loads(_PROJECTS_FILE.read_text(encoding="utf-8"))
                return any(
                    p.get("status") == "pending"
                    and p.get("failure_count", 0) < MAX_FAILURES
                    for p in projects
                )
            except Exception:
                return False
        return False


def get_next_project() -> Optional[Dict]:
    """Get highest priority pending project and atomically mark it as 'dispatched'.

    The entire read-modify-write is done under _file_lock to prevent the same
    project from being dispatched to multiple workers in a tight loop.
    """
    with _file_lock:
        if _PROJECTS_FILE.exists():
            try:
                projects = json.loads(_PROJECTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                return None
        else:
            return None

        pending = [
            p for p in projects
            if p.get("status") == "pending"
            and p.get("failure_count", 0) < MAX_FAILURES
        ]
        if not pending:
            return None
        pending.sort(key=lambda p: (-p.get("priority", 3), p.get("submitted_at", "")))
        chosen = pending[0]

        # Atomically mark as dispatched within the same lock
        for p in projects:
            if p["id"] == chosen["id"]:
                p["status"] = "dispatched"
                break
        _DATA.mkdir(parents=True, exist_ok=True)
        _PROJECTS_FILE.write_text(
            json.dumps(projects, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return chosen


# Permission model (v2.2 — balanced autonomy):
# KAIROS subprocesses run unattended, so --permission-mode auto blocks on
# file writes with no interactive user to approve. KAIROS already enforces
# safety through its own 4-layer model:
#   1. Pipeline state tracking (Phase A->D)
#   2. Code review (local_reviewer in Phase C)
#   3. Gate keeper (Phase D syntax/import/security checks)
#   4. CEO approval queue (L2/L3 for high-risk operations)
# Therefore we use --permission-mode bypass for autonomous execution,
# with memexa hooks providing the actual safety enforcement.

def _invoke_claude(prompt: str, model: str, budget: float,
                   max_turns: int, resume_session: Optional[str] = None) -> Dict:
    """Core claude invocation. Returns parsed result dict.

    Permission model (v2.2 AUTONOMOUS):
      - KAIROS subprocesses: --permission-mode bypass (unattended execution)
      - Safety enforced by: WORKFLOW pipeline + code review + gate keeper + approval queue
    """
    cmd = [
        _CLAUDE_CMD,
        "-p",
        "--output-format", "json",
        "--model", model,
        "--max-budget-usd", str(budget),
    ]

    cmd.extend(["--permission-mode", "bypassPermissions"])

    cmd.extend([
        "--max-turns", str(max_turns),
        "--add-dir", str(_WORKSPACE),
    ])

    if resume_session:
        cmd.extend(["--resume", resume_session])

    start = time.time()

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            cwd=str(_WORKSPACE),
        )

        duration = time.time() - start
        parsed = {}
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            pass

        output_text = parsed.get("result", result.stdout[:3000])

        return {
            "success": result.returncode == 0 and parsed.get("subtype") == "success",
            "output": output_text[:5000],
            "duration_seconds": round(duration, 2),
            "session_id": parsed.get("session_id"),
            "cost_usd": parsed.get("total_cost_usd", 0),
            "num_turns": parsed.get("num_turns", 0),
            "stop_reason": parsed.get("stop_reason", ""),
            "model_used": list(parsed.get("modelUsage", {}).keys())[0] if parsed.get("modelUsage") else model,
            "error": result.stderr[:500] if result.returncode != 0 else None,
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "duration_seconds": round(time.time() - start, 2),
                "error": "Timeout after 600s", "session_id": None, "cost_usd": 0, "num_turns": 0}
    except FileNotFoundError:
        return {"success": False, "output": "", "duration_seconds": 0,
                "error": f"Claude CLI not found: {_CLAUDE_CMD}", "session_id": None, "cost_usd": 0, "num_turns": 0}
    except Exception as e:
        return {"success": False, "output": "", "duration_seconds": round(time.time() - start, 2),
                "error": str(e), "session_id": None, "cost_usd": 0, "num_turns": 0}


def _inject_patterns_and_lessons(project: Dict, prompt: str) -> str:
    """Inject semantic patterns (W3) and structured lessons into prompt."""
    proj_id = project.get("id", "")

    # W3: Inject relevant semantic patterns
    try:
        from .semantic_memory import get_semantic_memory
        sm = get_semantic_memory()
        task_desc = project.get("title", "") + " " + prompt[:200]
        patterns = sm.retrieve(task_desc, top_k=3)
        if patterns:
            pattern_lines = ["[Historical patterns from past executions — apply if relevant]"]
            injected_ids = []
            for p in patterns:
                pattern_lines.append(f"- [{p.confidence:.0%}] {p.rule}")
                injected_ids.append(p.pattern_id)
            prompt = prompt + "\n\n" + "\n".join(pattern_lines) + "\n"
            project["injected_pattern_ids"] = injected_ids
            project["had_patterns"] = True
            logger.info("W3: Injected %d patterns into project %s", len(patterns), proj_id)
        else:
            project["had_patterns"] = False
    except Exception as e:
        logger.debug("W3 pattern injection skipped: %s", e)
        project["had_patterns"] = False

    # Lesson injection: retrieve relevant lessons from past experience
    try:
        from .lesson_store import get_lesson_store
        ls = get_lesson_store()
        task_context = project.get("title", "") + " " + prompt[:300]
        lessons = ls.retrieve_lessons(task_context, top_k=3)
        if lessons:
            lesson_block = ls.format_lessons_for_prompt(lessons)
            prompt = prompt + "\n\n" + lesson_block + "\n"
            project["injected_lesson_ids"] = [l.lesson_id for l in lessons]
            logger.info("Lessons: Injected %d lessons into project %s", len(lessons), proj_id)
        else:
            project["injected_lesson_ids"] = []
    except Exception as e:
        logger.debug("Lesson injection skipped: %s", e)
        project["injected_lesson_ids"] = []

    return prompt


def _self_heal_and_retry(project: Dict, result: Dict, prompt: str,
                          model: str, budget: float, max_turns: int) -> Dict:
    """Attempt self-healing based on error classification.

    Returns the final result after retry attempts (or the original if no retry).
    """
    error_msg = result.get("error") or ""
    error_type = ""
    if "Timeout" in error_msg:
        error_type = "timeout"
    elif "FileNotFoundError" in error_msg:
        error_type = "file_not_found"

    category, strategy = classify_error(error_msg, error_type)
    proj_id = project.get("id", "")

    logger.info("Error classified: %s → %s (retry=%s, delay=%.0fs)",
                proj_id, category.value, strategy.should_retry, strategy.delay_seconds)

    # Store classification in result for dashboard visibility
    result["error_category"] = category.value
    result["error_strategy"] = strategy.strategy_name

    if not strategy.should_retry:
        if strategy.fix_suggestion:
            logger.info("Fix suggestion for %s: %s", proj_id, strategy.fix_suggestion)
            result["fix_suggestion"] = strategy.fix_suggestion
        return result

    # RESOURCE errors: check if we should pause the entire daemon
    if category == ErrorCategory.RESOURCE:
        if strategy.wait_until:
            logger.warning("RESOURCE limit hit for %s. Reset at %s. Pausing.",
                           proj_id, strategy.wait_until)
            result["resource_wait_until"] = strategy.wait_until
        return result  # Caller handles pause

    # TRANSIENT errors: retry with exponential backoff
    session_id = result.get("session_id") or project.get("session_id")
    for attempt in range(strategy.max_retries):
        delay = get_delay_for_attempt(strategy, attempt)
        logger.info("Retry %d/%d for %s in %.0fs (%s)",
                    attempt + 1, strategy.max_retries, proj_id, delay, strategy.strategy_name)
        time.sleep(delay)

        # For timeout errors, increase the subprocess timeout
        retry_result = _invoke_claude(prompt, model, budget, max_turns,
                                       resume_session=session_id)
        if retry_result.get("success"):
            logger.info("Self-healed %s on retry %d", proj_id, attempt + 1)
            retry_result["self_healed"] = True
            retry_result["heal_attempts"] = attempt + 1
            retry_result["original_error"] = error_msg[:200]
            return retry_result

        # If same error category, continue retrying; if different, re-classify
        new_cat, new_strat = classify_error(
            retry_result.get("error") or "", "", {}
        )
        if new_cat == ErrorCategory.RESOURCE:
            retry_result["error_category"] = new_cat.value
            retry_result["resource_wait_until"] = new_strat.wait_until
            return retry_result  # Escalate to resource handling
        if new_cat == ErrorCategory.PERMANENT:
            break  # Don't retry permanent errors

    return result  # All retries exhausted


def _extract_agent_name_from_title(title: str) -> Optional[str]:
    """Extract agent name from BigLoop project title like '[BigLoop qa-director] ...'."""
    import re
    m = re.search(r"\[BigLoop\s+([\w-]+)\]", title)
    if not m:
        return None
    name = m.group(1)
    # Map "FULL" to dedicated orchestrator agent
    if name == "FULL":
        return "big-loop-orchestrator"
    return name


def _load_agent_definition(agent_name: str) -> str:
    """Load full agent .md definition (up to 4000 chars, frontmatter stripped).

    This ensures KAIROS-executed projects get the same specialized instructions
    that Claude Code's built-in Agent tool would provide.
    """
    if "/" in agent_name or ".." in agent_name or "\\" in agent_name:
        return ""
    agent_file = _WORKSPACE / ".claude" / "agents" / f"{agent_name}.md"
    if not agent_file.exists():
        return ""
    try:
        content = agent_file.read_text(encoding="utf-8")
        # Strip YAML frontmatter
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()[:4000]
        return content[:4000]
    except Exception:
        return ""


def execute_project(project: Dict) -> Dict:
    """Execute a project with self-healing error recovery.

    Flow:
    1. Inject agent definition (if BigLoop agent project)
    2. Inject patterns + lessons into prompt
    3. Execute via Claude CLI
    4. On failure: classify error → self-heal (retry/wait/fix) → escalate only if unrecoverable
    5. On success: collect feedback + generate lessons
    """
    proj_id = project["id"]
    prompt = project["prompt"]
    model = project.get("model", DEFAULT_MODEL)
    budget = project.get("max_budget_usd", DEFAULT_BUDGET)
    max_turns = project.get("max_turns", 50)  # Raised from 25 to reduce 44% retry rate

    logger.info("Executing project %s: %s", proj_id, project.get("title", "")[:50])

    # Inject agent .md definition for BigLoop agent projects
    title = project.get("title", "")
    agent_name = _extract_agent_name_from_title(title)
    if agent_name:
        agent_def = _load_agent_definition(agent_name)
        if agent_def:
            prompt = f"=== Agent Definition ({agent_name}) ===\n{agent_def}\n=== End Agent Definition ===\n\n{prompt}"
            logger.info("Injected agent definition for %s (%d chars)", agent_name, len(agent_def))

    # Inject patterns (W3) and lessons into prompt
    prompt = _inject_patterns_and_lessons(project, prompt)

    # Mark running (from dispatched or pending)
    projects = _load_projects()
    for p in projects:
        if p["id"] == proj_id:
            p["status"] = "running"
            p["started_at"] = datetime.utcnow().isoformat() + "Z"
    _save_projects(projects)
    logger.info("Project %s marked running", proj_id[:20])

    # Step 1: Initial execution
    session_id = project.get("session_id")
    result = _invoke_claude(prompt, model, budget, max_turns, resume_session=session_id)

    total_cost = result.get("cost_usd", 0)
    total_turns = result.get("num_turns", 0)
    all_output = result.get("output", "")

    # Self-healing: if failed, classify and attempt recovery
    if not result.get("success") and result.get("error"):
        healed = _self_heal_and_retry(project, result, prompt, model, budget, max_turns)
        if healed.get("success"):
            result = healed
            total_cost += healed.get("cost_usd", 0)
            total_turns += healed.get("num_turns", 0)
            all_output += "\n\n--- SELF-HEALED ---\n" + healed.get("output", "")
        else:
            # Copy error classification to result
            result["error_category"] = healed.get("error_category", "UNKNOWN")
            result["error_strategy"] = healed.get("error_strategy", "")
            result["fix_suggestion"] = healed.get("fix_suggestion", "")
            result["resource_wait_until"] = healed.get("resource_wait_until", "")

    # Auto-continue if hit max_turns (incomplete work, regardless of success flag)
    # error_max_turns means the project ran out of turns — it's incomplete, not failed
    hit_max_turns = (
        result.get("stop_reason") == "max_turns"
        or result.get("stop_reason") == "error_max_turns"
        or "max_turns" in (result.get("error") or "")
    )
    if hit_max_turns and result.get("session_id"):
        logger.info("Project %s hit max_turns, auto-continuing with +50%% turns...", proj_id)
        continuation_turns = int(max_turns * 1.5)  # Give more headroom for continuation
        continuation = _invoke_claude(
            "Continue the previous task. Complete any remaining work, then run tests and commit.",
            model, budget, continuation_turns,
            resume_session=result["session_id"],
        )
        total_cost += continuation.get("cost_usd", 0)
        total_turns += continuation.get("num_turns", 0)
        all_output += "\n\n--- CONTINUATION ---\n" + continuation.get("output", "")
        result["session_id"] = continuation.get("session_id") or result.get("session_id")
        # If continuation succeeded, override the failure
        if continuation.get("success"):
            result["success"] = True
            result["stop_reason"] = continuation.get("stop_reason", "")

    # Build final result
    final_result = {
        "success": result.get("success", False),
        "output": all_output[:8000],
        "duration_seconds": result.get("duration_seconds", 0),
        "session_id": result.get("session_id"),
        "cost_usd": round(total_cost, 4),
        "num_turns": total_turns,
        "model_used": result.get("model_used", model),
        "error": result.get("error"),
        "error_category": result.get("error_category", ""),
        "error_strategy": result.get("error_strategy", ""),
        "fix_suggestion": result.get("fix_suggestion", ""),
        "self_healed": result.get("self_healed", False),
        "heal_attempts": result.get("heal_attempts", 0),
    }

    # Update project
    projects = _load_projects()
    for p in projects:
        if p["id"] == proj_id:
            if final_result["success"]:
                p["status"] = "completed"
                p["completed_at"] = datetime.utcnow().isoformat() + "Z"
            else:
                p["failure_count"] = p.get("failure_count", 0) + 1
                debug_depth = p.get("debug_depth", 0)
                if p["failure_count"] >= MAX_FAILURES:
                    p["status"] = "failed"
                    p["completed_at"] = datetime.utcnow().isoformat() + "Z"
                    # R6: On permanent failure, submit debug sub-project if depth allows
                    if debug_depth < 3:
                        _submit_debug_project(project, final_result, debug_depth)
                else:
                    p["status"] = "pending"
                    logger.warning("Project %s failed (attempt %d/%d), will retry.",
                                   proj_id, p["failure_count"], MAX_FAILURES)
            p["result"] = final_result
            p["session_id"] = final_result.get("session_id")
            p["total_cost_usd"] = round(p.get("total_cost_usd", 0) + total_cost, 4)
    _save_projects(projects)

    # Save report
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = {"project": project, "result": final_result,
              "executed_at": datetime.utcnow().isoformat() + "Z"}
    (_REPORTS_DIR / f"{proj_id}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # R3: Save full trajectory (complete stdout for trace-aware evolution)
    _TRAJ_DIR = _DATA / "trajectories"
    _TRAJ_DIR.mkdir(parents=True, exist_ok=True)
    try:
        traj = {
            "project_id": proj_id,
            "title": project.get("title", ""),
            "prompt": project.get("prompt", "")[:2000],
            "model": model,
            "full_output": all_output,  # Complete, untruncated output
            "success": final_result["success"],
            "quality_score": final_result.get("quality_report", {}).get("score", 0),
            "cost_usd": total_cost,
            "num_turns": total_turns,
            "duration_seconds": final_result.get("duration_seconds", 0),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        traj_file = _TRAJ_DIR / f"{proj_id}.json"
        traj_file.write_text(json.dumps(traj, ensure_ascii=False, indent=2), encoding="utf-8")
        final_result["trajectory_file"] = str(traj_file)
        logger.info("Trajectory saved: %s (%d chars)", traj_file.name, len(all_output))
    except Exception as e:
        logger.debug("Trajectory save failed: %s", e)

    try:
        from .event_bus import log_event
        log_event("project_completed", agent="kairos", details={
            "project_id": proj_id, "success": final_result["success"],
            "cost": total_cost, "turns": total_turns,
        })
    except Exception:
        pass

    # Route A: Real quality verification FIRST (low-noise multi-signal)
    # Must run BEFORE collect_feedback so quality_report is available for scoring
    try:
        from .real_quality_verifier import verify_quality_sync
        qr = verify_quality_sync(project, final_result)
        logger.info("Quality: %s (%.2f) — %s", qr.verdict, qr.score, qr.details[:100])
        final_result["quality_report"] = qr.to_dict()

        # Store quality score in event bus for evolution consumption
        from .event_bus import log_event
        log_event("quality_verified", agent="real_quality_verifier", details={
            "project_id": proj_id,
            "score": qr.score,
            "score_1_5": qr.score_1_5,
            "verdict": qr.verdict,
            "signals": qr.signals,
        })
    except Exception as e:
        logger.debug("Quality verification skipped: %s", e)

    # W1/W2/W5: Feed result to evolution stack via feedback_collector
    # (now has quality_report from real_quality_verifier above)
    try:
        from .feedback_collector import collect_feedback
        feedback = collect_feedback(project, final_result)
        logger.info("Feedback: score=%d, w1=%s, w2=%s, w5=%s",
                     feedback.get("quality_score", 0),
                     feedback.get("w1_episode_recorded"),
                     feedback.get("w2_event_logged"),
                     feedback.get("w5_low_quality_tagged"))
    except Exception as e:
        logger.debug("Feedback collection skipped: %s", e)

    # Lesson learning: generate lessons from outcome + track injected lesson outcomes
    try:
        from .lesson_store import get_lesson_store
        ls = get_lesson_store()

        # Generate lesson from this execution
        if final_result["success"]:
            ls.generate_lesson_from_success(project, final_result)
        else:
            error_cat = final_result.get("error_category", "UNKNOWN")
            ls.generate_lesson_from_failure(
                project, final_result.get("error") or "", error_cat
            )

        # Track outcomes of injected lessons
        for lid in project.get("injected_lesson_ids", []):
            ls.record_outcome(lid, final_result["success"])

        logger.info("Lessons: generated + tracked for %s", proj_id)
    except Exception as e:
        logger.debug("Lesson learning skipped: %s", e)

    # Notify CEO on permanent failure (2 attempts exhausted)
    _notify_on_failure(project, final_result)

    status = "completed" if final_result["success"] else "FAILED"
    logger.info("Project %s %s in %.1fs ($%.3f, %d turns)",
                proj_id, status, final_result["duration_seconds"], total_cost, total_turns)

    return final_result


def _submit_debug_project(original_project: Dict, result: Dict, current_depth: int):
    """R6: Submit a debug sub-project that analyzes failure and attempts targeted fix.

    Instead of blindly retrying the same prompt, generates a debug prompt that
    includes the error output and asks for root cause analysis + fix.
    Inspired by AI-Scientist-v2's max_debug_depth mechanism.
    """
    title = original_project.get("title", "")[:40]
    error_output = (result.get("output") or result.get("error") or "unknown error")[-1500:]
    error_cat = result.get("error_category", "UNKNOWN")

    debug_prompt = (
        f"## Debug Task (depth {current_depth + 1}/3)\n\n"
        f"The following task FAILED after {MAX_FAILURES} attempts:\n"
        f"**Original task**: {original_project.get('title', '')}\n"
        f"**Error category**: {error_cat}\n\n"
        f"### Error output (last 1500 chars):\n```\n{error_output}\n```\n\n"
        f"### Your job:\n"
        f"1. Analyze the error output — what specifically went wrong?\n"
        f"2. Read the relevant source code to understand the root cause\n"
        f"3. Apply a targeted fix (not a blind retry)\n"
        f"4. Run tests to verify the fix\n"
        f"5. If you cannot fix it, document why and what would be needed\n"
    )

    try:
        debug_id = submit_project(
            title=f"[DEBUG-{current_depth + 1}] {title}",
            prompt=debug_prompt,
            priority=original_project.get("priority", 3) + 1,  # Higher priority than original
            model="opus",  # Complex debugging needs opus
            max_turns=30,
        )
        if debug_id:
            # Mark debug depth on the new project
            projects = _load_projects()
            for p in projects:
                if p["id"] == debug_id:
                    p["debug_depth"] = current_depth + 1
                    p["parent_project"] = original_project.get("id", "")
            _save_projects(projects)
            logger.info("R6: Debug sub-project %s submitted (depth=%d) for failed %s",
                        debug_id, current_depth + 1, original_project.get("id", ""))
    except Exception as e:
        logger.debug("Debug project submission failed: %s", e)


def _notify_on_failure(project: Dict, result: Dict):
    """#3: Write L2 approval item when project fails permanently (max retries exhausted)."""
    if result.get("success"):
        return

    proj_id = project.get("id", "")
    failure_count = project.get("failure_count", 0) + 1  # Current attempt

    if failure_count < MAX_FAILURES:
        return  # Will retry, don't notify yet

    try:
        from .approval_queue import submit_approval
        submit_approval(
            level="L2",
            category="kairos_failure",
            title=f"KAIROS project failed: {project.get('title', proj_id)[:50]}",
            context=(
                f"Project {proj_id} failed after {failure_count} attempts.\n"
                f"Error: {(result.get('error') or 'unknown')[:200]}\n"
                f"Cost spent: ${result.get('cost_usd', 0):.2f}\n"
                f"Last output: {(result.get('output') or '')[:300]}"
            ),
            proposal="Options: 1) Retry with different prompt 2) Increase budget 3) Abandon",
        )
        logger.warning("CEO notified: project %s permanently failed", proj_id)
    except Exception as e:
        logger.debug("Failed to notify CEO: %s", e)


def get_all_tasks() -> List[Dict]:
    """Get all projects (backward compat name for dashboard)."""
    return _load_projects()


def get_reports() -> List[Dict]:
    """Get project reports."""
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    reports = []
    for f in sorted(_REPORTS_DIR.glob("proj_*.json"), reverse=True):
        try:
            reports.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    # Also include old task_* reports
    for f in sorted(_REPORTS_DIR.glob("task_*.json"), reverse=True):
        try:
            reports.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return reports[:50]


def generate_summary_report() -> Dict:
    projects = _load_projects()
    completed = [p for p in projects if p.get("status") == "completed"]
    failed = [p for p in projects if p.get("status") == "failed"]
    pending = [p for p in projects if p.get("status") == "pending"]

    total_cost = sum(p.get("total_cost_usd", 0) for p in completed + failed)

    summary = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "completed": len(completed),
        "failed": len(failed),
        "pending": len(pending),
        "total_cost_usd": round(total_cost, 4),
        "completed_titles": [p.get("title", p["id"]) for p in completed],
        "failed_titles": [p.get("title", p["id"]) for p in failed],
    }
    (_DATA / "kairos_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _write_heartbeat(current_project: Optional[Dict] = None,
                     projects_done: int = 0, total_cost: float = 0):
    """#4: Write heartbeat file — CEO can see what KAIROS is doing RIGHT NOW."""
    hb_file = _DATA / "kairos_heartbeat.json"
    try:
        import os
        hb = {
            "pid": os.getpid(),
            "ts": datetime.utcnow().isoformat() + "Z",
            "alive": True,
            "projects_done": projects_done,
            "total_cost_usd": round(total_cost, 3),
        }
        if current_project:
            hb["current_project"] = {
                "id": current_project.get("id", ""),
                "title": current_project.get("title", "")[:80],
                "priority": current_project.get("priority", 3),
                "mode": current_project.get("mode", "workflow"),
                "started_at": current_project.get("started_at", ""),
                "had_patterns": current_project.get("had_patterns", False),
            }
        hb_file.write_text(json.dumps(hb, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _clear_heartbeat():
    """Clear heartbeat on graceful shutdown."""
    hb_file = _DATA / "kairos_heartbeat.json"
    try:
        hb = {"pid": 0, "ts": datetime.utcnow().isoformat() + "Z", "alive": False}
        hb_file.write_text(json.dumps(hb), encoding="utf-8")
    except Exception:
        pass


def _cleanup_stuck_projects():
    """Reset any projects stuck in 'running' back to 'pending'.

    Called on daemon exit to prevent projects from being permanently stuck.
    """
    projects = _load_projects()
    cleaned = 0
    for p in projects:
        if p.get("status") in ("running", "dispatched"):
            p["status"] = "pending"
            p["failure_count"] = p.get("failure_count", 0)  # Don't increment — not a real failure
            logger.warning("Cleanup: reset stuck project %s back to pending", p.get("id", "?"))
            cleaned += 1
    if cleaned:
        _save_projects(projects)
        logger.info("Cleaned up %d stuck projects", cleaned)


async def _try_evolution_cycle(completed_count: int, force: bool = False) -> int:
    """Trigger evolution cycle if enough projects completed or forced.

    Returns updated completed_count (reset to 0 after evolution runs).
    """
    EVOLUTION_TRIGGER_THRESHOLD = 5

    if not force and completed_count < EVOLUTION_TRIGGER_THRESHOLD:
        return completed_count

    try:
        from .evolution_orchestrator import run_evolution_cycle
        logger.info("=== EVOLUTION CYCLE (after %d completions) ===", completed_count)
        result = await run_evolution_cycle()
        summary = result.get("summary", "done")
        success = result.get("success", True)
        logger.info("Evolution %s: %s", "OK" if success else "FAILED", summary)
        return 0  # Reset counter
    except Exception as e:
        logger.warning("Evolution cycle skipped: %s", e)
        return completed_count


async def daemon_loop_async(
    duration_minutes: Optional[int] = None,
    max_workers: int = 2,
    poll_interval: int = POLL_INTERVAL,
):
    """Async daemon loop with parallel worker pool.

    Fills idle worker slots with pending projects, enabling concurrent
    execution of multiple claude -p processes. Triggers evolution cycle
    every 5 completed projects or when idle.
    """
    import asyncio
    from .worker_pool import WorkerPool

    # File logging for dashboard monitoring
    _DATA.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(str(_LOG_FILE), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [KAIROS] %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(fh)

    pool = WorkerPool(
        max_workers=max_workers,
        status_file=_DATA / "worker_pool_status.json",
    )

    start_time = time.time()
    deadline = start_time + duration_minutes * 60 if duration_minutes else None
    completed_since_evolution = 0

    logger.info("KAIROS v3 daemon started. Workers: %d, Poll: %ds.",
                max_workers, poll_interval)
    if deadline:
        logger.info("Auto-stop after %d min.", duration_minutes)

    _write_heartbeat()

    try:
        while True:
            if deadline and time.time() >= deadline:
                logger.info("Duration limit reached. Stopping.")
                break

            # Check pause signal
            pause_file = _DATA / "kairos_pause_signal"
            if pause_file.exists():
                logger.debug("Paused. Waiting...")
                await asyncio.sleep(poll_interval)
                continue

            # Fill available worker slots with pending projects
            status = pool.get_status()
            for _ in range(status.idle_workers):
                project = get_next_project()
                if not project:
                    break
                logger.info("Dispatching: %s (P%d) -> slot",
                            project.get("title", "")[:45],
                            project.get("priority", 3))
                await pool.submit(project, execute_project)

            # Track completions for evolution trigger
            status = pool.get_status()
            current_total = status.total_completed + status.total_failed
            if not hasattr(daemon_loop_async, '_last_total'):
                daemon_loop_async._last_total = 0
            new_completions = current_total - daemon_loop_async._last_total
            if new_completions > 0:
                completed_since_evolution += new_completions
                daemon_loop_async._last_total = current_total

            # Check if evolution should run (every 5 completions)
            completed_since_evolution = await _try_evolution_cycle(completed_since_evolution)

            # If nothing running and nothing pending, consult task brain
            if status.active_workers == 0:
                # Non-destructive check: peek at pending projects without dispatching
                has_pending = _has_pending_projects()
                if not has_pending:
                    # Run evolution one last time before considering stop
                    if completed_since_evolution > 0:
                        completed_since_evolution = await _try_evolution_cycle(
                            completed_since_evolution, force=True
                        )

                    try:
                        from .task_brain import get_task_brain
                        brain = get_task_brain()
                        new_projects = brain.on_queue_empty()
                        actually_submitted = 0
                        if new_projects:
                            for p in new_projects:
                                pid = submit_project(
                                    title=p["title"], prompt=p["prompt"],
                                    priority=p.get("priority", 3),
                                    model=p.get("model", DEFAULT_MODEL),
                                    max_turns=p.get("max_turns", 25),
                                )
                                if pid:  # Non-empty = actually submitted (not a dup)
                                    actually_submitted += 1
                            if actually_submitted > 0:
                                logger.info("Brain discovered %d new projects (%d submitted).",
                                            len(new_projects), actually_submitted)
                                continue  # Re-enter loop to dispatch new projects
                            else:
                                logger.debug("Brain generated %d projects but all duplicates.",
                                            len(new_projects))
                    except Exception as e:
                        logger.debug("Brain discovery skipped: %s", e)

                    logger.info("No projects and brain found nothing. Stopping.")
                    break

            # Write heartbeat with actual metrics from worker pool
            pool_status = pool.get_status()
            _write_heartbeat(
                projects_done=pool_status.total_completed,
                total_cost=pool_status.total_cost_usd,
            )
            await asyncio.sleep(poll_interval)

    except KeyboardInterrupt:
        logger.info("Stopped by user.")

    # Drain remaining workers
    await pool.drain()
    _cleanup_stuck_projects()
    _clear_heartbeat()
    summary = generate_summary_report()
    elapsed = (time.time() - start_time) / 60
    logger.info("SESSION COMPLETE: %d completed, %d failed, $%.3f, %.1f min",
                summary["completed"], summary["failed"],
                summary["total_cost_usd"], elapsed)


# Keep sync wrapper for backward compat with callers that expect sync daemon_loop
def daemon_loop(duration_minutes: Optional[int] = None,
                poll_interval: int = POLL_INTERVAL):
    """Sync wrapper around daemon_loop_async (single worker, backward compat)."""
    import asyncio
    asyncio.run(daemon_loop_async(
        duration_minutes=duration_minutes,
        max_workers=1,
        poll_interval=poll_interval,
    ))


# Backward compat aliases — used by test_kairos_smoke and other modules
submit_task = submit_project
get_next_task = get_next_project
execute_task = execute_project


def main():
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="KAIROS v3 Daemon")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--duration", type=int, default=None)
    parser.add_argument("--poll", type=int, default=POLL_INTERVAL)
    parser.add_argument("--workers", type=int, default=2,
                        help="Max parallel workers (default: 2)")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--evolve", action="store_true",
                        help="Run one evolution cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [KAIROS] %(message)s", datefmt="%H:%M:%S")

    if args.evolve:
        from .evolution_orchestrator import run_evolution_cycle_sync
        print("Running evolution cycle...")
        result = run_evolution_cycle_sync()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    elif args.summary:
        print(json.dumps(generate_summary_report(), ensure_ascii=False, indent=2))
    elif args.once:
        p = get_next_project()
        if p:
            r = execute_project(p)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            print("No pending projects.")
    else:
        print("=" * 50)
        print("  KAIROS v3 — Parallel Autonomous Executor")
        print(f"  Queue: {_PROJECTS_FILE}")
        print(f"  Workers: {args.workers}")
        if args.duration:
            print(f"  Duration: {args.duration} min")
        print("=" * 50)
        asyncio.run(daemon_loop_async(
            duration_minutes=args.duration,
            max_workers=args.workers,
            poll_interval=args.poll,
        ))


if __name__ == "__main__":
    main()
