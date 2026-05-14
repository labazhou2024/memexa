"""
Session Start Gate — Comprehensive session initialization check.

Runs at SessionStart hook. Outputs structured briefing to stdout
for Claude to process. Does NOT block — all checks are informational.

Checks:
  1. auto_trigger conditions (dream, mini-loop, test health, etc.)
  2. action_items from harness_state.json
  3. Pipeline state (any incomplete tasks from previous session)
  4. Pending CEO approvals
  5. Phase 0 context load checklist
  6. Memory integrity quick check
  7. Git state verification

Called by: SessionStart hook in settings.json
Output: Structured text to stdout (Claude reads this as hook output)
"""


import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.core._path_resolver import memory_dir

# [2026-04-21 TU-bonus] Force UTF-8 stdout/stderr on Windows console.
# Without this, Claude Code's hook capture gets GBK-encoded Chinese
# (e.g., "ϵͳ�����ã�" instead of "系统整体良好"). reconfigure() is a
# no-op on Unix / already-UTF-8 consoles.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass  # fallback: Python <3.7 or detached stream

logger = logging.getLogger(__name__)


def _find_workspace() -> Path:
    """Resolve workspace root robustly (Windows CJK path safe)."""
    marker = Path(".claude") / "config" / "settings.json"
    try:
        cwd = Path(os.getcwd())
        if (cwd / marker).exists():
            return cwd
    except OSError:
        pass
    try:
        candidate = Path(__file__).parent.parent.parent.parent
        if (candidate / marker).exists():
            return candidate
    except OSError:
        pass
    return Path(os.getcwd())


_WORKSPACE = _find_workspace()
_MEMEXA = _WORKSPACE / "memexa"
_HARNESS = _WORKSPACE / ".claude" / "config" / "harness_state.json"
# _PIPELINE removed in v6.0 (CC-Native: pipeline_state.py deleted)
_MEMORY_DIR = memory_dir()
_MEMORY_INDEX = _MEMORY_DIR / "MEMORY.md"


