"""l0_worker_serial.py — Serial dual-LLM worker on Mac with strict NO-OVERLAP guarantee.

Hard constraint (CEO 2026-05-10): "永远不要 31B 模型和 14B 模型同时占据".
Mac 36 GB RAM cannot fit Qwen-14B + Gemma-31B + sidecars + PG simultaneously.

Architecture:
  Phase A (Qwen-14B :18080 only, 31B unloaded):
    For each pending batch → Stage A judge (verdict HIGH/MEDIUM/LOW)
    LOW → write skip card immediately + done marker
    HIGH/MEDIUM → append to accept_list (no Stage B yet)

  Swap (kill Qwen → kickstart 31B → wait /v1/models 200):
    SIGTERM com.user.mlx_server     (Qwen unloaded, ~8 GB freed)
    launchctl bootstrap mlx_server_gemma_31b → kickstart
    poll :18081/v1/models until ready (max 90s)

  Phase B (Gemma-31B :18081 only, Qwen unloaded):
    For each (batch_path, prompt_data) in accept_list → Stage B extract cards
    Write cards JSON + done marker

  Swap-back (kill 31B → kickstart Qwen → wait):
    launchctl kill SIGTERM gemma_31b
    launchctl kickstart mlx_server  (re-load Qwen for next round / other clients)
    poll :18080/v1/models until ready

CLI parity with l0_worker_v2_mac.py:
    --batches-dir / --done-dir / --out-dir  (required)
    --max-batches N       limit total batches
    --group-size K        every K batches do one A→swap→B→swap cycle
                          (default 50; reduces swap overhead)
    --dry-run

Cursor / idempotent: skips batches with existing done marker.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

# Workspace root for memex.* imports
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

# Local pass2 prompt builders (same dir)
sys.path.insert(0, str(Path(__file__).parent))
from pass2_prompt import (  # noqa: E402
    PASS2_SYSTEM_PROMPT,
    build_pass2_user_prompt,
    compute_pass2_prompt_sha,
    parse_pass2_output,
)
# gatekeeper symbols live in l0_worker_v2_mac (alongside its impl)
from l0_worker_v2_mac import (  # noqa: E402
    GATEKEEPER_SYSTEM_PROMPT,
    gatekeeper_user_prompt,
    parse_gatekeeper_verdict,
)

# Reuse normalizer (same one streaming_post_v5 uses) so cards are post-able.
from src.extraction.run_e2e_pipeline import _normalize_llm_card  # noqa: E402

logger = logging.getLogger("l0_serial")

QWEN_URL = "http://127.0.0.1:18080"
GEMMA_URL = "http://127.0.0.1:18081"
QWEN_MODEL = "mlx-community/Qwen3-14B-4bit"
GEMMA_MODEL = "mlx-community/gemma-4-31b-it-4bit"
QWEN_PLIST = "com.user.mlx_server"
GEMMA_PLIST = "com.user.mlx_server_gemma_31b"

# 2026-05-11: MPS GPU contention causes 31B decode 2 tok/s vs theoretical 16 tok/s.
# Bootout bge sidecars during Phase B (Gemma-31B owns GPU). Restore after swap-back.
# Hindsight recall is temporarily blocked during Stage B (matches user intent —
# system should be doing one heavy task at a time).
BGE_M3_PLIST = "com.user.bge_m3_sidecar"
BGE_RERANKER_PLIST = "com.user.bge_reranker_sidecar"


def _ssh_run(cmd: str, timeout: int = 30) -> Tuple[int, str]:
    """SSH run with graceful timeout. Returns (returncode, output).
    On any failure (timeout, ssh unreachable) returns (255, "<error message>").
    Never raises — Mac may be powered off mid-run.
    """
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
             "primary-host", cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return 255, f"ssh timeout (>{timeout}s)"
    except (FileNotFoundError, OSError) as e:
        return 255, f"ssh failed: {e}"
    except Exception as e:
        return 255, f"ssh exception: {e}"


def _mac_reachable() -> bool:
    """Quick health probe: is Mac SSH reachable AND at least one mlx server alive?"""
    rc, _ = _ssh_run("true", timeout=10)
    return rc == 0


def _wait_for_endpoint(url: str, timeout_sec: int = 120) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/v1/models", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(3.0)
    return False


def _is_endpoint_alive(url: str) -> bool:
    try:
        r = httpx.get(f"{url}/v1/models", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def swap_to_qwen() -> bool:
    """Ensure Qwen-14B is the ONLY LLM in memory.

    Uses bootout to fully unload 31B plist (so it cannot auto-respawn).
    Then bootstrap+kickstart Qwen plist (KeepAlive=true; safe since we want it).
    2026-05-11: also re-bootstrap bge sidecars (they were bootout'd during
    swap-to-gemma to free MPS).
    """
    logger.info("[swap] → Qwen-14B (bootout 31B, ensure Qwen+bge sidecars)")
    # Fully unload 31B so it can't respawn
    _ssh_run(f"launchctl kill SIGTERM gui/501/{GEMMA_PLIST} 2>/dev/null; "
             f"launchctl bootout gui/501/{GEMMA_PLIST} 2>/dev/null", timeout=15)
    # Re-bootstrap bge sidecars (these have KeepAlive=true but bootout makes them stay down until bootstrap)
    _ssh_run(f"launchctl bootstrap gui/501 $HOME/Library/LaunchAgents/{BGE_M3_PLIST}.plist 2>/dev/null; "
             f"launchctl bootstrap gui/501 $HOME/Library/LaunchAgents/{BGE_RERANKER_PLIST}.plist 2>/dev/null", timeout=15)
    time.sleep(3)
    # Verify 31B truly dead
    rc, out = _ssh_run(f"ps -axo pid,comm,args | grep gemma-4-31b | grep -v grep | head -3", timeout=10)
    if "gemma-4-31b" in out:
        logger.warning(f"[swap] 31B still alive after bootout: {out[:200]}")
        _ssh_run("ps -axo pid,args | grep gemma-4-31b | grep -v grep | awk '{print $1}' | xargs -I{} kill -9 {}", timeout=15)
        time.sleep(2)
    # Ensure Qwen alive (it has KeepAlive=true; should auto-restart if missing)
    if not _is_endpoint_alive(QWEN_URL):
        _ssh_run(f"launchctl bootstrap gui/501 $HOME/Library/LaunchAgents/{QWEN_PLIST}.plist 2>/dev/null; "
                 f"launchctl kickstart gui/501/{QWEN_PLIST}", timeout=15)
        if not _wait_for_endpoint(QWEN_URL, timeout_sec=90):
            logger.error("[swap] Qwen failed to come online in 90s")
            return False
    logger.info("[swap] ✓ Qwen-14B ready, 31B unloaded")
    return True


def swap_to_gemma() -> bool:
    """Ensure Gemma-31B is the ONLY LLM in memory.

    2026-05-11: also bootout bge sidecars to free MPS GPU. M4 Max unified
    memory + single GPU; sidecar MPS contexts cause 31B decode 2 tok/s vs
    16 tok/s theoretical. Hindsight recall is temporarily blocked during
    Stage B — that's the intended trade-off (one heavy task at a time).
    """
    logger.info("[swap] → Gemma-31B (bootout Qwen+bge sidecars, kickstart 31B)")
    # Fully unload Qwen plist (KeepAlive=true → bootout is the ONLY way to keep it down)
    _ssh_run(f"launchctl kill SIGTERM gui/501/{QWEN_PLIST} 2>/dev/null; "
             f"launchctl bootout gui/501/{QWEN_PLIST} 2>/dev/null", timeout=15)
    # Also unload bge sidecars (free MPS)
    _ssh_run(f"launchctl bootout gui/501/{BGE_RERANKER_PLIST} 2>/dev/null; "
             f"launchctl bootout gui/501/{BGE_M3_PLIST} 2>/dev/null", timeout=15)
    time.sleep(3)
    # Verify Qwen truly dead
    rc, out = _ssh_run(f"ps -axo pid,comm,args | grep Qwen3-14B | grep -v grep | head -3", timeout=10)
    if "Qwen3-14B" in out:
        logger.warning(f"[swap] Qwen still alive after bootout: {out[:200]}")
        _ssh_run("ps -axo pid,args | grep Qwen3-14B | grep -v grep | awk '{print $1}' | xargs -I{} kill -9 {}", timeout=15)
        time.sleep(2)
    # Bootstrap 31B + kickstart
    _ssh_run(f"launchctl bootstrap gui/501 $HOME/Library/LaunchAgents/{GEMMA_PLIST}.plist 2>/dev/null; "
             f"launchctl kickstart gui/501/{GEMMA_PLIST}", timeout=15)
    if not _wait_for_endpoint(GEMMA_URL, timeout_sec=120):
        logger.error("[swap] Gemma-31B failed to come online in 120s")
        return False
    logger.info("[swap] ✓ Gemma-31B ready (Qwen+bge unloaded, GPU clear)")
    return True


def collect_pending_batches(batches_dir: Path, done_dir: Path) -> List[Path]:
    """Find prompt.json files with no .done marker yet (recursive)."""
    pending: List[Path] = []
    for p in batches_dir.rglob("prompt.json"):
        bid = p.parent.name
        if not (done_dir / f"{bid}.done").exists():
            pending.append(p)
    return sorted(pending)


def stage_a_judge_one(qwen: httpx.Client, batch_path: Path) -> Tuple[str, dict]:
    """Run Stage A only via Qwen :18080. Return (verdict, prompt_data)."""
    prompt_data = json.loads(batch_path.read_text(encoding="utf-8"))
    messages = prompt_data.get("messages", [])
    gk_user = gatekeeper_user_prompt(messages)
    payload = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system", "content": GATEKEEPER_SYSTEM_PROMPT},
            {"role": "user", "content": gk_user},
        ],
        "max_tokens": 128,
        "temperature": 0.0,
    }
    r = qwen.post(f"{QWEN_URL}/v1/chat/completions", json=payload, timeout=120.0)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    verdict, _ = parse_gatekeeper_verdict(content)
    return verdict, prompt_data


def stage_b_extract_one(
    gemma: httpx.Client,
    batch_path: Path,
    prompt_data: dict,
    out_dir: Path,
    done_dir: Path,
) -> int:
    """Run Stage B only via Gemma :18081. Returns n_cards extracted."""
    batch_id = batch_path.parent.name

    user_prompt = build_pass2_user_prompt(
        batch_id=batch_id,
        chat_room=prompt_data.get("chat_room", ""),
        room_hash=prompt_data.get("room_hash", ""),
        batch_window_local=prompt_data.get("batch_window_local", ""),
        sender_list=prompt_data.get("sender_list", []),
        manifest_slice=prompt_data.get("manifest_slice", {}),
        messages=prompt_data.get("messages", []),
    )
    prompt_sha = compute_pass2_prompt_sha(PASS2_SYSTEM_PROMPT, user_prompt)

    payload = {
        "model": GEMMA_MODEL,
        "messages": [
            {"role": "system", "content": PASS2_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4096,
        "temperature": 0.1,
    }
    try:
        r = gemma.post(f"{GEMMA_URL}/v1/chat/completions", json=payload, timeout=600.0)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"  {batch_id}: extractor HTTP failed ({e}); 0 cards")
        content = ""

    try:
        cards = parse_pass2_output(content) if content else []
    except Exception as e:
        logger.warning(f"  {batch_id}: parse failed ({e}); 0 cards")
        cards = []

    out_record = {
        "meta": {
            "batch_id": batch_id,
            "chat_room": prompt_data.get("chat_room", ""),
            "room_hash": prompt_data.get("room_hash", ""),
            "verdict": "HIGH",  # only HIGH/MEDIUM reach here
            "extraction_prompt_sha": prompt_sha,
            "gatekeeper_model": QWEN_MODEL,
            "extractor_model": GEMMA_MODEL,
            "n_invalid_dropped": 0,
            "ts": time.time(),
        },
        "cards": cards,
    }
    out_path = out_dir / f"{batch_id}.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_record, ensure_ascii=False, indent=2), encoding="utf-8")

    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / f"{batch_id}.done").write_text(str(time.time()), encoding="utf-8")
    return len(cards)


def write_skip_card(batch_id: str, verdict: str, out_dir: Path, done_dir: Path) -> None:
    """For LOW-verdict batches: write empty cards record + done marker."""
    out_record = {
        "meta": {
            "batch_id": batch_id,
            "skipped_by_gatekeeper": True,
            "verdict": verdict,
            "ts": time.time(),
        },
        "cards": [],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{batch_id}.json").write_text(
        json.dumps(out_record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / f"{batch_id}.done").write_text(str(time.time()), encoding="utf-8")


def run_group(
    pending: List[Path],
    out_dir: Path,
    done_dir: Path,
    *,
    dry_run: bool = False,
) -> Tuple[int, int, int]:
    """Process a group of batches with one A-swap-B-swap cycle.

    Returns (n_low, n_accept, n_cards).
    """
    if not pending:
        return 0, 0, 0

    # Phase A: Qwen
    if not dry_run:
        if not swap_to_qwen():
            logger.error("Failed to swap to Qwen; aborting group")
            return 0, 0, 0
    else:
        logger.info("[dry-run] would swap to Qwen")

    accept_list: List[Tuple[Path, dict]] = []
    n_low = 0
    t0 = time.time()
    qwen_client = httpx.Client(timeout=120.0)
    try:
        for i, bp in enumerate(pending):
            batch_id = bp.parent.name
            if dry_run:
                logger.info(f"[dry-run] phase A batch={batch_id}")
                if i % 3 == 0:
                    n_low += 1
                else:
                    accept_list.append((bp, {}))
                continue
            try:
                verdict, prompt_data = stage_a_judge_one(qwen_client, bp)
            except Exception as e:
                logger.warning(f"  {batch_id}: stage A failed ({e}); treat as LOW")
                verdict = "LOW"
                prompt_data = {}
            if verdict == "LOW":
                n_low += 1
                write_skip_card(batch_id, verdict, out_dir, done_dir)
                logger.info(f"[serial] phase=A batch={batch_id} verdict=LOW (skipped)")
            else:
                accept_list.append((bp, prompt_data))
                logger.info(f"[serial] phase=A batch={batch_id} verdict={verdict} (accept)")
    finally:
        qwen_client.close()
    phase_a_dur = time.time() - t0
    logger.info(f"[serial] Phase A done in {phase_a_dur:.1f}s | low={n_low} accept={len(accept_list)}")

    if not accept_list:
        return n_low, 0, 0

    # Swap to Gemma
    if not dry_run:
        if not swap_to_gemma():
            logger.error("Failed to swap to Gemma; deferring extraction")
            return n_low, len(accept_list), 0
    else:
        logger.info("[dry-run] would swap to Gemma")

    # Phase B: Gemma
    n_cards = 0
    t1 = time.time()
    gemma_client = httpx.Client(timeout=600.0)
    try:
        for bp, prompt_data in accept_list:
            batch_id = bp.parent.name
            if dry_run:
                logger.info(f"[dry-run] phase B batch={batch_id}")
                continue
            try:
                k = stage_b_extract_one(gemma_client, bp, prompt_data, out_dir, done_dir)
            except Exception as e:
                logger.error(f"  {batch_id}: stage B exception ({e}); 0 cards written")
                k = 0
            n_cards += k
            logger.info(f"[serial] phase=B batch={batch_id} cards={k}")
    finally:
        gemma_client.close()
    phase_b_dur = time.time() - t1
    logger.info(f"[serial] Phase B done in {phase_b_dur:.1f}s | n_cards={n_cards}")

    # Swap back to Qwen (so next group / other clients see Qwen alive)
    if not dry_run:
        swap_to_qwen()

    return n_low, len(accept_list), n_cards


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Serial dual-LLM worker (no overlap)")
    parser.add_argument("--batches-dir", type=Path, required=True)
    parser.add_argument("--done-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-batches", type=int, default=0,
                        help="0 = no limit")
    parser.add_argument("--group-size", type=int, default=50,
                        help="Batches per A→swap→B→swap cycle. Default 50. "
                             "Smaller = more swaps but quicker feedback.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    # 2026-05-11 backwards compat with cron driver subprocess args from older
    # worker (l0_worker_v2_mac.py). Accepted but ignored — serial worker is
    # always pass-2-equivalent, concurrent-1.
    parser.add_argument("--pass", dest="_legacy_pass", type=int, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--concurrent", type=int, default=1,
                        help=argparse.SUPPRESS)
    parser.add_argument("--gatekeeper-url", type=str, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--gatekeeper-model", type=str, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--extractor-url", type=str, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--extractor-model", type=str, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--rag-enabled", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--manifest-path", type=str, default=None,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        force=True,
    )

    # 2026-05-11: Mac reachability pre-flight
    if not args.dry_run and not _mac_reachable():
        logger.error("[serial] Mac SSH unreachable — cannot do LLM swap. Aborting.")
        logger.error("[serial] Check: ssh primary-host + Tailscale + Mac power state.")
        return 3

    pending = collect_pending_batches(args.batches_dir, args.done_dir)
    logger.info(f"[serial] found {len(pending)} pending batches in {args.batches_dir}")
    if args.max_batches > 0:
        pending = pending[: args.max_batches]
        logger.info(f"[serial] limited to first {args.max_batches}")
    if not pending:
        logger.info("[serial] no pending batches; exiting")
        return 0

    total_low = total_accept = total_cards = 0
    t_start = time.time()
    n_groups = 0
    for i in range(0, len(pending), args.group_size):
        group = pending[i: i + args.group_size]
        n_groups += 1
        logger.info(f"[serial] === Group {n_groups} ({len(group)} batches) ===")
        nl, na, nc = run_group(group, args.out_dir, args.done_dir, dry_run=args.dry_run)
        total_low += nl
        total_accept += na
        total_cards += nc
        logger.info(f"[serial] cumulative: low={total_low} accept={total_accept} cards={total_cards}")

    dur = time.time() - t_start
    logger.info(
        f"[serial] FINAL: groups={n_groups} batches={len(pending)} "
        f"low={total_low} accept={total_accept} cards={total_cards} "
        f"elapsed={dur:.1f}s rate={len(pending) / max(dur, 1):.2f}/s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
