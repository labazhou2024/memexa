"""big_loop_trigger — TU-A5 self_evolution_reconnect (2026-05-04).

post-commit hook entry. Increments commits_since_last_big_loop counter.
When counter ≥ MEMEX_BIG_LOOP_THRESHOLD (default 10) → background spawn
big_loop full cycle + reset counter. Fail-soft: never blocks commit.

CLI:
    python -m src.core.big_loop_trigger          # invoked by post-commit
    python -m src.core.big_loop_trigger --status # show counter, no inc
    python -m src.core.big_loop_trigger --reset  # zero counter
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
DATA_DIR = WORKSPACE / "data"
COUNTER_PATH = DATA_DIR / "commits_since_last_big_loop.txt"
LAST_LOOP_PATH = DATA_DIR / "last_big_loop.json"

DEFAULT_THRESHOLD = 10


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:  # pragma: no cover
        pass


def _read_counter() -> int:
    if not COUNTER_PATH.exists():
        return 0
    try:
        return int(COUNTER_PATH.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_counter(n: int) -> bool:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        COUNTER_PATH.write_text(str(n), encoding="utf-8")
        return True
    except OSError:
        return False


def _spawn_big_loop() -> dict:
    """Background spawn big_loop main. Returns spawn metadata."""
    try:
        cmd = [sys.executable, "-m", "src.core.big_loop"]
        log_path = DATA_DIR / "big_loop_logs" / f"loop_{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as f:
            p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                                 cwd=str(WORKSPACE))
        return {"spawned": True, "pid": p.pid, "log_path": str(log_path)[-80:]}
    except Exception as e:
        return {"spawned": False, "error": f"{type(e).__name__}: {e}"}


def _record_big_loop_fired() -> None:
    try:
        LAST_LOOP_PATH.write_text(json.dumps({
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2), encoding="utf-8")
    except OSError:
        pass


def trigger_check() -> dict:
    """Increment counter; if ≥ threshold → spawn + reset."""
    threshold = int(os.environ.get("MEMEX_BIG_LOOP_THRESHOLD",
                                   str(DEFAULT_THRESHOLD)))
    n = _read_counter() + 1
    _write_counter(n)
    out = {"counter": n, "threshold": threshold, "fired": False}

    if n >= threshold:
        spawn = _spawn_big_loop()
        out["fired"] = bool(spawn.get("spawned"))
        out["spawn_result"] = spawn
        if spawn.get("spawned"):
            _write_counter(0)  # reset on successful spawn
            _record_big_loop_fired()
            _emit_trace("big_loop_triggered", {
                "commits_since": n,
                "threshold": threshold,
                "pid": spawn.get("pid"),
            })
        else:
            # spawn failed but counter still inc'd; let next commit retry
            _emit_trace("big_loop_spawn_failed", {
                "commits_since": n,
                "error": spawn.get("error", ""),
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true",
                        help="Show counter only, no increment")
    parser.add_argument("--reset", action="store_true",
                        help="Reset counter to 0")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.status:
        result = {
            "counter": _read_counter(),
            "threshold": int(os.environ.get("MEMEX_BIG_LOOP_THRESHOLD",
                                            str(DEFAULT_THRESHOLD))),
            "last_loop_path": str(LAST_LOOP_PATH),
            "last_loop_exists": LAST_LOOP_PATH.exists(),
        }
    elif args.reset:
        _write_counter(0)
        result = {"reset": True, "counter": 0}
    else:
        result = trigger_check()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"big_loop counter: {result.get('counter')} (threshold {result.get('threshold')})")
        if result.get("fired"):
            print(f"  → FIRED big_loop: {result.get('spawn_result', {})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
