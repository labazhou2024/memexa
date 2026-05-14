"""TU-R2 (2026-04-23): fail-open audit CLI.

Reads memex/memex/data/events.jsonl and reports per-gate / per-reason
fail-open counts over a time window. Used as weekly CI digest to detect
gate bypass trends (see `feedback_autopilot_depth_and_dont_return_early.md`
and 2026-04-23 deep audit B2: session_gate silently bypassed on 13/20
recent commits because no trace was emitted).

Usage:
  python -m src.core.fail_open_audit [--hours 168] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

_MEMEX = Path(__file__).resolve().parents[2]
_EVENTS = _MEMEX / "memex" / "data" / "events.jsonl"

# B3 fix (security review blocker 3): sanitize task_id / gate name for
# ANSI injection before printing. Attacker-controlled directory name must
# not hijack terminal state.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(s: str) -> str:
    if not isinstance(s, str):
        return str(s)[:128]
    return _CTRL_RE.sub("", s)[:128]


def collect(hours: int = 168) -> dict:
    """Return {gate: {reason: count}, _total, _window_hours}."""
    if not _EVENTS.exists():
        return {"_total": 0, "_window_hours": hours, "gates": {}}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    per_gate: dict = {}
    total = 0
    with _EVENTS.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            etype = e.get("type") or e.get("event") or ""
            if "_fail_open" not in etype:
                continue
            # unified schema post-R1
            # P2 (2026-04-23): use centralized event_ts utility
            from src.core._hook_utils import event_ts
            ts = event_ts(e)
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except Exception:
                pass  # no ts, count anyway
            details = e.get("details") or e.get("payload") or {}
            reason = _sanitize(details.get("reason", "unknown"))
            gate = _sanitize(etype)
            per_gate.setdefault(gate, Counter())[reason] += 1
            total += 1
    return {
        "_total": total,
        "_window_hours": hours,
        "gates": {g: dict(c) for g, c in per_gate.items()},
    }


def format_report(data: dict) -> str:
    lines = [f"Fail-Open Audit (last {data['_window_hours']}h): "
             f"total={data['_total']}"]
    if not data["gates"]:
        lines.append("  (no fail_open events)")
        return "\n".join(lines)
    for gate, reasons in sorted(data["gates"].items()):
        total = sum(reasons.values())
        lines.append(f"  {gate}: {total}")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            lines.append(f"    - {reason}: {count}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fail_open_audit")
    p.add_argument("--hours", type=int, default=168,
                   help="time window (default 168h = 7 days)")
    p.add_argument("--json", action="store_true", default=False)
    args = p.parse_args(argv)
    data = collect(hours=args.hours)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(format_report(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
