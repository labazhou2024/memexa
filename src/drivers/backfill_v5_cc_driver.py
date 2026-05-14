"""V5 Claude Code session backfill driver — cron-callable incremental pipeline.

Wraps the canonical `claude_code_to_v5_converter.py` (which produced the
existing 531 cards in cc_posted/ on 2026-05-07) so every subsequent run only
adds NEW sessions. The converter itself is unchanged — this driver supplies
incremental --start-date based on a cursor and routes the output through the
same downstream path the original morning_orchestrator used:

  ~/.claude/projects/*.jsonl
    → claude_code_to_v5_converter.py
    → data/l0_v5/input_batches_claude_full/<date>/<bid>/prompt.json
    → Mac dual-LLM worker (l0_worker_serial.py)
    → data/l0_v5/work/cc_cards/<bid>.json
    → streaming_post_v5.py
    → memory_full_v5 bank
    → data/l0_v5/work/cc_posted/<bid>.posted

Why this driver exists
----------------------
The original pipeline was driven by `scripts/morning_orchestrator.ps1` as a
one-shot historical backfill (2026-01-01 → 2026-05-07). There was NO 6h cron
driver, so CC sessions stopped flowing into the graph on 2026-05-07.
This file is the missing piece: a cron-callable driver that keeps the CC
graph fed automatically.

Cursor file: data/backfill_v5_cc_progress.json (incompatible with previous
versions — use --since YYYY-MM-DD to override).

CLI:
  python -m tools.backfill_v5_cc_driver
  python -m tools.backfill_v5_cc_driver --since 2026-05-08 --verbose
  python -m tools.backfill_v5_cc_driver --max-batches 20 --dry-run
  python -m tools.backfill_v5_cc_driver --skip-build --skip-post
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

_REPO = Path(__file__).resolve().parents[1]
# OSS: writable data lives in the user's workspace, resolved via env
# (MEMEX_WORKSPACE_ROOT) or `~/.claude/projects/`. See docs/configuration.md.
from src.core._path_resolver import data_dir as _resolve_data_dir
_DATA = _resolve_data_dir()

_CURSOR_PATH = _DATA / "backfill_v5_cc_progress.json"

# Use the SAME paths as the original morning_orchestrator + claude_code_to_v5_converter.
_INPUT_BATCHES_DIR = _DATA / "l0_v5" / "input_batches_claude_full"
_CARDS_DIR = _DATA / "l0_v5" / "work" / "cc_cards"
_POSTED_DIR = _DATA / "l0_v5" / "work" / "cc_posted"
_DONE_DIR = _DATA / "l0_v5" / "work" / "done_v2_cc_full"

_CC_CONVERTER_MODULE = "data.l0_v5.code.claude_code_to_v5_converter"
_TRANSCRIPTS_ROOT = Path(os.path.expanduser("~/.claude/projects"))
_SELF_NAME = "Alice"  # matches existing 531 cards' sender_list

_MAC_WORKER = _REPO / "extraction" / "l0_worker_serial.py"
_API_WORKER = _REPO / "extraction" / "l0_worker_api.py"  # 2026-05-12: your-org LLM API
_STREAMING_POST = _REPO / "extraction" / "streaming_post_v5.py"

_DEFAULT_LOOKBACK_DAYS = 4
# 2026-05-11 v3 (Phase 2.2): default 20→5 after Phase 1 your-org backfill clears
# cc's 1,972 historical missed batches. cc Phase B is slowest (30-msg batches),
# small limit avoids cron 2400s timeout.
# 2026-05-13 v2: throughput_probe (data/ustc_llm_verify/throughput_probe_*/report.json)
# real-reasoner sweep verified c=5 = 3.4 b/min (vs c=3 = 2.1; c=7 = 3.0 due to
# server-side queue saturation). c=5 is the safe sweet spot — no 429, no error.
# With c=5, cc backlog 2,212 pending → ~11h (vs prior estimate 18-20h at c=3).
_DEFAULT_MAX_BATCHES = int(os.environ.get("MEMEX_V5_BATCH_LIMIT", "50"))
_DEFAULT_CONCURRENT = int(os.environ.get("MEMEX_your-org_CONCURRENT", "5"))

# 2026-05-11 v3 (Phase 2.1): driver source identifier for PG-aware pending check.
_SOURCE = "cc"  # → pg metadata.source == "claude_code"

_HINDSIGHT_BASE_URL = os.environ.get("MEMEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
_HINDSIGHT_BANK = os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5")


# ── Helpers (mirrors backfill_v5_email_driver) ────────────────────────────
def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        raise


def _load_cursor() -> dict:
    if not _CURSOR_PATH.exists():
        return {
            "last_built_ts": time.time() - _DEFAULT_LOOKBACK_DAYS * 86400,
            "last_run_ts": None, "n_runs": 0, "last_summary": {},
        }
    try:
        data = json.loads(_CURSOR_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("cursor is not a dict")
        data.setdefault("last_built_ts",
                        time.time() - _DEFAULT_LOOKBACK_DAYS * 86400)
        data.setdefault("last_run_ts", None)
        data.setdefault("n_runs", 0)
        data.setdefault("last_summary", {})
        return data
    except Exception as exc:
        print(f"[backfill_v5_cc_driver] WARN: cursor load failed ({exc})",
              file=sys.stderr)
        return {
            "last_built_ts": time.time() - _DEFAULT_LOOKBACK_DAYS * 86400,
            "last_run_ts": None, "n_runs": 0, "last_summary": {},
        }


def _save_cursor(cur: dict) -> None:
    try:
        _atomic_write_json(_CURSOR_PATH, cur)
    except Exception as exc:
        print(f"[backfill_v5_cc_driver] WARN: cursor save failed: {exc}",
              file=sys.stderr)


def _list_pending_batches(input_dir: Path, posted_dir: Path,
                          cards_dir: Path, limit: int,
                          source: str = _SOURCE) -> list[str]:
    """PG-aware pending — see wechat driver header for full rationale.

    For cc: marker_bids=552 but PG=3,750 → 3,198 PG cards have no local
    marker (historical your-org phase-split didn't write them). Without PG
    check, cron would re-extract these 3,198 every run, hindsight dedup'd
    them. This patch eliminates that waste.
    """
    if not input_dir.exists():
        return []

    # 2026-05-11 v3 (Phase 2.1): PG-aware pending check
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    try:
        from src.core.pg_bid_cache import query_pg_existing_bids
        pg_bids = query_pg_existing_bids(source)
    except Exception as exc:
        print(f"[backfill_v5_cc_driver] WARN: pg_bid_cache fail ({exc}), "
              "falling back to marker-only check", file=sys.stderr)
        pg_bids = set()

    pending: list[str] = []
    for date_dir in sorted(input_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for batch_dir in sorted(date_dir.iterdir()):
            if not (batch_dir.is_dir() and (batch_dir / "prompt.json").exists()):
                continue
            bid = batch_dir.name
            if bid in pg_bids:
                continue  # authoritative: already in graph
            # Marker compat: either <bid>.posted file OR <bid>.json
            if (posted_dir / f"{bid}.posted").exists() or (posted_dir / f"{bid}.json").exists():
                continue
            if (cards_dir / f"{bid}.json").exists():
                continue
            pending.append(bid)
            if limit > 0 and len(pending) >= limit:
                return pending
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
        try: proc.kill()
        except Exception: pass
    except Exception as exc:
        out["stderr"] = f"subprocess launch error: {exc}"
    out["duration_sec"] = round(time.time() - t0, 2)
    # 2026-05-12: persist stderr on non-zero rc OR non-trivial stderr.
    try:
        rc = out.get("returncode", 0) or 0
        err = (out.get("stderr") or "").strip()
        if rc != 0 or (err and len(err) > 30):
            log_dir = _REPO / "data" / "maintenance_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            cmd_brief = " ".join(str(c) for c in (cmd or [])[:5])[:120]
            log_path = log_dir / f"driver_stderr_cc_{ts}_rc{rc}.log"
            with log_path.open("w", encoding="utf-8") as fp:
                fp.write(f"=== driver_stderr cc {ts} rc={rc} dur={out['duration_sec']}s ===\n")
                fp.write(f"cmd: {cmd_brief}\n")
                fp.write(f"timed_out: {out.get('timed_out')}\n")
                fp.write(f"--- stderr ({len(err)} chars) ---\n")
                fp.write(err[:50000])
                stdout_tail = (out.get("stdout") or "")[-2000:]
                if stdout_tail:
                    fp.write(f"\n--- stdout (tail 2KB) ---\n{stdout_tail}\n")
    except Exception:
        pass
    return out


def _emit_trace(event: str, payload: dict) -> None:
    try:
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        from src.core.trace_sink import write_trace_event  # type: ignore
        write_trace_event(event, payload)
    except Exception:
        ts = datetime.now(timezone.utc).isoformat()
        print(f"[trace:{ts}] {event} {json.dumps(payload, ensure_ascii=False)}",
              file=sys.stderr)


# ── Stages ────────────────────────────────────────────────────────────────
def _stage_build_batches(cursor: dict, since_override: str | None,
                         dry_run: bool, verbose: bool) -> tuple[int, float]:
    """Step 2: invoke the canonical claude_code_to_v5_converter."""
    build_start_ts = time.time()
    if since_override:
        since_date = since_override
    else:
        since_date = datetime.fromtimestamp(
            cursor["last_built_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    cmd = [
        sys.executable, "-m", _CC_CONVERTER_MODULE,
        "--transcripts-root", str(_TRANSCRIPTS_ROOT),
        "--output", str(_INPUT_BATCHES_DIR),
        "--start-date", since_date,
        "--end-date", end_date,
        "--self-name", _SELF_NAME,
    ]
    if verbose:
        cmd.append("--verbose")
    if dry_run:
        print(f"[DRY-RUN] would run cc converter: {' '.join(cmd)}")
        return 0, build_start_ts
    if verbose:
        print(f"[step2] claude_code_to_v5_converter --start {since_date} --end {end_date}")
    res = _run_subprocess(cmd, timeout=600, verbose=verbose)
    if res["returncode"] != 0:
        print(f"[backfill_v5_cc_driver] WARN: converter rc={res['returncode']}",
              file=sys.stderr)
        if res["stderr"]:
            print(res["stderr"][:2000], file=sys.stderr)

    n_new = 0
    for line in res["stdout"].splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                n_new = int(obj.get("n_built", obj.get("batches_written", n_new)))
            except (json.JSONDecodeError, ValueError):
                pass
    return n_new, build_start_ts


def _stage_run_worker(pending: list[str], max_batches: int,
                     dry_run: bool, verbose: bool,
                     mode: str = "local") -> dict:
    if not pending:
        return {"returncode": 0, "n_submitted": 0, "duration_sec": 0.0}
    _DONE_DIR.mkdir(parents=True, exist_ok=True)
    # 2026-05-13: parallel mode unlocked after your-org API c=4 live-verify.
    # Mac mode (Gemma-31B) remains forced c=1 — local GPU can't do parallel.
    concurrent_n = _DEFAULT_CONCURRENT if mode == "api" else 1
    cmd = [
        sys.executable, str(_API_WORKER if mode == "api" else _MAC_WORKER), "--pass", "2",
        "--batches-dir", str(_INPUT_BATCHES_DIR),
        "--done-dir", str(_DONE_DIR),
        "--out-dir", str(_CARDS_DIR),
        "--concurrent", str(concurrent_n),
        "--max-batches", str(max_batches),
    ]
    if dry_run:
        print(f"[DRY-RUN] would run worker: {' '.join(cmd)}")
        return {"returncode": 0, "n_submitted": len(pending), "dry_run": True}
    if verbose:
        print(f"[step4] Mac worker on {len(pending)} cc batches")
    res = _run_subprocess(cmd, timeout=14400, verbose=verbose)
    if res["returncode"] != 0:
        print(f"[backfill_v5_cc_driver] WARN: worker rc={res['returncode']}",
              file=sys.stderr)
        if res["stderr"]:
            print(res["stderr"][:2000], file=sys.stderr)
    return {"returncode": res["returncode"], "n_submitted": len(pending),
            "duration_sec": res["duration_sec"],
            "timed_out": res.get("timed_out", False)}


def _count_dir(d: Path) -> int:
    return sum(1 for _ in d.iterdir()) if d.exists() else 0


def _stage_post_cards(dry_run: bool, verbose: bool) -> dict:
    n_before = _count_dir(_POSTED_DIR)
    cmd = [sys.executable, str(_STREAMING_POST),
           "--cards-dir", str(_CARDS_DIR),
           "--posted-marker-dir", str(_POSTED_DIR),
           "--exit-when-empty-rounds", "5", "--poll", "1"]
    if dry_run:
        print(f"[DRY-RUN] would run post: {' '.join(cmd)}")
        return {"returncode": 0, "n_newly_posted": 0, "dry_run": True}
    if verbose:
        print(f"[step5] posting cc cards to {_HINDSIGHT_BASE_URL}/{_HINDSIGHT_BANK}")
    res = _run_subprocess(cmd, timeout=14400, verbose=verbose)
    if res["returncode"] != 0:
        print(f"[backfill_v5_cc_driver] WARN: streaming_post rc={res['returncode']}",
              file=sys.stderr)
        if res["stderr"]:
            print(res["stderr"][:2000], file=sys.stderr)
    n_after = _count_dir(_POSTED_DIR)
    return {"returncode": res["returncode"],
            "n_newly_posted": max(0, n_after - n_before),
            "n_posted_total": n_after,
            "duration_sec": res["duration_sec"],
            "timed_out": res.get("timed_out", False)}


# ── Main ──────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
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

    t_start = time.time()
    any_failure = False

    try:
        cursor = _load_cursor()
    except Exception as exc:
        print(json.dumps({"error": "cursor_load_failed", "detail": str(exc)}))
        return 2

    if args.verbose or args.dry_run:
        print(f"[step1] cursor: last_built_ts={cursor['last_built_ts']}, "
              f"last_run={cursor['last_run_ts']}")

    run_ts_now = datetime.now(timezone.utc)
    build_epoch_now = run_ts_now.timestamp()

    n_built = 0
    if not args.skip_build:
        try:
            n_built, _ = _stage_build_batches(cursor, args.since,
                                              args.dry_run, args.verbose)
        except Exception as exc:
            print(f"[backfill_v5_cc_driver] ERROR build: {exc}", file=sys.stderr)
            any_failure = True

    _CARDS_DIR.mkdir(parents=True, exist_ok=True)
    _POSTED_DIR.mkdir(parents=True, exist_ok=True)
    pending = _list_pending_batches(_INPUT_BATCHES_DIR, _POSTED_DIR,
                                    _CARDS_DIR, args.max_batches)
    if args.verbose or args.dry_run:
        print(f"[step3] pending cc batches (limit={args.max_batches}): {len(pending)}")
        if args.dry_run and pending:
            for bid in pending[:10]:
                print(f"  - {bid}")

    n_extracted = 0
    if pending:
        try:
            wr = _stage_run_worker(pending, args.max_batches,
                                   args.dry_run, args.verbose,
                                   mode=args.mode)
            if wr.get("returncode", 0) != 0:
                any_failure = True
            if not args.dry_run:
                n_extracted = sum(1 for bid in pending
                                  if (_CARDS_DIR / f"{bid}.json").exists())
        except Exception as exc:
            print(f"[backfill_v5_cc_driver] ERROR worker: {exc}", file=sys.stderr)
            any_failure = True

    n_posted = 0
    if not args.skip_post:
        try:
            pr = _stage_post_cards(args.dry_run, args.verbose)
            if pr.get("returncode", 0) != 0:
                any_failure = True
            n_posted = pr.get("n_newly_posted", 0)
        except Exception as exc:
            print(f"[backfill_v5_cc_driver] ERROR post: {exc}", file=sys.stderr)
            any_failure = True

    duration_sec = round(time.time() - t_start, 2)
    exit_summary = ("partial_failure" if any_failure else
                    "up_to_date" if not pending else
                    "worker_produced_no_cards" if (pending and n_extracted == 0
                                                   and not args.dry_run) else "ok")
    summary = {
        "n_built": n_built, "n_pending": len(pending),
        "n_extracted": n_extracted, "n_posted": n_posted,
        "duration_sec": duration_sec, "exit_summary": exit_summary,
        "run_ts": run_ts_now.isoformat(), "dry_run": args.dry_run,
    }

    if not args.dry_run:
        cursor["last_built_ts"] = build_epoch_now
        cursor["last_run_ts"] = run_ts_now.isoformat()
        cursor["n_runs"] = cursor.get("n_runs", 0) + 1
        cursor["last_summary"] = summary
        _save_cursor(cursor)
    else:
        print(f"[DRY-RUN] would update cursor.last_built_ts={build_epoch_now}")

    _emit_trace("v5_cc_driver_run", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
