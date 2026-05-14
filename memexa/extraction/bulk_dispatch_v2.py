"""Win-side bulk dispatcher for L0 v5 — 50-batch PII loop.

Spec: docs/l0_v5/MASTER_PLAN.md §4.3, §5, §12.3
CEO directive 2026-05-06: your-org disk chat data ≤50 batches at any time.
                          Pull cards back immediately; delete your-org copy.

Loop logic (per chunk of ≤50 batches):
  1. Inject manifest_slice into each batch's prompt.json (tmp dir; archive untouched)
  2. Tar+ssh stream to your-org shard directory
  3. Poll done_dir on your-org until all batches processed or timeout
  4. Pull cards back via pull_cards_v2.py
  5. Audit your-org clean (zero prompt.json under batches/done)
  6. Append chunk audit result to data/l0_v5/dispatch_audit.jsonl

Usage:
    python bulk_dispatch_v2.py \\
        --start-date 2026-01-01 --end-date 2026-05-06 \\
        --pass 2 \\
        --ustc-host remote-server \\
        --ustc-base /tmp/memexa_l0_ustc/data/l0_verify_2026_05_06_ustc \\
        --shards "0,2" \\
        --chunk-size 50 \\
        --manifest-path data/identity_manifest.yaml
"""
import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Repository root: 3 levels up from data/l0_v5/code/
WORKSPACE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKSPACE))

from memexa.core.identity_manifest import ManifestStore

logger = logging.getLogger("bulk_dispatch_v2")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

AUDIT_JSONL = WORKSPACE / "data/l0_v5/dispatch_audit.jsonl"
DEFAULT_POLL_INTERVAL = 10
DEFAULT_TIMEOUT_PER_BATCH = 300
MIN_TIMEOUT = 120


