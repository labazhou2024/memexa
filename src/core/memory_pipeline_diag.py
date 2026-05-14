"""memory_pipeline_diag — diagnostic CLI to pinpoint where the
write→graph chain silently breaks.

Pipeline stages (each must succeed for a memory file to land in graph):
  Write tool ─────────► PostToolUse memory_write_hook         (1)
  memory_write_hook ──► subprocess.Popen ingest               (2)
  ingest ─────────────► append to ingest_queue.jsonl          (3)
  drain_queue ────────► haiku_extract_done                    (4)
  haiku output ───────► graph_memory.write_fact               (5)
  write_fact ─────────► Neo4j MERGE Fact                      (6)

Reads last 100 events from `.claude/data/traces.jsonl`. For each
`memory_write_hook_spawn` event found, traces forward through stages
2-6 and reports per-file completion ratio + identifies the weakest stage.

CLI:
  python -m src.core.memory_pipeline_diag             # last 100 events
  python -m src.core.memory_pipeline_diag --last 500  # custom window
  python -m src.core.memory_pipeline_diag --json      # machine-readable

AC-G1 (2026-04-25). Fail-soft on missing trace file (returns empty report).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional


# Script-mode safety — same pattern as session_gate / persistent_mode.
_pkg_root = Path(__file__).resolve().parents[2]
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))


def _trace_path() -> Path:
    """Resolve trace file via trace_sink._trace_file (respects env override)."""
    try:
        from src.core.trace_sink import _trace_file
        return _trace_file()
    except (ImportError, OSError):
        # Fallback to canonical path
        return _pkg_root.parent / ".claude" / "data" / "traces.jsonl"


def _read_last_n_events(n: int) -> List[Dict[str, Any]]:
    """Read last N events from trace_sink. Empty list on missing/unreadable."""
    p = _trace_path()
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    raw_lines = [ln for ln in text.splitlines() if ln.strip()]
    tail = raw_lines[-n:] if len(raw_lines) > n else raw_lines
    out: List[Dict[str, Any]] = []
    for ln in tail:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def diagnose(last_n: int = 100) -> Dict[str, Any]:
    """Walk the pipeline backward + forward through trace events.

    Returns a structured diagnosis with stage counts + missing files.
    """
    events = _read_last_n_events(last_n)
    by_event: Counter = Counter(e.get("event", "?") for e in events)

    # Stage counts
    spawn_count = by_event.get("memory_write_hook_spawn", 0)
    spawn_skip = by_event.get("memory_write_hook_skip", 0)
    spawn_err = by_event.get("memory_write_hook_error", 0)
    extract_start = by_event.get("haiku_extract_start", 0)
    extract_done = by_event.get("haiku_extract_done", 0)
    extract_fail = by_event.get("haiku_extract_fail", 0)
    drain_done = by_event.get("drain_queue_done", 0)
    write_succeeded = by_event.get("write_fact_succeeded", 0)
    write_skip_no_driver = by_event.get("write_fact_skipped_no_driver", 0)
    write_skip_empty_canon = by_event.get("write_fact_skipped_empty_canon", 0)
    write_skip_empty_span = by_event.get("write_fact_skipped_empty_span", 0)
    write_skip_merge_noop = by_event.get("write_fact_skipped_merge_noop", 0)
    write_skip_bad_type = by_event.get("write_fact_skipped_bad_input_type", 0)

    write_skip_total = (
        write_skip_no_driver + write_skip_empty_canon + write_skip_empty_span
        + write_skip_merge_noop + write_skip_bad_type
    )

    # File-by-file: track which spawn events have downstream extract_done +
    # write_fact_succeeded with matching source_path.
    spawned_files: List[str] = []
    extract_done_files: List[str] = []
    write_success_files: List[str] = []
    for e in events:
        ev = e.get("event", "")
        payload = e.get("payload", {}) or {}
        fp = (payload.get("file_path") or payload.get("source_path") or "").strip()
        if not fp:
            continue
        if ev == "memory_write_hook_spawn":
            spawned_files.append(fp)
        elif ev == "haiku_extract_done":
            extract_done_files.append(fp)
        elif ev == "write_fact_succeeded":
            write_success_files.append(fp)

    spawn_set = set(spawned_files)
    extract_done_set = set(extract_done_files)
    write_success_set = set(write_success_files)

    spawned_but_no_extract = sorted(spawn_set - extract_done_set)
    extract_done_but_no_write = sorted(extract_done_set - write_success_set)

    # Diagnose weakest stage
    weakest_stage = "no_data"
    weakest_reason = ""
    if extract_done > 0 and write_succeeded == 0:
        weakest_stage = "stage_5_write_fact"
        weakest_reason = (
            f"haiku_extract_done={extract_done} but write_fact_succeeded=0; "
            f"silent skip breakdown: no_driver={write_skip_no_driver}, "
            f"empty_canon={write_skip_empty_canon}, empty_span={write_skip_empty_span}, "
            f"merge_noop={write_skip_merge_noop}, bad_type={write_skip_bad_type}"
        )
    elif extract_done > 0 and write_succeeded > 0 and write_succeeded < extract_done * 0.3:
        weakest_stage = "stage_5_write_fact_low_yield"
        weakest_reason = (
            f"yield ratio {write_succeeded}/{extract_done} = "
            f"{write_succeeded/max(1,extract_done):.1%} (expected >30%)"
        )
    elif spawn_count > 0 and extract_done == 0:
        weakest_stage = "stage_4_haiku_extract"
        weakest_reason = f"spawn_count={spawn_count} but no haiku_extract_done"
    elif spawn_count == 0 and (spawn_skip + spawn_err) > 0:
        weakest_stage = "stage_2_hook_filter"
        weakest_reason = f"all hook fires skip/error: skip={spawn_skip} err={spawn_err}"
    elif spawn_count == 0:
        weakest_stage = "stage_1_no_writes_observed"
        weakest_reason = "no Write tool calls observed in window"
    elif extract_done > 0 and write_succeeded > 0:
        weakest_stage = "healthy"
        weakest_reason = "pipeline alive end-to-end"

    return {
        "events_read": len(events),
        "stage_counts": {
            "1_writes_observed": spawn_count,  # PostToolUse fires
            "2_hook_pass": spawn_count,
            "2_hook_skip": spawn_skip,
            "2_hook_error": spawn_err,
            "4_extract_start": extract_start,
            "4_extract_done": extract_done,
            "4_extract_fail": extract_fail,
            "5_drain_done": drain_done,
            "6_write_succeeded": write_succeeded,
            "6_write_skipped": write_skip_total,
            "6_write_skip_breakdown": {
                "no_driver": write_skip_no_driver,
                "empty_canon": write_skip_empty_canon,
                "empty_span": write_skip_empty_span,
                "merge_noop": write_skip_merge_noop,
                "bad_input_type": write_skip_bad_type,
            },
        },
        "yield_ratios": {
            "extract_to_write": (
                f"{write_succeeded}/{extract_done}" if extract_done else "n/a"
            ),
            "extract_to_write_pct": (
                round(write_succeeded / extract_done * 100, 1)
                if extract_done else None
            ),
            "spawn_to_extract": (
                f"{extract_done}/{spawn_count}" if spawn_count else "n/a"
            ),
        },
        "files_lost_per_stage": {
            "spawned_but_no_extract": spawned_but_no_extract[:5],
            "spawned_but_no_extract_count": len(spawned_but_no_extract),
            "extract_done_but_no_write": extract_done_but_no_write[:5],
            "extract_done_but_no_write_count": len(extract_done_but_no_write),
        },
        "weakest_stage": weakest_stage,
        "weakest_reason": weakest_reason,
    }


def _cli(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="memory_pipeline_diag")
    p.add_argument("--last", type=int, default=100,
                   help="Read last N events (default 100)")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output")
    args = p.parse_args(argv)

    report = diagnose(last_n=args.last)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # Human-readable summary
    sc = report["stage_counts"]
    print(f"Memory pipeline diagnosis (last {report['events_read']} trace events)")
    print()
    print("Stage counts:")
    print(f"  1. Writes observed (memory_write_hook_spawn) : {sc['1_writes_observed']}")
    print(f"     skip={sc['2_hook_skip']} error={sc['2_hook_error']}")
    print(f"  4. haiku_extract_start                       : {sc['4_extract_start']}")
    print(f"  4. haiku_extract_done                        : {sc['4_extract_done']}")
    print(f"     fail={sc['4_extract_fail']}")
    print(f"  5. drain_queue_done                          : {sc['5_drain_done']}")
    print(f"  6. write_fact_succeeded                      : {sc['6_write_succeeded']}")
    print(f"     skipped={sc['6_write_skipped']} breakdown={sc['6_write_skip_breakdown']}")
    print()
    print("Yield ratios:")
    print(f"  extract_to_write : {report['yield_ratios']['extract_to_write']}"
          f" ({report['yield_ratios']['extract_to_write_pct']}%)")
    print(f"  spawn_to_extract : {report['yield_ratios']['spawn_to_extract']}")
    print()
    print(f"WEAKEST STAGE: {report['weakest_stage']}")
    print(f"  reason: {report['weakest_reason']}")
    print()
    if report["files_lost_per_stage"]["spawned_but_no_extract_count"]:
        print(f"  Files lost in stage 4 (extract): "
              f"{report['files_lost_per_stage']['spawned_but_no_extract_count']}"
              f" — sample: {report['files_lost_per_stage']['spawned_but_no_extract'][:3]}")
    if report["files_lost_per_stage"]["extract_done_but_no_write_count"]:
        print(f"  Files lost in stage 6 (write):   "
              f"{report['files_lost_per_stage']['extract_done_but_no_write_count']}"
              f" — sample: {report['files_lost_per_stage']['extract_done_but_no_write'][:3]}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
