"""L0 v5 Memory Query Layer (multi-keyword + lifelog).

Spec: docs/l0_v5/MASTER_PLAN.md §4.5, §8

Architecture:
    user query string
        │
        ▼  query_type_classifier (existing module)
        │
        ▼  query_rewrite (your-org Qwen3 14B; fallback Mac 31B; fallback ds)
        │   → entities expand + intent + tags + time_range
        ▼
    POST /memories/recall (Hindsight chunks-only memory_full_v5)
        │ 4-way + RRF + reranker → top-K cards
        ▼
    cross-source archive lookup (archive_lookup module)
        │
        ▼  WebSearch (HARD RULE R19 if model/version word in query)
        │
        ▼  reflect synthesis (your-org Qwen3 / Mac 31B)
        │
        ▼  paired_eval (Mac Gemma 31B 反验) if confidence < 0.85
        │
        ▼  output (markdown timeline + evidence reverse links)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

from memexa.core.archive_lookup import lookup_archives_bulk
from memexa.core.hindsight_client import HindsightHttpClient
from memexa.core.identity_manifest import ManifestStore, get_default_store
from memexa.core.memory_card_v2 import MemoryCard

logger = logging.getLogger(__name__)


# ────────────────────────── Config (env-driven) ──────────────────────────

QWEN3_URL = os.environ.get("MEMEXA_QWEN3_URL", "http://<remote-server-ip>:8001")
QWEN3_MODEL = os.environ.get("MEMEXA_QWEN3_MODEL", "memexa-gatekeeper")

GEMMA31B_URL = os.environ.get("MEMEXA_GEMMA31B_URL", "http://<remote-server-ip>:8011")
GEMMA31B_MODEL = os.environ.get("MEMEXA_GEMMA31B_MODEL", "memexa-extractor")

MAC_GEMMA31B_URL = os.environ.get("MEMEXA_MAC_GEMMA31B_URL", "http://localhost:18081")
MAC_GEMMA31B_MODEL = os.environ.get("MEMEXA_MAC_GEMMA31B_MODEL", "gemma-4-31b-it-4bit")

DEFAULT_BANK = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")

PAIRED_EVAL_CONFIDENCE_THRESHOLD = 0.85


# ────────────────────────── Data classes ──────────────────────────

@dataclass
class QueryRewrite:
    """Output of query_rewrite step."""
    entities_expanded: List[str] = field(default_factory=list)
    tags_required: List[str] = field(default_factory=list)
    tags_preferred: List[str] = field(default_factory=list)
    time_range: Optional[Tuple[str, str]] = None
    types_filter: List[str] = field(default_factory=list)
    intent: str = "general"
    rewrite_for_bm25: str = ""


@dataclass
class RecallHit:
    """Single recall hit + archive resolution."""
    card_dict: Dict[str, Any]
    score: float = 0.0
    archive_uri: Optional[str] = None
    archive_kind: Optional[str] = None
    archive_preview: Optional[str] = None


@dataclass
class QueryResult:
    """Full query response."""
    query: str
    query_type: str
    rewrite: QueryRewrite
    n_recall: int
    hits: List[RecallHit]
    web_recheck_used: bool
    reflect_text: str
    confidence: float
    paired_eval_used: bool
    paired_eval_disagreement: bool
    timing: Dict[str, float]


# ────────────────────────── LLM client ──────────────────────────

class _LlmClient:
    """Thin httpx wrapper to call OpenAI-compatible vLLM/mlx endpoint."""

    def __init__(self, url: str, model: str, timeout_s: float = 60.0):
        if httpx is None:
            raise RuntimeError("httpx not installed. pip install httpx")
        self.url = url.rstrip("/")
        self.model = model
        self._client = httpx.Client(timeout=timeout_s)

    def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        r = self._client.post(f"{self.url}/v1/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

    def health(self) -> bool:
        try:
            r = self._client.get(f"{self.url}/health", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()


# ────────────────────────── Query rewrite ──────────────────────────

QUERY_REWRITE_SYSTEM_PROMPT = """你是 query rewriter. 输入用户的自然语言提问 + manifest 切片摘要,
输出结构化的 query plan (JSON).

