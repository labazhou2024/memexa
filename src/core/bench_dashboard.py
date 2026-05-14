"""bench_dashboard.py — Benchmark history trend dashboard CLI (U6 TU-6).

Usage:
  python -m src.core.bench_dashboard [--last-days 30] [--json]

Output (text mode):
  - 30-day trend table: date | mode | recall_real | recall_raw | mrr | gate
  - min/max/median stats for recall_real
  - red flags: rows where recall_real < 0.40 OR drop > 10pp from previous

Output (--json mode):
  {"trend": [...], "stats": {...}, "red_flags": [...]}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_MEMEX_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_HISTORY = _MEMEX_ROOT / "data" / "benchmark_results_history.jsonl"


def _load_rows(history_path: Path, last_days: int) -> list[dict]:
    """Load history JSONL and filter to entries within last_days."""
    if not history_path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=last_days)
    rows: list[dict] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_str = rec.get("timestamp_utc", "")
        try:
            ts_str_clean = ts_str.replace("Z", "+00:00")
            entry_dt = datetime.fromisoformat(ts_str_clean)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            if entry_dt >= cutoff:
                rows.append(rec)
        except (ValueError, TypeError):
            rows.append(rec)  # include if unparseable timestamp
    return rows


def _get_float(rec: dict, key: str, default: float = 0.0) -> float:
    """Safe float extraction."""
    v = rec.get(key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get_recall_real(rec: dict) -> float:
    return _get_float(rec, "recall_at_10_real_only")


def _get_recall_raw(rec: dict) -> float:
    return _get_float(rec, "recall_at_10_raw")


def _get_mrr(rec: dict) -> float:
    return _get_float(rec, "mrr_real_only")


def _get_gate(rec: dict) -> str:
    # gate_decision may be stored at top level or in gate_decision_reason[0]
    gate = rec.get("gate_decision", "")
    if not gate:
        gdr = rec.get("gate_decision_reason")
        if isinstance(gdr, list) and gdr:
            gate = str(gdr[0])
    return gate or "unknown"


def _get_date(rec: dict) -> str:
    ts_str = rec.get("timestamp_utc", "")
    try:
        ts_str_clean = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str_clean)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ts_str[:10] if len(ts_str) >= 10 else ts_str


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return s[mid]


def _compute_red_flags(rows: list[dict]) -> list[dict]:
    """Identify red-flag rows.

    A row is flagged if:
      - recall_real < 0.40 (absolute threshold)
      - drop > 10pp (0.10) from the immediately preceding row's recall_real
    """
    red_flags: list[dict] = []
    prev_recall: Optional[float] = None
    for rec in rows:
        recall_real = _get_recall_real(rec)
        flags: list[str] = []
        if recall_real < 0.40:
            flags.append(f"recall_real={recall_real:.3f}<0.40")
        if prev_recall is not None and (prev_recall - recall_real) > 0.10:
            drop = prev_recall - recall_real
            flags.append(f"drop={drop:.3f}>0.10pp_from_prev")
        if flags:
            red_flags.append({
                "date": _get_date(rec),
                "mode": rec.get("mode", "?"),
                "recall_real": recall_real,
                "flags": flags,
            })
        prev_recall = recall_real
    return red_flags


def _build_trend(rows: list[dict]) -> list[dict]:
    """Build trend table entries."""
    trend = []
    for rec in rows:
        trend.append({
            "date": _get_date(rec),
            "mode": rec.get("mode", "?"),
            "recall_real": round(_get_recall_real(rec), 4),
            "recall_raw": round(_get_recall_raw(rec), 4),
            "mrr": round(_get_mrr(rec), 4),
            "gate": _get_gate(rec),
        })
    return trend


def main(argv: list[str]) -> int:
    """Dashboard CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="src.core.bench_dashboard",
        description="Benchmark history trend dashboard.",
    )
    parser.add_argument(
        "--last-days", type=int, default=30,
        help="Show entries from the last N days (default: 30)",
    )
    parser.add_argument(
        "--json", dest="json_mode", action="store_true",
        help="Output machine-readable JSON",
    )
    parser.add_argument(
        "--history", default=None,
        help="Path to benchmark_results_history.jsonl (default: memex/data/)",
    )
    args = parser.parse_args(argv)

    history_path = Path(args.history) if args.history else _DEFAULT_HISTORY
    rows = _load_rows(history_path, args.last_days)

    if not rows:
        if args.json_mode:
            print(json.dumps({"trend": [], "stats": {}, "red_flags": []}, ensure_ascii=True))
        else:
            print("no data")
        return 0

    trend = _build_trend(rows)
    recall_values = [t["recall_real"] for t in trend]
    stats = {
        "min": round(min(recall_values), 4),
        "max": round(max(recall_values), 4),
        "median": round(_median(recall_values), 4),
        "count": len(recall_values),
    }
    red_flags = _compute_red_flags(rows)

    if args.json_mode:
        output = {
            "trend": trend,
            "stats": stats,
            "red_flags": red_flags,
        }
        print(json.dumps(output, ensure_ascii=True))
        return 0

    # Text mode output
    print(f"Benchmark trend (last {args.last_days} days, {len(rows)} entries)")
    print("-" * 70)
    header = f"{'date':<12} {'mode':<8} {'recall_real':>11} {'recall_raw':>10} {'mrr':>7} {'gate':<10}"
    print(header)
    print("-" * 70)
    for t in trend:
        print(
            f"{t['date']:<12} {t['mode']:<8} {t['recall_real']:>11.3f} "
            f"{t['recall_raw']:>10.3f} {t['mrr']:>7.4f} {t['gate']:<10}"
        )
    print("-" * 70)
    print(f"Stats: min={stats['min']:.3f}  max={stats['max']:.3f}  median={stats['median']:.3f}")

    if red_flags:
        print(f"\nRed flags ({len(red_flags)} rows):")
        for rf in red_flags:
            print(f"  {rf['date']} [{rf['mode']}] recall={rf['recall_real']:.3f} -- {'; '.join(rf['flags'])}")
    else:
        print("\nNo red flags.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
