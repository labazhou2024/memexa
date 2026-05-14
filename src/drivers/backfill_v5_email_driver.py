"""V5 Email backfill driver — cron-callable incremental pipeline.

Orchestrates the full v5 ingestion pipeline for email messages sourced from
the shared email+browser archive at data/extract_archive_email_browser/.

  Step 1: Load cursor from data/backfill_v5_email_progress.json
  Step 2: Build new input batches via tools/backfill_email_browser.py
          (runs full builder; it handles both email + browser, and the shared
          archive already separates them by source_kind field inside prompt.json)
  Step 3: Identify pending batches in data/extract_archive_email_browser/ whose
          prompt.json has source_kind=="email" AND batch_id not yet posted
          (not in posted_v5_email/<batch_id>) or extracted (not in cards_v2_email/)
  Step 4: Run Mac dual-LLM worker on pending batches
  Step 5: POST extracted cards to hindsight v5 bank (memory_full_v5)
  Step 6: Save cursor + emit trace summary

Cursor file schema (data/backfill_v5_email_progress.json):
{
  "last_built_ts": 1715000000.0,   // Unix epoch; builder --since value
  "last_run_ts":  "2026-05-10T06:00:00+00:00",  // ISO-8601 UTC last cron run
  "n_runs": 3,                     // total cron invocations (informational)
  "last_summary": {                // echo of last run's final JSON summary
    "n_built": 5,
    "n_extracted": 4,
    "n_posted": 4,
    "duration_sec": 120.3,
    "exit_summary": "ok"
  }
}

Source discrimination:
  The archive at data/extract_archive_email_browser/ stores BOTH email and
  browser_session batches under the same directory tree. Each batch's prompt.json
  has a top-level field "source_kind" which is either "email" or "browser_session"
  (or similar). This driver only processes batches where source_kind == "email".

CLI:
  python -m tools.backfill_v5_email_driver
  python -m tools.backfill_v5_email_driver --since 2026-05-01
  python -m tools.backfill_v5_email_driver --max-batches 10
  python -m tools.backfill_v5_email_driver --dry-run --verbose
  python -m tools.backfill_v5_email_driver --skip-build
  python -m tools.backfill_v5_email_driver --skip-post

Exit codes:
  0  cards extracted AND posted (or n_pending=0 → already up-to-date)
  1  subprocess failure (partial success may have occurred)
  2  cursor load failed or broken environment
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path constants — all derived from _REPO so the file is location-independent
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parents[1]
# OSS: writable data lives in the user's workspace, resolved via env
# (MEMEX_WORKSPACE_ROOT) or `~/.claude/projects/`. See docs/configuration.md.
from src.core._path_resolver import data_dir as _resolve_data_dir
_DATA = _resolve_data_dir()

_CURSOR_PATH = _DATA / "backfill_v5_email_progress.json"

# Input: v5-format email batches (with messages[] array, ready for API/Mac worker).
# Was previously legacy `extract_archive_email_browser` (phase1 v1 SPO with `prompt`
# string field) — that's incompatible with l0_worker_api (expects v5 messages).
# 2026-05-12: switched to v5 dir; v5_email_batch_builder.py writes here.
_INPUT_BATCHES_DIR = _DATA / "l0_v5" / "input_batches_email"
_CARDS_DIR = _DATA / "l0_v5" / "work" / "cards_v2_email"
_POSTED_DIR = _DATA / "l0_v5" / "work" / "posted_v5_email"

_EMAIL_BUILDER = _REPO / "ingestion" / "v5_email_batch_builder.py"
_MAC_WORKER = _REPO / "extraction" / "l0_worker_serial.py"  # 2026-05-11: strict no-overlap
_API_WORKER = _REPO / "extraction" / "l0_worker_api.py"  # 2026-05-12: your-org LLM API
_STREAMING_POST = _REPO / "extraction" / "streaming_post_v5.py"

# Source kind value that identifies email batches in the shared archive
_EMAIL_SOURCE_KIND = "email"

_DEFAULT_LOOKBACK_DAYS = 7
# 2026-05-11 v2: phase-split throughput max (see wechat driver comment)
# 2026-05-11 v3 (Phase 2.2): default 20→5 after Phase 1 your-org backfill.
_DEFAULT_MAX_BATCHES = int(os.environ.get("MEMEX_V5_BATCH_LIMIT", "5"))

# 2026-05-11 v3 (Phase 2.1): driver source identifier for PG-aware pending check.
_SOURCE = "email"

_HINDSIGHT_BASE_URL = os.environ.get("MEMEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
_HINDSIGHT_BANK = os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically write payload as JSON to path (tmp-rename pattern)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        import os as _os
        _os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _load_cursor() -> dict:
    """Load progress cursor; return default if missing or corrupt."""
    if not _CURSOR_PATH.exists():
        default_ts = time.time() - _DEFAULT_LOOKBACK_DAYS * 86400
        return {
            "last_built_ts": default_ts,
            "last_run_ts": None,
            "n_runs": 0,
            "last_summary": {},
        }
    try:
        raw = _CURSOR_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("cursor is not a dict")
        # Ensure required keys present with sensible defaults
        if "last_built_ts" not in data or not isinstance(data["last_built_ts"], (int, float)):
            data["last_built_ts"] = time.time() - _DEFAULT_LOOKBACK_DAYS * 86400
        data.setdefault("last_run_ts", None)
        data.setdefault("n_runs", 0)
        data.setdefault("last_summary", {})
        return data
    except Exception as exc:
        print(f"[backfill_v5_email_driver] WARN: cursor load failed ({exc}), using default",
              file=sys.stderr)
        return {
            "last_built_ts": time.time() - _DEFAULT_LOOKBACK_DAYS * 86400,
            "last_run_ts": None,
            "n_runs": 0,
            "last_summary": {},
        }


def _save_cursor(cur: dict) -> None:
    """Write cursor atomically to disk; log but never raise."""
    try:
        _atomic_write_json(_CURSOR_PATH, cur)
    except Exception as exc:
        print(f"[backfill_v5_email_driver] WARN: cursor save failed: {exc}",
              file=sys.stderr)


def _is_email_batch(batch_dir: Path) -> bool:
    """Return True if prompt.json in this batch has source_kind == 'email'.

    Soft-fail: returns False on any read/parse error so the batch is skipped
    (conservative) rather than accidentally processing a browser batch.
    """
    pjson = batch_dir / "prompt.json"
    if not pjson.exists():
        return False
    try:
        raw = pjson.read_bytes().decode("utf-8", errors="replace")
        # Fast path: look for source_kind field without full JSON parse
        # (prompt.json can be large due to embedded prompt text)
        import re
        m = re.search(r'"source_kind"\s*:\s*"([^"]+)"', raw)
        if m:
            return m.group(1) == _EMAIL_SOURCE_KIND
        # Fallback: try JSON parse on the suffix metadata part
        # Some prompt.json files have trailing metadata keys; try a quick parse
        try:
            data = json.loads(raw)
            return data.get("source_kind", "") == _EMAIL_SOURCE_KIND
        except (json.JSONDecodeError, ValueError):
            # Cannot parse: conservative skip
            return False
    except OSError:
        return False


def _list_pending_batches(
    input_dir: Path,
    posted_dir: Path,
    cards_dir: Path,
    limit: int,
    source: str = _SOURCE,
) -> list[str]:
    """Return email batch_ids that are built but NOT yet extracted or posted.

    Only batches whose prompt.json declares source_kind == "email" are
    considered; browser_session batches in the same archive are silently skipped.

    A batch is considered done if ANY of:
    - PG `memory_full_v5` has a card with this batch_id (NEW, authoritative)
    - posted_dir/<batch_id>.posted marker exists (local cache)
    - cards_dir/<batch_id>.json exists (extracted, awaiting POST)

    PG check via src.core.pg_bid_cache (1h TTL). See wechat driver header.
    """
    if not input_dir.exists():
        return []

    # Collect all batch directories from input (date/<batch_id>/ layout)
    all_batch_dirs: list[tuple[str, Path]] = []
    for date_dir in sorted(input_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for batch_dir in sorted(date_dir.iterdir()):
            if batch_dir.is_dir() and (batch_dir / "prompt.json").exists():
                all_batch_dirs.append((batch_dir.name, batch_dir))

    # 2026-05-11 v3 (Phase 2.1): PG-aware pending check
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    try:
        from src.core.pg_bid_cache import query_pg_existing_bids
        pg_bids = query_pg_existing_bids(source)
    except Exception as exc:
        print(f"[backfill_v5_email_driver] WARN: pg_bid_cache fail ({exc}), "
              "falling back to marker-only check", file=sys.stderr)
        pg_bids = set()

    pending: list[str] = []
    for bid, batch_dir in all_batch_dirs:
        if bid in pg_bids:
            continue
        if not _is_email_batch(batch_dir):
            continue
        already_posted = (posted_dir / f"{bid}.posted").exists()
        already_extracted = (cards_dir / f"{bid}.json").exists()
        if not already_posted and not already_extracted:
            pending.append(bid)
        if limit > 0 and len(pending) >= limit:
            break

    return pending


def _run_subprocess(
    cmd: list[str],
    timeout: int = 1800,
    verbose: bool = False,
) -> dict:
    """Run a subprocess, optionally streaming stdout.

    Returns dict:
    {
        "cmd": [...],
        "returncode": int,
        "stdout": str,       # captured if not verbose
        "stderr": str,
        "duration_sec": float,
        "timed_out": bool,
    }
    """
    t0 = time.time()
    result = {
        "cmd": cmd,
        "returncode": -1,
        "stdout": "",
        "stderr": "",
        "duration_sec": 0.0,
        "timed_out": False,
    }
    try:
        if verbose:
            # Stream stdout directly to our stdout; capture stderr
            proc = subprocess.Popen(
                cmd,
                stdout=None,           # inherit parent stdout
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            _, stderr_output = proc.communicate(timeout=timeout)
            result["returncode"] = proc.returncode
            result["stderr"] = stderr_output or ""
        else:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            result["returncode"] = proc.returncode
            result["stdout"] = proc.stdout or ""
            result["stderr"] = proc.stderr or ""
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["returncode"] = -1
        try:
            proc.kill()
        except Exception:
            pass
    except Exception as exc:
        result["stderr"] = f"subprocess launch error: {exc}"
        result["returncode"] = -1
    result["duration_sec"] = round(time.time() - t0, 2)
    return result


def _emit_trace(event: str, payload: dict) -> None:
    """Emit a trace event via src.core.trace_sink if available; else stderr."""
    try:
        # Ensure memex package importable
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        from src.core.trace_sink import write_trace_event  # type: ignore
        write_trace_event(event, payload)
    except Exception:
        # Graceful degradation: log to stderr so cron logs capture it
        ts = datetime.now(timezone.utc).isoformat()
        print(f"[trace:{ts}] {event} {json.dumps(payload, ensure_ascii=False)}",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def _stage_build_batches(
    cursor: dict,
    since_override: str | None,
    dry_run: bool,
    verbose: bool,
) -> tuple[int, float]:
    """Step 2: Run backfill_email_browser.py to build new input batches.

    Runs the full builder (tools/backfill_email_browser.py --since <date>
    --skip-browser). The builder writes batches to
    data/extract_archive_email_browser/ for both email and browser content;
    Step 3 discriminates by source_kind.

    Returns (n_new_batches_estimate, build_start_ts).
    The build_start_ts is set at the START of this stage so any messages
    arriving during the build will be caught on the next run.
    """
    build_start_ts = time.time()

    if since_override:
        try:
            dt_since = datetime.fromisoformat(since_override).replace(
                tzinfo=timezone.utc
            )
            since_ts = dt_since.timestamp()
        except ValueError:
            print(f"[backfill_v5_email_driver] WARN: invalid --since '{since_override}', "
                  "falling back to cursor", file=sys.stderr)
            since_ts = cursor["last_built_ts"]
    else:
        since_ts = cursor["last_built_ts"]

    # 2026-05-12: v5_email_batch_builder uses --start/--end (YYYY-MM-DD)
    # and writes to input_batches_email directly (no --skip-browser; v5
    # builder is email-only by design).
    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cmd = [
        sys.executable,
        str(_EMAIL_BUILDER),
        "--start", since_date,
        "--end", end_date,
        "--skip-existing",
        "--out", str(_INPUT_BATCHES_DIR),
    ]

    if dry_run:
        print(f"[DRY-RUN] would run email builder: {' '.join(cmd)}")
        return 0, build_start_ts

    if verbose:
        print(f"[step2] building email batches since {since_date}: {' '.join(cmd)}")

    res = _run_subprocess(cmd, timeout=600, verbose=verbose)
    if res["returncode"] != 0:
        print(
            f"[backfill_v5_email_driver] WARN: backfill_email_browser exited "
            f"{res['returncode']} — continuing with existing batches",
            file=sys.stderr,
        )
        if res["stderr"]:
            print(res["stderr"][:2000], file=sys.stderr)

    # Parse n_new_batches from stdout (best effort)
    n_new = 0
    for line in res["stdout"].splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                n_new = int(obj.get("n_new_batches", obj.get("n_built", n_new)))
            except (json.JSONDecodeError, ValueError):
                pass

    return n_new, build_start_ts


def _stage_run_worker(
    pending: list[str],
    max_batches: int,
    dry_run: bool,
    verbose: bool,
    mode: str = "local",
) -> dict:
    """Step 4: Run Mac worker to extract cards from pending email batches."""
    if not pending:
        return {"returncode": 0, "n_submitted": 0, "duration_sec": 0.0}

    # 2026-05-10 fix: worker mac CLI is --pass {1,2} --batches-dir --done-dir
    # --out-dir --max-batches (not --input-dir/--output-dir/--source/--limit)
    done_dir = _DATA / "l0_v5" / "work" / "done_v2_email"
    done_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(_API_WORKER if mode == "api" else _MAC_WORKER),
        "--pass", "2",
        "--batches-dir", str(_INPUT_BATCHES_DIR),
        "--done-dir", str(done_dir),
        "--out-dir", str(_CARDS_DIR),
        "--concurrent", str(int(os.environ.get("MEMEX_your-org_CONCURRENT", "5")) if mode == "api" else 1),
        "--max-batches", str(max_batches),
    ]

    if dry_run:
        print(f"[DRY-RUN] would run worker: {' '.join(cmd)}")
        return {"returncode": 0, "n_submitted": len(pending), "duration_sec": 0.0,
                "dry_run": True}

    if verbose:
        print(f"[step4] running Mac worker on {len(pending)} pending email batches")
        print(f"  cmd: {' '.join(cmd)}")

    res = _run_subprocess(cmd, timeout=14400, verbose=verbose)
    if res["returncode"] != 0:
        print(f"[backfill_v5_email_driver] WARN: worker exited {res['returncode']} "
              "(partial success possible)", file=sys.stderr)
        if res["stderr"]:
            print(res["stderr"][:2000], file=sys.stderr)

    return {
        "returncode": res["returncode"],
        "n_submitted": len(pending),
        "duration_sec": res["duration_sec"],
        "timed_out": res.get("timed_out", False),
    }


def _count_marker_dir(marker_dir: Path) -> int:
    """Count entries in a marker directory (flat files or subdirs)."""
    if not marker_dir.exists():
        return 0
    return sum(1 for _ in marker_dir.iterdir())


def _stage_post_cards(dry_run: bool, verbose: bool) -> dict:
    """Step 5: POST extracted email cards to hindsight v5 bank."""
    n_posted_before = _count_marker_dir(_POSTED_DIR)

    cmd = [
        sys.executable,
        str(_STREAMING_POST),
        "--cards-dir", str(_CARDS_DIR),
        "--posted-marker-dir", str(_POSTED_DIR),
        # 2026-05-10 fix: streaming_post_v5 uses env vars MEMEX_HINDSIGHT_URL/BANK
        "--exit-when-empty-rounds", "5",
        "--poll", "1",
    ]

    if dry_run:
        print(f"[DRY-RUN] would run post: {' '.join(cmd)}")
        return {"returncode": 0, "n_newly_posted": 0, "duration_sec": 0.0,
                "dry_run": True}

    if verbose:
        print(f"[step5] posting email cards to {_HINDSIGHT_BASE_URL}/{_HINDSIGHT_BANK}")
        print(f"  cmd: {' '.join(cmd)}")

    res = _run_subprocess(cmd, timeout=14400, verbose=verbose)
    if res["returncode"] != 0:
        print(f"[backfill_v5_email_driver] WARN: streaming_post exited {res['returncode']}",
              file=sys.stderr)
        if res["stderr"]:
            print(res["stderr"][:2000], file=sys.stderr)

    n_posted_after = _count_marker_dir(_POSTED_DIR)
    n_newly_posted = max(0, n_posted_after - n_posted_before)

    return {
        "returncode": res["returncode"],
        "n_newly_posted": n_newly_posted,
        "n_posted_total": n_posted_after,
        "duration_sec": res["duration_sec"],
        "timed_out": res.get("timed_out", False),
    }


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="V5 Email backfill driver — incremental cron pipeline",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default=None,
        help="Override cursor start date (ISO date, e.g. 2026-05-01)",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=_DEFAULT_MAX_BATCHES,
        metavar="N",
        help=f"Cap batches processed per run (default {_DEFAULT_MAX_BATCHES}, "
             "env MEMEX_V5_BATCH_LIMIT overrides default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without spawning any subprocesses",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip Step 2 (use existing batches only)",
    )
    parser.add_argument(
        "--skip-post",
        action="store_true",
        help="Skip Step 5 (extract cards only, do not POST)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream subprocess stdout to terminal",
    )
    parser.add_argument(
        "--mode",
        choices=["local", "api"],
        default=os.environ.get("MEMEX_V5_WORKER_MODE", "local"),
        help="Extractor mode: local=Mac dual-LLM, api=your-org API",
    )
    args = parser.parse_args()

    t_run_start = time.time()
    any_failure = False
    env_broken = False

    # ------------------------------------------------------------------
    # Step 1: Load cursor
    # ------------------------------------------------------------------
    try:
        cursor = _load_cursor()
    except Exception as exc:
        print(json.dumps({"error": "cursor_load_failed", "detail": str(exc)}))
        return 2

    if args.verbose or args.dry_run:
        print(f"[step1] cursor loaded: last_built_ts={cursor['last_built_ts']}, "
              f"last_run_ts={cursor['last_run_ts']}")

    # Record the run-start timestamp NOW (before building) so messages arriving
    # during this run are caught on the next invocation.
    run_ts_now = datetime.now(timezone.utc)
    build_epoch_now = run_ts_now.timestamp()

    # ------------------------------------------------------------------
    # Step 2: Build new batches
    # ------------------------------------------------------------------
    n_built = 0
    if not args.skip_build:
        try:
            n_built, _build_ts = _stage_build_batches(
                cursor, args.since, args.dry_run, args.verbose
            )
        except Exception as exc:
            print(f"[backfill_v5_email_driver] ERROR in build stage: {exc}",
                  file=sys.stderr)
            any_failure = True
    else:
        if args.verbose:
            print("[step2] skipped (--skip-build)")

    # ------------------------------------------------------------------
    # Step 3: Identify pending batches (email source_kind only)
    # ------------------------------------------------------------------
    _CARDS_DIR.mkdir(parents=True, exist_ok=True)
    _POSTED_DIR.mkdir(parents=True, exist_ok=True)

    pending = _list_pending_batches(
        _INPUT_BATCHES_DIR,
        _POSTED_DIR,
        _CARDS_DIR,
        args.max_batches,
    )

    if args.verbose or args.dry_run:
        print(f"[step3] pending email batches (limit={args.max_batches}): {len(pending)}")
        if args.dry_run and pending:
            for bid in pending[:10]:
                print(f"  - {bid}")
            if len(pending) > 10:
                print(f"  ... and {len(pending) - 10} more")

    # ------------------------------------------------------------------
    # Step 4: Run Mac worker on pending batches
    # ------------------------------------------------------------------
    n_extracted = 0
    worker_result: dict = {}
    if pending:
        try:
            worker_result = _stage_run_worker(
                pending, args.max_batches, args.dry_run, args.verbose,
                mode=args.mode,
            )
            if worker_result.get("returncode", 0) != 0:
                any_failure = True
            # Count newly extracted cards dirs
            if not args.dry_run:
                # Worker writes {bid}.json files, NOT directories. Old check
                # always returned 0 → false "worker_produced_no_cards" alerts.
                n_extracted = sum(
                    1 for bid in pending
                    if (_CARDS_DIR / f"{bid}.json").exists()
                )
            else:
                n_extracted = 0
        except Exception as exc:
            print(f"[backfill_v5_email_driver] ERROR in worker stage: {exc}",
                  file=sys.stderr)
            any_failure = True
    else:
        if args.verbose:
            print("[step4] no pending batches — skipping worker")

    # ------------------------------------------------------------------
    # Step 5: POST cards
    # ------------------------------------------------------------------
    n_posted = 0
    post_result: dict = {}
    if not args.skip_post:
        try:
            post_result = _stage_post_cards(args.dry_run, args.verbose)
            if post_result.get("returncode", 0) != 0:
                any_failure = True
            n_posted = post_result.get("n_newly_posted", 0)
        except Exception as exc:
            print(f"[backfill_v5_email_driver] ERROR in post stage: {exc}",
                  file=sys.stderr)
            any_failure = True
    else:
        if args.verbose:
            print("[step5] skipped (--skip-post)")

    # ------------------------------------------------------------------
    # Step 6: Save cursor + emit summary
    # ------------------------------------------------------------------
    duration_sec = round(time.time() - t_run_start, 2)

    exit_summary = "ok"
    if env_broken:
        exit_summary = "env_broken"
    elif any_failure:
        exit_summary = "partial_failure"
    elif pending and n_extracted == 0 and not args.dry_run:
        exit_summary = "worker_produced_no_cards"
    elif not pending:
        exit_summary = "up_to_date"

    summary = {
        "n_built": n_built,
        "n_pending": len(pending),
        "n_extracted": n_extracted,
        "n_posted": n_posted,
        "duration_sec": duration_sec,
        "exit_summary": exit_summary,
        "run_ts": run_ts_now.isoformat(),
        "dry_run": args.dry_run,
    }

    # Update cursor
    if not args.dry_run:
        cursor["last_built_ts"] = build_epoch_now
        cursor["last_run_ts"] = run_ts_now.isoformat()
        cursor["n_runs"] = cursor.get("n_runs", 0) + 1
        cursor["last_summary"] = summary
        _save_cursor(cursor)
    else:
        print(f"[DRY-RUN] would update cursor.last_built_ts={build_epoch_now}")

    # Emit trace event
    _emit_trace("v5_email_driver_run", summary)

    # Final JSON summary to stdout (cron/caller captures this)
    print(json.dumps(summary, ensure_ascii=False))

    if env_broken:
        return 2
    if any_failure:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