def _load_harness() -> Dict:
    if _HARNESS.exists():
        try:
            return json.loads(_HARNESS.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _section(title: str) -> str:
    return f"\n{'='*50}\n{title}\n{'='*50}"


# ================================================================
# Check 1: Auto-triggers
# ================================================================

def check_auto_triggers() -> str:
    """Run auto_trigger.check_triggers(), execute or queue actionable items, format results."""
    try:
        # Import directly to avoid subprocess overhead
        sys.path.insert(0, str(_MEMEXA))
        from src.core.auto_trigger import check_triggers, format_briefing
        actions = check_triggers()
        if not actions:
            return "All systems nominal -- no auto-triggers fired."

        # Execute or queue actionable triggers
        auto_executed = _auto_submit_triggers(actions)
        briefing = format_briefing(actions)
        if auto_executed:
            briefing += f"\n\n**Auto-executed**: {', '.join(auto_executed)}"
        return briefing
    except Exception as e:
        return f"Auto-trigger check failed: {e}"


def _auto_submit_triggers(actions: list) -> list:
    """Convert auto-trigger actions into harness action_items.

    KAIROS is frozen. Instead of submitting to KAIROS daemon,
    write action items directly to harness_state.json.
    These are displayed at SessionStart and Claude acts on them.

    For autoDream: run directly in this process (no daemon needed).

    Returns list of action descriptions.
    """
    submitted = []
    action_items = []

    for action in actions:
        action_type = action.get("type", "")
        data = action.get("data", {})

        if action_type == "auto_dream":
            # Run autoDream directly (no KAIROS needed)
            try:
                from src.core.auto_dream import AutoDream
                # Run orient + gather + prune (skip consolidate which needs LLM)
                ad = AutoDream()
                episodes = ad.gather()
                prune_result = ad.prune()
                # Also prune expired patterns
                from src.core.pattern_extractor import prune_expired_patterns
                pruned = prune_expired_patterns(ttl_days=30)
                submitted.append(f"AutoDream: {len(episodes)} episodes gathered, {pruned} patterns pruned")
            except Exception as e:
                action_items.append(f"[AutoDream] Memory consolidation needed but failed: {e}")

        elif action_type == "auto_fix_tests":
            # F2 fix (2026-04-24): coerce to int and gate on >0 to avoid
            # the "[URGENT] Tests failing: 0 failed" contradiction. Prior
            # code used `data.get('failed', '?')` which (a) can compare
            # str to int and TypeError (swallowed by outer except), and
            # (b) produces a 0-case message when success=False due to
            # timeout/collection error with zero concrete failures.
            try:
                failed = int(data.get('failed', 0) or 0)
                errors = int(data.get('errors', 0) or 0)
            except (TypeError, ValueError):
                failed, errors = 0, 0
            if failed > 0 or errors > 0:
                action_items.append(
                    f"[URGENT] Tests failing: {failed} failed, {errors} errors. "
                    f"Run: pytest tests/ --tb=short to diagnose and fix."
                )
                submitted.append("AutoFix queued as action_item")
            else:
                submitted.append(
                    "AutoFix skipped: tests unsuccessful but no specific "
                    "failures (likely timeout/collection error)"
                )

        elif action_type == "auto_postmortem":
            action_items.append(
                f"[PostMortem] {data.get('error_count', 0)} errors in last 100 events. "
                f"Read events.jsonl, identify patterns, fix root causes."
            )
            submitted.append("PostMortem queued as action_item")

        elif action_type == "mini_loop":
            # F2 fix (2026-04-24): coerce to int and gate on >0 for the
            # same reason as auto_fix_tests (see comment above). Prior
            # default '?' would cause silent TypeError.
            try:
                commits_since = int(data.get('commits_since', 0) or 0)
            except (TypeError, ValueError):
                commits_since = 0
            if commits_since <= 0:
                # No commits since last loop → skip emitting a stale marker.
                submitted.append("MiniLoop skipped: 0 commits since last loop")
                continue
            # Queue mini-loop and reset the counter so it doesn't fire every session
            action_items.append(
                f"[MiniLoop] {commits_since} commits since last loop. "
                f"Run quality cycle: pytest -> fix -> commit."
            )
            try:
                from src.core.auto_trigger import reset_mini_loop_counter
                reset_mini_loop_counter()
                submitted.append("MiniLoop queued + counter reset")
            except Exception:
                submitted.append("MiniLoop queued (counter reset failed)")

        elif action_type == "consolidate_memory":
            action_items.append(
                f"[Memory] {data.get('pending', 0)} episodes pending consolidation. "
                f"Run: python -c \"from src.core.pattern_extractor import extract_from_session; "
                f"print(f'Added: {{extract_from_session()}} patterns')\""
            )
            submitted.append("Memory consolidation queued as action_item")

    # Write action_items to harness_state.json
    # F2 fix (2026-04-24): use atomic_update_json + file-lock so this
    # write doesn't race with heartbeat_service._emit_replan_action_item
    # (also writing to harness_state) and silently clobber it. Prior
    # bare write_text pattern would lose updates under concurrent access.
    if action_items:
        try:
            from src.core._atomic_state import atomic_update_json

            def _mut(harness):
                existing = harness.get("action_items_for_user", [])
                # Deduplicate by prefix
                existing_prefixes = {item[:30] for item in existing}
                new_items = [item for item in action_items
                             if item[:30] not in existing_prefixes]
                if new_items:
                    harness["action_items_for_user"] = (existing + new_items)[-10:]
                return harness

            atomic_update_json(_HARNESS, _mut)
        except Exception:
            pass

    return submitted


# ================================================================
# Check 2: Action items
# ================================================================

def check_action_items() -> str:
    """Read and format action_items from harness_state."""
    harness = _load_harness()
    items = harness.get("action_items_for_user", [])
    if not items:
        return "No pending action items."

    lines = [f"  {i+1}. {item}" for i, item in enumerate(items)]
    return "Pending action items for CEO:\n" + "\n".join(lines)


# ================================================================
# Check 3: Pipeline state
# ================================================================

def check_pipeline_state() -> str:
    """Pipeline state tracking removed in v6.0 (CC-Native).
    CC Task system handles phase tracking natively."""
    return "Pipeline tracking: CC-Native (TaskCreate with dependencies)"


# ================================================================
# Check 4: Pending approvals
# ================================================================

def check_pending_approvals() -> str:
    """Check CEO approval queue.

    P3 (2026-04-23) fix: use approval_queue.get_queue_path() as single
    source-of-truth. Previously this function derived its own path from
    `_MEMEXA`, diverging from approval_queue's `_QUEUE_FILE` (logic B5
    finding: hook runner cwd reset → _find_workspace() → os.getcwd()
    fallback → wrong file).
    """
    try:
        from src.core.approval_queue import get_queue_path
        approval_file = get_queue_path()
    except Exception:
        # Fallback: keep old behavior during bootstrap errors
        approval_file = _MEMEXA / "memexa" / "data" / "pending_approvals.json"
    if not approval_file.exists():
        return "No pending approvals."

    try:
        approvals = json.loads(approval_file.read_text(encoding="utf-8"))
        if not approvals:
            return "Approval queue empty."

        l3 = [a for a in approvals if a.get("level") == "L3"]
        l2 = [a for a in approvals if a.get("level") == "L2"]

        lines = []
        if l3:
            lines.append(f"  L3 BLOCKING ({len(l3)}):")
            for a in l3[:3]:
                lines.append(f"    - [{a.get('id', '?')}] {a.get('title', '?')}")
        if l2:
            lines.append(f"  L2 PENDING ({len(l2)}):")
            for a in l2[:3]:
                lines.append(f"    - [{a.get('id', '?')}] {a.get('title', '?')}")

        return "CEO Approval Queue:\n" + "\n".join(lines)
    except Exception as e:
        return f"Approval queue error: {e}"


# ================================================================
# Check 5: Phase 0 context load checklist
# ================================================================

def check_phase0_files() -> str:
    """Verify Phase 0 required files exist."""
    required = [
        (".claude/config/harness_state.json", "Core state"),
        ("memexa/WORKFLOW.md", "Workflow protocol"),
        ("CLAUDE.md", "Behavior spec"),
        (".claude/config/settings.json", "Hooks config"),
    ]

    issues = []
    for path, desc in required:
        full = _WORKSPACE / path
        if not full.exists():
            issues.append(f"  MISSING: {path} ({desc})")

    # Check memory index
    if _MEMORY_INDEX.exists():
        index_text = _MEMORY_INDEX.read_text(encoding="utf-8")
        import re
        refs = set(re.findall(r'\(([a-zA-Z0-9_]+\.md)\)', index_text))
        missing_refs = [r for r in refs if not (_MEMORY_DIR / r).exists()]
        if missing_refs:
            issues.append(f"  MEMORY: {len(missing_refs)} referenced files missing: {', '.join(missing_refs[:5])}")

        if _MEMORY_DIR.exists():
            actual = {f.name for f in _MEMORY_DIR.glob("*.md") if f.name != "MEMORY.md"}
            orphans = actual - refs
            if orphans:
                issues.append(f"  MEMORY: {len(orphans)} orphan files not in index: {', '.join(list(orphans)[:5])}")
    else:
        issues.append("  MISSING: memory/MEMORY.md (memory index)")

    # Count agents
    agents_dir = _WORKSPACE / ".claude" / "agents"
    agent_count = len(list(agents_dir.glob("*.md"))) if agents_dir.exists() else 0

    if issues:
        return "Phase 0 Issues:\n" + "\n".join(issues) + f"\n  Agents found: {agent_count}"

    return f"Phase 0 files OK. Agents: {agent_count}. Memory index: clean."


# ================================================================
# Check 6: Git state
# ================================================================

def check_git_state() -> str:
    """Quick git state verification."""
    harness = _load_harness()
    results = []

    for repo_name, repo_info in harness.get("git_repos", {}).items():
        repo_path = _WORKSPACE / repo_name
        if not (repo_path / ".git").exists():
            continue

        try:
            head = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=str(repo_path), timeout=5,
            )
            current = head.stdout.strip()
            recorded = repo_info.get("last_commit", "?")

            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, cwd=str(repo_path), timeout=5,
            )
            current_branch = branch.stdout.strip()

            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=str(repo_path), timeout=5,
            )
            dirty_count = len([l for l in dirty.stdout.strip().splitlines() if l.strip()])

            status = "OK"
            if current != recorded:
                status = f"DRIFT(harness={recorded}, HEAD={current})"
            if dirty_count:
                status += f" +{dirty_count} uncommitted"

            results.append(f"  {repo_name}: {current_branch}@{current} — {status}")
        except Exception:
            results.append(f"  {repo_name}: git check failed")

    return "Git State:\n" + "\n".join(results) if results else "No git repos to check."


