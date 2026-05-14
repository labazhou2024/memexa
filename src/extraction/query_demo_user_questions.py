"""LIVE multi-keyword query demo on memory_full_v5.

User-supplied test questions (CEO directive 2026-05-06):
1. "我购买 mac 的全部流程及型号是什么"
2. "我的专利情况目前如何"

Output: ranked list of relevant cards with full reverse-link chain
        (card_id → batch_id → archive prompt.json → original messages).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from src.core.archive_lookup import lookup_archive, ArchiveNotFound
from src.core.identity_manifest import ManifestStore
from src.core.memory_card_v2 import MemoryCard

logger = logging.getLogger("query_demo")


HINDSIGHT_URL = os.environ.get("MEMEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
BANK_ID = os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5")
REMOTE_QWEN3_URL = os.environ.get("MEMEX_QWEN3_URL", "http://<remote-server-ip>:8001")
REMOTE_GEMMA_URL = os.environ.get("MEMEX_GEMMA31B_URL", "http://<remote-server-ip>:8011")


def query_rewrite_with_llm(query: str, manifest_summary: str) -> Dict[str, Any]:
    """Query rewrite via your-org Gemma 4 31B for entity expansion + intent."""
    system = (
        "你是 query rewriter. 输出 JSON 含 entities_expanded(list), "
        "tags(list), intent(str), keywords_for_bm25(list). "
        "用 manifest 切片提示扩展 entity. 严格 JSON, 末加 END_OF_OUTPUT."
    )
    user = f"# query\n{query}\n\n# manifest\n{manifest_summary}\n\n# JSON output:"
    try:
        r = httpx.post(
            f"{REMOTE_GEMMA_URL}/v1/chat/completions",
            json={
                "model": "memex-extractor",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 1500,
                "temperature": 0.0,
            },
            timeout=120.0,
        )
        if r.status_code != 200:
            return {"_fallback": True, "_error": r.status_code}
        content = r.json()["choices"][0]["message"]["content"]
        text = content.split("END_OF_OUTPUT")[0].strip()
        if "```" in text:
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        fb = text.find("{")
        lb = text.rfind("}")
        if fb < 0 or lb < fb:
            return {"_fallback": True}
        return json.loads(text[fb:lb + 1])
    except Exception as e:
        return {"_fallback": True, "_error": str(e)}


def recall_with_query(query: str, tags: List[str] = None, budget: str = "high") -> List[Dict]:
    """Hindsight chunks-only recall."""
    body = {"query": query, "budget": budget, "max_tokens": 8000}
    if tags:
        body["tags"] = tags
    r = httpx.post(
        f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/memories/recall",
        json=body, timeout=60.0,
    )
    r.raise_for_status()
    return r.json().get("results") or r.json().get("memory_facts") or []


def reflect_synthesize(question: str, hits: List[Dict]) -> str:
    """your-org Gemma 4 31B synthesizes a coherent answer from retrieved cards."""
    if not hits:
        return "(召回 0 张相关 card. 没有材料综合.)"

    cards_summary = []
    for i, h in enumerate(hits[:10]):
        text = h.get("text", "")
        try:
            card = MemoryCard.from_retain_content(text)
            narrative = card.narrative
            when = card.when_start
            types = ",".join(card.types)
            cid = h.get("id", "?")[:12]
            cards_summary.append(
                f"[{i + 1}][card={cid}][{when}][types={types}] {narrative[:300]}"
            )
        except Exception as e:
            # Use raw text snippet
            cards_summary.append(f"[{i + 1}][raw] {text[:300]}")

    user_prompt = (
        f"# 用户问题\n{question}\n\n"
        f"# 召回的相关事件卡 (按相关性排序)\n"
        + "\n".join(cards_summary)
        + "\n\n# 任务: 综合上述 cards, 直接回答用户问题. "
          "如果数据不足, 明示 '当前数据未找到此信息'. "
          "不编造. 优先时序输出. 引用时用 [1] [2] 等编号."
    )
    system = (
        "你是事实型答疑助手. 严格基于提供的 cards 回答, "
        "不编造任何未在 cards 出现的事实. "
        "数字/型号/价格必须有 card 证据."
    )

    try:
        r = httpx.post(
            f"{REMOTE_GEMMA_URL}/v1/chat/completions",
            json={
                "model": "memex-extractor",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 4096,
                "temperature": 0.2,
            },
            timeout=180.0,
        )
        if r.status_code != 200:
            return f"(reflect failed HTTP {r.status_code})"
        return r.json()["choices"][0]["message"].get("content", "(empty content)")
    except Exception as e:
        return f"(reflect error: {e})"


def archive_evidence(hits: List[Dict]) -> List[Dict[str, Any]]:
    """Reverse-link each card to its archive."""
    results = []
    for h in hits[:5]:
        meta = h.get("metadata") or {}
        text = h.get("text", "")
        batch_id = meta.get("batch_id") or h.get("document_id", "")
        try:
            card = MemoryCard.from_retain_content(text)
            when_start = card.when_start
            source = card.source
            narrative = card.narrative
        except Exception:
            when_start = meta.get("when_start")
            source = meta.get("source", "wechat")
            narrative = text[:200]
        try:
            archive = lookup_archive(source=source, batch_id=batch_id, when_start=when_start)
            archive_uri = archive.get("archive_uri")
        except ArchiveNotFound:
            archive_uri = "(archive not found)"
        results.append({
            "card_id": h.get("id", "?")[:12],
            "batch_id": batch_id,
            "when_start": when_start,
            "narrative": narrative,
            "archive_uri": archive_uri,
        })
    return results


def run_question(question: str, manifest_summary: str) -> Dict[str, Any]:
    print(f"\n{'=' * 70}")
    print(f"❓ {question}")
    print(f"{'=' * 70}")
    t0 = time.time()

    # Step 1: query rewrite
    rewrite = query_rewrite_with_llm(question, manifest_summary)
    print(f"\n📝 query rewrite:")
    if rewrite.get("_fallback"):
        print(f"   (fallback — error: {rewrite.get('_error', 'unknown')})")
        entities_expanded = []
        keywords = [question]
    else:
        entities_expanded = rewrite.get("entities_expanded", [])
        keywords = rewrite.get("keywords_for_bm25", []) or [question]
        print(f"   entities: {entities_expanded[:5]}")
        print(f"   intent:   {rewrite.get('intent')}")
        print(f"   keywords: {keywords[:5]}")

    # Step 2: recall
    queries_to_try = [question] + entities_expanded[:3]
    seen_ids = set()
    all_hits = []
    for q in queries_to_try:
        hits = recall_with_query(q, budget="high")
        for h in hits:
            if h["id"] not in seen_ids:
                seen_ids.add(h["id"])
                all_hits.append(h)
    print(f"\n🔍 recall: {len(all_hits)} unique cards from {len(queries_to_try)} queries")
    if all_hits:
        for h in all_hits[:5]:
            text = h.get("text", "")
            try:
                card = MemoryCard.from_retain_content(text)
                preview = card.narrative[:120]
            except Exception:
                preview = text[:120]
            print(f"   - [{h['id'][:12]}] {preview}")

    # Step 3: synthesize answer
    answer = reflect_synthesize(question, all_hits)
    print(f"\n💬 综合回答:\n{answer}")

    # Step 4: archive evidence
    evidence = archive_evidence(all_hits)
    print(f"\n📎 反查链 (top 5):")
    for e in evidence:
        print(f"   - [{e['card_id']}] [{e['when_start']}]")
        print(f"     batch_id={e['batch_id']}")
        print(f"     archive={e['archive_uri']}")
        print(f"     narrative: {e['narrative'][:200]}")

    duration = time.time() - t0
    print(f"\n⏱ duration: {duration:.1f}s")

    return {
        "question": question,
        "rewrite": rewrite,
        "n_recall": len(all_hits),
        "answer": answer,
        "evidence": evidence,
        "duration_s": round(duration, 1),
    }


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", nargs="*", default=[
        "我购买 mac 的全部流程及型号是什么",
        "我的专利情况目前如何",
    ])
    parser.add_argument("--out", type=Path,
                        default=Path("data/l0_v5/query_demo_results.json"))
    parser.add_argument("--manifest-path", type=str,
                        default="data/identity_manifest.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    # Load manifest summary (compact)
    store = ManifestStore.load(args.manifest_path)
    summary_lines = []
    for cid, p in list(store.persons.items())[:30]:
        akas = [p.primary_name] + [a.surface for a in p.aka if a.is_active()][:3]
        summary_lines.append(f"  {cid}: {akas}")
    manifest_summary = "\n".join(summary_lines)

    results = []
    for q in args.questions:
        res = run_question(q, manifest_summary)
        results.append(res)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n📄 wrote results to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