def _run(
    cmd: List[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with consistent error surfacing."""
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed (rc={result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr[:500]}"
        )
    return result


def _ssh(host: str, remote_cmd: str, *, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["ssh", host, remote_cmd], check=check)


def _append_audit(entry: Dict[str, Any]) -> None:
    AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _date_in_range(date_str: str, start: str, end: str) -> bool:
    return start <= date_str <= end


def collect_eligible_batches(
    archive_root: Path,
    start_date: str,
    end_date: str,
) -> List[Tuple[str, str, Path]]:
    """Return list of (date_str, batch_id, prompt_path) within [start, end]."""
    eligible: List[Tuple[str, str, Path]] = []
    if not archive_root.exists():
        logger.warning("archive root does not exist: %s", archive_root)
        return eligible

    for date_dir in sorted(archive_root.iterdir()):
        if not date_dir.is_dir():
            continue
        d = date_dir.name
        if not _date_in_range(d, start_date, end_date):
            continue
        for batch_dir in sorted(date_dir.iterdir()):
            if not batch_dir.is_dir():
                continue
            prompt = batch_dir / "prompt.json"
            if not prompt.exists():
                continue
            eligible.append((d, batch_dir.name, prompt))

    return eligible


def _load_prompt(prompt_path: Path) -> Dict[str, Any]:
    with open(prompt_path, encoding="utf-8") as f:
        return json.load(f)


def _extract_senders_and_meta(
    prompt_data: Dict[str, Any],
) -> Tuple[List[str], str, Tuple[str, str]]:
    """Return (sender_wxid_hashes, room_hash, (window_start, window_end))."""
    sender_list: List[str] = prompt_data.get("sender_list", [])
    room_hash: str = prompt_data.get("room_hash", "")
    window: Dict[str, str] = prompt_data.get("batch_window_local", {})
    window_start = window.get("start", "1970-01-01T00:00:00+00:00")
    window_end = window.get("end", "2099-01-01T00:00:00+00:00")
    return sender_list, room_hash, (window_start, window_end)


def inject_manifest_slice(
    prompt_data: Dict[str, Any],
    store: ManifestStore,
) -> Dict[str, Any]:
    """Return a new dict with manifest_slice injected; original dict unchanged."""
    sender_hashes, room_hash, time_window = _extract_senders_and_meta(prompt_data)
    manifest_slice = store.extraction_slice_for_batch(
        sender_wxid_hashes=sender_hashes,
        room_hash=room_hash,
        time_window_iso=time_window,
    )
    injected = dict(prompt_data)
    injected["manifest_slice"] = manifest_slice
    return injected


def dispatch_chunk_to_ustc(
    chunk: List[Tuple[str, str, Path]],
    shard_id: int,
    ustc_host: str,
    remote_shard_dir: str,
    tmp_root: Path,
    store: ManifestStore,
    archive_root: Path,
) -> List[str]:
    """Stream one chunk to your-org via tar+ssh.  Returns list of batch_ids dispatched."""
    logger.info("Dispatching chunk of %d batches to shard%d", len(chunk), shard_id)

    # Write manifest-injected prompt.json files to a tmp staging dir
    staging = tmp_root / f"shard{shard_id}"
    staging.mkdir(parents=True, exist_ok=True)

    batch_ids: List[str] = []
    for date_str, batch_id, prompt_path in chunk:
        try:
            prompt_data = _load_prompt(prompt_path)
            injected = inject_manifest_slice(prompt_data, store)
        except Exception as exc:
            logger.warning("manifest injection failed for %s: %s", batch_id, exc)
            # Still dispatch original (better than skipping)
            injected = _load_prompt(prompt_path)

        out_dir = staging / date_str / batch_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "prompt.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(injected, f, ensure_ascii=False)
        batch_ids.append(batch_id)

    # Make remote shard dir
    _ssh(ustc_host, f"mkdir -p {remote_shard_dir}")

    # Build file list (relative to staging root)
    list_file = tmp_root / f"shard{shard_id}_list.txt"
    with open(list_file, "w", encoding="utf-8", newline="\n") as f:
        for date_str, batch_id, _ in chunk:
            f.write(f"{date_str}/{batch_id}/prompt.json\n")

    # tar + ssh streaming — two explicit Popen objects connected via pipe,
    # no shell=True required.  Works on both Unix and Windows (Git-bash / WSL).
    staging_unix = str(staging).replace("\\", "/")
    list_unix = str(list_file).replace("\\", "/")

    tar_cmd = ["tar", "-cf", "-", "-T", list_unix]
    ssh_cmd = [
        "ssh", ustc_host,
        f"cd {remote_shard_dir} && tar -xf - 2>/dev/null",
    ]

    t0 = time.time()
    try:
        tar_proc = subprocess.Popen(
            tar_cmd,
            cwd=staging_unix,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        ssh_proc = subprocess.Popen(
            ssh_cmd,
            stdin=tar_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # Allow tar to receive SIGPIPE if ssh exits early
        if tar_proc.stdout is not None:
            tar_proc.stdout.close()
        _, ssh_stderr = ssh_proc.communicate()
        tar_proc.wait()
        rc = ssh_proc.returncode
    except Exception as pipe_exc:
        logger.warning("tar+ssh pipe exception: %s", pipe_exc)
        rc = 1
        ssh_stderr = b""

    elapsed = time.time() - t0
    if rc != 0:
        err_txt = ssh_stderr.decode(errors="replace")[:300] if ssh_stderr else ""
        logger.warning("tar+ssh pipe error (rc=%d): %s", rc, err_txt)
    logger.info("Chunk streamed in %.1fs (%d batches)", elapsed, len(chunk))

    # Verify remote count
    p = _run(
        ["ssh", ustc_host, f"find {remote_shard_dir} -name prompt.json | wc -l"],
        check=False,
    )
    remote_count = int(p.stdout.strip() or "0")
    logger.info("your-org shard%d remote prompt.json count: %d", shard_id, remote_count)
    if remote_count < len(chunk):
        logger.warning(
            "Remote count %d < chunk size %d — some files may be missing",
            remote_count, len(chunk),
        )

    return batch_ids


# ─────────────────────────── polling ───────────────────────────────


def poll_ustc_until_done(
    batch_ids: List[str],
    ustc_host: str,
    remote_done_dir: str,
    timeout_seconds: int,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
) -> Tuple[bool, int]:
    """Poll your-org done_dir until all batch_ids have .done sentinels or timeout.

    Returns (all_done: bool, n_done: int).
    """
    deadline = time.time() + timeout_seconds
    target = len(batch_ids)
    logger.info(
        "Polling %s for %d done sentinels (timeout=%ds)",
        remote_done_dir, target, timeout_seconds,
    )

    while time.time() < deadline:
        p = _run(
            ["ssh", ustc_host,
             f"find {remote_done_dir} -name '*.done' 2>/dev/null | wc -l"],
            check=False,
        )
        n_done = int(p.stdout.strip() or "0")
        logger.info("  done: %d / %d", n_done, target)
        if n_done >= target:
            return True, n_done
        time.sleep(poll_interval)

    # Final recheck
    p = _run(
        ["ssh", ustc_host,
         f"find {remote_done_dir} -name '*.done' 2>/dev/null | wc -l"],
        check=False,
    )
    n_done = int(p.stdout.strip() or "0")
    return n_done >= target, n_done


# ─────────────────────────── pull + audit ──────────────────────────


def pull_and_clean(
    shard_id: int,
    ustc_host: str,
    ustc_base: str,
    script_dir: Path,
) -> bool:
    """Invoke pull_cards_v2.py for this shard.  Returns True on success."""
    pull_script = script_dir / "pull_cards_v2.py"
    cmd = [
        sys.executable, str(pull_script),
        "--ustc-host", ustc_host,
        "--shard", str(shard_id),
        "--strict-clean",
        "--ustc-base", ustc_base,
    ]
    logger.info("Pulling shard%d via pull_cards_v2.py ...", shard_id)
    result = subprocess.run(cmd, capture_output=False, text=True)
    if result.returncode != 0:
        logger.error("pull_cards_v2.py failed for shard%d (rc=%d)", shard_id, result.returncode)
        return False
    return True


def audit_ustc_clean(
    ustc_host: str,
    remote_base: str,
) -> Dict[str, Any]:
    """Verify no PII-bearing files remain under remote_base.

    Checks: prompt.json, *.done, cards_v2/, pass1_out/ files.
    Returns audit dict.
    """
    pii_patterns = [
        ("prompt.json",
         f"find {remote_base} -name 'prompt.json' 2>/dev/null | wc -l"),
        ("done_sentinels",
         f"find {remote_base} -name '*.done' 2>/dev/null | wc -l"),
        ("cards_v2",
         f"find {remote_base}/cards_v2 -type f 2>/dev/null | wc -l"),
        ("pass1_out",
         f"find {remote_base}/pass1_out -type f 2>/dev/null | wc -l"),
    ]
    findings: Dict[str, int] = {}
    for label, remote_cmd in pii_patterns:
        p = _run(["ssh", ustc_host, remote_cmd], check=False)
        findings[label] = int(p.stdout.strip() or "0")

    passed = all(v == 0 for v in findings.values())
    return {"passed": passed, "findings": findings}


# ─────────────────────────── main loop ─────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description="L0 v5 bulk dispatcher — 50-batch PII loop (Win-side)"
    )
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--pass", dest="pass_num", type=int, default=2,
                    choices=[1, 2], help="LLM pass number (1=identity, 2=main cards)")
    ap.add_argument("--ustc-host", default="remote-server")
    ap.add_argument("--ustc-base",
                    default="/tmp/memexa_l0_ustc/data/l0_verify_2026_05_06_ustc")
    ap.add_argument("--shards", default="0,2",
                    help="Comma-separated GPU shard IDs (e.g. '0,2')")
    ap.add_argument("--chunk-size", type=int, default=50,
                    help="Max batches per dispatch chunk (PII limit ≤50)")
    ap.add_argument("--manifest-path",
                    default=str(WORKSPACE / "data/identity_manifest.yaml"))
    ap.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    ap.add_argument("--timeout-per-batch", type=int, default=DEFAULT_TIMEOUT_PER_BATCH,
                    help="Seconds of your-org processing budget per batch")
    ap.add_argument("--dry-run", action="store_true",
                    help="Collect batches and plan dispatch, but do not transfer")
    args = ap.parse_args()

    shard_ids: List[int] = [int(s.strip()) for s in args.shards.split(",") if s.strip()]
    n_shards = len(shard_ids)

    logger.info("=== L0 v5 bulk_dispatch_v2 ===")
    logger.info("Date range: %s → %s | pass=%d | shards=%s | chunk=%d",
                args.start_date, args.end_date,
                args.pass_num, shard_ids, args.chunk_size)

    # Load identity manifest (read-only for extraction slice injection)
    logger.info("Loading manifest from %s ...", args.manifest_path)
    try:
        store = ManifestStore.load(args.manifest_path)
        logger.info("Manifest loaded: %s", store.stats())
    except Exception as exc:
        logger.warning("Manifest load failed (%s); proceeding without slice injection", exc)
        store = ManifestStore()  # empty fallback

    # Collect eligible batches
    archive_root = WORKSPACE / "data/extract_archive"
    all_batches = collect_eligible_batches(archive_root, args.start_date, args.end_date)
    logger.info("Total eligible batches: %d", len(all_batches))

    if not all_batches:
        logger.info("Nothing to dispatch.")
        return

    # Shard assignment (round-robin across active shard_ids)
    shard_buckets: Dict[int, List[Tuple[str, str, Path]]] = {sid: [] for sid in shard_ids}
    for i, item in enumerate(all_batches):
        sid = shard_ids[i % n_shards]
        shard_buckets[sid].append(item)

    script_dir = Path(__file__).parent

    with tempfile.TemporaryDirectory(prefix="l0v5_dispatch_") as tmp_str:
        tmp_root = Path(tmp_str)
        session_ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        total_dispatched = 0
        total_failed_chunks = 0

        for shard_id in shard_ids:
            shard_batches = shard_buckets[shard_id]
            if not shard_batches:
                logger.info("shard%d: no batches", shard_id)
                continue

            logger.info("=== shard%d: %d batches total ===", shard_id, len(shard_batches))

            # Chunk the shard into groups of ≤chunk_size (PII limit)
            chunks = [
                shard_batches[i: i + args.chunk_size]
                for i in range(0, len(shard_batches), args.chunk_size)
            ]

            remote_batches_dir = f"{args.ustc_base}/batches/shard{shard_id}"
            remote_done_dir = f"{args.ustc_base}/done/shard{shard_id}"

            for chunk_idx, chunk in enumerate(chunks):
                chunk_label = f"shard{shard_id}_chunk{chunk_idx}"
                logger.info("--- %s: %d batches ---", chunk_label, len(chunk))

                chunk_audit: Dict[str, Any] = {
                    "session_ts": session_ts,
                    "chunk_label": chunk_label,
                    "shard_id": shard_id,
                    "pass_num": args.pass_num,
                    "chunk_size": len(chunk),
                    "batch_ids": [b[1] for b in chunk],
                    "status": "PENDING",
                }

                if args.dry_run:
                    logger.info("  [dry-run] would dispatch %d batches", len(chunk))
                    chunk_audit["status"] = "DRY_RUN"
                    _append_audit(chunk_audit)
                    continue

                try:
                    # Step a+b+c: inject manifest + tar+ssh stream
                    batch_ids = dispatch_chunk_to_ustc(
                        chunk=chunk,
                        shard_id=shard_id,
                        ustc_host=args.ustc_host,
                        remote_shard_dir=remote_batches_dir,
                        tmp_root=tmp_root,
                        store=store,
                        archive_root=archive_root,
                    )

                    # Step d: poll until done
                    timeout = max(MIN_TIMEOUT, len(chunk) * args.timeout_per_batch)
                    all_done, n_done = poll_ustc_until_done(
                        batch_ids=batch_ids,
                        ustc_host=args.ustc_host,
                        remote_done_dir=remote_done_dir,
                        timeout_seconds=timeout,
                        poll_interval=args.poll_interval,
                    )
                    chunk_audit["poll_result"] = {"all_done": all_done, "n_done": n_done}

                    if not all_done:
                        logger.warning(
                            "%s: your-org timed out — %d/%d done. Continuing pull anyway.",
                            chunk_label, n_done, len(chunk),
                        )

                    # Step e: pull cards back + delete your-org copy
                    pull_ok = pull_and_clean(
                        shard_id=shard_id,
                        ustc_host=args.ustc_host,
                        ustc_base=args.ustc_base,
                        script_dir=script_dir,
                    )
                    chunk_audit["pull_ok"] = pull_ok

                    # Step f: PII residue audit
                    audit_result = audit_ustc_clean(
                        ustc_host=args.ustc_host,
                        remote_base=args.ustc_base,
                    )
                    chunk_audit["pii_audit"] = audit_result

                    if not audit_result["passed"]:
                        logger.error(
                            "PII AUDIT FAIL for %s — halting! findings: %s",
                            chunk_label, audit_result["findings"],
                        )
                        chunk_audit["status"] = "AUDIT_FAIL"
                        _append_audit(chunk_audit)
                        total_failed_chunks += 1
                        # Halt per §12.3: audit fail → halt
                        sys.exit(1)

                    chunk_audit["status"] = "OK" if (pull_ok and all_done) else "PARTIAL"
                    total_dispatched += len(chunk)

                except Exception as exc:
                    logger.error("Chunk %s failed: %s", chunk_label, exc, exc_info=True)
                    chunk_audit["status"] = "ERROR"
                    chunk_audit["error"] = str(exc)
                    total_failed_chunks += 1

                _append_audit(chunk_audit)
                logger.info("  %s → %s", chunk_label, chunk_audit["status"])

    logger.info(
        "=== dispatch complete: %d batched dispatched, %d failed chunks ===",
        total_dispatched, total_failed_chunks,
    )
    logger.info("Audit log: %s", AUDIT_JSONL)

    if total_failed_chunks > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
