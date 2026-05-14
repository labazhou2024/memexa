"""Win-side puller + your-org cleaner for L0 v5 (schema v2 cards).

Spec: docs/l0_v5/MASTER_PLAN.md §4.3, §5, §12.3
CEO directive 2026-05-06: after successful pull, delete ALL chat/card data
                          from your-org; PII audit failure = hard halt.

Per shard:
  1. scp pull /tmp/memex_l0_ustc/.../cards_v2/shard*  → Win data/l0_v5/cards_v2/shard*/
  2. scp pull /tmp/memex_l0_ustc/.../pass1_out/shard*  → Win data/l0_v5/pass1/shard*/
  3. ssh delete your-org: cards_v2/, pass1_out/, batches/, done/, logs/<shard>.log
  4. PII audit: grep for any remaining prompt.json / *.done / chat-content files.
               Any found → AUDIT FAIL, hard halt.

Usage:
    python pull_cards_v2.py \\
        --ustc-host remote-server \\
        --shard 0 \\
        --strict-clean \\
        --ustc-base /tmp/memex_l0_ustc/data/l0_verify_2026_05_06_ustc
"""
import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Repository root: 3 levels up from data/l0_v5/code/
WORKSPACE = Path(__file__).resolve().parents[3]

logger = logging.getLogger("pull_cards_v2")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

DEFAULT_REMOTE_HOST = "remote-server"
DEFAULT_REMOTE_BASE = "/tmp/memex_l0_ustc/data/l0_verify_2026_05_06_ustc"

# Local output directories relative to WORKSPACE
LOCAL_CARDS_V2 = "data/l0_v5/cards_v2"
LOCAL_PASS1 = "data/l0_v5/pass1"


# ─────────────────────────── helpers ───────────────────────────────