# ================================================================
# Main
# ================================================================

def _write_phase0_marker():
    """Phase 0 marker removed in v6.0 (CC-Native).
    pretool_gate no longer checks for Phase 0 completion."""
    pass


def _generate_ceo_briefing() -> str:
    """生成中文自然语言 CEO 汇报。"""
    try:
        sys.path.insert(0, str(_MEMEXA))
        from src.core.cto_briefing import generate_briefing
        return generate_briefing()
    except Exception as e:
        return f"CEO 汇报生成失败: {e}"


def check_resumable_task() -> str:
    """A4 (plan v2, 2026-04-21): detect crash-resumable task.

    Reads .claude/harness/tasks/_latest pointer and the task's state.json.
    If there's a unit in status=running or failed at current_unit_idx,
    emit a briefing so Claude can pick up via CLI.

    Silent (returns "") if:
      - no _latest pointer
      - task dir missing
      - state.json missing or malformed
      - current_unit_idx < 0 (task not started scheduling)
      - all units done/pending (nothing to resume)
    """
    try:
        sys.path.insert(0, str(_MEMEXA))
        from src.core.task_dir_layout import current_task_id, load_state
    except Exception:
        return ""
    try:
        tid = current_task_id()
        if not tid:
            return ""
        state = load_state(tid)
        if not state:
            return ""
        idx = state.get("current_unit_idx", -1)
        if idx < 0:
            return ""
        units = state.get("units", [])
        if not units or idx >= len(units):
            return ""
        unit = units[idx]
        status = unit.get("status", "")
        if status not in ("running", "failed"):
            return ""
        total = len(units)
        done = sum(1 for u in units if u.get("status") in ("done", "skipped"))
        phase = state.get("current_phase", "?")
        desc = unit.get("description", "")[:120]
        marker = "RUNNING" if status == "running" else "FAILED"
        return (
            f"RESUMABLE TASK [{marker}]: {tid}\n"
            f"  Phase {phase} / unit {idx+1}/{total} ({done} done): {unit.get('id', '?')} — {desc}\n"
            f"  Resume: python -m src.core.task_unit_scheduler resume {tid}"
        )
    except Exception:
        return ""


