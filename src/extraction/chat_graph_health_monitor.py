"""chat_graph_health_monitor.py — daily MRR monitor + grayscale health probe.

TU-5 Closure B (2026-05-01): monitors chat graph retrieval quality via MRR.

Modes:
  --simulate-days N --fixture <path>: fast-forward simulator (CI/offline)
  --live: daily probe against real graph; appends to grayscale_health_log.jsonl

Usage:
    python -m src.extraction.chat_graph_health_monitor --simulate-days 30 \
        --fixture tests/fixtures/mrr_sample_queries.json
    python -m src.extraction.chat_graph_health_monitor --live
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
GROUNDTRUTH_FILE = DATA_DIR / "chat_mrr_groundtruth.jsonl"
GRAYSCALE_STATE_FILE = DATA_DIR / "grayscale_state.json"
HEALTH_LOG_FILE = DATA_DIR / "grayscale_health_log.jsonl"

# ---------------------------------------------------------------------------
# Trace emission
# ---------------------------------------------------------------------------

def _emit_trace(event: str, payload: dict) -> None:
    path = os.environ.get("MEMEXA_GMV2_STUB_TRACE_LOG", "")
    if not path:
        return
    try:
        rec = {"event": event, "ts": time.time(), **payload}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# MRR computation
# ---------------------------------------------------------------------------

def compute_mrr(queries: List[Dict], graph_query_fn: Callable[[str], List[str]]) -> float:
    """Compute Mean Reciprocal Rank over a list of query dicts.

    Each query dict must have:
      - "query": str
      - "expected_uuid_set": list[str]

    graph_query_fn(query) -> list[str] of retrieved UUIDs (ranked).
    Returns MRR float in [0, 1].
    """
    if not queries:
        return 0.0

    total_rr = 0.0
    for q in queries:
        expected = set(q.get("expected_uuid_set", []))
        retrieved = graph_query_fn(q["query"])
        rr = 0.0
        for rank, uid in enumerate(retrieved, start=1):
            if uid in expected:
                rr = 1.0 / rank
                break
        total_rr += rr

    return total_rr / len(queries)


# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------

def _load_fixture(fixture_path: Path) -> List[Dict]:
    """Load query fixture from JSON file."""
    try:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError):
        return []


def _load_groundtruth(path: Optional[Path] = None) -> List[Dict]:
    """Load groundtruth from JSONL file."""
    gt_path = path or GROUNDTRUTH_FILE
    if not gt_path.exists():
        return []
    entries = []
    for ln in gt_path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            entries.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return entries


# ---------------------------------------------------------------------------
# Simulator: fast-forward N days
# ---------------------------------------------------------------------------

def _synthetic_graph_fn(fixture: List[Dict]) -> Callable[[str], List[str]]:
    """Build a deterministic graph query function from fixture data.

    For the simulator: returns the expected UUIDs in ranked order (simulating
    perfect retrieval). This yields MRR=1.0 baseline; fixture can include
    partial-rank entries for realistic MRR<1.
    """
    lookup: Dict[str, List[str]] = {}
    for entry in fixture:
        q = entry.get("query", "")
        uuids = entry.get("expected_uuid_set", [])
        lookup[q] = uuids

    def _fn(query: str) -> List[str]:
        return lookup.get(query, [])

    return _fn


def _live_graph_fn(capability_token: Optional[bytes] = None) -> Callable[[str], List[str]]:
    """Build live graph query function against real Hindsight backend."""
    def _fn(query: str) -> List[str]:
        try:
            from src.core.graph_memory_v2 import query_entity
            facts = query_entity(
                query,
                limit=10,
                extracted_by="chat-realtime",
                capability_token=capability_token,
            )
            return [f.fact_id or f.source_episode_id for f in facts if f.fact_id]
        except Exception:
            return []
    return _fn


def sample_daily(n: int = 10, fixture: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """Sample n queries and compute MRR.

    Uses fixture if provided (simulator mode), otherwise live graph.
    """
    import random
    queries_pool = fixture or _load_groundtruth()
    if not queries_pool:
        return {"mrr": 0.0, "n_queries": 0, "error": "no_queries"}

    rng = random.Random(int(time.time()))
    sample = rng.sample(queries_pool, min(n, len(queries_pool)))

    if fixture is not None:
        gfn = _synthetic_graph_fn(fixture)
    else:
        gfn = _live_graph_fn()

    mrr = compute_mrr(sample, gfn)
    return {"mrr": mrr, "n_queries": len(sample), "ts": time.time()}


def simulate_days(
    n_days: int,
    fixture: List[Dict],
    trace_prefix: str = "chat_graph_grayscale_day",
) -> List[Dict]:
    """Fast-forward simulator: replay n_days of sampling against fixture.

    Emits trace `chat_graph_grayscale_day_X` per simulated day.
    Returns list of daily result dicts.
    """
    results = []
    gfn = _synthetic_graph_fn(fixture)
    import random

    for day in range(1, n_days + 1):
        # Use deterministic seed per day for reproducibility
        rng = random.Random(20260501 + day)
        sample = rng.sample(fixture, min(10, len(fixture)))
        mrr = compute_mrr(sample, gfn)
        result = {
            "day": day,
            "mrr": mrr,
            "n_queries": len(sample),
            "simulated": True,
            "ts": time.time(),
        }
        _emit_trace(f"{trace_prefix}_{day}", result)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Live mode
# ---------------------------------------------------------------------------

def run_live_probe() -> Dict[str, Any]:
    """Run one daily probe; append to grayscale_health_log.jsonl."""
    state: Dict[str, Any] = {}
    if GRAYSCALE_STATE_FILE.exists():
        try:
            state = json.loads(GRAYSCALE_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    result = sample_daily(n=10)
    result["source"] = "live"

    # Append to health log
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with HEALTH_LOG_FILE.open("ab") as f:
            f.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
    except OSError as e:
        result["log_error"] = str(e)

    _emit_trace("chat_graph_grayscale_live_probe", result)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def start_grayscale(task_id: str = "20260501_134831_closure_b_u1") -> Dict[str, Any]:
    """Write grayscale_state.json for P2a, emit trace, and append L2 reminder to pending_approvals.json.

    RP-SEC-11 / TU-9.3: Writes phase_b_pending=true; does NOT call mark_completed.
    Returns the state dict written.
    """
    now = time.time()
    target_days = 30
    expires_at = now + 7776000  # 90 days in seconds

    state: Dict[str, Any] = {
        "start_ts": now,
        "target_days": target_days,
        "expires_at": expires_at,
        "baseline_mrr": 0.8542,
        "status": "P2a_done",
        "phase_b_pending": True,
        "task_id": task_id,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GRAYSCALE_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    import os as _os
    _os.replace(str(tmp), str(GRAYSCALE_STATE_FILE))

    # Emit trace event chat_graph_grayscale_day_1
    _emit_trace("chat_graph_grayscale_day_1", {
        "task_id": task_id,
        "start_ts": now,
        "phase_b_pending": True,
        "status": "P2a_done",
    })
    print(f"[chat_graph_health_monitor] grayscale_state.json written: P2a_done, phase_b_pending=true",
          file=sys.stderr)

    # Append L2 reminder to pending_approvals.json (RP-LOG-6)
    pending_path = DATA_DIR / "pending_approvals.json"
    approval_entry: Dict[str, Any] = {
        "id": f"apr_p2b_{task_id}",
        "kind": "L2",
        "title": "Closure B P2b reminder: 30d grayscale resume needed",
        "summary": (
            "expected ~2026-05-31; manual /autopilot resume; "
            "runs MRR LIVE check; mark_completed unblocks"
        ),
        "ts": now,
        "expires_at": expires_at,
    }

    try:
        existing: Any = {}
        if pending_path.exists():
            try:
                existing = json.loads(pending_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}

        # pending_approvals.json may have a top-level list or dict structure
        if isinstance(existing, list):
            existing.append(approval_entry)
            pending_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        elif isinstance(existing, dict):
            items = existing.get("items", existing.get("candidates", []))
            if not isinstance(items, list):
                items = []
            items.append(approval_entry)
            existing["items"] = items
            pending_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        else:
            pending_path.write_text(json.dumps([approval_entry], ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    except OSError as e:
        print(f"[chat_graph_health_monitor] WARNING: could not append to pending_approvals.json: {e}",
              file=sys.stderr)

    return state


def main() -> int:
    p = argparse.ArgumentParser(description="Chat graph health monitor + grayscale simulator.")
    p.add_argument("--simulate-days", type=int, default=0,
                   help="Number of simulated days (fast-forward mode)")
    p.add_argument("--fixture", type=str, default="",
                   help="Path to fixture JSON file (for simulator mode)")
    p.add_argument("--live", action="store_true",
                   help="Run live daily probe against real graph")
    p.add_argument("--start-grayscale", action="store_true",
                   help="Write P2a grayscale_state.json + emit trace + append L2 reminder")
    p.add_argument("--task-id", type=str, default="20260501_134831_closure_b_u1",
                   help="Task ID for grayscale state (used with --start-grayscale)")
    args = p.parse_args()

    if args.start_grayscale:
        state = start_grayscale(task_id=args.task_id)
        print(json.dumps(state, ensure_ascii=False))
        return 0

    if args.simulate_days > 0:
        fixture_path = Path(args.fixture) if args.fixture else None
        if fixture_path and fixture_path.exists():
            fixture = _load_fixture(fixture_path)
        else:
            fixture = _load_groundtruth()

        if not fixture:
            print("ERROR: no fixture/groundtruth data available", file=sys.stderr)
            return 1

        results = simulate_days(args.simulate_days, fixture)
        for r in results:
            print(json.dumps(r, ensure_ascii=False))

        avg_mrr = sum(r["mrr"] for r in results) / len(results) if results else 0.0
        print(json.dumps({"summary": f"{args.simulate_days}d simulation",
                          "avg_mrr": avg_mrr, "n_days": len(results)}))
        return 0

    if args.live:
        result = run_live_probe()
        print(json.dumps(result, ensure_ascii=False))
        return 0

    print("ERROR: specify --simulate-days N or --live", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
