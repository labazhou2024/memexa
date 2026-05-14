"""memory_sync_cli — on-demand drain + backfill for graph memory ingest.

Problem: the heartbeat service enqueues memory edits into
`memex/data/ingest_queue.jsonl` via ``_check_memory_staleness``, but
``_check_drain_queue`` only runs when the heartbeat itself runs — i.e.,
as a daemon or cron job. In sessions where heartbeat doesn't fire
(normal user coding), the queue grows unbounded and memory files never
reach the graph.

This module provides two on-demand commands:

  - ``drain``   process queued items, ingest each into graph, remove from queue.
  - ``backfill-missing``   find memory/*.md with zero facts in graph AND
    not in queue; ingest them directly.
  - ``status``   queue size + never-queued-never-ingested count.

Runs under the CEO session, not the heartbeat. Safe to call repeatedly —
idempotent per-file (ingest_file dedups by span hash).

Usage:

    python -m src.core.memory_sync_cli drain --limit 50
    python -m src.core.memory_sync_cli backfill-missing --limit 30
    python -m src.core.memory_sync_cli status
"""
from __future__ import annotations


import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from src.core._path_resolver import memory_dir


_WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent
_QUEUE_FILE = Path(__file__).resolve().parent.parent / "data" / "ingest_queue.jsonl"
_DEFAULT_MEMORY_DIR = memory_dir()


def _read_queue(path: Path = _QUEUE_FILE) -> List[dict]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    return items


def _write_queue(items: List[dict], path: Path = _QUEUE_FILE) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def drain(limit: int = 50, timeout: float = 60.0,
          queue_path: Path = _QUEUE_FILE) -> dict:
    """Process up to ``limit`` items from the queue; ingest each; remove
    successfully-ingested items from the queue file.

    Returns a summary dict:
      {"before": N, "ingested": M, "failed": K, "after": L, "duration_s": T}
    """
    t0 = time.time()
    queue = _read_queue(queue_path)
    before = len(queue)
    if before == 0:
        return {"before": 0, "ingested": 0, "failed": 0, "after": 0,
                "duration_s": 0.0, "status": "empty_queue"}

    # Import here so tests can stub
    try:
        from src.core.graph_memory import ingest_file
    except Exception as e:
        return {"before": before, "ingested": 0, "failed": before,
                "after": before, "status": f"import_error:{e}"}

    ingested = 0
    failed = 0
    remaining: List[dict] = []
    cutoff = t0 + timeout

    for i, entry in enumerate(queue):
        if i >= limit or time.time() > cutoff:
            remaining.extend(queue[i:])
            break
        fp = entry.get("file_path", "")
        path = Path(fp)
        if not path.exists():
            # Vanished: drop from queue (can't ingest what's gone)
            failed += 1
            continue
        try:
            ingest_file(path)
            ingested += 1
        except Exception as e:
            # Log + keep in queue for retry
            sys.stderr.write(f"drain: ingest_file({path.name}) failed: {str(e)[:120]}\n")
            failed += 1
            remaining.append(entry)

    _write_queue(remaining, queue_path)
    return {
        "before": before,
        "ingested": ingested,
        "failed": failed,
        "after": len(remaining),
        "duration_s": round(time.time() - t0, 2),
        "status": "ok",
    }


def _files_in_graph() -> set:
    """Query Fact.source_path distinct values."""
    try:
        from src.core.graph_memory import _get_driver
    except Exception:
        return set()
    try:
        d = _get_driver()
        with d.session() as s:
            r = s.run(
                "MATCH (f:Fact) "
                "WHERE f.source_path IS NOT NULL "
                "RETURN DISTINCT f.source_path as src"
            )
            return {(rec["src"] or "") for rec in r}
    except Exception:
        return set()


def _compute_missing(memory_dir: Path = _DEFAULT_MEMORY_DIR) -> List[Path]:
    """Return memory/*.md files with NO facts in graph (and not MEMORY.md)."""
    if not memory_dir.is_dir():
        return []
    in_graph = _files_in_graph()
    missing = []
    for f in sorted(memory_dir.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        # source_path may be absolute str; accept substring match
        basename = f.name
        if any(basename in src for src in in_graph):
            continue
        missing.append(f)
    return missing


def backfill_missing(limit: int = 30, timeout: float = 120.0,
                     memory_dir: Path = _DEFAULT_MEMORY_DIR) -> dict:
    """Find memory files with 0 facts in graph; ingest up to ``limit``."""
    t0 = time.time()
    missing = _compute_missing(memory_dir)
    before = len(missing)
    if before == 0:
        return {"before": 0, "ingested": 0, "after": 0,
                "duration_s": 0.0, "status": "nothing_missing"}

    try:
        from src.core.graph_memory import ingest_file
    except Exception as e:
        return {"before": before, "ingested": 0, "after": before,
                "status": f"import_error:{e}"}

    ingested = 0
    failed = 0
    cutoff = t0 + timeout

    for i, path in enumerate(missing):
        if i >= limit or time.time() > cutoff:
            break
        try:
            ingest_file(path)
            ingested += 1
        except Exception as e:
            sys.stderr.write(f"backfill: ingest_file({path.name}) failed: {str(e)[:120]}\n")
            failed += 1

    # Recompute after for reporting accuracy
    after_missing = _compute_missing(memory_dir)
    return {
        "before": before,
        "ingested": ingested,
        "failed": failed,
        "after": len(after_missing),
        "duration_s": round(time.time() - t0, 2),
        "status": "ok",
    }


def status() -> dict:
    """Queue size + never-queued count."""
    q = _read_queue()
    missing = _compute_missing()
    queued_paths = {e.get("file_path", "") for e in q}
    never_queued = [p for p in missing if str(p) not in queued_paths]
    return {
        "queue_size": len(q),
        "missing_files": len(missing),
        "never_queued": len(never_queued),
    }


def _cli(argv: Optional[List[str]] = None) -> int:
    # UTF-8 stdout (same pattern as graph_memory._cli)
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="src.core.memory_sync_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("drain", help="process ingest_queue.jsonl")
    pd.add_argument("--limit", type=int, default=50)
    pd.add_argument("--timeout", type=float, default=60.0)

    pbm = sub.add_parser("backfill-missing",
                         help="ingest memory files with 0 facts in graph")
    pbm.add_argument("--limit", type=int, default=30)
    pbm.add_argument("--timeout", type=float, default=120.0)

    ps = sub.add_parser("status", help="queue + missing-files summary")
    _ = ps  # suppress lint

    args = p.parse_args(argv)
    if args.cmd == "drain":
        r = drain(limit=args.limit, timeout=args.timeout)
    elif args.cmd == "backfill-missing":
        r = backfill_missing(limit=args.limit, timeout=args.timeout)
    elif args.cmd == "status":
        r = status()
    else:
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