def check_crash_recovery() -> str:
    """Detect unclean previous shutdown -> trigger autoDream catchup.

    OOM-SAFE design (2026-04-17 fix):
    - Only runs on session_source='startup', NOT on 'resume' (which fires every
      time a session is interrupted/restored - was causing infinite recovery loop)
    - Uses 'recovery_attempted' flag to prevent re-running within same session
    - Bounded: gathers max 100 events (not 500), skips prune
    """
    # Skip recovery on resume events to prevent OOM loop
    source = os.environ.get("CLAUDE_SESSION_START_SOURCE", "startup")
    if source == "resume":
        return ""

    harness = _load_harness()
    dream = harness.get("auto_dream", {})
    last_clean = dream.get("clean_shutdown", True)  # Default True (safe on first run)
    recovery_attempted = dream.get("recovery_attempted", False)

    if last_clean or recovery_attempted:
        # Either clean shutdown OR recovery already attempted this cycle - skip
        return ""

    # Unclean shutdown - mark recovery attempted FIRST to prevent re-entry
    if "auto_dream" not in harness:
        harness["auto_dream"] = {}
    harness["auto_dream"]["recovery_attempted"] = True
    try:
        _HARNESS.write_text(json.dumps(harness, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return "[CRASH RECOVERY] Failed to mark recovery flag"

    # Bounded gather: only count, don't load
    try:
        from src.core.event_bus import get_event_count
        event_count = get_event_count()
        return (
            f"\n[CRASH RECOVERY] Previous session ended uncleanly. "
            f"Skipped full autoDream (would consume {event_count} events). "
            f"Run manually: python -c \"from src.core.auto_dream import AutoDream; "
            f"import asyncio; asyncio.run(AutoDream().run())\""
        )
    except Exception as e:
        return f"\n[CRASH RECOVERY] Detection failed: {type(e).__name__}"


_SID_TTL_SEC = 3600  # [Fix C] Reuse sid within this window to prevent drift


def _generate_session_id() -> str:
    """Generate or preserve session_id for helpful_count tracking.

    [Fix C, 2026-04-18] If current_session_id.txt exists AND mtime < 1h old,
    return the existing sid (do NOT overwrite). This prevents mid-session
    sid drift when SessionStart hook re-fires (e.g., after PostCompact,
    auto-resume, or internal Claude Code events).

    Fresh sid generated only if:
      - File doesn't exist
      - File mtime > TTL (belongs to stale session from prior day)
      - File content is malformed (<16 chars, missing '-')
    """
    import uuid as _uuid
    import time as _time
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from src.core.pattern_extractor import (
            set_current_session_id, _CURRENT_SESSION_FILE,
        )

        # [Fix C] Check for existing usable sid
        if _CURRENT_SESSION_FILE.exists():
            try:
                age_sec = _time.time() - _CURRENT_SESSION_FILE.stat().st_mtime
                if age_sec < _SID_TTL_SEC:
                    existing = _CURRENT_SESSION_FILE.read_text(encoding="utf-8").strip()
                    # [LOW-3] Strict UUID validation via uuid.UUID(). This
                    # rejects corrupted/truncated sids that would otherwise
                    # mis-attribute primed entries to a bogus session.
                    # Accept either standard UUID or "fallback-<timestamp>" format.
                    if existing:
                        if existing.startswith("fallback-") and len(existing) > 10:
                            return existing  # fallback IDs from pattern_extractor
                        try:
                            _uuid.UUID(existing)  # raises ValueError on invalid
                            return existing
                        except ValueError:
                            pass  # Fall through to fresh gen
            except (OSError, ValueError):
                pass  # Fall through to fresh gen

        # File missing / stale / malformed → generate new
        sid = str(_uuid.uuid4())
        set_current_session_id(sid)
        return sid
    except Exception:
        return ""


# ================================================================
# Check 8: Replay last commit's gate_skipped events (AC-C3)
# ================================================================

def _revive_hindsight_if_dead() -> str:
    """TU-4 (autopilot 20260428_070949_daemon_watch): probe hindsight daemon
    on session resume; if dead, attempt watchdog launch.

    Two-layer try/except (logic-iter1-7 fix): outer catches ImportError ONLY
    so socket / runtime errors land in inner branch with distinct return string.

    Returns banner-ready string ("" if alive; non-empty if attempted action).
    Never raises — degrades gracefully to stderr warn line.
    """
    try:
        # Outer try: ImportError only (degraded if watchdog module unavailable)
        from tools.hindsight_daemon_watchdog import probe, launch_daemon, _resolve_api_key  # noqa
    except ImportError as ie:
        msg = f"[hindsight] watchdog import failed: {type(ie).__name__} (degraded)"
        print(msg, file=sys.stderr)
        return msg
    try:
        # Inner try: probe + (conditional) launch; catches socket/runtime
        alive, reason = probe()
        if alive:
            return ""
        api_key = _resolve_api_key()
        if not api_key:
            return "[hindsight] revive failed: no_api_key (env DEEPSEEK_API_KEY + config.yaml both empty)"
        ok, lreason = launch_daemon(api_key)
        if ok:
            return f"[hindsight] revived ({lreason})"
        return f"[hindsight] revive failed: {lreason}"
    except Exception as e:
        # Stage4 security-iter2 LOW-3 fix: defense-in-depth — apply same redaction
        # as watchdog._safe() in case exception text leaks API key fragments.
        try:
            from tools.hindsight_daemon_watchdog import _safe as _wd_safe
            redacted = _wd_safe(str(e))[:120]
        except Exception:
            redacted = str(e)[:120]
        return f"[hindsight] revive failed: {type(e).__name__}: {redacted}"


def _replay_last_commit_gates(threshold: int = 4) -> tuple:
    """Replay last commit's gate_skipped events at SessionStart.

    AC-C3 (plan_v4): detect sessions that start after a commit where gates
    were skipped on a complex task.  Blocks (fail-closed) only when BOTH:
      - last commit had >= threshold gate_skipped events, AND
      - the active task_spec.json has complexity == "complex"

    All infra failures are fail-soft: return (True, "skip: <reason>") so
    session start is never blocked by a monitoring glitch.

    Returns:
        (ok: bool, reason: str) — ok=False means session should be gated.
    """
    # Step 1: resolve LAST_COMMIT_SHA via git log
    try:
        raw_sha = subprocess.check_output(
            ["git", "log", "-1", "--format=%H"],
            cwd=str(_WORKSPACE),
            timeout=5,
        )
        last_sha_raw = raw_sha.decode("utf-8", errors="replace").strip()
        if not last_sha_raw:
            raise ValueError("empty git output")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError, ValueError):
        return (True, "skip: git_unavailable")

    # Step 2: validate SHA to prevent injection (S-B3)
    try:
        from src.core._git_helpers import validate_sha
        validated_sha = validate_sha(last_sha_raw)
    except ValueError:
        return (True, "skip: invalid_sha")

    # Step 3: resolve last commit ISO timestamp
    try:
        ts_out = subprocess.check_output(
            ["git", "log", "-1", "--format=%cI", validated_sha],
            cwd=str(_WORKSPACE),
            timeout=5,
        )
        last_iso = ts_out.decode("utf-8", errors="replace").strip()
        if not last_iso:
            raise ValueError("empty timestamp output")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError, ValueError):
        return (True, "skip: git_unavailable")

    # Step 4: read gate_skipped trace events since last commit
    try:
        from src.core.trace_sink import read_traces
        events = read_traces(since_iso=last_iso, event_filter=["gate_skipped"], limit=100)
        skip_count = len(events)
    except Exception:
        # Fail-soft: trace read failure must not block session start
        return (True, "skip: trace_unavailable")

    # Step 5: resolve complexity from task_spec.json
    complexity = "unknown"
    try:
        task_spec_path = _MEMEXA / "memexa" / "data" / "task_spec.json"
        if task_spec_path.exists():
            spec_data = json.loads(task_spec_path.read_text(encoding="utf-8"))
            complexity = spec_data.get("complexity", "unknown")
    except (OSError, json.JSONDecodeError, KeyError):
        complexity = "unknown"

    # Step 6: gate decision
    if skip_count >= threshold and complexity == "complex":
        reason = (
            f"BLOCKED: last commit had {skip_count} skipped gates on complex task; "
            f"replay required"
        )
        return (False, reason)

    return (True, f"OK: skip_count={skip_count} threshold={threshold} complexity={complexity}")


