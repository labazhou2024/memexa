"""Phase B: RAG-enabled extraction tools for Pass-2.

Spec: docs/l0_v5/MASTER_PLAN.md §12.5

Activates AFTER ≥500 cards in v5 bank + ≥30 manifest persons.

Tools (prompt-engineered, NOT native vLLM tool_call — works with any LLM):
- recall_graph: query memory_full_v5 for prior context
- manifest_lookup: disambiguate person/org by surface + room/time
- shared_context_query: between-person relation lookup

Loop limit: 3 tool calls per batch (anti-runaway).
All calls emit pass2_rag_call trace events for audit.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from memexa.core.archive_lookup import lookup_archive, ArchiveNotFound
from memexa.core.identity_manifest import ManifestStore
from memexa.core.memory_card_v2 import MemoryCard

logger = logging.getLogger("pass2_rag_tools")


HINDSIGHT_URL = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
BANK_ID = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")

MAX_TOOL_CALLS_PER_BATCH = 3
RECALL_LIMIT_DEFAULT = 5

TOOLS_PROMPT_SECTION = """

【RAG 工具协议 (Phase B)】

你可以调用以下 3 个工具查询历史 / 身份信息. 当遇到无法在本 batch 内消解的指代时使用.

每条工具调用必须严格按以下格式输出在 cards JSON 之前:

```
TOOL_CALL: <tool_name>
TOOL_ARGS: {"key": "value", ...}
END_TOOL_CALL
```

工具:

1. **recall_graph** — 查 memory_full_v5 的历史 cards
   args: {
     "query_text": "自由文本",
     "tags_filter": ["entity:<sha16>", "room:<hash>", ...] (可选),
     "limit": 5 (默认)
   }
   returns: 至多 5 条 cards 摘要 (narrative + when_start + entities)

2. **manifest_lookup** — 按 surface 查 manifest 候选
   args: {
     "surface_form": "老张",
     "context_room_hash": "<hash>" (可选),
     "kind": "person|organization|inanimate|public_figure" (可选)
   }
   returns: 候选列表 (canonical_id + primary_name + confidence)

3. **shared_context_query** — 查两人关系/共现
   args: {
     "person_a_canonical_id": "person_xxx",
     "person_b_canonical_id": "person_yyy"
   }
   returns: relation_type + how_known + shared_contexts

【限制】

- 每个 batch 最多调用 3 次工具 (硬限制)
- 不调工具也可以直接输出 cards (优先内嵌 manifest_slice)
- 工具结果会以 'TOOL_RESULT:' 形式追加到对话, 你看到后再决定是否再调或输出 cards

【输出 cards 时】

