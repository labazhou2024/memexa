"""
Phase-split V5 worker — decouples gatekeeper (Stage A) from extractor (Stage B).

Why this exists
---------------
The original `l0_worker_v2_ustc.py` interleaves Stage A and Stage B per batch:
    for batch in batches:
        verdict = Qwen.judge(batch)
        if verdict != LOW:
            cards = Gemma.extract(batch)
        write(batch_id.json)

That couples a fast judge (Qwen-14B) to a slow extractor (Gemma-31B). When
Gemma is bottlenecked by other GPU workloads, Qwen idles on its dedicated
GPU, holding ~43 GB of pre-allocated KV cache while doing nothing.

Phase-split worker decouples them:
  1. Run --phase gate over all pending batches using only Qwen.
     Each batch becomes a tiny `<gate-dir>/<batch_id>.json` (~120 B).
  2. Shut down Qwen vllm (frees the gatekeeper GPU — colleague processes
     on the same GPU stay untouched).
  3. Run --phase extract over the gate results at Gemma's pace, sharing
     the GPUs with other compute jobs.

Usage on your-org
-------------
Phase A:
    python phase_split_worker.py --phase gate \
        --batches-dir /tmp/memex_email_browser/data/input_batches_email \
        --cards-dir   /tmp/memex_email_browser/data/cards_email \
        --gate-dir    /tmp/memex_email_browser/data/gate_email \
        --gatekeeper-url   http://127.0.0.1:8316 \
        --gatekeeper-model Qwen/Qwen3-14B-AWQ \
        --concurrent 8

Phase B (one process per Gemma vllm):
    python phase_split_worker.py --phase extract \
        --batches-dir /tmp/memex_email_browser/data/input_batches_email \
        --cards-dir   /tmp/memex_email_browser/data/cards_email \
        --gate-dir    /tmp/memex_email_browser/data/gate_email \
        --extractor-url   http://127.0.0.1:8313 \
        --extractor-model mlx-community/gemma-4-31b-it-4bit \
        --concurrent 2 --shard 0/3
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Imports from the existing worker + prompt module. Both live next to us
# on your-org at /tmp/memex_l0_cc_full/code/.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from l0_worker_v2_ustc import (  # noqa: E402
    VllmClient,
    gatekeeper_user_prompt,
    parse_gatekeeper_verdict,
    GATEKEEPER_SYSTEM_PROMPT,
)
from pass2_prompt import (  # noqa: E402
    PASS2_SYSTEM_PROMPT,
    build_pass2_user_prompt,
    compute_pass2_prompt_sha,
    parse_pass2_output,
    validate_card_dict,
)

logger = logging.getLogger("phase_split")


# ─── Collection ───────────────────────────────────────────────────────────
def list_batches(batches_dir: Path) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    for p in batches_dir.rglob("prompt.json"):
        out.append((p.parent.name, p))
    return sorted(out, key=lambda x: x[0])


def needs_gate(batch_id: str, gate_dir: Path, cards_dir: Path) -> bool:
    if (cards_dir / f"{batch_id}.json").exists():
        return False
    if (gate_dir / f"{batch_id}.json").exists():
        return False
    return True


def needs_extract(batch_id: str, cards_dir: Path) -> bool:
    return not (cards_dir / f"{batch_id}.json").exists()


def assign_shard(batch_id: str, shard_idx: int, n_shards: int) -> bool:
    if n_shards <= 1:
        return True
    return (hash(batch_id) % n_shards) == shard_idx


# ─── Phase A: gate-only ───────────────────────────────────────────────────
def gate_one(
    batch_id: str,
    prompt_path: Path,
    gate_dir: Path,
    gatekeeper: VllmClient,
) -> Tuple[str, bool, str]:
    try:
        prompt = json.loads(prompt_path.read_text(encoding="utf-8"))
        messages = prompt.get("messages", [])
        if not messages:
            verdict = "LOW"
        else:
            gk_user = gatekeeper_user_prompt(messages)
            content, _ = gatekeeper.chat(
                system=GATEKEEPER_SYSTEM_PROMPT,
                user=gk_user,
                max_tokens=128,
                temperature=0.0,
            )
            verdict, _ = parse_gatekeeper_verdict(content)
        rec = {
            "batch_id": batch_id,
            "verdict": verdict,
            "gatekeeper_model": gatekeeper.model_name,
            "ts": time.time(),
        }
        gate_dir.mkdir(parents=True, exist_ok=True)
        (gate_dir / f"{batch_id}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8"
        )
        return batch_id, True, verdict
    except Exception as exc:
        return batch_id, False, f"{type(exc).__name__}: {exc}"


def run_phase_gate(args: argparse.Namespace) -> int:
    batches_dir = Path(args.batches_dir)
    cards_dir = Path(args.cards_dir)
    gate_dir = Path(args.gate_dir)

    gatekeeper = VllmClient(args.gatekeeper_url, args.gatekeeper_model)
    if not gatekeeper.health():
        logger.error(f"gatekeeper not healthy at {args.gatekeeper_url}")
        return 3

    all_batches = list_batches(batches_dir)
    pending = [(bid, p) for bid, p in all_batches if needs_gate(bid, gate_dir, cards_dir)]
    cards_done = sum(1 for _ in cards_dir.glob("*.json")) if cards_dir.exists() else 0
    gated_done = sum(1 for _ in gate_dir.glob("*.json")) if gate_dir.exists() else 0
    logger.info(
        f"Phase A gate over {batches_dir.name}: pending={len(pending)} "
        f"total_batches={len(all_batches)} cards_done={cards_done} gated_done={gated_done}"
    )

    t0 = time.time()
    done = 0
    fail = 0
    counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "OTHER": 0}
    with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
        futures = [
            ex.submit(gate_one, bid, p, gate_dir, gatekeeper)
            for bid, p in pending
        ]
        for i, fut in enumerate(as_completed(futures), 1):
            bid, ok, verdict_or_err = fut.result()
            if ok:
                done += 1
                key = verdict_or_err if verdict_or_err in counts else "OTHER"
                counts[key] += 1
            else:
                fail += 1
                logger.warning(f"  gate fail {bid}: {verdict_or_err}")
            if i % 50 == 0 or i == len(pending):
                dt = time.time() - t0
                rate = done / dt if dt > 0 else 0
                logger.info(
                    f"PROGRESS gate: {i}/{len(pending)} done={done} fail={fail} "
                    f"L/M/H/?={counts['LOW']}/{counts['MEDIUM']}/{counts['HIGH']}/{counts['OTHER']} "
                    f"rate={rate:.2f}/s elapsed={int(dt)}s"
                )
    return 0


# ─── Phase B: extract-only ────────────────────────────────────────────────
def extract_one(
    batch_id: str,
    prompt_path: Path,
    gate_path: Path,
    cards_dir: Path,
    extractor: VllmClient,
) -> Tuple[str, bool, int]:
    try:
        prompt_data = json.loads(prompt_path.read_text(encoding="utf-8"))
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        verdict = gate.get("verdict", "MEDIUM")
        messages = prompt_data.get("messages", [])
        cards_dir.mkdir(parents=True, exist_ok=True)
        out_path = cards_dir / f"{batch_id}.json"

        if verdict == "LOW":
            out_path.write_text(json.dumps({
                "meta": {
                    "batch_id": batch_id,
                    "skipped_by_gatekeeper": True,
                    "verdict": "LOW",
                    "gatekeeper_model": gate.get("gatekeeper_model"),
                    "ts": time.time(),
                },
                "cards": [],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            return batch_id, True, 0

        user_prompt = build_pass2_user_prompt(
            batch_id=batch_id,
            chat_room=prompt_data.get("chat_room", ""),
            room_hash=prompt_data.get("room_hash", ""),
            batch_window_local=prompt_data.get("batch_window_local", ""),
            sender_list=prompt_data.get("sender_list", []),
            manifest_slice=prompt_data.get("manifest_slice", {}),
            messages=messages,
            chinese_calendar_window=prompt_data.get("chinese_calendar_window"),
            user_calendar_window=prompt_data.get("user_calendar_window"),
        )
        prompt_sha = compute_pass2_prompt_sha(PASS2_SYSTEM_PROMPT, user_prompt)
        content, _ = extractor.chat(
            system=PASS2_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=8192,
            temperature=0.2,
        )
        try:
            cards = parse_pass2_output(content)
        except Exception:
            cards = []
        for c in cards:
            c["batch_id"] = batch_id
            c["extraction_prompt_sha"] = prompt_sha
            c["schema_v"] = 2
            c.setdefault("attestation_tier", "paired_v2")
            c.setdefault("source", prompt_data.get("source_kind", "wechat"))
        valid: List[Dict] = []
        invalid_count = 0
        for c in cards:
            if validate_card_dict(c):
                invalid_count += 1
            else:
                valid.append(c)
        out_path.write_text(json.dumps({
            "meta": {
                "batch_id": batch_id,
                "chat_room": prompt_data.get("chat_room", ""),
                "room_hash": prompt_data.get("room_hash", ""),
                "verdict": verdict,
                "extraction_prompt_sha": prompt_sha,
                "gatekeeper_model": gate.get("gatekeeper_model"),
                "extractor_model": extractor.model_name,
                "n_invalid_dropped": invalid_count,
                "ts": time.time(),
            },
            "cards": valid,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return batch_id, True, len(valid)
    except Exception as exc:
        return batch_id, False, 0


def run_phase_extract(args: argparse.Namespace) -> int:
    batches_dir = Path(args.batches_dir)
    cards_dir = Path(args.cards_dir)
    gate_dir = Path(args.gate_dir)

    extractor = VllmClient(args.extractor_url, args.extractor_model)
    if not extractor.health():
        logger.error(f"extractor not healthy at {args.extractor_url}")
        return 3

    shard_idx, n_shards = 0, 1
    if args.shard:
        a, b = args.shard.split("/")
        shard_idx, n_shards = int(a), int(b)

    all_batches = list_batches(batches_dir)
    pending: List[Tuple[str, Path, Path]] = []
    for bid, p in all_batches:
        gate_path = gate_dir / f"{bid}.json"
        if not gate_path.exists():
            continue
        if not needs_extract(bid, cards_dir):
            continue
        if not assign_shard(bid, shard_idx, n_shards):
            continue
        pending.append((bid, p, gate_path))

    total_gated = sum(1 for _ in gate_dir.glob("*.json")) if gate_dir.exists() else 0
    total_cards = sum(1 for _ in cards_dir.glob("*.json")) if cards_dir.exists() else 0
    logger.info(
        f"Phase B extract shard {shard_idx}/{n_shards} over {cards_dir.name}: "
        f"pending={len(pending)} total_gated={total_gated} cards_done={total_cards}"
    )

    t0 = time.time()
    done = 0
    fail = 0
    cards_total = 0
    with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
        futures = [
            ex.submit(extract_one, bid, pp, gp, cards_dir, extractor)
            for bid, pp, gp in pending
        ]
        for i, fut in enumerate(as_completed(futures), 1):
            bid, ok, n_cards = fut.result()
            if ok:
                done += 1
                cards_total += n_cards
            else:
                fail += 1
            if i % 25 == 0 or i == len(pending):
                dt = time.time() - t0
                rate = done / dt if dt > 0 else 0
                logger.info(
                    f"PROGRESS extract shard {shard_idx}: {i}/{len(pending)} "
                    f"done={done} fail={fail} cards={cards_total} "
                    f"rate={rate:.3f}/s elapsed={int(dt)}s"
                )
    return 0


# ─── CLI ──────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=["gate", "extract"], required=True)
    p.add_argument("--batches-dir", required=True)
    p.add_argument("--cards-dir", required=True)
    p.add_argument("--gate-dir", required=True)
    p.add_argument("--gatekeeper-url", default=None)
    p.add_argument("--gatekeeper-model", default="Qwen/Qwen3-14B-AWQ")
    p.add_argument("--extractor-url", default=None)
    p.add_argument("--extractor-model", default="mlx-community/gemma-4-31b-it-4bit")
    p.add_argument("--concurrent", type=int, default=4)
    p.add_argument("--shard", default=None, help="e.g. 0/3 to take 1/3 of batches")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    if args.phase == "gate":
        if not args.gatekeeper_url:
            logger.error("--gatekeeper-url required for --phase gate")
            return 2
        return run_phase_gate(args)
    else:
        if not args.extractor_url:
            logger.error("--extractor-url required for --phase extract")
            return 2
        return run_phase_extract(args)


if __name__ == "__main__":
    sys.exit(main())
