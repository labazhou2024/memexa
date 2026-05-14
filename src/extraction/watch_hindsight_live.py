"""LIVE observer for Hindsight bank as cards are POSTed.

Polls bank stats + per-document detail. Per CEO directive 2026-05-06:
盯着进入 hindsight 处理的过程.

For each new doc: print full HEADER → MemoryCard rehydrate, then
list all auto-built links (semantic + temporal + entity).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Set

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.core.memory_card_v2 import MemoryCard

HINDSIGHT_URL = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")


def fetch_bank_stats(bank_id: str) -> Dict:
    r = httpx.get(f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/stats", timeout=10.0)
    r.raise_for_status()
    return r.json()


def fetch_memory_list(bank_id: str, limit: int = 200) -> list:
    r = httpx.get(
        f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/memories/list?limit={limit}",
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def fetch_memory_detail(bank_id: str, mem_id: str) -> Dict:
    r = httpx.get(
        f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/memories/{mem_id}",
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def fetch_graph(bank_id: str) -> Dict:
    r = httpx.get(f"{HINDSIGHT_URL}/v1/default/banks/{bank_id}/graph", timeout=15.0)
    if r.status_code != 200:
        return {}
    return r.json()


def dump_new_memory(mem: Dict, idx: int, bank_id: str) -> None:
    print(f"\n{'═' * 80}")
    print(f"💾 [Hindsight Insert #{idx}] mem_id={mem['id'][:12]}")
    print(f"{'═' * 80}")
    print(f"  fact_type:    {mem.get('fact_type')}")
    print(f"  doc_id:       {mem.get('document_id', mem.get('chunk_id', '?'))[:30]}")
    print(f"  date:         {mem.get('date')}")
    print(f"  occurred:     {mem.get('occurred_start')} .. {mem.get('occurred_end')}")
    print(f"  mentioned_at: {mem.get('mentioned_at')}")
    print(f"  context:      {mem.get('context','')[:100]}")
    print(f"  proof_count:  {mem.get('proof_count')}")
    print(f"  tags ({len(mem.get('tags',[]))}):")
    for t in mem.get("tags", [])[:10]:
        print(f"    • {t}")
    if len(mem.get("tags", [])) > 10:
        print(f"    ... +{len(mem['tags']) - 10} more")

    # rehydrate HEADER → Card
    text = mem.get("text", "")
    try:
        card = MemoryCard.from_retain_content(text)
        print(f"  ✓ HEADER rehydrate OK (schema_v={card.schema_v})")
        print(f"  narrative: {card.narrative[:300]}")
        if card.identity_assertions:
            print(f"  ★ identity_assertions ({len(card.identity_assertions)}):")
            for a in card.identity_assertions:
                print(f"    • {a.asserted_relation} → {a.asserted_value!r} "
                      f"(conf={a.confidence})")
        if card.time_resolutions:
            print(f"  ★ time_resolutions ({len(card.time_resolutions)}):")
            for t in card.time_resolutions:
                print(f"    • {t.surface_form!r} → "
                      f"{t.resolved_start}..{t.resolved_end} "
                      f"(method={t.resolution_method})")
        if card.relation_assertions:
            print(f"  ★ relation_assertions ({len(card.relation_assertions)}):")
            for r in card.relation_assertions:
                print(f"    • {r.person_a} ↔ {r.person_b} "
                      f"({r.relation_type}, ctx={r.context[:50]!r})")
        if card.unresolved_references:
            print(f"  ⚠ unresolved_references: {card.unresolved_references}")
    except Exception as e:
        print(f"  ✗ HEADER rehydrate failed: {e}")
        print(f"  raw text head: {text[:300]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bank-id", default="memory_full_v5")
    parser.add_argument("--max-detail", type=int, default=10,
                        help="Detailed dump for first N new memories")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--max-iterations", type=int, default=720,
                        help="Max poll iterations (default 720 = 1h at 5s)")
    args = parser.parse_args(argv)

    print(f"📡 watching Hindsight bank={args.bank_id} (poll={args.poll}s)\n")
    seen: Set[str] = set()

    last_stats = {}
    detailed_count = 0
    for it in range(args.max_iterations):
        try:
            stats = fetch_bank_stats(args.bank_id)
        except Exception as e:
            print(f"  stats fetch err: {e}")
            time.sleep(args.poll)
            continue

        # Diff stats
        if stats != last_stats:
            print(f"\n[{time.strftime('%H:%M:%S')}] Bank stats: "
                  f"nodes={stats.get('total_nodes')} "
                  f"links={stats.get('total_links')} "
                  f"docs={stats.get('total_documents')} "
                  f"pending={stats.get('pending_operations')}")
            ll = stats.get("links_by_link_type", {})
            if ll:
                print(f"  links by type: {ll}")
            last_stats = stats

        # Fetch new memories
        if stats.get("total_nodes", 0) > 0:
            try:
                mems = fetch_memory_list(args.bank_id, limit=200)
            except Exception as e:
                print(f"  list fetch err: {e}")
                continue
            new_mems = [m for m in mems if m["id"] not in seen]
            for m in new_mems:
                seen.add(m["id"])
                if detailed_count < args.max_detail:
                    dump_new_memory(m, idx=len(seen), bank_id=args.bank_id)
                    detailed_count += 1

        time.sleep(args.poll)

    return 0


if __name__ == "__main__":
    sys.exit(main())