在所有工具调用后, 像往常一样输出 schema v2 cards JSON (开始 marker `{"cards":...`).
最后必须是 END_OF_OUTPUT.
"""


@dataclass
class RagToolCall:
    tool_name: str
    args: Dict[str, Any]
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: float = 0.0


def _exec_recall_graph(args: Dict[str, Any]) -> Dict[str, Any]:
    query_text = args.get("query_text", "")
    tags_filter = args.get("tags_filter", []) or []
    limit = int(args.get("limit", RECALL_LIMIT_DEFAULT))
    body = {"query": query_text, "budget": "low",
            "max_tokens": min(2000, limit * 400)}
    if tags_filter:
        body["tags"] = tags_filter
    try:
        r = httpx.post(
            f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/memories/recall",
            json=body, timeout=30.0,
        )
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        results = r.json().get("results", [])[:limit]
        # Compress to abstract form
        abstracts = []
        for h in results:
            text = h.get("text", "")
            try:
                card = MemoryCard.from_retain_content(text)
                abstracts.append({
                    "card_id": h.get("id", "?")[:12],
                    "narrative": card.narrative[:300],
                    "when_start": card.when_start,
                    "entities": [e.canonical_name for e in card.entities[:5]],
                    "types": card.types,
                })
            except Exception:
                abstracts.append({
                    "card_id": h.get("id", "?")[:12],
                    "narrative": text[:300],
                    "raw": True,
                })
        return {"cards": abstracts, "count": len(abstracts)}
    except Exception as e:
        return {"error": str(e)}


def _exec_manifest_lookup(args: Dict[str, Any], store: ManifestStore) -> Dict[str, Any]:
    surface = args.get("surface_form", "")
    kind = args.get("kind")  # 'person' / 'organization' / 'inanimate' / 'public_figure'
    if not surface:
        return {"error": "surface_form required"}

    candidates: List[Dict[str, Any]] = []

    # By surface match (substring)
    matches = store.lookup_by_surface(surface)
    for m in matches[:10]:
        candidates.append({
            "canonical_id": getattr(m, "canonical_id", "?"),
            "primary_name": getattr(m, "primary_name", "?"),
            "confidence": "exact",
            "kind": "person" if "person" in str(type(m)).lower() else
                    "organization" if "organization" in str(type(m)).lower() else
                    "inanimate" if "inanimate" in str(type(m)).lower() else
                    "public_figure" if "public" in str(type(m)).lower() else "unknown",
        })

    # By pinyin (only persons)
    if not candidates and surface.isascii():
        pinyin_cands = store.lookup_by_pinyin(surface.lower())
        for p in pinyin_cands[:10]:
            candidates.append({
                "canonical_id": p.canonical_id,
                "primary_name": p.primary_name,
                "confidence": "pinyin",
                "kind": "person",
            })

    return {"candidates": candidates[:10]}


def _exec_shared_context_query(args: Dict[str, Any], store: ManifestStore) -> Dict[str, Any]:
    a = args.get("person_a_canonical_id")
    b = args.get("person_b_canonical_id")
    if not a or not b:
        return {"error": "both person_*_canonical_id required"}
    if a == b:
        return {"error": "cannot query relation with self"}
    rel = store.lookup_relation(a, b)
    if rel is None:
        return {"relation": None, "shared_contexts": [], "how_known": []}
    return {
        "relation_type": rel.relation_type,
        "how_known": [
            {
                "via": h.via, "when": h.when, "context": h.context,
                "evidence_card_ids": h.evidence_card_ids,
                "confidence": h.confidence,
            }
            for h in rel.how_known
        ],
        "shared_contexts": [
            {
                "context_type": s.context_type,
                "rooms": list(s.rooms),
                "first_co_occur_ts": s.first_co_occur_ts,
                "last_co_occur_ts": s.last_co_occur_ts,
                "co_occur_count": s.co_occur_count,
            }
            for s in rel.shared_contexts
        ],
    }


def parse_tool_call(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Find a TOOL_CALL block in LLM output."""
    if "TOOL_CALL:" not in text:
        return None
    start = text.find("TOOL_CALL:")
    end = text.find("END_TOOL_CALL", start)
    if end < 0:
        end = start + 800  # bound
    block = text[start:end]
    name = ""
    args_obj: Dict[str, Any] = {}
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("TOOL_CALL:"):
            name = s.replace("TOOL_CALL:", "", 1).strip()
        elif s.startswith("TOOL_ARGS:"):
            arg_text = s.replace("TOOL_ARGS:", "", 1).strip()
            # Sometimes args span multiple lines
            try:
                args_obj = json.loads(arg_text)
            except json.JSONDecodeError:
                # try to find a JSON object after TOOL_ARGS: in the block
                br_start = block.find("{", block.find("TOOL_ARGS:"))
                br_end = -1
                depth = 0
                if br_start >= 0:
                    for i in range(br_start, len(block)):
                        if block[i] == "{":
                            depth += 1
                        elif block[i] == "}":
                            depth -= 1
                            if depth == 0:
                                br_end = i
                                break
                    if br_end > br_start:
                        try:
                            args_obj = json.loads(block[br_start:br_end + 1])
                        except json.JSONDecodeError:
                            args_obj = {}
    if not name:
        return None
    return name, args_obj


def execute_tool(
    name: str, args: Dict[str, Any], store: ManifestStore
) -> Tuple[Dict[str, Any], float]:
    """Execute one tool, return (result, duration_ms)."""
    t0 = time.time()
    if name == "recall_graph":
        result = _exec_recall_graph(args)
    elif name == "manifest_lookup":
        result = _exec_manifest_lookup(args, store)
    elif name == "shared_context_query":
        result = _exec_shared_context_query(args, store)
    else:
        result = {"error": f"unknown tool: {name}"}
    duration_ms = (time.time() - t0) * 1000
    return result, duration_ms


def format_tool_result(name: str, args: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Append tool result back to conversation."""
    return (
        "\nTOOL_RESULT:\n"
        f"  tool: {name}\n"
        f"  args: {json.dumps(args, ensure_ascii=False)[:300]}\n"
        f"  result: {json.dumps(result, ensure_ascii=False)[:1000]}\n"
        "END_TOOL_RESULT\n"
    )


__all__ = [
    "TOOLS_PROMPT_SECTION",
    "MAX_TOOL_CALLS_PER_BATCH",
    "parse_tool_call",
    "execute_tool",
    "format_tool_result",
    "RagToolCall",
]
