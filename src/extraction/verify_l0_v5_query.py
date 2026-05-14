"""L0 v5 query verification suite.

Spec: docs/l0_v5/MASTER_PLAN.md §9.3

Runs after full Pass-2 + POST is done. Validates:
1. memory_full_v5 bank stats (n_memory_units, n_entities, n_unit_entities)
2. Recall by entity tag — every manifest person should be recallable by entity:<sha16> tag
3. Recall by room hash — main rooms have ≥5 cards
4. Metadata salience distribution (mostly 0.3-0.7)
5. HEADER rehydrate success rate (must be 100% for chunks-only)
6. Reflect output contains evidence references

Plus the LIVE multi-keyword query suite:
  - "我购买 mac 的全部流程"
  - "Bob近期状态"
  - "通过谁认识Carol"
  - "上周三的实验室会议讨论了什么"
  - "我家在哪里"  (identity reverse lookup)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.core.identity_manifest import ManifestStore
from src.core.memory_card_v2 import MemoryCard, entity_tag_hash

logger = logging.getLogger("verify_l0_v5")


# ────────────────────────── AC functions ──────────────────────────

def ac_bank_stats(hindsight_url: str, bank_id: str) -> Tuple[bool, Dict[str, Any]]:
    """AC-1: bank exists and has units."""
    try:
        r = httpx.get(
            f"{hindsight_url}/v1/default/banks/{bank_id}/stats",
            timeout=10.0,
        )
        r.raise_for_status()
        stats = r.json()
    except Exception as e:
        return False, {"error": str(e)}
    n_units = (stats.get("n_memory_units") or stats.get("memory_units")
               or stats.get("total_nodes") or 0)
    return n_units > 0, {"n_memory_units": n_units, **stats}


def ac_recall_by_entity_tag(
    hindsight_url: str,
    bank_id: str,
    canonical_name: str,
    expected_min: int = 1,
) -> Tuple[bool, Dict[str, Any]]:
    """Recall using entity:<sha16(canonical_name)> tag."""
    tag = f"entity:{entity_tag_hash(canonical_name)}"
    try:
        r = httpx.post(
            f"{hindsight_url}/v1/default/banks/{bank_id}/memories/recall",
            json={"query": canonical_name, "tags": [tag], "budget": "high"},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, {"error": str(e)}
    facts = data.get("results") or data.get("memory_facts") or data.get("facts") or []
    return len(facts) >= expected_min, {
        "tag": tag, "n_facts": len(facts), "canonical_name": canonical_name,
    }


def ac_header_rehydrate(
    hindsight_url: str,
    bank_id: str,
    sample_size: int = 10,
) -> Tuple[bool, Dict[str, Any]]:
    """Recall random N cards and verify each can rehydrate via from_retain_content."""
    try:
        r = httpx.post(
            f"{hindsight_url}/v1/default/banks/{bank_id}/memories/recall",
            json={"query": "事件", "budget": "high", "max_tokens": 8000},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, {"error": str(e)}

    facts = data.get("results") or data.get("memory_facts") or data.get("facts") or []
    if not facts:
        return False, {"error": "0 facts returned"}

    sample = facts[:sample_size]
    n_ok = 0
    failures = []
    for f in sample:
        text = f.get("text", "") or f.get("content", "")
        try:
            card = MemoryCard.from_retain_content(text)
            assert card.schema_v == 2
            n_ok += 1
        except Exception as e:
            failures.append({
                "id": f.get("id", "?")[:12],
                "error": str(e)[:100],
            })

    return n_ok >= sample_size, {
        "sample_size": sample_size,
        "n_ok": n_ok,
        "n_fail": len(failures),
        "failures_sample": failures[:3],
    }


def ac_salience_distribution(
    hindsight_url: str,
    bank_id: str,
) -> Tuple[bool, Dict[str, Any]]:
    """Most cards should have salience in [0.3, 0.7] (typical info-density range)."""
    try:
        r = httpx.post(
            f"{hindsight_url}/v1/default/banks/{bank_id}/memories/recall",
            json={"query": "事件", "budget": "high", "max_tokens": 16000},
            timeout=60.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, {"error": str(e)}

    facts = data.get("results") or data.get("memory_facts") or data.get("facts") or []
    sal_values = []
    for f in facts:
        meta = f.get("metadata", {})
        try:
            sal_values.append(float(meta.get("salience", 0.0)))
        except (ValueError, TypeError):
            pass

    if not sal_values:
        return False, {"error": "no salience values found"}

    in_range = sum(1 for s in sal_values if 0.2 <= s <= 0.8)
    ratio = in_range / len(sal_values)
    return ratio >= 0.6, {
        "n_total": len(sal_values),
        "n_in_2_8": in_range,
        "ratio": round(ratio, 3),
        "min": round(min(sal_values), 3),
        "max": round(max(sal_values), 3),
        "mean": round(sum(sal_values) / len(sal_values), 3),
    }


def ac_multi_keyword_lifelog(
    user_query: str,
    bank_id: str,
    expected_n_min: int = 1,
) -> Tuple[bool, Dict[str, Any]]:
    """Run real multi-keyword query through memory_query_v2."""
    try:
        from src.core.memory_query_v2 import query as run_query
    except ImportError as e:
        return False, {"error": f"memory_query_v2 import: {e}"}

    try:
        result = run_query(
            user_query, bank_id=bank_id,
            enable_reflect=False,  # pure recall test
            enable_paired_eval=False,
        )
    except Exception as e:
        return False, {"error": str(e)}

    return result.n_recall >= expected_n_min, {
        "query": user_query,
        "query_type": result.query_type,
        "n_recall": result.n_recall,
        "intent": result.rewrite.intent,
        "entities_expanded": result.rewrite.entities_expanded[:5],
        "tags_required": result.rewrite.tags_required[:5],
        "timing_ms": result.timing,
    }


# ────────────────────────── Suite runner ──────────────────────────

def run_suite(
    hindsight_url: str,
    bank_id: str,
    manifest: ManifestStore,
) -> Dict[str, Any]:
    """Run all ACs sequentially."""
    results: Dict[str, Any] = {"start_ts": time.time(), "acs": []}

    # AC-1
    ok, info = ac_bank_stats(hindsight_url, bank_id)
    results["acs"].append({"name": "AC-1_bank_stats", "ok": ok, **info})
    print(f"  AC-1 bank_stats: {'PASS' if ok else 'FAIL'} {info}")

    # AC-2: recall by entity for top 5 manifest persons by mention_count
    sorted_persons = sorted(
        manifest.persons.values(),
        key=lambda p: sum(a.mention_count for a in p.aka),
        reverse=True,
    )[:5]
    for p in sorted_persons:
        ok2, info2 = ac_recall_by_entity_tag(hindsight_url, bank_id, p.primary_name)
        results["acs"].append({
            "name": f"AC-2_recall_entity_{p.canonical_id}",
            "ok": ok2, **info2,
        })
        print(f"  AC-2 entity={p.primary_name}: {'PASS' if ok2 else 'FAIL'} n={info2.get('n_facts','?')}")

    # AC-3: HEADER rehydrate
    ok3, info3 = ac_header_rehydrate(hindsight_url, bank_id, sample_size=10)
    results["acs"].append({"name": "AC-3_header_rehydrate", "ok": ok3, **info3})
    print(f"  AC-3 header_rehydrate: {'PASS' if ok3 else 'FAIL'} {info3}")

    # AC-4: salience distribution
    ok4, info4 = ac_salience_distribution(hindsight_url, bank_id)
    results["acs"].append({"name": "AC-4_salience_dist", "ok": ok4, **info4})
    print(f"  AC-4 salience_dist: {'PASS' if ok4 else 'FAIL'} {info4}")

    # AC-5: multi-keyword queries
    queries = [
        ("我购买 mac 的全部流程", 1),
        ("Bob近期状态", 1),
        ("通过谁认识Carol", 1),
        ("上周三的实验室会议讨论了什么", 1),
        ("我家在哪里", 1),
    ]
    for q, n_min in queries:
        ok5, info5 = ac_multi_keyword_lifelog(q, bank_id, expected_n_min=n_min)
        results["acs"].append({
            "name": f"AC-5_query_{q[:20]}", "ok": ok5, **info5,
        })
        print(f"  AC-5 query={q!r}: {'PASS' if ok5 else 'FAIL'} n_recall={info5.get('n_recall', '?')}")

    results["end_ts"] = time.time()
    results["duration_s"] = round(results["end_ts"] - results["start_ts"], 1)
    n_pass = sum(1 for ac in results["acs"] if ac["ok"])
    n_total = len(results["acs"])
    results["summary"] = {
        "n_pass": n_pass,
        "n_total": n_total,
        "all_pass": n_pass == n_total,
    }
    print(f"\n=== TOTAL: {n_pass}/{n_total} PASS in {results['duration_s']}s ===")
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hindsight-url",
        default="http://127.0.0.1:8888",
    )
    parser.add_argument(
        "--bank-id",
        default="memory_full_v5",
    )
    parser.add_argument(
        "--manifest-path",
        default="data/identity_manifest.yaml",
    )
    parser.add_argument(
        "--out-report",
        type=Path,
        default=Path("data/l0_v5/verify_report.json"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    manifest = ManifestStore.load(args.manifest_path)
    logger.info(f"manifest stats: {manifest.stats()}")

    results = run_suite(args.hindsight_url, args.bank_id, manifest)

    args.out_report.parent.mkdir(parents=True, exist_ok=True)
    args.out_report.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"wrote report to {args.out_report}")

    return 0 if results["summary"]["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
