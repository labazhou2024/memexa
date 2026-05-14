"""
L6 Evolution Trigger (Phase 4, 2026-04-18)

10-session AND 72h elapsed trigger for prompt_evolver. Race-safe via filelock.

Design (per verifier R3):
- Counter key in harness_state.json: prompt_evolution.sessions_since_last_evolve
- Two AND conditions:
    sessions >= MEMEXA_EVOLUTION_SESSION_THRESHOLD (default 10)
    AND
    elapsed_since_last_evolve >= 72h
- "New session" = cold start only (resume/clear NOT counted)
    detect: harness_state.auto_dream.last_shutdown_reason != "resume"
- Clear counter on success (not on trigger) — avoid retry storm
- filelock cross-platform (filelock lib, already installed)
- ENV flag: MEMEXA_L6_EVOLUTION=0 default (CEO must opt in explicitly)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = ["is_enabled", "increment_session_counter", "check_and_trigger",
           "get_counter_status"]


_WORKSPACE = Path(__file__).parent.parent.parent.parent
_HARNESS_STATE = _WORKSPACE / ".claude" / "config" / "harness_state.json"
_LOCK_FILE = _WORKSPACE / ".claude" / "config" / ".harness_state.lock"

_DEFAULT_THRESHOLD = int(os.environ.get("MEMEXA_EVOLUTION_SESSION_THRESHOLD", "10"))
_MIN_ELAPSED_HOURS = 72
_EXCLUDE_SHUTDOWN_REASONS = {"resume", "clear"}

# [B1 fix 2026-04-18] Outcome source for PromptEvolver.evolve().
# kairos_feedback.jsonl is keyed by agent_role; we match against eligible
# agent names. Empty list → evolve() short-circuits on MIN_OUTCOMES check.
_FEEDBACK_FILE = (
    Path(__file__).parent.parent / "data" / "kairos_feedback.jsonl"
)
_OUTCOMES_LOOKBACK = 20  # most-recent N records per agent


def _is_eligible_agent_role(role: str) -> bool:
    """[AC-9 2026-04-20] Whitelist guard for agent_role values in loaded outcomes.

    Replaces the legacy blacklist-only approach. A role is eligible iff:
      1. It passes the safe-name regex from prompt_evolver (_is_safe_agent_name)
      2. It does NOT start with "test_" (mock/test injection protection)
      3. It does NOT start with "_test" (internal test helper protection)
      4. It IS in _ELIGIBLE_AGENTS (whitelist, not blacklist)

    The whitelist check (condition 4) is the primary gate; conditions 2-3 are
    redundant defence-in-depth so that even if _ELIGIBLE_AGENTS is misconfigured
    the common test-prefix patterns are still rejected.
    """
    if not isinstance(role, str) or not role:
        return False
    # Import lazily to avoid circular imports at module load time.
    # [LOG-R1-015 2026-04-20] Prefer _get_eligible_agents() so env changes take
    # effect within 30s without a process restart; fall back to the constant
    # if the dynamic accessor is unavailable (older prompt_evolver builds).
    try:
        from memexa.core.prompt_evolver import _is_safe_agent_name
        try:
            from memexa.core.prompt_evolver import _get_eligible_agents
            eligible = _get_eligible_agents()
        except ImportError:
            from memexa.core.prompt_evolver import _ELIGIBLE_AGENTS as eligible  # type: ignore
    except Exception:
        # Fallback: at minimum reject test prefixes
        return not (role.startswith("test_") or role.startswith("_test"))
    # Whitelist check (primary)
    if role not in eligible:
        return False
    # Defence-in-depth: reject test-prefixed names even if they somehow enter the whitelist
    if role.startswith("test_") or role.startswith("_test"):
        return False
    # Safe-name regex check
    if not _is_safe_agent_name(role):
        return False
    return True


def _load_outcomes_for_agent(agent_name: str, limit: int = _OUTCOMES_LOOKBACK):
    """Load recent outcomes for an agent from kairos_feedback.jsonl.

    Returns a list of {task, score, output_summary} dicts matching the
    schema PromptEvolver.evolve() expects. Empty if no data — caller gets
    honest "no_outcomes_yet" status rather than silent skip.
    """
    if not _FEEDBACK_FILE.exists():
        return []
    matches = []
    try:
        # bounded tail read (last 5000 lines is ample; corpus is 1.3k today)
        with open(_FEEDBACK_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-5000:]
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # [AC-9 2026-04-20] Whitelist check: agent_role in record must pass
            # _is_eligible_agent_role() before it is counted as evidence.
            # This replaces the legacy blacklist-only pid.startswith("test_") guard
            # for agent_role matching — the pid guard below is kept for project IDs.
            rec_role = rec.get("agent_role", "")
            if rec_role != agent_name:
                continue
            if not _is_eligible_agent_role(rec_role):
                continue
            pid = str(rec.get("project_id", ""))
            if pid.startswith("test_") or pid.startswith("proj_test_"):
                continue  # exclude mock/test data from evolution evidence
            # [Gap 2e + LOG-M3 Round2 2026-04-19] Carry raw_output — explicit
            # None check (not or-chain) so an empty string remains empty and
            # isn't silently replaced by summary.
            raw = rec.get("raw_output")
            if raw is None:
                raw = rec.get("summary") or ""
            matches.append({
                "task": rec.get("action") or rec.get("title", "")[:120],
                "score": int(rec.get("quality_score", 3)),
                "output_summary": (rec.get("summary") or "")[:200],
                "raw_output": raw[:2000] if isinstance(raw, str) else "",
                "score_source": rec.get("score_source", "unknown"),
            })
        return matches[-limit:]
    except Exception:
        return []


def _read_agent_prompt(agent_name: str) -> str:
    """Read agent .md, strip YAML frontmatter. Empty string if missing."""
    agent_file = _WORKSPACE / ".claude" / "agents" / f"{agent_name}.md"
    if not agent_file.exists():
        return ""
    try:
        raw = agent_file.read_text(encoding="utf-8")
        # Strip leading --- ... --- frontmatter
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                return parts[2].lstrip()
        return raw
    except Exception:
        return ""


def _evolve_one_agent(agent_name: str) -> dict:
    """Run one evolve cycle for a single agent. Sync bridge to async evolve().

    Returns status dict: one of
      - {"agent": ..., "status": "no_outcomes_yet", "have": N, "need": MIN}
      - {"agent": ..., "status": "no_prompt_file"}
      - {"agent": ..., "status": "evolved_deployed", "new_score": ...}
      - {"agent": ..., "status": "evolved_rejected", "reason": ...}
      - {"agent": ..., "status": "error:<msg>"}
    """
    outcomes = _load_outcomes_for_agent(agent_name)
    try:
        from memexa.core.prompt_evolver import MIN_OUTCOMES
    except Exception:
        MIN_OUTCOMES = 5
    if len(outcomes) < MIN_OUTCOMES:
        return {
            "agent": agent_name,
            "status": "no_outcomes_yet",
            "have": len(outcomes),
            "need": MIN_OUTCOMES,
        }

    current_prompt = _read_agent_prompt(agent_name)
    if not current_prompt:
        return {"agent": agent_name, "status": "no_prompt_file"}

    try:
        import asyncio
        from memexa.core.prompt_evolver import get_prompt_evolver
        evolver = get_prompt_evolver()
        # [LOG-H4 Round3 2026-04-19] Detect-then-execute with SEPARATE
        # try/except scopes. Round 2 conflated "no loop" with "runtime
        # error inside evolver" — both caught by the same except,
        # falling through to asyncio.run in an active loop.
        _has_loop = False
        try:
            asyncio.get_running_loop()
            _has_loop = True
        except RuntimeError:
            # No running loop — plain asyncio.run path below.
            _has_loop = False

        if _has_loop:
            # Running inside an active loop — spawn worker thread with its
            # own loop so the synchronous call-site contract is preserved.
            import concurrent.futures

            def _runner():
                return asyncio.run(
                    evolver.evolve(agent_name, current_prompt, outcomes)
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                new_prompt = ex.submit(_runner).result(timeout=300)
        else:
            new_prompt = asyncio.run(
                evolver.evolve(agent_name, current_prompt, outcomes)
            )
        if new_prompt:
            return {"agent": agent_name, "status": "evolved_deployed"}
        return {"agent": agent_name, "status": "evolved_rejected"}
    except Exception as e:
        return {"agent": agent_name, "status": f"error:{type(e).__name__}:{e}"[:150]}


def is_enabled() -> bool:
    """ENV flag. Default OFF — prompt evolution modifies .md files,
    CEO must explicitly enable via MEMEXA_L6_EVOLUTION=1.
    """
    return os.environ.get("MEMEXA_L6_EVOLUTION", "0") == "1"


def _get_lock():
    """Return a filelock.FileLock. Missing lib → no-op context manager."""
    try:
        from filelock import FileLock
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(_LOCK_FILE), timeout=5.0)
    except ImportError:
        import contextlib
        @contextlib.contextmanager
        def _noop():
            yield
        return _noop()


def _read_harness_state() -> dict:
    if not _HARNESS_STATE.exists():
        return {}
    try:
        return json.loads(_HARNESS_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_harness_state(state: dict) -> None:
    import tempfile
    _HARNESS_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(_HARNESS_STATE.parent), suffix=".json.tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        Path(tmp_path).replace(_HARNESS_STATE)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _is_cold_start(state: dict) -> bool:
    """Check if current session is a cold start (not resume/clear)."""
    auto_dream = state.get("auto_dream", {})
    last_reason = auto_dream.get("last_shutdown_reason", "")
    return last_reason not in _EXCLUDE_SHUTDOWN_REASONS


def increment_session_counter() -> dict:
    """Called from SessionStart hook. Returns status dict.

    Only increments on cold start (not resume). Race-safe via filelock.
    """
    lock = _get_lock()
    try:
        with lock:
            state = _read_harness_state()
            if not _is_cold_start(state):
                return {"skipped": True, "reason": "resume_or_clear"}

            pe = state.setdefault("prompt_evolution", {})
            pe["sessions_since_last_evolve"] = pe.get("sessions_since_last_evolve", 0) + 1
            pe["last_count_bump_at"] = datetime.now(timezone.utc).isoformat()

            _write_harness_state(state)
            return {
                "incremented": True,
                "count": pe["sessions_since_last_evolve"],
            }
    except Exception as e:
        return {"skipped": True, "reason": f"error:{e}"}


def _hours_since(ts_iso: Optional[str]) -> float:
    if not ts_iso:
        return 1e9  # infinity
    try:
        t = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return 1e9


def check_and_trigger() -> dict:
    """Check both AND conditions. If met, fire prompt_evolver.

    [SEC-MED + LOGIC-HIGH 2026-04-18] Atomic check-and-claim:
    counter is zeroed AND in_progress=True set inside the SAME lock scope
    before evolver runs. This prevents double-trigger from concurrent hooks.

    Returns dict describing what happened (for observability).
    """
    if not is_enabled():
        return {"triggered": False, "reason": "disabled_env_flag"}

    # Single-critical-section: check AND claim (zero counter + set in_progress)
    claim = {}
    lock = _get_lock()
    try:
        with lock:
            state = _read_harness_state()
            pe = state.setdefault("prompt_evolution", {})

            # [LOGIC-HIGH Round2 + SEC-MED Round2 fix 2026-04-18]
            # Stale-lock self-heal: if in_progress flag is > 4h old, auto-clear.
            # Protects against SIGKILL/OOM leaving in_progress=True forever.
            if pe.get("in_progress"):
                since = pe.get("in_progress_since")
                if since and _hours_since(since) > 4.0:
                    pe["in_progress"] = False
                    pe["in_progress_since"] = None
                    pe["last_stale_clear_at"] = datetime.now(timezone.utc).isoformat()
                    _write_harness_state(state)
                    # fall through to threshold check
                else:
                    return {
                        "triggered": False,
                        "reason": "already_in_progress",
                        "since": since,
                    }

            count = pe.get("sessions_since_last_evolve", 0)
            last_evolve = pe.get("last_successful_evolve_at")
            elapsed_h = _hours_since(last_evolve)

            threshold = _DEFAULT_THRESHOLD
            cond_count = count >= threshold
            cond_time = elapsed_h >= _MIN_ELAPSED_HOURS

            if not (cond_count and cond_time):
                return {
                    "triggered": False,
                    "reason": "threshold_unmet",
                    "count": count,
                    "threshold": threshold,
                    "elapsed_hours": round(elapsed_h, 1),
                    "min_elapsed_hours": _MIN_ELAPSED_HOURS,
                }

            # CLAIM: zero counter + set in_progress BEFORE releasing lock
            now_iso = datetime.now(timezone.utc).isoformat()
            pe["in_progress"] = True
            pe["in_progress_since"] = now_iso
            pe["sessions_since_last_evolve"] = 0  # prevent next hook from re-triggering
            pe["last_trigger_at"] = now_iso
            _write_harness_state(state)
            claim = {"count": count, "elapsed_h": elapsed_h}

        # Out-of-lock work (evolver may be slow; safe because in_progress=True)
        try:
            from memexa.core.prompt_evolver import (
                is_agent_eligible,
                _ELIGIBLE_AGENTS,
            )
        except Exception as e:
            # Release claim on import error
            with lock:
                state = _read_harness_state()
                pe = state.setdefault("prompt_evolution", {})
                pe["in_progress"] = False
                _write_harness_state(state)
            return {"triggered": False, "reason": f"import_error:{e}"}

        # [B2 fix 2026-04-18] Honor MEMEXA_EVOLVE_AGENTS env var (single source
        # of truth in prompt_evolver._ELIGIBLE_AGENTS). Previous hardcoded
        # tuple silently ignored env override.
        eligible = sorted(a for a in _ELIGIBLE_AGENTS if is_agent_eligible(a))

        if not eligible:
            with lock:
                state = _read_harness_state()
                pe = state.setdefault("prompt_evolution", {})
                pe["in_progress"] = False
                pe["last_result"] = "no_eligible_agents"
                _write_harness_state(state)
            return {"triggered": False, "reason": "no_eligible_agents"}

        autorun = os.environ.get("MEMEXA_L6_AUTORUN", "0") == "1"

        if not autorun:
            # Queue for CEO approval, release in_progress claim (counter stays 0
            # until CEO approves; next hook won't re-trigger)
            with lock:
                state = _read_harness_state()
                pe = state.setdefault("prompt_evolution", {})
                pe["in_progress"] = False
                pe["eligible_agents"] = eligible
                pe["pending_approval"] = True
                _write_harness_state(state)
            return {
                "triggered": True,
                "queued_for_approval": True,
                "eligible_agents": eligible,
                "reason": "conditions_met",
                **claim,
            }

        # [B1 fix 2026-04-18] TRUE evolve path — real work, no longer a stub.
        # Calls _evolve_one_agent which loads outcomes from kairos_feedback.jsonl
        # and invokes PromptEvolver.evolve(). Honest status when no data.
        results = [_evolve_one_agent(a) for a in eligible]

        # Mark success: clear in_progress + record timestamp
        with lock:
            state = _read_harness_state()
            pe = state.setdefault("prompt_evolution", {})
            pe["in_progress"] = False
            pe["last_successful_evolve_at"] = datetime.now(timezone.utc).isoformat()
            pe["last_results"] = results
            pe["pending_approval"] = False
            _write_harness_state(state)

        return {
            "triggered": True,
            "executed": True,
            "results": results,
            **claim,
        }
    except Exception as e:
        # [LOGIC-LOW Round2 fix 2026-04-18] Defend against data-loss cleanup:
        # if _read_harness_state returns {} but file is non-empty, abort rather
        # than overwrite the full harness_state with just {prompt_evolution:...}
        try:
            with lock:
                state = _read_harness_state()
                # Only release if we can read existing state; otherwise skip
                # to avoid overwriting valid state with partial {}.
                if state or not _HARNESS_STATE.exists():
                    pe = state.setdefault("prompt_evolution", {})
                    pe["in_progress"] = False
                    _write_harness_state(state)
        except Exception:
            pass
        return {"triggered": False, "reason": f"error:{e}"}


def get_counter_status() -> dict:
    """Read-only snapshot for dashboard.

    [B4 fix 2026-04-18] Surface in_progress + eligible_agents + last_results
    so the L7 dashboard can show stuck claims and recent evolve outcomes.
    """
    state = _read_harness_state()
    pe = state.get("prompt_evolution", {})
    count = pe.get("sessions_since_last_evolve", 0)
    last_evolve = pe.get("last_successful_evolve_at")
    elapsed_h = _hours_since(last_evolve)
    return {
        "count": count,
        "threshold": _DEFAULT_THRESHOLD,
        "elapsed_hours": round(elapsed_h, 1),
        "min_elapsed_hours": _MIN_ELAPSED_HOURS,
        "last_evolve": last_evolve,
        "sessions_until_trigger": max(0, _DEFAULT_THRESHOLD - count),
        "enabled": is_enabled(),
        "pending_approval": pe.get("pending_approval", False),
        "in_progress": pe.get("in_progress", False),
        "in_progress_since": pe.get("in_progress_since"),
        "eligible_agents": pe.get("eligible_agents", []),
        "last_results": pe.get("last_results", []),
    }


def main():
    """CLI: python -m memexa.core.evolution_trigger [status|increment|check]"""
    import sys
    if len(sys.argv) < 2:
        print("Usage: evolution_trigger [status|increment|check]", file=sys.stderr)
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "status":
        print(json.dumps(get_counter_status(), ensure_ascii=False, indent=2))
    elif mode == "increment":
        print(json.dumps(increment_session_counter(), ensure_ascii=False, indent=2))
    elif mode == "check":
        print(json.dumps(check_and_trigger(), ensure_ascii=False, indent=2))
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