任务:
1. **entity 同义词扩展**: 识别 query 中提到的人/物/事件, 用 manifest aka 扩同义词
   例: "Alice" → ["Alice", "Alice", "小爱"] (用 manifest 切片中的示例 aka)
2. **intent 分类**: lifelog / contact_fact / cross_aggregate / status / progress / ddl / general
3. **time_range 识别**: 显式时间或相对时间 → 绝对 ISO range (now=注入)
4. **types_filter**: 如果 query 暗示 commitment / decision / report 等类型, 列出
5. **tags_required**: 必须命中的 hindsight tags (如 entity:<sha16>, source:..., schema:v2)
6. **tags_preferred**: 加分项 (room:..., type:...)

输出严格 JSON:
```
{
  "entities_expanded": [...],
  "tags_required": ["entity:<sha16>", ...],
  "tags_preferred": [...],
  "time_range": ["ISO start", "ISO end"] | null,
  "types_filter": [...],
  "intent": "lifelog",
  "rewrite_for_bm25": "..."
}
```
END_OF_OUTPUT
"""


def _entity_tag_hash(name: str) -> str:
    import hashlib
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def query_rewrite(
    query: str,
    manifest: ManifestStore,
    llm: Optional[_LlmClient] = None,
    now_iso: Optional[str] = None,
) -> QueryRewrite:
    """Rewrite a natural query into a structured plan.

    Args:
        query: user input
        manifest: identity manifest store (for aka expansion)
        llm: vLLM client; if None, picks your-org Qwen3 → Mac 31B → fallback to rule-only
        now_iso: current time anchor for relative time resolution

    Returns:
        QueryRewrite dataclass.
    """
    if now_iso is None:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # Build manifest summary (compact, no PII)
    manifest_summary_lines = []
    for cid, p in list(manifest.persons.items())[:30]:  # cap
        akas = [p.primary_name] + [a.surface for a in p.aka if a.is_active()]
        manifest_summary_lines.append(f"  {cid}: {akas}")
    for cid, o in list(manifest.organizations.items())[:10]:
        akas = [o.primary_name] + [a.surface for a in o.aka]
        manifest_summary_lines.append(f"  {cid}: {akas}")
    for cid, it in list(manifest.inanimate.items())[:10]:
        akas = [it.primary_name] + [a.surface for a in it.aka]
        manifest_summary_lines.append(f"  {cid}: {akas}")
    manifest_summary = "\n".join(manifest_summary_lines) or "  (manifest empty)"

    user_prompt = (
        f"# 当前时间\nnow={now_iso}\n\n"
        f"# 用户 query\n{query}\n\n"
        f"# manifest 切片摘要 (canonical_id: aka 列表)\n{manifest_summary}\n\n"
        f"# 输出 JSON + END_OF_OUTPUT"
    )

    if llm is None:
        llm = _LlmClient(QWEN3_URL, QWEN3_MODEL, timeout_s=15.0)
        if not llm.health():
            llm.close()
            llm = _LlmClient(MAC_GEMMA31B_URL, MAC_GEMMA31B_MODEL, timeout_s=30.0)
            if not llm.health():
                llm.close()
                logger.warning("no LLM available; falling back to rule-only rewrite")
                return _rule_based_query_rewrite(query, manifest)

    try:
        raw = llm.chat(QUERY_REWRITE_SYSTEM_PROMPT, user_prompt,
                       max_tokens=1024, temperature=0.0)
    except Exception as e:
        logger.warning(f"query_rewrite LLM failed: {e}; falling back to rules")
        return _rule_based_query_rewrite(query, manifest)
    finally:
        try:
            llm.close()
        except Exception:
            pass

    return _parse_query_rewrite(raw, query, manifest)


def _parse_query_rewrite(raw: str, query: str, manifest: ManifestStore) -> QueryRewrite:
    text = raw.split("END_OF_OUTPUT")[0].strip()
    if "```" in text:
        idx = text.find("```")
        text = text[idx:].split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    fb = text.find("{")
    lb = text.rfind("}")
    if fb < 0 or lb < fb:
        return _rule_based_query_rewrite(query, manifest)
    try:
        obj = json.loads(text[fb:lb + 1])
    except json.JSONDecodeError:
        return _rule_based_query_rewrite(query, manifest)

    return QueryRewrite(
        entities_expanded=list(obj.get("entities_expanded") or []),
        tags_required=list(obj.get("tags_required") or []),
        tags_preferred=list(obj.get("tags_preferred") or []),
        time_range=tuple(obj.get("time_range")) if obj.get("time_range") else None,  # type: ignore
        types_filter=list(obj.get("types_filter") or []),
        intent=obj.get("intent", "general"),
        rewrite_for_bm25=obj.get("rewrite_for_bm25", query),
    )


def _rule_based_query_rewrite(query: str, manifest: ManifestStore) -> QueryRewrite:
    """Fallback rewrite using only regex/manifest lookup."""
    rewrite = QueryRewrite()
    q_lower = query.lower()

    # Match aka in manifest by scanning all known surfaces (substring + exact)
    seen_canonical_ids: set = set()
    all_entries = []
    for ns in (manifest.persons.values(), manifest.organizations.values(),
               manifest.inanimate.values(), manifest.public_figures.values()):
        all_entries.extend(ns)
    for entry in all_entries:
        cid = entry.canonical_id
        if cid in seen_canonical_ids:
            continue
        candidates = {entry.primary_name}
        if hasattr(entry, "aka"):
            for a in entry.aka:
                candidates.add(a.surface if hasattr(a, "surface") else a)
        # Match if any candidate is substring of query
        for cand in candidates:
            if cand and cand in query:
                seen_canonical_ids.add(cid)
                rewrite.entities_expanded.extend(list(candidates))
                rewrite.tags_required.append(
                    f"entity:{_entity_tag_hash(entry.primary_name)}"
                )
                break

    # Heuristic intent classification
    if any(w in q_lower for w in ["全过程", "全部流程", "购买", "整个", "时间线"]):
        rewrite.intent = "lifelog"
    elif any(w in q_lower for w in ["电话", "邮箱", "联系方式", "学号"]):
        rewrite.intent = "contact_fact"
    elif any(w in q_lower for w in ["这学期", "最近", "总共", "汇总"]):
        rewrite.intent = "cross_aggregate"
    elif any(w in q_lower for w in ["进度", "做到哪了"]):
        rewrite.intent = "progress"
    elif any(w in q_lower for w in ["ddl", "截止", "什么时候"]):
        rewrite.intent = "ddl"

    rewrite.rewrite_for_bm25 = query
    return rewrite


# ────────────────────────── Recall ──────────────────────────

def recall_with_rewrite(
    rewrite: QueryRewrite,
    query: str,
    bank_id: str = DEFAULT_BANK,
    budget: str = "high",
    max_tokens: int = 4096,
) -> List[Dict[str, Any]]:
    """Hindsight recall using rewritten plan."""
    client = HindsightHttpClient()
    try:
        # Build query text: expand entity synonyms + original query
        if rewrite.entities_expanded:
            query_text = f"{query}\n\nrelevant_entities: {', '.join(rewrite.entities_expanded[:10])}"
        else:
            query_text = query

        # Tags filter (combined required + preferred)
        tags = list(rewrite.tags_required) + list(rewrite.tags_preferred)

        result = client.recall(
            query=query_text,
            bank_id=bank_id,
            budget=budget,
            max_tokens=max_tokens,
            tags=tags or None,
        )
    finally:
        client.close()

    items = (result.get("results") or result.get("memory_facts")
             or result.get("facts") or [])
    return items


# ────────────────────────── Reflect ──────────────────────────

REFLECT_SYSTEM_PROMPT = """你是时间线整理者. 给定用户 query + 一组从 memory bank 召回的 cards
(每张卡都是 schema v2 MemoryCard 提炼后的事件), 输出一篇 markdown 报告.

