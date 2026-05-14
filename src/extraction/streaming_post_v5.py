"""Streaming POST worker for L0 v5 cards → memory_full_v5.

Watches data/l0_v5/work/cards_v2/ for new card output files. As each
appears, parses cards, normalizes LLM-emitted JSON, validates, and
POSTs sync to Hindsight.

Per CEO directive 2026-05-06:
- 不等 Pass-2 全完, hindsight 入图随产随入
- 详细 dump 前 N 个 POST + 校验链路
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Set

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from src.core.memory_card_v2 import (
    Entity, IdentityAssertion, MemoryCard, RelationAssertion,
    TimeResolution,
)

# Reuse normalizer from the e2e pipeline (sibling module).
from src.extraction.run_e2e_pipeline import _normalize_llm_card  # noqa

logger = logging.getLogger("stream_post")


HINDSIGHT_URL = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
BANK_ID = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")


def post_card(card_dict: Dict[str, Any], client: httpx.Client, verbose: bool = False) -> Dict[str, Any]:
    """Build payload + POST sync. Returns dict {ok, status, error, latency_ms}.

    Phase 2.3 (2026-05-11): tightened ok-check to parse body's `success` field
    in addition to HTTP status. Old behaviour treated 200 as ok even when the
    server-side persistence failed (silent → `success:false` ghost). This is
    the root cause of the 1,853 wechat ghost markers (HANDOFF §D.14).
    """
    t0 = time.time()
    try:
        card_dict = _normalize_llm_card(card_dict)
        ents = [Entity(**e) for e in card_dict.get("entities", [])]
        ias = [IdentityAssertion(**a) for a in card_dict.get("identity_assertions", [])]
        trs = [TimeResolution(**t) for t in card_dict.get("time_resolutions", [])]
        ras = [RelationAssertion(**r) for r in card_dict.get("relation_assertions", [])]
        card_dict["entities"] = ents
        card_dict["identity_assertions"] = ias
        card_dict["time_resolutions"] = trs
        card_dict["relation_assertions"] = ras
        card = MemoryCard(**card_dict)
        payload = card.to_retain_payload()
        if verbose:
            logger.info(f"  payload tags ({len(payload['tags'])}): {payload['tags'][:8]}")
            logger.info(f"  payload metadata keys: {sorted(payload['metadata'].keys())}")
            logger.info(f"  payload content len: {len(payload['content'])} chars")
        r = client.post(
            f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/memories",
            json={"items": [payload], "async": False},
        )
        latency_ms = (time.time() - t0) * 1000
        if r.status_code in (200, 201, 202):
            # Phase 2.3: parse body. RetainResponse schema:
            #   {success, bank_id, items_count, async, operation_id}
            try:
                body = r.json()
            except Exception:
                body = {}
            body_ok = bool(body.get("success", False)) and \
                      int(body.get("items_count", 0)) >= 1
            if body_ok:
                return {"ok": True, "status": r.status_code, "latency_ms": latency_ms,
                        "card_id": card.card_id(), "items_count": body.get("items_count")}
            return {"ok": False, "status": r.status_code, "latency_ms": latency_ms,
                    "error": f"body_not_ok success={body.get('success')} "
                             f"items_count={body.get('items_count')}"}
        return {"ok": False, "status": r.status_code, "latency_ms": latency_ms,
                "error": r.text[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500],
                "latency_ms": (time.time() - t0) * 1000}


def verify_batch_in_pg(batch_id: str, client: httpx.Client,
                       min_count: int = 1, retries: int = 2,
                       sleep_between: float = 0.5) -> Dict[str, Any]:
    """Phase 2.3 batch-level POST-then-GET verify.

    After all cards of a file have POSTed ok, hit GET /memories/list?q=<batch_id>
    to confirm at least `min_count` cards landed in PG. This catches:
    - Server returned 200 + success:true but PG write rolled back transient
    - LLM consolidation/dedup silently dropped the record
    - Document-id dedup collapsed two batches into one (counts<expected)

    Why not per-card verify: would add 1 HTTP round-trip per card (5-10 cards/
    batch × 4738 batches = 30k extra calls). Per-batch verify is 1× per batch.

    On verify_fail → caller doesn't write marker, file re-queued for retry next
    round. retries handles eventual-consistency: hindsight may need ~500ms to
    index new card into the full-text shard.

    Returns: {ok: bool, total: int, batch_id: str, attempts: int, error: str}
    """
    url = f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/memories/list"
    last_total = 0
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(sleep_between)
        try:
            r = client.get(url, params={"q": batch_id, "limit": 1})
            if r.status_code != 200:
                continue
            body = r.json()
            last_total = int(body.get("total", 0))
            if last_total >= min_count:
                return {"ok": True, "total": last_total, "batch_id": batch_id,
                        "attempts": attempt + 1}
        except Exception as e:
            return {"ok": False, "total": last_total, "batch_id": batch_id,
                    "attempts": attempt + 1, "error": str(e)[:200]}
    return {"ok": False, "total": last_total, "batch_id": batch_id,
            "attempts": retries + 1,
            "error": f"verify_total={last_total} < min_count={min_count}"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards-dir", type=Path,
                        default=Path("data/l0_v5/work/cards_v2"))
    parser.add_argument("--posted-marker-dir", type=Path,
                        default=Path("data/l0_v5/work/posted_v5"))
    parser.add_argument("--max-detail", type=int, default=10,
                        help="Detailed log for first N POSTs")
    parser.add_argument("--poll", type=float, default=3.0)
    parser.add_argument("--max-iterations", type=int, default=2880,
                        help="Max iterations (default 2880 = 4h at 5s)")
    parser.add_argument("--exit-when-empty-rounds", type=int, default=20,
                        help="Exit after N consecutive idle polls (default 20 = 60s idle)")
    # Phase 2.3 (2026-05-11 v3) — POST-then-GET verify flags
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip POST-then-GET batch verify (legacy fast path; not recommended)")
    parser.add_argument("--verify-retries", type=int, default=2,
                        help="Verify retries on /memories/list (default 2 = 3 total attempts)")
    parser.add_argument("--verify-sleep", type=float, default=0.5,
                        help="Sleep between verify retries (default 0.5s; covers BGE index lag)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    args.cards_dir.mkdir(parents=True, exist_ok=True)
    args.posted_marker_dir.mkdir(parents=True, exist_ok=True)

    # Seen set = files already processed (marker exists)
    seen: Set[str] = set(
        p.stem for p in args.posted_marker_dir.glob("*.posted")
    )
    logger.info(f"streaming POST started: bank={BANK_ID} url={HINDSIGHT_URL}")
    logger.info(f"  cards dir: {args.cards_dir}")
    logger.info(f"  posted markers: {args.posted_marker_dir}")
    logger.info(f"  resuming with {len(seen)} already-posted markers")

    client = httpx.Client(timeout=120.0)
    n_posted_total = 0
    n_failed_total = 0
    n_cards_total = 0
    detailed_count = 0
    idle_polls = 0
    # 2026-05-11: per-file retry cap. Without this, when hindsight is transiently
    # broken and rejects a card, seen.discard(f.stem) re-queues it forever →
    # new_files never empties → idle_polls never increments → driver hangs.
    # After MAX_RETRIES, dead-letter the file (write .dead marker) and skip.
    file_retries: Dict[str, int] = {}
    MAX_RETRIES_PER_FILE = 3

    for it in range(args.max_iterations):
        files = sorted(args.cards_dir.glob("*.json"))
        new_files = [f for f in files if f.stem not in seen]
        # Treat "all remaining files are over-retried" as idle (won't produce
        # progress; let exit-when-empty-rounds eventually fire).
        retryable = [f for f in new_files
                     if file_retries.get(f.stem, 0) < MAX_RETRIES_PER_FILE]
        if not retryable:
            idle_polls += 1
            if idle_polls >= args.exit_when_empty_rounds:
                logger.info(f"\n📊 streaming POST done (idle {idle_polls} rounds)")
                logger.info(f"  total cards files seen: {n_cards_total}")
                logger.info(f"  total posted: {n_posted_total}")
                logger.info(f"  total failed: {n_failed_total}")
                logger.info(f"  dead-lettered: {sum(1 for n in file_retries.values() if n >= MAX_RETRIES_PER_FILE)}")
                break
            time.sleep(args.poll)
            continue
        idle_polls = 0
        new_files = retryable  # process only retryable files this round

        for f in new_files:
            seen.add(f.stem)
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"  read fail {f.name}: {e}")
                continue
            cards = d.get("cards", [])
            if not cards:
                # Mark as processed (skipped - 0 cards)
                (args.posted_marker_dir / f"{f.stem}.posted").write_text("0", encoding="utf-8")
                continue

            n_file_ok = 0
            n_file_fail = 0
            for ci, card_dict in enumerate(cards):
                n_cards_total += 1
                detailed = detailed_count < args.max_detail
                if detailed:
                    logger.info(f"\n{'═' * 80}")
                    logger.info(f"💾 [POST #{n_posted_total + n_failed_total + 1}] {f.name} card #{ci + 1}")
                result = post_card(card_dict, client, verbose=detailed)
                if result["ok"]:
                    n_posted_total += 1
                    n_file_ok += 1
                    if detailed:
                        logger.info(f"  ✓ POST OK: status={result['status']} "
                                    f"latency={result['latency_ms']:.0f}ms "
                                    f"card_id={result.get('card_id')}")
                        detailed_count += 1
                else:
                    n_failed_total += 1
                    n_file_fail += 1
                    logger.error(f"  ✗ POST FAIL: {result.get('status', '?')} "
                                 f"err={result.get('error', '?')[:200]}")
            # Mark file as processed only when every card in this file succeeded.
            # Bug fix 2026-05-11: previously this wrote the marker even after a
            # POST 500, causing batches that hit a transient TEI 500 to be
            # skipped permanently on retry. Now: any failure → no marker → retry
            # next streaming_post run picks it up.
            #
            # Phase 2.3 (2026-05-11 v3): POST-then-GET batch-level verify before
            # marker write. Even when all per-card POSTs returned 200+success:true,
            # call /memories/list?q=<batch_id> to confirm the cards are actually
            # query-able in PG. Catches the ghost-marker class: HTTP-success but
            # PG-rollback / consolidation-drop. If verify fails → no marker → retry.
            verify_ok = True
            if not args.skip_verify and n_file_fail == 0:
                v = verify_batch_in_pg(
                    f.stem, client,
                    min_count=1, retries=args.verify_retries,
                    sleep_between=args.verify_sleep,
                )
                verify_ok = bool(v.get("ok"))
                if not verify_ok:
                    n_file_fail += 1  # bump to trigger retry path below
                    logger.error(
                        f"  ✗ VERIFY FAIL {f.name}: total={v.get('total',0)} "
                        f"attempts={v.get('attempts','?')} "
                        f"err={v.get('error','?')[:100]}"
                    )
                elif n_posted_total <= 5:  # log first few verifies
                    logger.info(
                        f"  ✓ VERIFY OK {f.name}: total={v['total']} "
                        f"attempts={v['attempts']}"
                    )
            if n_file_fail == 0 and verify_ok:
                (args.posted_marker_dir / f"{f.stem}.posted").write_text(
                    f"{len(cards)}", encoding="utf-8"
                )
            else:
                file_retries[f.stem] = file_retries.get(f.stem, 0) + 1
                if file_retries[f.stem] >= MAX_RETRIES_PER_FILE:
                    # Dead-letter: write .dead marker + keep in seen[]; won't
                    # retry, won't block exit. Operator can inspect/remove.
                    (args.posted_marker_dir / f"{f.stem}.dead").write_text(
                        f"failed_after_{MAX_RETRIES_PER_FILE}_retries:{n_file_ok}/{len(cards)}",
                        encoding="utf-8",
                    )
                    logger.error(
                        f"  DEAD-LETTER {f.name} after {MAX_RETRIES_PER_FILE} "
                        f"retries: {n_file_ok}/{len(cards)} OK"
                    )
                else:
                    # Allow re-attempt: discard from seen[] so file reappears
                    seen.discard(f.stem)
                    logger.warning(
                        f"  retain {f.name} for retry {file_retries[f.stem]}/"
                        f"{MAX_RETRIES_PER_FILE}: {n_file_ok}/{len(cards)} OK"
                    )

        # Periodic summary
        if (it + 1) % 10 == 0:
            logger.info(
                f"[poll #{it + 1}] cards_seen={n_cards_total} "
                f"posted={n_posted_total} failed={n_failed_total}"
            )

        time.sleep(args.poll)

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
