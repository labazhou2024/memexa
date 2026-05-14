"""
mini_loop_runner.py -- AC-C1 pre-commit probe runner (2026-04-24).

Runs probe commands before complex-task commits to validate gate health.
Operates in two modes:
  - sync (complexity == "complex"): runs immediately, blocks commit on failure
  - async (other complexity): fire-and-forget, no commit block

Trace events emitted: mini_loop_runner_start, mini_loop_runner_probe,
mini_loop_runner_done, mini_loop_runner_l2_emitted.

Called as:
  python -m src.core.mini_loop_runner run [--pre-commit] [--tid TID]
  python -m src.core.mini_loop_runner status
  python -m src.core.mini_loop_runner config-show
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WORKSPACE = Path(__file__).resolve().parents[3]
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CONFIG_PATH = _DATA_DIR / "mini_loop_config.json"
_SPEC_PATH = _DATA_DIR / "task_spec.json"
_RESULTS_PATH = _DATA_DIR / "mini_loop_results.jsonl"

# Used by tests to override file paths via environment variables
_CONFIG_PATH_ENV = "MEMEXA_MINI_LOOP_CONFIG_PATH"
_SPEC_PATH_ENV = "MEMEXA_MINI_LOOP_SPEC_PATH"
_RESULTS_PATH_ENV = "MEMEXA_MINI_LOOP_RESULTS_PATH"

# Grace period: spec mtime older than SPEC_STALE_PROBE_SEC is considered
# stale only when status is NOT "in_progress". TU-4 (plan_v1, 2026-04-25)
# extracts this to ``src.core._thresholds`` so persistent_mode (REGEN
# semantics, 24h) and mini_loop_runner (PROBE semantics, 30min) share a
# single source while preserving the priority-inverted 2x2 matrix.
from src.core._thresholds import SPEC_STALE_PROBE_SEC
_SPEC_STALE_MINUTES = SPEC_STALE_PROBE_SEC // 60

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict = {
    "commit_threshold": 1,
    "sync_for_complexity": "complex",
    "probe_commands": [
        "python -m src.core.gates.integration_gate check {tid}",
        "python -m src.core.gates.hook_chain_probe {tid}",
    ],
}


# ---------------------------------------------------------------------------
# Path helpers (test-redirectable)
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    override = os.environ.get(_CONFIG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return _CONFIG_PATH


def _spec_path() -> Path:
    override = os.environ.get(_SPEC_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return _SPEC_PATH


def _results_path() -> Path:
    override = os.environ.get(_RESULTS_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return _RESULTS_PATH


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_config() -> Dict:
    """Load mini_loop_config.json; returns DEFAULT_CONFIG if file missing."""
    cfg_path = _config_path()
    if not cfg_path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning("mini_loop_runner: config not a dict, using defaults")
            return dict(DEFAULT_CONFIG)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("mini_loop_runner: failed to load config (%s), using defaults", exc)
        return dict(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Sync determination
# ---------------------------------------------------------------------------


def _should_run_sync(complexity: Optional[str]) -> bool:
    """Return True iff complexity == 'complex' (exact string match)."""
    return complexity == "complex"


# ---------------------------------------------------------------------------
# Stale spec detection
# ---------------------------------------------------------------------------


def _detect_stale_spec(spec_path: Optional[Path] = None) -> Tuple[bool, str]:
    """Check whether task_spec.json is stale.

    Returns (is_stale: bool, reason: str).
    Reason values:
      "spec_missing"      -- file does not exist
      "completed_status"  -- status field == "completed"
      "old_in_progress"   -- mtime > 30 min old AND status != "in_progress"
      "corrupted"         -- JSON parse error or unexpected structure
      ""                  -- not stale (healthy)
    """
    path = spec_path if spec_path is not None else _spec_path()

    if not path.exists():
        return (True, "spec_missing")

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return (True, "corrupted")

    if not isinstance(data, dict):
        return (True, "corrupted")

    status = data.get("status", "")

    if status == "completed":
        return (True, "completed_status")

    # Check mtime-based staleness for non-in_progress statuses
    try:
        mtime = path.stat().st_mtime
        age_minutes = (time.time() - mtime) / 60.0
        if age_minutes > _SPEC_STALE_MINUTES and status != "in_progress":
            return (True, "old_in_progress")
    except OSError:
        pass

    return (False, "")


# ---------------------------------------------------------------------------
# Trace helper (fail-soft)
# ---------------------------------------------------------------------------


def _emit_trace(event: str, payload: Dict) -> None:
    """Emit a trace event. Never raises (fail-soft)."""
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception as exc:
        logger.debug("mini_loop_runner: trace emit failed: %s", exc)


# ---------------------------------------------------------------------------
# L2 approval helper
# ---------------------------------------------------------------------------


def _emit_l2_approval(task_id: Optional[str], failed_probes: List[Dict]) -> None:
    """Emit an L2 pending approval when sync probes fail for complex tasks."""
    try:
        from src.core.approval_queue import submit_approval

        failed_cmds = [p["cmd"] for p in failed_probes]
        submit_approval(
            level="L2",
            category="gate_probe_failure",
            title=f"[MiniLoop] Pre-commit gate probes failed for task {task_id or 'unknown'}",
            context=(
                f"mini_loop_runner detected {len(failed_cmds)} failed probe(s) "
                f"during sync pre-commit check for complex task '{task_id}'."
            ),
            proposal=(
                "Investigate failing probes before proceeding with commit. "
                "Failed commands: " + "; ".join(failed_cmds)
            ),
            evidence=[f"exit_code={p['exit_code']} cmd={p['cmd']}" for p in failed_probes],
            impact="Commit blocked until CEO acknowledges or probes pass.",
            blocked_tasks=[task_id] if task_id else [],
        )
        _emit_trace("mini_loop_runner_l2_emitted", {
            "task_id": task_id,
            "failed_probe_count": len(failed_probes),
        })
    except Exception as exc:
        logger.warning("mini_loop_runner: L2 emit failed: %s", exc)


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_probes(
    active_tid: Optional[str],
    complexity: Optional[str],
    probe_commands: Optional[List[str]] = None,
    sync: Optional[bool] = None,
) -> Dict:
    """Run gate probes and return summary dict.

    Args:
        active_tid:      Active task ID for env propagation and log.
        complexity:      Task complexity string (e.g. "complex", "simple").
        probe_commands:  Override probe command list. If None, loaded from config.
        sync:            Override sync/async mode. If None, derived from complexity.

    Returns:
        {
          "task_id": str | None,
          "complexity": str | None,
          "sync": bool,
          "probes": [{"cmd": str, "exit_code": int, "duration_ms": int,
                       "stdout_tail": str}],
          "any_failed": bool,
        }
    """
    cfg = _load_config()

    if probe_commands is None:
        probe_commands = cfg.get("probe_commands", DEFAULT_CONFIG["probe_commands"])

    if sync is None:
        sync = _should_run_sync(complexity)

    _emit_trace("mini_loop_runner_start", {
        "task_id": active_tid,
        "complexity": complexity,
        "sync": sync,
        "probe_count": len(probe_commands),
    })

    probe_results: List[Dict] = []

    for raw_cmd in probe_commands:
        # Interpolate {tid} placeholder safely
        tid_str = active_tid or ""
        cmd = raw_cmd.replace("{tid}", tid_str)

        # Build subprocess env with task binding
        try:
            from src.core.task_binding import propagate_to_subprocess
            sub_env = propagate_to_subprocess(task_id=active_tid)
        except Exception as exc:
            logger.warning("mini_loop_runner: propagate_to_subprocess failed: %s", exc)
            sub_env = dict(os.environ)
            if active_tid:
                sub_env["MEMEXA_ACTIVE_TASK_ID"] = active_tid

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                env=sub_env,
                timeout=120,
            )
            exit_code = proc.returncode
            stdout_raw = proc.stdout or ""
            # Keep last ~500 chars of stdout as tail
            stdout_tail = stdout_raw[-500:] if len(stdout_raw) > 500 else stdout_raw
        except subprocess.TimeoutExpired:
            exit_code = -1
            stdout_tail = "<timeout>"
        except OSError as exc:
            exit_code = -2
            stdout_tail = f"<os_error: {exc}>"

        duration_ms = int((time.monotonic() - t0) * 1000)

        probe_entry = {
            "cmd": cmd,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "stdout_tail": stdout_tail.strip(),
        }
        probe_results.append(probe_entry)

        _emit_trace("mini_loop_runner_probe", {
            "task_id": active_tid,
            "cmd": cmd,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
        })

    any_failed = any(p["exit_code"] != 0 for p in probe_results)

    result = {
        "task_id": active_tid,
        "complexity": complexity,
        "sync": sync,
        "probes": probe_results,
        "any_failed": any_failed,
    }

    # Write to results JSONL for trace
    _write_results(result)

    # L2 approval if sync complex probes failed
    if any_failed and sync and complexity == "complex":
        failed = [p for p in probe_results if p["exit_code"] != 0]
        _emit_l2_approval(active_tid, failed)

    _emit_trace("mini_loop_runner_done", {
        "task_id": active_tid,
        "any_failed": any_failed,
        "probe_count": len(probe_results),
    })

    return result


def _write_results(result: Dict) -> None:
    """Append result summary to _results.jsonl. Fail-soft."""
    try:
        rpath = _results_path()
        rpath.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(result, ensure_ascii=False) + "\n"
        with open(rpath, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        logger.warning("mini_loop_runner: write results failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="mini_loop_runner")
    sub = p.add_subparsers(dest="cmd", required=True)

    # run
    p_run = sub.add_parser("run", help="Run gate probes")
    p_run.add_argument("--pre-commit", action="store_true",
                       help="Pre-commit mode: exit non-zero if sync+complex+any_failed")
    p_run.add_argument("--tid", default=None, help="Override active task ID")
    p_run.add_argument("--complexity", default=None, help="Override complexity")

    # status
    sub.add_parser("status", help="Show last run status")

    # config-show
    sub.add_parser("config-show", help="Print resolved config")

    args = p.parse_args(argv)

    if args.cmd == "run":
        # Resolve task ID and complexity
        tid = args.tid
        if tid is None:
            try:
                from src.core.task_binding import get_active_task_id
                tid = get_active_task_id()
            except Exception:
                tid = os.environ.get("MEMEXA_ACTIVE_TASK_ID")

        complexity = args.complexity
        if complexity is None:
            # Try to read from task_spec
            try:
                spec_data = json.loads(_spec_path().read_text(encoding="utf-8"))
                complexity = spec_data.get("complexity")
            except Exception:
                complexity = None

        result = run_probes(active_tid=tid, complexity=complexity)
        print(json.dumps(result, ensure_ascii=False, indent=2))

        if args.pre_commit and result["any_failed"] and result["sync"]:
            print(
                f"[mini_loop_runner] BLOCKING commit: {sum(1 for p in result['probes'] if p['exit_code']!=0)} "
                f"probe(s) failed for complex task.",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.cmd == "status":
        rpath = _results_path()
        if not rpath.exists():
            print("No results yet.")
            return 0
        # Show last entry
        try:
            lines = rpath.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                last = json.loads(lines[-1])
                print(json.dumps(last, ensure_ascii=False, indent=2))
            else:
                print("No results yet.")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error reading results: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.cmd == "config-show":
        cfg = _load_config()
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