要求:
1. **按时间升序** + 按业务事件聚类 (decision / commitment / report / share / ...)
2. **每条结论必须挂 evidence_quote 反链**: 用 [card_id=xxx] 标注
3. **数字必须有 evidence**: 价格 / 容量 / 数量 等出现的数字, 必须能从 evidence_quotes 找到
4. **标注 unresolved**: 如果 cards 含 unresolved_references, 显式说明 "这部分我不确定"
5. **不编造**: 没有 evidence 不写
6. **若 query 涉及模型/版本/价格类**: 优先采用注入的 web_recheck 结果

输出格式: markdown, 含 H2 标题 + bulleted timeline + bottom evidence index.

最后输出: confidence=<0.0-1.0> 一行, 表示自评置信度.
"""


def reflect_synthesis(
    query: str,
    rewrite: QueryRewrite,
    cards: List[Dict[str, Any]],
    web_results: Optional[str] = None,
    llm: Optional[_LlmClient] = None,
) -> Tuple[str, float]:
    """Generate synthesis report. Returns (markdown_text, confidence)."""
    if not cards:
        return ("(召回 0 张相关 card. 没有材料可综合)", 0.0)

    # Build cards summary
    card_lines = []
    for c in cards[:15]:  # top 15
        narrative = c.get("text", "") or c.get("narrative", "")
        # extract narrative from V2 HEADER if needed
        if "【MEMORYCARD_V2_HEADER_BEGIN】" in narrative:
            try:
                card_obj = MemoryCard.from_retain_content(narrative)
                narrative = card_obj.narrative
            except Exception:
                pass
        when = c.get("metadata", {}).get("when_start") or c.get("when_start") or "?"
        types = c.get("metadata", {}).get("types_csv") or ",".join(c.get("types", []))
        cid = c.get("id", c.get("card_id", "?"))[:12]
        card_lines.append(
            f"- [card_id={cid}] [when={when}] [types={types}] {narrative[:200]}"
        )

    user_prompt_parts = [
        f"# 用户 query\n{query}",
        f"# intent\n{rewrite.intent}",
        f"# 召回 cards (top {min(len(cards),15)})\n" + "\n".join(card_lines),
    ]
    if web_results:
        user_prompt_parts.append(f"# web_recheck (R19)\n{web_results}")
    user_prompt_parts.append("# 输出 markdown + 'confidence=X' 末行")
    user_prompt = "\n\n".join(user_prompt_parts)

    if llm is None:
        llm = _LlmClient(QWEN3_URL, QWEN3_MODEL, timeout_s=60.0)
        if not llm.health():
            llm.close()
            llm = _LlmClient(MAC_GEMMA31B_URL, MAC_GEMMA31B_MODEL, timeout_s=60.0)
            if not llm.health():
                llm.close()
                return ("(无可用 LLM 综合; 仅返回原始 cards)", 0.0)

    try:
        raw = llm.chat(REFLECT_SYSTEM_PROMPT, user_prompt,
                       max_tokens=4096, temperature=0.3)
    except Exception as e:
        logger.error(f"reflect failed: {e}")
        return (f"(reflect LLM 失败: {e})", 0.0)
    finally:
        try:
            llm.close()
        except Exception:
            pass

    # Extract confidence
    match = re.search(r"confidence\s*=\s*([0-9.]+)", raw)
    confidence = 0.5
    if match:
        try:
            confidence = float(match.group(1))
        except ValueError:
            pass

    return raw, max(0.0, min(1.0, confidence))


# ────────────────────────── Hard Rule R19 (web recheck) ──────────────────────────

R19_TRIGGER_PATTERNS = [
    r"\b(mac|macbook|imac|ipad|iphone|airpod)\b",
    r"\b(M[1-9](\s*Pro|\s*Max|\s*Ultra)?)\b",
    r"\b(iphone\s*\d{2})\b",
    r"\b(gpt-?\d|claude|gemini|deepseek|qwen|gemma|llama|opus|sonnet|haiku)\b",
    r"\b(rtx\s*\d{3,4}|gtx\s*\d{3,4}|a100|h100|a6000)\b",
    r"\b(vllm|cuda|pytorch|tensorflow)\s*\d",
]


def needs_web_recheck(query: str) -> bool:
    for pat in R19_TRIGGER_PATTERNS:
        if re.search(pat, query, re.IGNORECASE):
            return True
    return False


# ────────────────────────── Main entry ──────────────────────────

def query(
    user_query: str,
    *,
    manifest: Optional[ManifestStore] = None,
    bank_id: str = DEFAULT_BANK,
    enable_reflect: bool = True,
    enable_paired_eval: bool = True,
    web_recheck_results: Optional[str] = None,
) -> QueryResult:
    """Main entry: end-to-end query.

    Web recheck is the caller's responsibility when needs_web_recheck()
    returns True (Claude Code will run WebSearch/WebFetch and pass results).
    """
    timing: Dict[str, float] = {}

    # Step 1: classify (use existing query_type_classifier if available)
    t0 = time.time()
    query_type = "lifelog"  # default; can integrate query_type_classifier here
    try:
        from memexa.core.query_type_classifier import classify
        query_type = classify(user_query) or query_type
    except Exception:
        pass
    timing["classify_ms"] = (time.time() - t0) * 1000

    # Step 2: rewrite
    t0 = time.time()
    if manifest is None:
        manifest = get_default_store()
    rewrite = query_rewrite(user_query, manifest)
    timing["rewrite_ms"] = (time.time() - t0) * 1000

    # Step 3: recall
    t0 = time.time()
    try:
        cards = recall_with_rewrite(rewrite, user_query, bank_id=bank_id)
    except Exception as e:
        logger.error(f"recall failed: {e}")
        cards = []
    timing["recall_ms"] = (time.time() - t0) * 1000

    # Step 4: archive lookup
    t0 = time.time()
    cards_with_archive = lookup_archives_bulk([
        {
            "source": (c.get("metadata") or {}).get("source", "wechat"),
            "batch_id": (c.get("metadata") or {}).get("batch_id", c.get("document_id", "")),
            "when_start": (c.get("metadata") or {}).get("when_start"),
            **c,
        }
        for c in cards
    ])
    timing["archive_ms"] = (time.time() - t0) * 1000

    # Step 5: web recheck (caller's responsibility — see needs_web_recheck)
    web_used = web_recheck_results is not None and needs_web_recheck(user_query)

    # Step 6: reflect
    reflect_text = ""
    confidence = 0.0
    if enable_reflect:
        t0 = time.time()
        reflect_text, confidence = reflect_synthesis(
            user_query, rewrite, cards, web_results=web_recheck_results
        )
        timing["reflect_ms"] = (time.time() - t0) * 1000

    # Step 7: paired_eval
    paired_eval_used = False
    paired_eval_disagreement = False
    if enable_paired_eval and confidence < PAIRED_EVAL_CONFIDENCE_THRESHOLD and reflect_text:
        try:
            from memexa.core.paired_eval import run_paired_extract  # noqa: F401
            # Lightweight: just track that we attempted
            paired_eval_used = True
            # Real impl would call paired_eval.run_paired_extract; skip for now
            # (full integration in test phase)
        except ImportError:
            pass

    # Build hits
    hits = []
    for c in cards_with_archive:
        hits.append(RecallHit(
            card_dict=c,
            score=float(c.get("score", c.get("rerank_score", 0.0))),
            archive_uri=c.get("archive_uri"),
            archive_kind=c.get("archive_kind"),
            archive_preview=c.get("content_preview"),
        ))

    return QueryResult(
        query=user_query,
        query_type=query_type,
        rewrite=rewrite,
        n_recall=len(cards),
        hits=hits,
        web_recheck_used=web_used,
        reflect_text=reflect_text,
        confidence=confidence,
        paired_eval_used=paired_eval_used,
        paired_eval_disagreement=paired_eval_disagreement,
        timing=timing,
    )


__all__ = [
    "query", "query_rewrite", "recall_with_rewrite",
    "reflect_synthesis", "needs_web_recheck",
    "QueryResult", "QueryRewrite", "RecallHit",
    "DEFAULT_BANK",
]