def main():
    """Run all session start checks and output briefing."""
    # [V-3] Generate session_id FIRST so it's available for all later priming
    session_id = _generate_session_id()

    # [L6 Phase 4 2026-04-18] Increment evolution session counter (cold start only).
    # Also check if trigger conditions met; if so, queue for CEO approval.
    try:
        from src.core.evolution_trigger import (
            increment_session_counter, check_and_trigger,
        )
        increment_session_counter()  # silently skipped on resume/clear
        trigger_result = check_and_trigger()
        # Do not print unless something meaningful happened; dashboard handles display
        if trigger_result.get("queued_for_approval"):
            print("[L6] Prompt evolution trigger conditions met — "
                  "CEO approval queued.")
    except Exception:
        pass  # non-blocking

    # TU-3 (2026-04-23 cold-start trim): default output is compact.
    # Set MEMEXA_SESSION_START_VERBOSE=1 to restore full KAIROS narrative +
    # self-evolution dashboard. Compact mode keeps the critical signals
    # (action items, approvals, git drift, auto-triggers) as ≤1-line each.
    verbose = os.environ.get("MEMEXA_SESSION_START_VERBOSE", "0") == "1"

    # 0. Crash recovery check (silent if last shutdown was clean)
    recovery = check_crash_recovery()
    if recovery:
        print(recovery)

    # TU-4 (autopilot 20260428_070949_daemon_watch): probe hindsight daemon
    # on session resume; if dead, attempt watchdog launch.
    try:
        _revive_msg = _revive_hindsight_if_dead()
        if _revive_msg:
            print(_revive_msg)
    except Exception:
        pass  # never block session start

    # AC-C3 (plan_v4): replay last commit's gate_skipped events
    _replay_ok, _replay_reason = _replay_last_commit_gates()
    if not _replay_ok:
        print(f"[SESSION START GATE] {_replay_reason}")
        # Enqueue L2 action_item for CEO (non-blocking)
        try:
            from src.core.approval_queue import submit_approval
            submit_approval(
                level="L2",
                category="gate_enforcement",
                title="SessionStart: gate replay required after complex task",
                context=_replay_reason,
                proposal=(
                    "Run: python -m src.core.gates.integration_gate check "
                    + "<last_tid> to verify gate coverage before continuing."
                ),
                evidence=[_replay_reason],
            )
        except Exception:
            pass  # fail-soft: approval queue failure must not block session start
        # Emit trace event (fail-soft)
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event(
                "session_start_replay_blocked",
                {"reason": _replay_reason},
            )
        except Exception:
            pass  # fail-soft

    # Header (compact: single-line date; verbose: full morning briefing)
    today = datetime.now().strftime("%Y-%m-%d %A")
    if verbose:
        print(_section("memexa Morning Briefing"))
        print(f"Date: {today}")
        print(_generate_ceo_briefing())
        print(_section("System Checks"))
    else:
        print(f"[SessionStart] {today}")

    # Critical signals — always printed (these ARE the control signals)
    phase0 = check_phase0_files()
    if "Issues" in phase0:
        print(phase0)

    action = check_action_items()
    if "No pending" not in action:
        if verbose:
            print(action)
        else:
            # Compact: count + top-1 only
            lines = [ln for ln in action.splitlines()
                     if ln.strip().startswith(("1.", "2.", "3."))]
            n = sum(1 for ln in action.splitlines()
                    if ln.strip().startswith(tuple(f"{i}." for i in range(1, 20))))
            if lines:
                print(f"[action_items] {n} pending; top: {lines[0].strip()[:100]}")

    resumable = check_resumable_task()
    if resumable:
        # Keep resumable whether verbose or not (it's a crash-recovery signal)
        print(resumable if verbose else resumable.splitlines()[0][:160])

    approval = check_pending_approvals()
    if "empty" not in approval and "No pending" not in approval:
        if verbose:
            print(approval)
        else:
            # Compact: L3 / L2 counts only
            import re as _re
            l3_m = _re.search(r"L3[^\n]*\((\d+)\)", approval)
            l2_m = _re.search(r"L2[^\n]*\((\d+)\)", approval)
            l3 = l3_m.group(1) if l3_m else "0"
            l2 = l2_m.group(1) if l2_m else "0"
            print(f"[CEO_approvals] L3={l3} L2={l2} (run: python -m src.core.pending_approvals list)")

    git = check_git_state()
    if "DRIFT" in git or "uncommitted" in git:
        if verbose:
            print(git)
        else:
            # Compact: one line per repo with drift
            for ln in git.splitlines():
                s = ln.strip()
                if ("DRIFT" in s or "uncommitted" in s) and ":" in s:
                    print(f"[git] {s[:160]}")

    triggers = check_auto_triggers()
    if "nominal" not in triggers:
        if verbose:
            print(triggers)
        else:
            # Compact: count critical (🔴) / warning (🟠) indicators
            red = triggers.count("🔴")
            orange = triggers.count("🟠")
            if red or orange:
                print(f"[auto_triggers] 🔴={red} 🟠={orange} (verbose: MEMEXA_SESSION_START_VERBOSE=1)")

    # L7 Self-evolution dashboard — only in verbose
    if verbose:
        try:
            from src.core.evolution_dashboard import render_brief, is_enabled as dash_on
            if dash_on():
                dash = render_brief()
                if dash:
                    print(dash)
        except Exception:
            pass

    _write_phase0_marker()
    if verbose:
        print(_section("Ready"))


if __name__ == "__main__":
    main()