def _run(
    cmd: List[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
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


def _ssh_count_files(host: str, remote_path: str) -> int:
    """Return number of files under remote_path (0 if path doesn't exist)."""
    p = _run(
        ["ssh", host, f"find {remote_path} -type f 2>/dev/null | wc -l"],
        check=False,
    )
    return int(p.stdout.strip() or "0")


# ─────────────────────────── scp pull ──────────────────────────────


def scp_pull_dir(
    host: str,
    remote_dir: str,
    local_dir: Path,
) -> Tuple[bool, int]:
    """Pull remote_dir/* into local_dir via scp -r.

    Returns (success, local_file_count).
    Skips pull if remote has 0 files (avoids scp error on missing path).
    """
    local_dir.mkdir(parents=True, exist_ok=True)

    n_remote = _ssh_count_files(host, remote_dir)
    if n_remote == 0:
        logger.info("  remote %s: 0 files — skip pull", remote_dir)
        return True, 0

    logger.info("  pulling %d files from %s:%s → %s", n_remote, host, remote_dir, local_dir)
    t0 = time.time()
    result = _run(
        ["scp", "-r", "-q", f"{host}:{remote_dir}/.", f"{str(local_dir)}/"],
        check=False,
    )
    elapsed = time.time() - t0

    if result.returncode != 0:
        logger.error("  scp FAILED (rc=%d): %s", result.returncode, result.stderr[:200])
        return False, 0

    local_count = len(list(local_dir.rglob("*.json")))
    logger.info("  pulled in %.1fs → %d json files locally", elapsed, local_count)
    return True, local_count


# ─────────────────────────── your-org cleanup ──────────────────────────


def delete_ustc_shard(
    host: str,
    ustc_base: str,
    shard_id: int,
) -> None:
    """Delete shard-specific directories and log file from your-org."""
    targets = [
        f"{ustc_base}/cards_v2/shard{shard_id}",
        f"{ustc_base}/pass1_out/shard{shard_id}",
        f"{ustc_base}/batches/shard{shard_id}",
        f"{ustc_base}/done/shard{shard_id}",
    ]
    for t in targets:
        logger.info("  rm -rf %s", t)
        _ssh(host, f"rm -rf {t}", check=False)

    # Remove shard-specific log file
    log_path = f"{ustc_base}/logs/shard{shard_id}.log"
    logger.info("  rm -f %s", log_path)
    _ssh(host, f"rm -f {log_path}", check=False)


# ─────────────────────────── PII audit ─────────────────────────────


# Files that constitute PII residue under the your-org base directory
_PII_AUDIT_PATTERNS: List[Tuple[str, str]] = [
    ("prompt_json",
     "find {base} -name 'prompt.json' 2>/dev/null | wc -l"),
    ("done_sentinels",
     "find {base} -name '*.done' 2>/dev/null | wc -l"),
    ("cards_v2_files",
     "find {base}/cards_v2 -type f 2>/dev/null | wc -l"),
    ("pass1_out_files",
     "find {base}/pass1_out -type f 2>/dev/null | wc -l"),
    ("batches_files",
     "find {base}/batches -type f 2>/dev/null | wc -l"),
    ("done_dir_files",
     "find {base}/done -type f 2>/dev/null | wc -l"),
]


def pii_audit(
    host: str,
    ustc_base: str,
) -> Dict[str, Any]:
    """Check no PII-bearing files remain under ustc_base.

    Returns dict with keys: passed (bool), findings (label -> count).
    """
    findings: Dict[str, int] = {}
    for label, cmd_template in _PII_AUDIT_PATTERNS:
        cmd = cmd_template.format(base=ustc_base)
        p = _run(["ssh", host, cmd], check=False)
        findings[label] = int(p.stdout.strip() or "0")

    passed = all(v == 0 for v in findings.values())
    return {"passed": passed, "findings": findings}


def print_audit_report(audit: Dict[str, Any]) -> None:
    logger.info("--- PII residue audit ---")
    for label, count in audit["findings"].items():
        status = "CLEAN" if count == 0 else f"WARN: {count} residue files"
        logger.info("  %-25s %s", label, status)
    if audit["passed"]:
        logger.info("  OVERALL: PASS")
    else:
        logger.error("  OVERALL: FAIL — PII residue detected!")


# ─────────────────────────── per-shard pull ────────────────────────


def pull_shard(
    shard_id: int,
    ustc_host: str,
    ustc_base: str,
    local_base: Path,
    *,
    strict_clean: bool,
    pull_pass1: bool = True,
) -> Dict[str, Any]:
    """Pull one shard.  Returns result dict.

    Steps:
      1. Pull cards_v2/shard{N}
      2. Pull pass1_out/shard{N} (if pull_pass1)
      3. Delete shard data from your-org
      4. PII audit
    """
    logger.info("=== pull_shard: shard%d ===", shard_id)
    result: Dict[str, Any] = {
        "shard_id": shard_id,
        "cards_v2_pulled": 0,
        "pass1_pulled": 0,
        "pull_ok": False,
        "audit": None,
        "status": "PENDING",
    }

    # Step 1: pull cards_v2
    remote_cards = f"{ustc_base}/cards_v2/shard{shard_id}"
    local_cards = local_base / LOCAL_CARDS_V2 / f"shard{shard_id}"
    ok_cards, n_cards = scp_pull_dir(ustc_host, remote_cards, local_cards)
    result["cards_v2_pulled"] = n_cards

    # Step 2: pull pass1_out
    pass1_ok = True
    n_pass1 = 0
    if pull_pass1:
        remote_pass1 = f"{ustc_base}/pass1_out/shard{shard_id}"
        local_pass1 = local_base / LOCAL_PASS1 / f"shard{shard_id}"
        pass1_ok, n_pass1 = scp_pull_dir(ustc_host, remote_pass1, local_pass1)
        result["pass1_pulled"] = n_pass1

    pull_ok = ok_cards and pass1_ok
    result["pull_ok"] = pull_ok

    if not pull_ok:
        logger.error("shard%d: pull FAILED — skipping your-org delete", shard_id)
        result["status"] = "PULL_FAILED"
        return result

    # Step 3: delete your-org shard data
    if strict_clean:
        logger.info("shard%d: deleting your-org data ...", shard_id)
        delete_ustc_shard(ustc_host, ustc_base, shard_id)

    # Step 4: PII audit
    audit = pii_audit(ustc_host, ustc_base)
    result["audit"] = audit
    print_audit_report(audit)

    if not audit["passed"]:
        logger.error(
            "shard%d: PII AUDIT FAIL — halting per §12.3! findings: %s",
            shard_id, audit["findings"],
        )
        result["status"] = "AUDIT_FAIL"
        return result

    result["status"] = "OK"
    logger.info(
        "shard%d: DONE — %d cards_v2, %d pass1 pulled; your-org clean.",
        shard_id, n_cards, n_pass1,
    )
    return result


# ─────────────────────────── main ──────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description="L0 v5 Win-side puller + your-org cleaner (per shard)"
    )
    ap.add_argument("--ustc-host", default=DEFAULT_REMOTE_HOST)
    ap.add_argument(
        "--ustc-base",
        default=DEFAULT_REMOTE_BASE,
        help="Remote your-org base path",
    )
    ap.add_argument("--shard", type=int, required=True,
                    help="Shard ID to pull (e.g. 0 or 2)")
    ap.add_argument("--strict-clean", action="store_true",
                    help="Delete your-org data after successful pull (required for PII compliance)")
    ap.add_argument("--no-pass1", action="store_true",
                    help="Skip pulling pass1_out (useful if Pass-1 not yet run)")
    ap.add_argument("--local-base", default=str(WORKSPACE),
                    help="Local workspace root (default: repo root)")
    args = ap.parse_args()

    if not args.strict_clean:
        logger.warning(
            "--strict-clean not set. your-org data will NOT be deleted. "
            "This violates PII protocol unless this is a debug run."
        )

    local_base = Path(args.local_base)

    result = pull_shard(
        shard_id=args.shard,
        ustc_host=args.ustc_host,
        ustc_base=args.ustc_base,
        local_base=local_base,
        strict_clean=args.strict_clean,
        pull_pass1=not args.no_pass1,
    )

    logger.info("=== result: %s ===", result["status"])
    if result["status"] == "AUDIT_FAIL":
        sys.exit(2)
    if result["status"] in ("PULL_FAILED", "ERROR"):
        sys.exit(1)


if __name__ == "__main__":
    main()
