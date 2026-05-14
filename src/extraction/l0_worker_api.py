"""L0 v5 worker — Phase A/B via your-org LLM API.

Replaces local Mac dual-LLM (l0_worker_serial) with your-org API calls:
  - Gatekeeper: qwen3.6-chat (Phase A LOW/MEDIUM/HIGH verdict)
  - Extractor:  deepseek-v4-flash-ascend (Phase B V2 envelope cards)

Mirrors l0_worker_serial CLI contract:
  --batches-dir   input dir with <date>/<bid>/prompt.json layout
  --done-dir      where <bid>.done marker files go
  --out-dir       where <bid>.json card output goes
  --max-batches   cap per run (0 = unlimited)
  --dry-run, --verbose

Differs: NO local model swap (your-org routes per-model). Single concurrent
worker per process (your-org platform 1-concurrent limit). Inherits 10s pacing
from UstcLLMClient global lock.

Throughput baseline (from BENCHMARK_REPORT.md):
  ~22-26s per Phase-B-size batch (gatekeeper + extractor combined)
  ~7× faster than Mac on-demand Gemma-31B (~195s/batch)

ASSERT (HARD RULE 2026-05-11): gatekeeper_model != extractor_model.
Both models read from env; assertion at startup.

Source dispatching: same as l0_worker_serial — driver picks input/output dirs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO))

from pass2_prompt import (  # noqa: E402
    PASS2_SYSTEM_PROMPT,
    build_pass2_user_prompt,
    compute_pass2_prompt_sha,
    parse_pass2_output,
)
# Source-aware additions (2026-05-12 v2). Gracefully fall back if running an
# older pass2_prompt.py that lacks these symbols.
try:  # noqa: E402
    from pass2_prompt import normalize_source, get_pass2_system_prompt
    _SOURCE_AWARE = True
except ImportError:
    _SOURCE_AWARE = False
    def normalize_source(prompt_data):  # type: ignore[no-redef]
        return prompt_data.get("source") or prompt_data.get("source_kind") or "wechat"
    def get_pass2_system_prompt(source):  # type: ignore[no-redef]
        return PASS2_SYSTEM_PROMPT
try:
    from l0_worker_v2_ustc import (  # noqa: E402
        GATEKEEPER_SYSTEM_PROMPT, gatekeeper_user_prompt,
        parse_gatekeeper_verdict,
    )
except Exception:
    GATEKEEPER_SYSTEM_PROMPT = None
    gatekeeper_user_prompt = None
    parse_gatekeeper_verdict = None

from src.extraction.ustc_llm_client import (  # noqa: E402
    UstcLLMClient, get_client,
)

logger = logging.getLogger("l0_worker_api")

GATEKEEPER_MODEL = os.environ.get("MEMEX_your-org_GATEKEEPER_MODEL", "qwen3.6-chat")
EXTRACTOR_MODEL = os.environ.get("MEMEX_your-org_EXTRACTOR_MODEL",
                                  "deepseek-v4-flash-ascend")


def collect_pending(batches_dir: Path, done_dir: Path) -> List[Path]:
    """Find all prompt.json under batches_dir/date/<bid>/, skip those with .done."""
    if not batches_dir.exists():
        return []
    out: List[Path] = []
    for date_dir in sorted(batches_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for bd in sorted(date_dir.iterdir()):
            pj = bd / "prompt.json"
            if not pj.exists():
                continue
            bid = bd.name
            if (done_dir / f"{bid}.done").exists():
                continue
            out.append(pj)
    return out


def _write_done(bid: str, done_dir: Path):
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / f"{bid}.done").write_text(str(time.time()), encoding="utf-8")


def _write_out(bid: str, out_dir: Path, payload: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{bid}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def process_one(
    client: UstcLLMClient,
    prompt_path: Path,
    out_dir: Path,
    done_dir: Path,
    dry_run: bool = False,
    verbose: bool = False,
) -> Tuple[str, int]:
    """Process one batch. Returns (verdict, n_cards)."""
    bid = prompt_path.parent.name
    try:
        prompt_data = json.loads(prompt_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"  {bid}: prompt parse fail {exc}")
        return ("FAIL", 0)

    # Slim manifest_slice — v5 builders inject the full 665-entry manifest
    # (~420 KB / 100k+ tokens). Empty-strip (prior version) caused HIGH-verdict
    # 96% zero-card rate (2026-05-12 e2e validation): extractor lost ground
    # truth for entity resolution, prompt rule #10 ("不知道→留空") triggered
    # empty cards output. Slim to: (a) all senders' canonical persons,
    # (b) public_figures (small, always kept), (c) up to 30 other persons whose
    # primary_name/aka surfaces appear in messages. Cap ~5k tokens.
    if "manifest_slice" in prompt_data:
        ms = prompt_data["manifest_slice"]
        msgs_text = "\n".join((m.get("content") or "") for m in prompt_data.get("messages", []))
        sender_hashes = {s.get("wxid_hash") for s in prompt_data.get("sender_list", [])}
        def _person_wxids(p: dict) -> list:
            return p.get("identifiers", {}).get("wxid_hashes", []) or p.get("wxid_hashes", []) or []

        def _person_surfaces(p: dict) -> list:
            out = [p.get("primary_name", "")]
            for a in (p.get("aka") or []):
                if isinstance(a, dict):
                    if a.get("surface"):
                        out.append(a["surface"])
                elif isinstance(a, str):
                    out.append(a)
            return [s for s in out if s]

        slim_persons = {}
        # (a) sender-linked persons (incl. self user)
        for cid, p in ms.get("persons", {}).items():
            if set(_person_wxids(p)) & sender_hashes or p.get("is_self"):
                slim_persons[cid] = p
        # (b) mentioned in messages by primary_name / aka surface
        mention_quota = 30 - len(slim_persons)
        for cid, p in ms.get("persons", {}).items():
            if cid in slim_persons or mention_quota <= 0:
                continue
            if any(s in msgs_text for s in _person_surfaces(p)):
                slim_persons[cid] = p
                mention_quota -= 1
        prompt_data["manifest_slice"] = {
            "persons": slim_persons,
            "organizations": {},
            "inanimate": {},
            "public_figures": ms.get("public_figures", {}),
        }

    msgs = prompt_data.get("messages", [])
    # Compute source early so both LOW-skip and full-extract paths can stamp it.
    source = normalize_source(prompt_data)
    if not msgs:
        if not dry_run:
            _write_out(bid, out_dir, {
                "meta": {"batch_id": bid, "skipped": "no_messages",
                         "source": source, "source_aware_prompt": _SOURCE_AWARE,
                         "ts": time.time()},
                "cards": [],
            })
            _write_done(bid, done_dir)
        return ("EMPTY", 0)

    # ── Phase A: Gatekeeper ────────────────────────────────────────────
    if gatekeeper_user_prompt is None:
        # No GK module loaded — assume MEDIUM (always extract)
        verdict = "MEDIUM"
        gk_usage = {}
    else:
        gk_user = gatekeeper_user_prompt(msgs)
        gk_r = client.gatekeeper(GATEKEEPER_SYSTEM_PROMPT, gk_user,
                                  timeout=60, label=f"gk_{bid[:8]}")
        if not gk_r["ok"]:
            logger.warning(f"  {bid}: gatekeeper fail ({gk_r.get('error')})")
            return ("FAIL", 0)
        verdict, _ = parse_gatekeeper_verdict(gk_r["content"])
        gk_usage = gk_r.get("usage", {})

    if verdict == "LOW":
        if not dry_run:
            _write_out(bid, out_dir, {
                "meta": {"batch_id": bid, "skipped_by_gatekeeper": True,
                         "source": source, "source_aware_prompt": _SOURCE_AWARE,
                         "verdict": verdict, "gatekeeper_model": GATEKEEPER_MODEL,
                         "gk_usage": gk_usage, "ts": time.time()},
                "cards": [],
            })
            _write_done(bid, done_dir)
        if verbose:
            logger.info(f"  {bid}: LOW (skip extract)")
        return (verdict, 0)

    # ── Phase B: Extractor ─────────────────────────────────────────────
    # Source-aware: route to per-source system+user prompt template.
    system_prompt = get_pass2_system_prompt(source)
    if _SOURCE_AWARE:
        user_prompt = build_pass2_user_prompt(
            batch_id=bid,
            chat_room=prompt_data.get("chat_room", ""),
            room_hash=prompt_data.get("room_hash", ""),
            batch_window_local=prompt_data.get("batch_window_local", ""),
            sender_list=prompt_data.get("sender_list", []),
            manifest_slice=prompt_data.get("manifest_slice", {}),
            messages=msgs,
            chinese_calendar_window=prompt_data.get("chinese_calendar_window"),
            user_calendar_window=prompt_data.get("user_calendar_window"),
            source=source,
            cwd=prompt_data.get("cwd"),
            room_tier=prompt_data.get("room_tier") or prompt_data.get("room_tier_hint"),
            session_id=prompt_data.get("session_id"),
            known_speakers=prompt_data.get("known_speakers"),
            passive_listener_session=prompt_data.get("passive_listener_session", False),
        )
    else:
        user_prompt = build_pass2_user_prompt(
            batch_id=bid,
            chat_room=prompt_data.get("chat_room", ""),
            room_hash=prompt_data.get("room_hash", ""),
            batch_window_local=prompt_data.get("batch_window_local", ""),
            sender_list=prompt_data.get("sender_list", []),
            manifest_slice=prompt_data.get("manifest_slice", {}),
            messages=msgs,
        )
    prompt_sha = compute_pass2_prompt_sha(system_prompt, user_prompt)

    if dry_run:
        logger.info(f"  {bid}: [DRY] verdict={verdict} source={source} prompt_chars={len(user_prompt)}")
        return (verdict, 0)

    ext_r = client.extractor(system_prompt, user_prompt,
                              timeout=240, label=f"ext_{bid[:8]}",
                              max_tokens=16384)
    if not ext_r["ok"]:
        logger.warning(f"  {bid}: extractor fail ({ext_r.get('error')}); cards=0")
        cards: list = []
        ext_usage = {}
    else:
        try:
            cards = parse_pass2_output(ext_r["content"])
            # Sanitize non-ASCII canonical_id (qwen3.6-reasoner sometimes outputs
            # `person_胡老师` instead of `person_v5synth_<hex>`; PG SQL_ASCII
            # rejects). Per prompt rule 10 ("不知道→留空"), null is safe;
            # hindsight identity_resolver re-binds canonical: tags downstream.
            for card in cards:
                for ent in card.get("entities", []) or []:
                    cid = ent.get("canonical_id")
                    if cid:
                        try:
                            cid.encode("ascii")
                        except (UnicodeEncodeError, AttributeError):
                            ent["canonical_id"] = None
                            if ent.get("resolution_confidence") in (None, "certain", "inferred"):
                                ent["resolution_confidence"] = "ambiguous"
        except Exception as e:
            finish = ext_r.get("finish_reason")
            logger.warning(
                f"  {bid}: parse fail {e}; cards=0 finish={finish}"
            )
            cards = []
            try:
                dump_dir = out_dir.parent / "parse_fail"
                dump_dir.mkdir(parents=True, exist_ok=True)
                existing = sorted(dump_dir.glob("*.txt"),
                                  key=lambda p: p.stat().st_mtime)
                while len(existing) >= 100:
                    existing[0].unlink(missing_ok=True)
                    existing = existing[1:]
                dump_path = dump_dir / f"{bid}.txt"
                hdr = (
                    f"# finish={finish} comp_tokens="
                    f"{ext_r.get('usage', {}).get('completion_tokens', '?')}\n"
                )
                dump_path.write_text(
                    hdr + (ext_r.get("content") or "")[:8192],
                    encoding="utf-8",
                )
            except Exception:
                pass
        ext_usage = ext_r.get("usage", {})

    # Stamp metadata on every card (overrides any LLM placeholder echo).
    for c in cards:
        c["batch_id"] = bid
        c["extraction_prompt_sha"] = prompt_sha
        c["source"] = source
        c["schema_v"] = 2
        c.setdefault("attestation_tier", "paired_v2")
    out_record = {
        "meta": {
            "batch_id": bid,
            "chat_room": prompt_data.get("chat_room", ""),
            "room_hash": prompt_data.get("room_hash", ""),
            "source": source,
            "source_aware_prompt": _SOURCE_AWARE,
            "verdict": verdict,
            "extraction_prompt_sha": prompt_sha,
            "gatekeeper_model": GATEKEEPER_MODEL,
            "extractor_model": EXTRACTOR_MODEL,
            "gk_usage": gk_usage,
            "ext_usage": ext_usage,
            "ts": time.time(),
        },
        "cards": cards,
    }
    _write_out(bid, out_dir, out_record)
    _write_done(bid, done_dir)
    if verbose:
        logger.info(f"  {bid}: verdict={verdict} cards={len(cards)} "
                    f"ext_lat={ext_r.get('latency_s','?')}s")
    return (verdict, len(cards))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batches-dir", type=Path, required=True)
    parser.add_argument("--done-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-batches", type=int, default=0,
                        help="0 = unlimited; cron should pass small value e.g. 5")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    # Accept (and ignore) flags from l0_worker_serial for CLI compat:
    parser.add_argument("--pass", dest="_legacy_pass", type=int, default=None)
    parser.add_argument("--concurrent", type=int, default=1,
                        help="Hard-pinned to 1 by your-org API rate-limit")
    parser.add_argument("--group-size", type=int, default=0,
                        help="Ignored (no model swap with API)")
    args = parser.parse_args(argv)

    # ── HARD RULE assertion ─────────────────────────────────────
    if GATEKEEPER_MODEL == EXTRACTOR_MODEL:
        sys.stderr.write(
            f"[l0_worker_api] FATAL: gatekeeper_model == extractor_model "
            f"({GATEKEEPER_MODEL}). Dual-LLM safety net collapsed. Aborting.\n"
        )
        return 4

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 2026-05-13 LIVE re-verify: your-org API c=4 all-ok (data/ustc_llm_verify/live_2026_05_13/).
    # Old 1-concurrent hard pin removed. Default still 1 for safety; cron drivers
    # pass --concurrent 3 to unlock parallel.
    if args.concurrent > 1:
        logger.info(f"  --concurrent={args.concurrent} parallel mode "
                    f"(live-verified 2026-05-13 platform supports c=4)")

    pending = collect_pending(args.batches_dir, args.done_dir)
    if args.max_batches > 0:
        pending = pending[:args.max_batches]
    logger.info(f"l0_worker_api: pending={len(pending)} "
                 f"batches_dir={args.batches_dir} "
                 f"out_dir={args.out_dir} done_dir={args.done_dir} "
                 f"gk={GATEKEEPER_MODEL} ext={EXTRACTOR_MODEL} "
                 f"concurrent={args.concurrent} dry_run={args.dry_run}")

    if not pending:
        return 0

    client = get_client()
    t0 = time.time()
    counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "EMPTY": 0, "FAIL": 0}
    total_cards = 0

    def _run_one(pj):
        return process_one(client, pj, args.out_dir, args.done_dir,
                           dry_run=args.dry_run, verbose=args.verbose)

    if args.concurrent > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        completed = 0
        with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
            fut_to_pj = {ex.submit(_run_one, pj): pj for pj in pending}
            for fut in as_completed(fut_to_pj):
                try:
                    verdict, n = fut.result()
                except Exception as exc:
                    verdict, n = "FAIL", 0
                    logger.warning(f"  parallel task failed: {exc}")
                counts[verdict if verdict in counts else "FAIL"] = counts.get(verdict, 0) + 1
                total_cards += n
                completed += 1
                if completed % 5 == 0 or completed == len(pending):
                    dt = time.time() - t0
                    rate = completed / dt if dt else 0
                    logger.info(
                        f"PROGRESS: {completed}/{len(pending)} "
                        f"L/M/H/E/F={counts['LOW']}/{counts['MEDIUM']}/{counts['HIGH']}/"
                        f"{counts['EMPTY']}/{counts['FAIL']} "
                        f"cards={total_cards} rate={rate:.3f}/s elapsed={int(dt)}s"
                    )
    else:
        for i, pj in enumerate(pending):
            verdict, n = _run_one(pj)
            counts[verdict if verdict in counts else "FAIL"] = counts.get(verdict, 0) + 1
            total_cards += n
            if (i + 1) % 5 == 0 or i + 1 == len(pending):
                dt = time.time() - t0
                rate = (i + 1) / dt if dt else 0
                logger.info(
                    f"PROGRESS: {i + 1}/{len(pending)} "
                    f"L/M/H/E/F={counts['LOW']}/{counts['MEDIUM']}/{counts['HIGH']}/"
                    f"{counts['EMPTY']}/{counts['FAIL']} "
                    f"cards={total_cards} rate={rate:.3f}/s elapsed={int(dt)}s"
                )

    dt = time.time() - t0
    logger.info(
        f"DONE: {len(pending)} batches in {dt:.1f}s, total_cards={total_cards}, "
        f"L/M/H/E/F={counts['LOW']}/{counts['MEDIUM']}/{counts['HIGH']}/"
        f"{counts['EMPTY']}/{counts['FAIL']}"
    )
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
