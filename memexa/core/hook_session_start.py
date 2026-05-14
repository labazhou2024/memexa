"""hook_session_start — TU-A4 self_evolution_reconnect (2026-05-04).

SessionStart hook: 检 sessions_since_last_dream OR 距上次 dream ≥72h
→ 后台 spawn auto_dream consolidate. Fail-soft: 不阻塞 SessionStart.

Also (TU-A7 bridge): SessionStart fallback for approval_timeout_processor
当 last_run.json mtime ≥48h 时触发兜底处理（cron 万一断了）.

CLI:
    python -m memexa.core.hook_session_start          # invoked by hook
    python -m memexa.core.hook_session_start --check  # status only, no spawn
    python -m memexa.core.hook_session_start --force-dream  # force trigger
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

WORKSPACE = Path(__file__).resolve().parents[2]
DATA_DIR = WORKSPACE / "data"
LAST_DREAM_PATH = DATA_DIR / "last_dream.json"
APPROVAL_TIMEOUT_LAST_RUN = DATA_DIR / "approval_timeout_last_run.json"
HARNESS_PATH = WORKSPACE / ".claude" / "config" / "harness_state.json"

DREAM_SESSION_THRESHOLD = 5
DREAM_HOURS_THRESHOLD = 72.0
APPROVAL_FALLBACK_HOURS = 48.0


def _emit_trace(event: str, payload: Dict[str, Any]) -> None:
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:  # pragma: no cover
        pass


def _hours_since(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        return (time.time() - path.stat().st_mtime) / 3600.0
    except OSError:
        return None


def _read_dream_state() -> Dict[str, Any]:
    out = {"last_dream_at": None, "sessions_since": 0,
           "hours_since": None, "should_trigger": False, "reason": ""}
    if LAST_DREAM_PATH.exists():
        try:
            data = json.loads(LAST_DREAM_PATH.read_text(encoding="utf-8"))
            out["last_dream_at"] = data.get("ts")
            out["sessions_since"] = int(data.get("sessions_since_last_dream", 0))
        except (OSError, json.JSONDecodeError):
            pass
    out["hours_since"] = _hours_since(LAST_DREAM_PATH)
    # Decide trigger
    if out["hours_since"] is None:
        out["should_trigger"] = True
        out["reason"] = "no_prior_dream_record"
    elif out["sessions_since"] >= DREAM_SESSION_THRESHOLD:
        out["should_trigger"] = True
        out["reason"] = f"sessions_since>={DREAM_SESSION_THRESHOLD}"
    elif out["hours_since"] >= DREAM_HOURS_THRESHOLD:
        out["should_trigger"] = True
        out["reason"] = f"hours_since>={DREAM_HOURS_THRESHOLD}"
    return out


def _trigger_auto_dream() -> Dict[str, Any]:
    """Background spawn auto_dream consolidate. Returns immediately."""
    try:
        cmd = [sys.executable, "-m", "memexa.core.auto_dream", "consolidate"]
        # Background, don't wait. stdout to log file.
        log_path = DATA_DIR / "dream_reports" / f"dream_{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            subprocess.Popen(
                cmd, stdout=f, stderr=subprocess.STDOUT,
                cwd=str(WORKSPACE),
            )
        return {"spawned": True, "log_path": str(log_path)[-80:]}
    except Exception as e:
        return {"spawned": False, "error": f"{type(e).__name__}: {e}"}


def _check_approval_fallback() -> Dict[str, Any]:
    """TU-A7 bridge: fire approval_timeout_processor when cron seems stalled."""
    out = {"fallback_fired": False, "reason": ""}
    hours = _hours_since(APPROVAL_TIMEOUT_LAST_RUN)
    if hours is None:
        out["reason"] = "no_prior_run"
    elif hours >= APPROVAL_FALLBACK_HOURS:
        out["reason"] = f"hours_since>={APPROVAL_FALLBACK_HOURS}"
    else:
        out["reason"] = f"hours_since={hours:.1f} < {APPROVAL_FALLBACK_HOURS}"
        return out
    # Fire fallback
    try:
        cmd = [sys.executable, "-m", "tools.approval_timeout_processor", "process"]
        log_path = DATA_DIR / "approval_timeout_logs" / f"fallback_{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                             cwd=str(WORKSPACE))
        out["fallback_fired"] = True
        _emit_trace("approval_timeout_fallback_triggered",
                    {"reason": out["reason"], "log": str(log_path)[-60:]})
    except Exception as e:
        out["reason"] += f" (spawn failed: {e})"
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Status only, no background spawn")
    parser.add_argument("--force-dream", action="store_true",
                        help="Force trigger auto_dream regardless of thresholds")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON to stdout")
    args = parser.parse_args()

    state = _read_dream_state()
    approval_state = {"checked": False}

    spawn_result = {"spawned": False}
    if args.force_dream or (not args.check and state["should_trigger"]):
        spawn_result = _trigger_auto_dream()
        _emit_trace("auto_dream_triggered", {
            "trigger_reason": state["reason"],
            "hours_since": state["hours_since"],
            "sessions_since": state["sessions_since"],
            "force": args.force_dream,
        })

    if not args.check:
        approval_state = _check_approval_fallback()
        approval_state["checked"] = True

    summary = {
        "dream_state": state,
        "spawn_result": spawn_result,
        "approval_fallback": approval_state,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"dream: should_trigger={state['should_trigger']} reason={state['reason']!r}")
        print(f"  hours_since={state['hours_since']} sessions_since={state['sessions_since']}")
        if spawn_result.get("spawned"):
            print(f"  → SPAWNED auto_dream: {spawn_result['log_path']}")
        if approval_state.get("fallback_fired"):
            print(f"approval: FALLBACK FIRED ({approval_state['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
