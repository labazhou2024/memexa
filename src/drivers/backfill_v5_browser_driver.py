"""V5 Browser backfill driver — cron-callable incremental pipeline.

Orchestrates the full v5 ingestion pipeline for browser_session messages:
  Step 1: Load cursor from data/backfill_v5_browser_progress.json
  Step 2: Build new input batches via tools/v5_browser_batch_builder.py
  Step 3: Identify pending batches in data/l0_v5/input_batches_browser/
          not in PG memory_full_v5 (PG-aware) and not in local marker dirs
  Step 4: Run extractor worker (local Mac OR your-org API via --mode)
  Step 5: POST extracted cards to hindsight v5 bank (memory_full_v5)
  Step 6: Save cursor + emit trace summary

Source: browser visits (Chrome/Edge SQLite → v5_browser_batch_builder.py)
PG metadata.source: "browser_session"

CLI:
  python -m tools.backfill_v5_browser_driver
  python -m tools.backfill_v5_browser_driver --mode=api --verbose
  python -m tools.backfill_v5_browser_driver --max-batches 5 --dry-run
  python -m tools.backfill_v5_browser_driver --since 2026-05-01
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
# OSS: writable data lives in the user's workspace, resolved via env
# (MEMEX_WORKSPACE_ROOT) or `~/.claude/projects/`. See docs/configuration.md.
from src.core._path_resolver import data_dir as _resolve_data_dir
_DATA = _resolve_data_dir()

_CURSOR_PATH = _DATA / "backfill_v5_browser_progress.json"
_INPUT_BATCHES_DIR = _DATA / "l0_v5" / "input_batches_browser"
_CARDS_DIR = _DATA / "l0_v5" / "work" / "cards_v2_browser"
_POSTED_DIR = _DATA / "l0_v5" / "work" / "posted_v5_browser"

_BROWSER_BUILDER = _REPO / "ingestion" / "v5_browser_batch_builder.py"
_MAC_WORKER = _REPO / "extraction" / "l0_worker_serial.py"
_API_WORKER = _REPO / "extraction" / "l0_worker_api.py"
_STREAMING_POST = _REPO / "extraction" / "streaming_post_v5.py"

_DEFAULT_LOOKBACK_DAYS = 3
_DEFAULT_MAX_BATCHES = int(os.environ.get("MEMEX_V5_BATCH_LIMIT", "5"))
_SOURCE = "browser"  # → pg metadata.source == "browser_session"

_HINDSIGHT_BASE_URL = os.environ.get("MEMEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
_HINDSIGHT_BANK = os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5")


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _load_cursor() -> dict:
    if not _CURSOR_PATH.exists():
        return {
            "last_built_ts": time.time() - _DEFAULT_LOOKBACK_DAYS * 86400,
            "last_run_ts": None, "n_runs": 0, "last_summary": {},
        }
    try:
        d = json.loads(_CURSOR_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("cursor not dict")
        d.setdefault("last_built_ts",
                     time.time() - _DEFAULT_LOOKBACK_DAYS * 86400)
        d.setdefault("last_run_ts", None)
        d.setdefault("n_runs", 0)
        d.setdefault("last_summary", {})
        return d
    except Exception as exc:
        print(f"[backfill_v5_browser_driver] WARN: cursor load failed ({exc})",
              file=sys.stderr)
        return {
            "last_built_ts": time.time() - _DEFAULT_LOOKBACK_DAYS * 86400,
            "last_run_ts": None, "n_runs": 0, "last_summary": {},
        }


def _save_cursor(cur: dict) -> None:
    try:
        _atomic_write_json(_CURSOR_PATH, cur)
    except Exception as exc:
        print(f"[backfill_v5_browser_driver] WARN: cursor save failed: {exc}",
              file=sys.stderr)


def _list_pending_batches(
    input_dir: Path,
    posted_dir: Path,
    cards_dir: Path,
    limit: int,
    source: str = _SOURCE,
) -> list[str]:
    if not input_dir.exists():
        return []

    all_bids: list[str] = []
    for date_dir in sorted(input_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for bd in sorted(date_dir.iterdir()):
            if bd.is_dir() and (bd / "prompt.json").exists():
                all_bids.append(bd.name)

    # PG-aware
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    try:
        from src.core.pg_bid_cache import query_pg_existing_bids
        pg_bids = query_pg_existing_bids(source)
    except Exception as exc:
        print(f"[backfill_v5_browser_driver] WARN: pg_bid_cache fail ({exc})",
              file=sys.stderr)
        pg_bids = set()

    pending: list[str] = []
    for bid in all_bids:
        if bid in pg_bids:
            continue
        if (posted_dir / f"{bid}.posted").exists():
            continue
        if (cards_dir / f"{bid}.json").exists():
            continue
        pending.append(bid)
        if limit > 0 and len(pending) >= limit:
            break
    return pending


def _run_subprocess(cmd: list[str], timeout: int = 1800,
                    verbose: bool = False) -> dict:
    t0 = time.time()
    out: dict = {"cmd": cmd, "returncode": -1, "stdout": "", "stderr": "",
                 "duration_sec": 0.0, "timed_out": False}
    try:
        if verbose:
            proc = subprocess.Popen(cmd, stdout=None, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace")
            _, err = proc.communicate(timeout=timeout)
            out["returncode"] = proc.returncode
            out["stderr"] = err or ""
        else:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout)
            out["returncode"] = proc.returncode
            out["stdout"] = proc.stdout or ""
            out["stderr"] = proc.stderr or ""
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
        try:
            proc.kill()
        except Exception:
            pass
    except Exception as exc:
        out["stderr"] = f"subprocess error: {exc}"
    out["duration_sec"] = round(time.time() - t0, 2)
    return out


def _stage_build_batches(cursor: dict, since_override: str | None,
                          dry_run: bool, verbose: bool) -> tuple[int, float]:
    build_start_ts = time.time()
    if since_override:
        try:
            since_ts = datetime.fromisoformat(since_override).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            since_ts = cursor["last_built_ts"]
    else:
        since_ts = cursor["last_built_ts"]

    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc) - timedelta(days=1)
    end_dt = datetime.now(timezone.utc)
    start_str = since_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    cmd = [
        sys.executable, str(_BROWSER_BUILDER),
        "--start", start_str, "--end", end_str,
        "--skip-existing",
        "--out", str(_INPUT_BATCHES_DIR),
    ]
    if dry_run:
        print(f"[DRY] would run: {' '.join(cmd)}")
        return 0, build_start_ts
    if verbose:
        print(f"[step2] building browser batches start={start_str} end={end_str}")
    r = _run_subprocess(cmd, timeout=600, verbose=verbose)
    if r["returncode"] != 0:
        print(f"[browser] builder WARN rc={r['returncode']} stderr={r['stderr'][:300]}",
              file=sys.stderr)
    return 0, build_start_ts  # n_built tracked via pending count later


def _stage_run_worker(pending: list[str], max_batches: int,
                       dry_run: bool, verbose: bool,
                       mode: str = "local") -> dict:
    if not pending:
        return {"returncode": 0, "n_submitted": 0, "duration_sec": 0.0}
    done_dir = _DATA / "l0_v5" / "work" / "done_v2_browser"
    done_dir.mkdir(parents=True, exist_ok=True)
    worker_path = _API_WORKER if mode == "api" else _MAC_WORKER
    cmd = [
        sys.executable, str(worker_path),
        "--pass", "2",
        "--batches-dir", str(_INPUT_BATCHES_DIR),
        "--done-dir", str(done_dir),
        "--out-dir", str(_CARDS_DIR),
        "--concurrent", str(int(os.environ.get("MEMEX_your-org_CONCURRENT", "5")) if mode == "api" else 1),
        "--max-batches", str(max_batches),
    ]
    if dry_run:
        print(f"[DRY] would run worker (mode={mode}): {' '.join(cmd)}")
        return {"returncode": 0, "n_submitted": len(pending), "dry_run": True}
    if verbose:
        print(f"[step4] worker (mode={mode}) on {len(pending)} pending")
    r = _run_subprocess(cmd, timeout=14400, verbose=verbose)
    if r["returncode"] != 0:
        print(f"[browser] worker WARN rc={r['returncode']}", file=sys.stderr)
        if r["stderr"]:
            print(r["stderr"][:2000], file=sys.stderr)
    return {"returncode": r["returncode"], "n_submitted": len(pending),
            "duration_sec": r["duration_sec"], "timed_out": r.get("timed_out", False)}


def _stage_post_cards(dry_run: bool, verbose: bool) -> dict:
    cmd = [
        sys.executable, str(_STREAMING_POST),
        "--cards-dir", str(_CARDS_DIR),
        "--posted-marker-dir", str(_POSTED_DIR),
        "--max-iterations", "60",
        "--exit-when-empty-rounds", "10",
    ]
    if dry_run:
        print(f"[DRY] would POST: {' '.join(cmd)}")
        return {"returncode": 0, "dry_run": True}
    if verbose:
        print(f"[step5] streaming_post to {_HINDSIGHT_BASE_URL}/{_HINDSIGHT_BANK}")
    env = {**os.environ,
           "MEMEX_HINDSIGHT_URL": _HINDSIGHT_BASE_URL,
           "MEMEX_HINDSIGHT_BANK": _HINDSIGHT_BANK}
    r = _run_subprocess(cmd, timeout=900, verbose=verbose)
    return {"returncode": r["returncode"], "duration_sec": r["duration_sec"]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None)
    p.add_argument("--max-batches", type=int, default=_DEFAULT_MAX_BATCHES)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--skip-post", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--mode", choices=["local", "api"],
                   default=os.environ.get("MEMEX_V5_WORKER_MODE", "local"),
                   help="Extractor mode: local=Mac dual-LLM, api=your-org API")
    args = p.parse_args()

    t0 = time.time()
    try:
        cursor = _load_cursor()
    except Exception as exc:
        print(json.dumps({"error": "cursor_load_failed", "detail": str(exc)}))
        return 2

    # Step 2: build
    if not args.skip_build:
        try:
            _stage_build_batches(cursor, args.since, args.dry_run, args.verbose)
        except Exception as exc:
            print(f"[browser] WARN: builder fail {exc}", file=sys.stderr)

    # Step 3: pending
    pending = _list_pending_batches(_INPUT_BATCHES_DIR, _POSTED_DIR,
                                     _CARDS_DIR, args.max_batches)
    if args.verbose:
        print(f"[step3] {len(pending)} pending batches found")

    # Step 4: worker
    worker_result = {"returncode": 0, "n_submitted": 0}
    if pending:
        worker_result = _stage_run_worker(pending, args.max_batches,
                                           args.dry_run, args.verbose,
                                           mode=args.mode)

    # Step 5: post
    n_posted = 0
    if not args.skip_post:
        post_result = _stage_post_cards(args.dry_run, args.verbose)
    else:
        post_result = {"returncode": 0, "skipped": True}

    # Step 6: cursor
    cursor["last_run_ts"] = datetime.now(timezone.utc).isoformat()
    cursor["n_runs"] = cursor.get("n_runs", 0) + 1
    cursor["last_summary"] = {
        "n_pending": len(pending),
        "n_extracted": worker_result.get("n_submitted", 0),
        "n_posted": n_posted,
        "mode": args.mode,
        "duration_sec": round(time.time() - t0, 2),
    }
    if not args.dry_run:
        _save_cursor(cursor)

    summary = {
        "source": _SOURCE,
        "mode": args.mode,
        "n_pending": len(pending),
        "n_submitted": worker_result.get("n_submitted", 0),
        "worker_rc": worker_result.get("returncode", 0),
        "post_rc": post_result.get("returncode", 0),
        "duration_sec": round(time.time() - t0, 2),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if worker_result.get("returncode", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
