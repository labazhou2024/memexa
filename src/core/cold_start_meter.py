"""TU-0 cold-start context footprint meter.

Measures bytes of every source that the Claude Code session auto-injects
at cold start (claudeMd, auto-memory, SessionStart hook stdout). Used as
regression gate for CLAUDE.md / MEMORY.md / session_start_gate trim effort
(plan 20260423_200000_reduce_cold_start).

CLI:
  python -m src.core.cold_start_meter             # print JSON histogram
  python -m src.core.cold_start_meter --baseline  # save snapshot to data/
  python -m src.core.cold_start_meter --check     # exit 1 if over limits
"""
from __future__ import annotations


import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional
from src.core._path_resolver import memory_dir

_MEMEXA = Path(__file__).resolve().parents[2]
_WORKSPACE = _MEMEXA.parent
_MEMORY_DIR = memory_dir()

# v1 thresholds (per plan_v1.md):
LIMITS = {
    "claude_md": 20000,
    "memexa_claudemd": 500,
    "memory_md": 24000,
    "session_start_gate_stdout": 2500,
}


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _session_start_gate_bytes() -> int:
    """Run session_start_gate.py and count stdout bytes.

    Captures STDOUT only (stderr is telemetry, not injected into context).
    Environment is stripped to mirror fresh cold-start (no MEMEXA_* leakage).
    """
    script = _MEMEXA / "memexa" / "core" / "session_start_gate.py"
    if not script.exists():
        return 0
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MEMEXA_", "CLAUDE_"))}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            encoding="utf-8", errors="replace",
            env=env, timeout=30, cwd=str(_MEMEXA),
        )
        return len(r.stdout.encode("utf-8"))
    except Exception:
        return -1


def collect() -> Dict[str, int]:
    """Collect current byte counts for each cold-start source."""
    return {
        "claude_md": _size(_WORKSPACE / "CLAUDE.md"),
        "memexa_claudemd": _size(_MEMEXA / "CLAUDE.md"),
        "memory_md": _size(_MEMORY_DIR / "MEMORY.md"),
        "session_start_gate_stdout": _session_start_gate_bytes(),
    }


def total(sizes: Dict[str, int]) -> int:
    """Total injected bytes (memory_md clamped to 24400 auto-memory soft limit)."""
    clamped_memory = min(sizes.get("memory_md", 0), 24400)
    return (
        sizes.get("claude_md", 0)
        + sizes.get("memexa_claudemd", 0)
        + clamped_memory
        + max(sizes.get("session_start_gate_stdout", 0), 0)
    )


def check(sizes: Dict[str, int]) -> Dict[str, bool]:
    """Return per-source pass/fail against LIMITS."""
    out = {}
    for k, lim in LIMITS.items():
        v = sizes.get(k, 0)
        out[k] = (0 <= v <= lim)
    return out


def save_baseline(sizes: Dict[str, int],
                  path: Optional[Path] = None) -> Path:
    path = path or (_MEMEXA / "data" / "cold_start_baseline.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sizes": sizes, "total": total(sizes),
                    "limits": LIMITS}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_baseline(path: Optional[Path] = None) -> Optional[dict]:
    path = path or (_MEMEXA / "data" / "cold_start_baseline.json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cold_start_meter")
    ap.add_argument("--baseline", action="store_true",
                    help="save current measurements as baseline")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any source exceeds limit")
    ap.add_argument("--json", action="store_true", default=True,
                    help="JSON output (default)")
    args = ap.parse_args(argv)

    sizes = collect()
    report = {
        "sizes": sizes,
        "total": total(sizes),
        "limits": LIMITS,
        "check": check(sizes),
    }
    baseline = load_baseline()
    if baseline:
        report["baseline_total"] = baseline["total"]
        report["reduction_pct"] = (
            100.0 * (baseline["total"] - report["total"]) / max(1, baseline["total"])
        )

    if args.baseline:
        p = save_baseline(sizes)
        report["baseline_saved"] = str(p)

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.check:
        if not all(report["check"].values()):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
