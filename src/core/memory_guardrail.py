"""Memory Runaway Guardrail — Rule 18 (2026-05-05).

Reference incident: 2026-05-02 20:30:01 Windows Resource-Exhaustion-Detector
event 2004. python.exe (PID 33592) accumulated 89.6 GB virtual memory
because backfill_wechat_driver passed `extract_triples=lambda m,c: []` as
injected dependency; upstream batched 56k WeChat msgs into memory waiting
for extraction results that never came. Pagefile auto-grew 16GB → 92GB
(non-reversible without reboot + size-cap).

This module:
  1. Polls current process commit charge (psutil VMS) on a daemon thread.
  2. When threshold crossed, dumps thread stacks + child process tree to
     `data/memory_guardrail_dumps/<ts>.txt` and writes JSONL alert.
  3. Writes `data/memory_runaway_blocked.flag` for pretool_gate Rule 18 to
     deny new Bash spawns of known-dangerous pipelines.
  4. Optionally `os._exit(2)` when a hard-kill threshold is crossed.

CLI:
  python -m src.core.memory_guardrail status   # current usage + flag state
  python -m src.core.memory_guardrail clear    # remove block flag (CEO ack)
  python -m src.core.memory_guardrail monitor  # foreground watchdog (debug)

Library:
  from src.core.memory_guardrail import MemoryGuardrail
  MemoryGuardrail.start_default()  # idempotent; daemon thread, fail-open.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]


def _workspace_root() -> Path:
    marker = Path(".claude") / "config" / "settings.json"
    try:
        cwd = Path(os.getcwd())
        if (cwd / marker).exists():
            return cwd
    except OSError:
        pass
    try:
        p = Path(__file__).resolve()
        for _ in range(8):
            p = p.parent
            if (p / marker).exists():
                return p
    except OSError:
        pass
    return Path(os.getcwd())


_WS = _workspace_root()
_DATA_DIR = _WS / "memex" / "data"
_FLAG_FILE = _DATA_DIR / "memory_runaway_blocked.flag"
_ALERTS_JSONL = _DATA_DIR / "memory_guardrail_alerts.jsonl"
_DUMP_DIR = _DATA_DIR / "memory_guardrail_dumps"

_WARN_GB_DEFAULT = 30.0
_BLOCK_GB_DEFAULT = 40.0
_KILL_GB_DEFAULT = 60.0
_INTERVAL_S_DEFAULT = 15.0


@dataclass
class GuardrailReading:
    ts: str
    pid: int
    vms_gb: float
    rss_gb: float
    children_vms_gb: float
    threshold_warn_gb: float
    threshold_block_gb: float
    threshold_kill_gb: float
    state: str  # "ok" | "warn" | "block" | "kill"
    note: str = ""

    def to_jsonl(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


@dataclass
class MemoryGuardrail:
    threshold_warn_gb: float = _WARN_GB_DEFAULT
    threshold_block_gb: float = _BLOCK_GB_DEFAULT
    threshold_kill_gb: float = _KILL_GB_DEFAULT
    interval_s: float = _INTERVAL_S_DEFAULT
    hard_kill: bool = False  # True → os._exit(2) on kill threshold
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    @classmethod
    def start_default(cls) -> "MemoryGuardrail":
        """Idempotent helper: install a daemon watchdog with default thresholds.

        Reads env overrides:
          MEMEX_MEM_WARN_GB / MEMEX_MEM_BLOCK_GB / MEMEX_MEM_KILL_GB / MEMEX_MEM_INTERVAL_S
          MEMEX_MEM_HARD_KILL ∈ {"1","true","yes"}
        """
        warn = float(os.environ.get("MEMEX_MEM_WARN_GB", _WARN_GB_DEFAULT))
        block = float(os.environ.get("MEMEX_MEM_BLOCK_GB", _BLOCK_GB_DEFAULT))
        kill = float(os.environ.get("MEMEX_MEM_KILL_GB", _KILL_GB_DEFAULT))
        interval = float(os.environ.get("MEMEX_MEM_INTERVAL_S", _INTERVAL_S_DEFAULT))
        hard = os.environ.get("MEMEX_MEM_HARD_KILL", "").strip().lower() in ("1", "true", "yes")
        g = cls(threshold_warn_gb=warn, threshold_block_gb=block,
                threshold_kill_gb=kill, interval_s=interval, hard_kill=hard)
        g.start_monitor()
        return g

    def check_now(self) -> GuardrailReading:
        """One-shot reading. Always returns; never raises."""
        ts = datetime.now(timezone.utc).isoformat()
        if psutil is None:
            return GuardrailReading(ts=ts, pid=os.getpid(), vms_gb=0.0, rss_gb=0.0,
                                    children_vms_gb=0.0,
                                    threshold_warn_gb=self.threshold_warn_gb,
                                    threshold_block_gb=self.threshold_block_gb,
                                    threshold_kill_gb=self.threshold_kill_gb,
                                    state="ok", note="psutil unavailable")
        try:
            proc = psutil.Process()
            mi = proc.memory_info()
            vms_gb = mi.vms / (1024 ** 3)
            rss_gb = mi.rss / (1024 ** 3)
            child_vms = 0.0
            try:
                for ch in proc.children(recursive=True):
                    try:
                        child_vms += ch.memory_info().vms
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            child_vms_gb = child_vms / (1024 ** 3)
            total_vms = vms_gb + child_vms_gb
            if total_vms >= self.threshold_kill_gb:
                state = "kill"
            elif total_vms >= self.threshold_block_gb:
                state = "block"
            elif total_vms >= self.threshold_warn_gb:
                state = "warn"
            else:
                state = "ok"
            return GuardrailReading(ts=ts, pid=os.getpid(), vms_gb=round(vms_gb, 3),
                                    rss_gb=round(rss_gb, 3),
                                    children_vms_gb=round(child_vms_gb, 3),
                                    threshold_warn_gb=self.threshold_warn_gb,
                                    threshold_block_gb=self.threshold_block_gb,
                                    threshold_kill_gb=self.threshold_kill_gb,
                                    state=state)
        except Exception as e:  # noqa: BLE001 — guardrail must never raise
            return GuardrailReading(ts=ts, pid=os.getpid(), vms_gb=0.0, rss_gb=0.0,
                                    children_vms_gb=0.0,
                                    threshold_warn_gb=self.threshold_warn_gb,
                                    threshold_block_gb=self.threshold_block_gb,
                                    threshold_kill_gb=self.threshold_kill_gb,
                                    state="ok", note=f"check_now error: {e!r}")

    def start_monitor(self) -> None:
        """Start daemon watchdog. Idempotent (no-op if already running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(target=self._loop, name="memory_guardrail", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        last_state = "ok"
        while not self._stop.wait(self.interval_s):
            try:
                r = self.check_now()
            except Exception:
                continue
            if r.state == "ok" and last_state == "ok":
                continue  # quiet path
            self._emit_alert(r)
            if r.state in ("block", "kill") and last_state not in ("block", "kill"):
                self._write_flag(r)
                self._dump_stacks(r)
            if r.state == "kill" and self.hard_kill:
                # Hard exit: bypass cleanup; the runaway is already too big.
                os._exit(2)  # noqa: SLF001 — intentional, see docstring.
            last_state = r.state

    def _emit_alert(self, r: GuardrailReading) -> None:
        try:
            _ALERTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
            with _ALERTS_JSONL.open("a", encoding="utf-8") as f:
                f.write(r.to_jsonl() + "\n")
        except OSError:
            pass

    def _write_flag(self, r: GuardrailReading) -> None:
        """Write the flag file pretool_gate Rule 18 reads."""
        try:
            _FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": r.ts,
                "pid": r.pid,
                "vms_gb": r.vms_gb,
                "children_vms_gb": r.children_vms_gb,
                "state": r.state,
                "threshold_block_gb": r.threshold_block_gb,
                "reason": "memory_guardrail tripped",
            }
            tmp = _FLAG_FILE.with_suffix(_FLAG_FILE.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, _FLAG_FILE)
        except OSError:
            pass

    def _dump_stacks(self, r: GuardrailReading) -> None:
        """Dump current python thread stacks + child proc tree for postmortem."""
        try:
            _DUMP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = r.ts.replace(":", "-").replace(".", "-")
            dump = _DUMP_DIR / f"dump_{stamp}_pid{r.pid}.txt"
            with dump.open("w", encoding="utf-8") as f:
                f.write(f"# Memory guardrail dump\n")
                f.write(f"# {r.to_jsonl()}\n\n")
                f.write("=== thread stacks ===\n")
                for tid, frame in sys._current_frames().items():
                    f.write(f"\n--- thread {tid} ---\n")
                    f.write("".join(traceback.format_stack(frame)))
                if psutil is not None:
                    f.write("\n=== child process tree ===\n")
                    try:
                        for ch in psutil.Process().children(recursive=True):
                            try:
                                mi = ch.memory_info()
                                f.write(f"  pid={ch.pid} name={ch.name()} "
                                        f"vms_gb={mi.vms/1e9:.2f} "
                                        f"rss_gb={mi.rss/1e9:.2f} "
                                        f"cmd={' '.join(ch.cmdline())[:200]}\n")
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                continue
                    except Exception:
                        pass
        except OSError:
            pass


def is_blocked() -> bool:
    """Cheap check for callers (pretool_gate)."""
    return _FLAG_FILE.exists()


def read_flag() -> Optional[dict]:
    if not _FLAG_FILE.exists():
        return None
    try:
        return json.loads(_FLAG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_flag() -> bool:
    try:
        _FLAG_FILE.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cli_status() -> int:
    g = MemoryGuardrail()
    r = g.check_now()
    print(r.to_jsonl())
    flag = read_flag()
    if flag:
        print(f"[FLAG SET] {json.dumps(flag, ensure_ascii=False)}")
        return 1
    print("[no flag]")
    return 0


def _cli_clear() -> int:
    if clear_flag():
        print("flag cleared")
        return 0
    print("no flag to clear")
    return 0


def _cli_monitor() -> int:
    print("[memory_guardrail] foreground monitor — Ctrl+C to stop")
    g = MemoryGuardrail.start_default()
    try:
        while True:
            time.sleep(g.interval_s)
            r = g.check_now()
            print(r.to_jsonl())
    except KeyboardInterrupt:
        g.stop()
        return 0


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        return _cli_status()
    if cmd == "clear":
        return _cli_clear()
    if cmd == "monitor":
        return _cli_monitor()
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    print("usage: python -m src.core.memory_guardrail {status|clear|monitor}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
