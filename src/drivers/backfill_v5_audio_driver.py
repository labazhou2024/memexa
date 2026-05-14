"""V5 Audio backfill driver — cron-callable incremental pipeline.

Orchestrates the full v5 ingestion pipeline for audio recordings:
  Step 0: Detect new recordings on /Volumes/L23/RECORD (mac side)
          + ~/Downloads (manual drops) and rsync to ~/MEMEX_audio/raw/
  Step 1: For each not-yet-processed raw audio, run audio_pipeline.py
          (Silero VAD + mlx-whisper + ECAPA + clustering) → transcript.jsonl
  Step 2: Build v5 prompt.json batches via v5_audio_batch_builder.py
  Step 3: Identify pending batches in data/l0_v5/input_batches_audio/
          (PG-aware, exclude already-in-v5 + local markers)
  Step 4: Run extractor worker (your-org API by default)
  Step 5: POST extracted cards to hindsight v5 bank
  Step 6: Save cursor + emit trace summary

Source: audio recordings (recording pen / Mac mic)
PG metadata.source: "audio"

This driver runs on Win and SSH'es to Mac for Step 0+1 (audio data lives there
+ Whisper/ECAPA models are local to Mac). Step 2-5 run on Win against rsync'd
transcripts.

CLI:
  python -m tools.backfill_v5_audio_driver
  python -m tools.backfill_v5_audio_driver --mode=api --verbose
  python -m tools.backfill_v5_audio_driver --max-batches 5 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_REPO = Path(__file__).resolve().parents[1]
# OSS: writable data lives in the user's workspace, resolved via env
# (MEMEX_WORKSPACE_ROOT) or `~/.claude/projects/`. See docs/configuration.md.
from src.core._path_resolver import data_dir as _resolve_data_dir
_DATA = _resolve_data_dir()

_CURSOR_PATH = _DATA / "backfill_v5_audio_progress.json"
_INPUT_BATCHES_DIR = _DATA / "l0_v5" / "input_batches_audio"
_CARDS_DIR = _DATA / "l0_v5" / "work" / "cards_v2_audio"
_POSTED_DIR = _DATA / "l0_v5" / "work" / "posted_v5_audio"

_AUDIO_BUILDER = _REPO / "ingestion" / "v5_audio_batch_builder.py"
_API_WORKER = _REPO / "extraction" / "l0_worker_api.py"
_MAC_WORKER = _REPO / "extraction" / "l0_worker_serial.py"
_STREAMING_POST = _REPO / "extraction" / "streaming_post_v5.py"
_LOCAL_TRANSCRIPTS_MIRROR = _DATA / "audio" / "transcripts"  # rsync'd from Mac

_DEFAULT_MAX_BATCHES = int(os.environ.get("MEMEX_V5_BATCH_LIMIT", "5"))
_SOURCE = "audio"

_HINDSIGHT_BASE_URL = os.environ.get("MEMEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
_HINDSIGHT_BANK = os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5")

# SSH alias for Mac Studio (defined in ~/.ssh/config)
_MAC_SSH = os.environ.get("MEMEX_MAC_SSH_ALIAS", "primary-host")
_MAC_AUDIO_ROOT = "~/MEMEX_audio"
_MAC_RECORDER_VOL = "/Volumes/L23/RECORD"
_MAC_DOWNLOADS_GLOB = "~/Downloads/*.m4a ~/Downloads/*.mp3 ~/Downloads/*.wav"


# -----------------------------------------------------------------------------
# Cursor
# -----------------------------------------------------------------------------
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
            "last_run_ts": None, "n_runs": 0,
            "processed_audio_sha": [],          # list of audio file SHAs seen
            "session_ids_built": [],            # list of session_id done
            "last_summary": {},
        }
    try:
        d = json.loads(_CURSOR_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("cursor not dict")
        d.setdefault("processed_audio_sha", [])
        d.setdefault("session_ids_built", [])
        d.setdefault("last_summary", {})
        d.setdefault("n_runs", 0)
        return d
    except Exception as exc:
        print(f"[audio driver] WARN cursor load fail: {exc}", file=sys.stderr)
        return {"last_run_ts": None, "n_runs": 0,
                "processed_audio_sha": [], "session_ids_built": [],
                "last_summary": {}}


def _save_cursor(cur: dict) -> None:
    try:
        _atomic_write_json(_CURSOR_PATH, cur)
    except Exception as exc:
        print(f"[audio driver] WARN cursor save fail: {exc}", file=sys.stderr)


# -----------------------------------------------------------------------------
# Subprocess helper (Win → Mac via SSH or local)
# -----------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 1800, verbose: bool = False) -> dict:
    t0 = time.time()
    out: dict = {"cmd": cmd, "returncode": -1, "stdout": "", "stderr": "",
                 "duration_sec": 0.0, "timed_out": False}
    try:
        if verbose:
            print(f"[run] {' '.join(shlex.quote(c) for c in cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        out["returncode"] = proc.returncode
        out["stdout"] = proc.stdout or ""
        out["stderr"] = proc.stderr or ""
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
    except Exception as exc:
        out["stderr"] = f"subprocess error: {exc}"
    out["duration_sec"] = round(time.time() - t0, 2)
    return out


def _ssh_mac(remote_cmd: str, timeout: int = 1800, verbose: bool = False) -> dict:
    """Run a shell command on the Mac via SSH."""
    return _run(["ssh", _MAC_SSH, remote_cmd], timeout=timeout, verbose=verbose)


def _ssh_mac_stdin(remote_cmd: str, stdin: str, timeout: int = 1800,
                    verbose: bool = False) -> dict:
    """Run a shell command on Mac, piping a body to it via stdin.

    Used for embedded bash scripts to avoid quote/escape hell in the
    Python-level command string.
    """
    t0 = time.time()
    out: dict = {"cmd": ["ssh", _MAC_SSH, remote_cmd],
                 "returncode": -1, "stdout": "", "stderr": "",
                 "duration_sec": 0.0, "timed_out": False}
    try:
        if verbose:
            print(f"[ssh-stdin] {remote_cmd}")
        proc = subprocess.run(
            ["ssh", _MAC_SSH, remote_cmd],
            input=stdin, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out["returncode"] = proc.returncode
        out["stdout"] = proc.stdout or ""
        out["stderr"] = proc.stderr or ""
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
    except Exception as exc:
        out["stderr"] = f"subprocess error: {exc}"
    out["duration_sec"] = round(time.time() - t0, 2)
    return out


# -----------------------------------------------------------------------------
# Step 0 — Detect + ingest recorder files into ~/MEMEX_audio/raw
# -----------------------------------------------------------------------------
def _stage_ingest_recordings(verbose: bool) -> dict:
    """Sync recorder + Downloads audio drops into ~/MEMEX_audio/raw/<YYYY-MM-DD>.

    Idempotent — rsync skips files already there. Returns dict with
      n_synced, n_total_raw, etc.
    """
    # Ensure ingest stage script is on Mac (scp local copy if missing).
    local_script = _REPO / "scripts" / "mac_audio_ingest_stage.sh"
    remote_script = "$HOME/MEMEX_audio/ingest_stage.sh"
    # scp idempotent — overwrites if local is newer in normal use
    _run(["scp", str(local_script), f"{_MAC_SSH}:MEMEX_audio/ingest_stage.sh"],
         timeout=30, verbose=verbose)
    r = _ssh_mac(f"chmod +x {remote_script} && bash {remote_script}",
                 timeout=120, verbose=verbose)
    if r["returncode"] != 0:
        print(f"[step0] ingest WARN rc={r['returncode']} stderr={r['stderr'][:200]}",
              file=sys.stderr)
        return {"n_synced": 0, "n_total_raw": 0, "error": r["stderr"][:200]}
    try:
        return json.loads(r["stdout"].strip().splitlines()[-1])
    except Exception:
        return {"n_synced": 0, "n_total_raw": 0,
                "raw_stdout": r["stdout"][-300:]}


# -----------------------------------------------------------------------------
# Step 1 — Run audio_pipeline.py on Mac for each not-yet-processed raw file
# -----------------------------------------------------------------------------
def _stage_run_pipeline(cursor: dict, verbose: bool,
                        max_files: int = 5,
                        dry_run: bool = False) -> dict:
    """Per raw file not in cursor.processed_audio_sha, run audio_pipeline.py."""
    list_script = (
        "find $HOME/MEMEX_audio/raw -type f "
        "\\( -name '*.WAV' -o -name '*.wav' -o -name '*.mp3' "
        "-o -name '*.MP3' -o -name '*.m4a' \\) "
        "-exec shasum -a 256 {} \\;"
    )
    r = _ssh_mac(list_script, timeout=60, verbose=verbose)
    if r["returncode"] != 0:
        return {"n_processed": 0, "error": r["stderr"][:200]}

    processed_set = set(cursor.get("processed_audio_sha") or [])
    new_files: list[tuple[str, str]] = []  # (sha, path)
    for line in r["stdout"].splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        sha, p = parts[0], parts[1]
        if sha not in processed_set:
            new_files.append((sha, p))
    new_files = new_files[:max_files]

    n_processed = 0
    new_sessions: list[str] = []
    for sha, p in new_files:
        session_id = f"audio_{sha[:16]}"
        if dry_run:
            print(f"[DRY] would run pipeline on {p} → session={session_id}")
            continue
        if verbose:
            print(f"[step1] pipeline on {p} → session={session_id}")
        cmd = (
            f"$HOME/MEMEX_audio/run_pipeline.sh "
            f"--input {shlex.quote(p)} "
            f"--output-dir $HOME/MEMEX_audio/transcripts "
            f"--session-id {shlex.quote(session_id)}"
        )
        r2 = _ssh_mac(cmd, timeout=7200, verbose=verbose)
        if r2["returncode"] == 0:
            processed_set.add(sha)
            new_sessions.append(session_id)
            n_processed += 1
        else:
            print(f"[step1] pipeline FAIL {p}: rc={r2['returncode']} "
                  f"stderr={r2['stderr'][:300]}", file=sys.stderr)

    cursor["processed_audio_sha"] = sorted(processed_set)
    cursor["session_ids_built"] = sorted(
        set(cursor.get("session_ids_built", [])) | set(new_sessions)
    )
    return {"n_processed": n_processed, "new_sessions": new_sessions}


# -----------------------------------------------------------------------------
# Step 1.5 — rsync transcripts Mac → Win local mirror
# -----------------------------------------------------------------------------
def _stage_rsync_transcripts(verbose: bool) -> dict:
    """Pull Mac ~/MEMEX_audio/transcripts/* → data/audio/transcripts/.

    rsync via SSH; preserves timestamps; only copies new sessions.

    Note: workspace path contains Chinese characters (OneDrive 桌面).
    Wrap LOCAL destination in explicit quotes and avoid shell expansion.
    """
    _LOCAL_TRANSCRIPTS_MIRROR.mkdir(parents=True, exist_ok=True)
    # Use rsync over SSH. -a preserves; --delete=false ensures no remote drift.
    # Pass dest as raw str (subprocess handles quoting on Win).
    dest = str(_LOCAL_TRANSCRIPTS_MIRROR).rstrip("\\/") + "/"
    cmd = ["rsync", "-az", "--no-perms", "--no-owner", "--no-group",
           f"{_MAC_SSH}:MEMEX_audio/transcripts/", dest]
    r = _run(cmd, timeout=600, verbose=verbose)
    if r["returncode"] != 0:
        # Fall back to scp -r (Win sometimes ships without rsync, or rsync
        # fails on the destination path encoding).
        cmd2 = ["scp", "-r",
                f"{_MAC_SSH}:MEMEX_audio/transcripts/",
                str(_LOCAL_TRANSCRIPTS_MIRROR.parent) + "/"]
        r2 = _run(cmd2, timeout=900, verbose=verbose)
        if r2["returncode"] != 0:
            return {"rsync_rc": r["returncode"], "scp_rc": r2["returncode"],
                    "stderr": (r["stderr"] + " | " + r2["stderr"])[:400]}
        # scp drops dir as transcripts/ at parent; that may create
        # data/audio/transcripts/ already.
    n_sessions = (
        len([p for p in _LOCAL_TRANSCRIPTS_MIRROR.iterdir() if p.is_dir()])
        if _LOCAL_TRANSCRIPTS_MIRROR.exists() else 0
    )
    return {"rsync_rc": r["returncode"], "n_sessions_local": n_sessions}


# -----------------------------------------------------------------------------
# Step 2 — Build v5 prompt.json batches per session
# -----------------------------------------------------------------------------
def _stage_build_batches(cursor: dict, dry_run: bool,
                          verbose: bool) -> dict:
    n_built = 0
    if not _LOCAL_TRANSCRIPTS_MIRROR.exists():
        return {"n_built": 0, "warn": "no transcripts mirrored"}
    for sd in sorted(_LOCAL_TRANSCRIPTS_MIRROR.iterdir()):
        if not sd.is_dir():
            continue
        if not (sd / "transcript.jsonl").exists():
            continue
        # Skip if already built (cursor tracks session_id; but we also peek
        # input_batches_audio to confirm)
        marker = sd / ".v5_batches_built"
        if marker.exists():
            continue
        cmd = [
            sys.executable, str(_AUDIO_BUILDER),
            "--session-dir", str(sd),
            "--out", str(_INPUT_BATCHES_DIR),
            "--skip-existing",
        ]
        if verbose:
            cmd.append("--verbose")
        if dry_run:
            print(f"[DRY] {' '.join(cmd)}")
            continue
        r = _run(cmd, timeout=300, verbose=verbose)
        if r["returncode"] == 0:
            marker.write_text(json.dumps({"built_at": time.time()}),
                              encoding="utf-8")
            n_built += 1
        else:
            print(f"[step2] builder FAIL {sd}: stderr={r['stderr'][:200]}",
                  file=sys.stderr)
    return {"n_built": n_built}


# -----------------------------------------------------------------------------
# Step 3 — List pending batches (PG-aware)
# -----------------------------------------------------------------------------
def _list_pending_batches(limit: int) -> list[str]:
    if not _INPUT_BATCHES_DIR.exists():
        return []
    all_bids: list[str] = []
    for date_dir in sorted(_INPUT_BATCHES_DIR.iterdir()):
        if not date_dir.is_dir():
            continue
        for bd in sorted(date_dir.iterdir()):
            if bd.is_dir() and (bd / "prompt.json").exists():
                all_bids.append(bd.name)

    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    try:
        from src.core.pg_bid_cache import query_pg_existing_bids
        pg_bids = query_pg_existing_bids(_SOURCE)
    except Exception as exc:
        print(f"[step3] WARN pg_bid_cache fail ({exc})", file=sys.stderr)
        pg_bids = set()

    pending: list[str] = []
    for bid in all_bids:
        if bid in pg_bids:
            continue
        if (_POSTED_DIR / f"{bid}.posted").exists():
            continue
        if (_CARDS_DIR / f"{bid}.json").exists():
            continue
        pending.append(bid)
        if len(pending) >= limit:
            break
    return pending


# -----------------------------------------------------------------------------
# Step 4 — Run extractor worker
# -----------------------------------------------------------------------------
def _stage_run_worker(pending: list[str], max_batches: int,
                       dry_run: bool, verbose: bool, mode: str) -> dict:
    if not pending:
        return {"returncode": 0, "n_submitted": 0, "duration_sec": 0.0}
    done_dir = _DATA / "l0_v5" / "work" / "done_v2_audio"
    done_dir.mkdir(parents=True, exist_ok=True)
    _CARDS_DIR.mkdir(parents=True, exist_ok=True)
    worker_path = _API_WORKER if mode == "api" else _MAC_WORKER
    cmd = [
        sys.executable, str(worker_path),
        "--pass", "2",
        "--batches-dir", str(_INPUT_BATCHES_DIR),
        "--done-dir", str(done_dir),
        "--out-dir", str(_CARDS_DIR),
        "--concurrent", "1",
        "--max-batches", str(max_batches),
    ]
    if dry_run:
        print(f"[DRY] would run worker (mode={mode}): {' '.join(cmd)}")
        return {"returncode": 0, "n_submitted": len(pending), "dry_run": True}
    if verbose:
        print(f"[step4] worker (mode={mode}) on {len(pending)} pending")
    # 2026-05-13: 2400s → 3600s. your-org API 98s/batch concurrent=1 (per
    # HANDOFF §C.-13 LIVE). 30 batches × 98s = 49min > 40min 旧 timeout →
    # 5/13 05:36 跑实测 2408s 刚好 TIMEOUT. 60min 给 30 batches 留 ~10min buffer.
    r = _run(cmd, timeout=3600, verbose=verbose)
    if r["returncode"] != 0:
        print(f"[audio] worker WARN rc={r['returncode']}", file=sys.stderr)
        if r["stderr"]:
            print(r["stderr"][:2000], file=sys.stderr)
    return {"returncode": r["returncode"], "n_submitted": len(pending),
            "duration_sec": r["duration_sec"],
            "timed_out": r.get("timed_out", False)}


# -----------------------------------------------------------------------------
# Step 5 — POST to hindsight
# -----------------------------------------------------------------------------
def _stage_post_cards(dry_run: bool, verbose: bool) -> dict:
    _POSTED_DIR.mkdir(parents=True, exist_ok=True)
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
    r = _run(cmd, timeout=900, verbose=verbose)
    return {"returncode": r["returncode"], "duration_sec": r["duration_sec"]}


# -----------------------------------------------------------------------------
# Step 6 — Main
# -----------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-batches", type=int, default=_DEFAULT_MAX_BATCHES)
    p.add_argument("--max-audio-files", type=int, default=5,
                   help="Limit audio_pipeline runs per cycle (Mac CPU budget)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-ingest", action="store_true",
                   help="Skip Mac rsync from recorder/Downloads")
    p.add_argument("--skip-pipeline", action="store_true",
                   help="Skip ASR pipeline (assume transcripts already exist)")
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--skip-post", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--mode", choices=["local", "api"],
                   default=os.environ.get("MEMEX_V5_WORKER_MODE", "api"))
    args = p.parse_args()

    t0 = time.time()
    cursor = _load_cursor()

    summary: dict[str, Any] = {"source": _SOURCE, "mode": args.mode}

    # Step 0 — ingest
    if not args.skip_ingest:
        summary["ingest"] = _stage_ingest_recordings(args.verbose)

    # Step 1 — pipeline
    if not args.skip_pipeline:
        summary["pipeline"] = _stage_run_pipeline(cursor, args.verbose,
                                                   args.max_audio_files,
                                                   dry_run=args.dry_run)
        # Pull transcripts to Win (rsync is idempotent; safe in dry-run mode)
        summary["rsync"] = _stage_rsync_transcripts(args.verbose)

    # Step 2 — build batches
    if not args.skip_build:
        summary["build"] = _stage_build_batches(cursor, args.dry_run,
                                                  args.verbose)

    # Step 3 — pending
    pending = _list_pending_batches(args.max_batches)
    summary["n_pending"] = len(pending)

    # Step 4 — worker
    worker_result = (_stage_run_worker(pending, args.max_batches,
                                        args.dry_run, args.verbose, args.mode)
                     if pending else {"returncode": 0, "n_submitted": 0})
    summary["worker"] = worker_result

    # Step 5 — post
    if not args.skip_post:
        summary["post"] = _stage_post_cards(args.dry_run, args.verbose)
    else:
        summary["post"] = {"skipped": True}

    # Step 6 — cursor
    cursor["last_run_ts"] = datetime.now(timezone.utc).isoformat()
    cursor["n_runs"] = cursor.get("n_runs", 0) + 1
    cursor["last_summary"] = {
        "n_pending": len(pending),
        "n_extracted": worker_result.get("n_submitted", 0),
        "mode": args.mode,
        "duration_sec": round(time.time() - t0, 2),
    }
    if not args.dry_run:
        _save_cursor(cursor)

    summary["duration_sec"] = round(time.time() - t0, 2)
    print(json.dumps(summary, ensure_ascii=False))
    rc = worker_result.get("returncode", 0)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
