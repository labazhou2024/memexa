"""LIVE observer for Pass-2 cards as they're produced.

Tail data/l0_v5/work/cards_v2/ for new files. For each new card output:
- Print full meta (gatekeeper verdict, extractor model, prompt sha)
- Print full per-card content (narrative, entities, types, identity_assertions,
  time_resolutions, relation_assertions, unresolved_references)
- Mark first N files with extra detail

Used per CEO directive 2026-05-06: 详细完整的盯着 Gemma 思考过程.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Set


def dump_card_file(path: Path, idx: int, verbose: bool = True) -> None:
    try:
        d = json.load(open(path, "r", encoding="utf-8"))
    except Exception as e:
        print(f"[{idx}] {path.name}: read fail {e}")
        return

    meta = d.get("meta", {})
    cards = d.get("cards", [])

    print(f"\n{'═' * 80}")
    print(f"📦 [Pass-2 #{idx}] {path.name}")
    print(f"{'═' * 80}")
    print(f"  batch_id:       {meta.get('batch_id')}")
    print(f"  chat_room:      {meta.get('chat_room')}")
    print(f"  room_hash:      {meta.get('room_hash','?')[:16]}...")
    print(f"  verdict:        {meta.get('verdict')}")
    print(f"  gatekeeper:     {meta.get('gatekeeper_model')}")
    print(f"  extractor:      {meta.get('extractor_model')}")
    print(f"  prompt_sha:     {meta.get('extraction_prompt_sha')}")
    print(f"  n_cards:        {len(cards)} (n_invalid_dropped={meta.get('n_invalid_dropped',0)})")

    if not cards:
        print(f"  ⚠️ 0 cards extracted (gate verdict={meta.get('verdict')})")
        return

    for ci, c in enumerate(cards):
        print(f"\n  ┌─ card #{ci + 1}/{len(cards)} ─────────────────────────────────")
        print(f"  │ when:    {c.get('when_start')} ~ {c.get('when_end')}")
        print(f"  │ types:   {c.get('types')}")
        print(f"  │ salience:{c.get('salience')} ({c.get('salience_reason','')})")
        print(f"  │ speaker: {c.get('speaker_role')}")
        print(f"  │ attest:  {c.get('attestation_tier')}")
        narrative = c.get("narrative", "")
        if verbose:
            print(f"  │ narrative ({len(narrative)} chars):")
            for line in narrative.split("\n"):
                print(f"  │   {line}")
        else:
            print(f"  │ narrative head: {narrative[:200]}")

        ents = c.get("entities", [])
        print(f"  │ entities ({len(ents)}):")
        for e in ents[:5]:
            print(f"  │   - {e.get('canonical_name')!r} (id={e.get('canonical_id')}) "
                  f"role={e.get('role_in_card')} surface={e.get('surface_form')!r} "
                  f"conf={e.get('resolution_confidence')}")
        if len(ents) > 5:
            print(f"  │   ... +{len(ents) - 5} more")

        eq = c.get("evidence_quotes", [])
        print(f"  │ evidence_quotes ({len(eq)}):")
        for q in eq:
            print(f"  │   ← {q[:120]}")

        ias = c.get("identity_assertions", [])
        if ias:
            print(f"  │ ★ identity_assertions ({len(ias)}):")
            for a in ias:
                print(f"  │   • {a.get('asserted_relation')} → {a.get('asserted_value')!r} "
                      f"(conf={a.get('confidence')}, quote={a.get('quote','')[:60]!r})")

        trs = c.get("time_resolutions", [])
        if trs:
            print(f"  │ ★ time_resolutions ({len(trs)}):")
            for t in trs:
                print(f"  │   • {t.get('surface_form')!r} → "
                      f"{t.get('resolved_start')}..{t.get('resolved_end')} "
                      f"(method={t.get('resolution_method')}, conf={t.get('confidence')})")

        ras = c.get("relation_assertions", [])
        if ras:
            print(f"  │ ★ relation_assertions ({len(ras)}):")
            for r in ras:
                print(f"  │   • {r.get('person_a')!r} ↔ {r.get('person_b')!r} "
                      f"({r.get('relation_type')}, ctx={r.get('context','')[:40]!r})")

        unrs = c.get("unresolved_references", [])
        if unrs:
            print(f"  │ ⚠ unresolved_references: {unrs}")

        print(f"  └────────────────────────────────────────────────────────────────")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cards-dir", type=Path,
                        default=Path("data/l0_v5/work/cards_v2"))
    parser.add_argument("--max-detail", type=int, default=10,
                        help="Show full detail for first N files")
    parser.add_argument("--poll", type=float, default=3.0,
                        help="Polling interval (s)")
    parser.add_argument("--exit-on-final", action="store_true",
                        help="Exit when run_500.log shows FINAL")
    args = parser.parse_args(argv)

    seen: Set[str] = set()
    detailed_count = 0
    args.cards_dir.mkdir(parents=True, exist_ok=True)
    print(f"📡 watching {args.cards_dir} (poll={args.poll}s, max_detail={args.max_detail})\n")

    while True:
        try:
            files = sorted(args.cards_dir.glob("*.json"))
            new_ones = [f for f in files if f.name not in seen]
            for f in new_ones:
                seen.add(f.name)
                detailed = detailed_count < args.max_detail
                dump_card_file(f, idx=len(seen), verbose=detailed)
                if detailed:
                    detailed_count += 1
            if args.exit_on_final:
                logp = args.cards_dir.parent / "run_500.log"
                if logp.exists():
                    tail = logp.read_text(encoding="utf-8", errors="ignore")[-1500:]
                    if "FINAL: all_ok" in tail:
                        print("\n📡 watcher: FINAL detected, exiting")
                        return 0
            time.sleep(args.poll)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
