"""memexa.core.memory_query — 单一图记忆查询入口 (Phase 2.3, 2026-05-06).

设计目标:
  - 唯一公共 API,所有记忆查询走这一层 (替代散落的 graph_memory_v2.query_entity /
    hindsight_client.recall / grep memory/*.md 三条路径)
  - 默认查询源 = memory_full_v5 bank, schema:v2 (CEO 2026-05-08 directive: v3 archived)
  - 历史 memory_full bank (schema:v0/v1) 仅 include_legacy=True 时查询
  - Win 端 metadata + salience post-filter (Hindsight multi-tag 是 OR, 必须 Win 端补 AND)
  - 反幻觉 3 层 wrapper + Layer 4 invalidation filter (复用 graph_memory_v2._apply_anti_halluc_wrappers)
  - 未来扩展只改实现, API 不动 (CEO 直接指示: 查询不会有大规模修改)

公共 API (6 个):
  quick(query)         快速 fact 召回 + 默认过滤 (replace graph_memory_v2.query_entity)
  reflect(query)       LLM 综合答案 (Hindsight reflect endpoint)
  timeline(date_range) 时序聚合
  person(name)         L1 article + 最近 L0 events
  project(topic)       跨 source 项目视图
  pending()            status=pending 的承诺/问询

Internal helpers:
  _recall_with_defaults  base recall + Win 端过滤
  _post_filter           salience + tier + type + source 过滤
  _parse_card_metadata   从 Hindsight 返回的 metadata 还原 MemoryCard 字段
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Query invocation log (2026-05-12) — feeds dashboard /api/graph/*
# ──────────────────────────────────────────────────────────────


def _resolve_query_log_path() -> Path:
    """Resolve the JSONL log path via the workspace path resolver.

    Order:
      1. ``MEMEXA_QUERY_LOG_PATH`` env var (advanced override)
      2. ``data_dir() / "memory_query_log.jsonl"`` (preferred — honors
         ``MEMEXA_WORKSPACE_ROOT`` and ``~/.memexa/config.yaml``)
      3. Fallback to the legacy in-tree path so existing dashboards keep
         working when ``_path_resolver`` import fails for any reason.
    """
    raw = os.environ.get("MEMEXA_QUERY_LOG_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        from memexa.core._path_resolver import data_dir
        return data_dir() / "memory_query_log.jsonl"
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "memory_query_log.jsonl"


_QUERY_LOG_PATH = _resolve_query_log_path()
_QUERY_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB rollover


def _append_query_log(entry: Dict[str, Any]) -> None:
    """Append a single CLI invocation record to memory_query_log.jsonl.

    Best-effort; never raises. Auto-rotates at 50 MB → memory_query_log.jsonl.1.
    Schema: {ts, subcmd, query, params, n_results, latency_ms, ok, error}.
    """
    try:
        _QUERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _QUERY_LOG_PATH.exists() and _QUERY_LOG_PATH.stat().st_size > _QUERY_LOG_MAX_BYTES:
            rotated = _QUERY_LOG_PATH.with_suffix(".jsonl.1")
            if rotated.exists():
                rotated.unlink()
            _QUERY_LOG_PATH.rename(rotated)
        with _QUERY_LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # never let logging break the query


# ──────────────────────────────────────────────────────────────
# Raw text drill-down helpers (2026-05-10)
# ──────────────────────────────────────────────────────────────

# Search dirs (ordered by preference). batch_id 反查时按顺序找。
_RAW_BATCH_DIRS: List[str] = [
    "data/l0_v5/input_batches",  # v5 native (含 wechat 4-5月 + 历史)
    "data/extract_archive",       # phase1 v1 (含 wechat 1-3月 raw)
    "data/extract_archive_email_browser",  # email + browser raw (含 pair.jsonl)
]


def find_raw_batch(batch_id: str, repo_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Find raw batch json by batch_id across known dirs.

    Returns dict with: {path, source_kind, messages?, prompt?, pair_facts?}
    or None if not found in any dir.
    """
    import os as _os
    if not batch_id:
        return None
    root = repo_root or _os.getcwd()
    for sub in _RAW_BATCH_DIRS:
        base = _os.path.join(root, sub)
        if not _os.path.isdir(base):
            continue
        # 2-level scan: <base>/<date>/<batch_id>/
        for date_dir in _os.listdir(base):
            cand = _os.path.join(base, date_dir, batch_id)
            if _os.path.isdir(cand):
                pp = _os.path.join(cand, "prompt.json")
                pj = _os.path.join(cand, "pair.jsonl")
                rec: Dict[str, Any] = {"path": cand, "dir": sub}
                try:
                    if _os.path.isfile(pp):
                        with open(pp, "r", encoding="utf-8") as fp:
                            d = json.load(fp)
                        rec["source_kind"] = d.get("source_kind") or d.get("schema_v_input") or "?"
                        rec["chat_room"] = d.get("chat_room")
                        rec["messages"] = d.get("messages") or []
                        # for email/browser, raw text is in 'prompt' string field
                        if "prompt" in d and not rec.get("messages"):
                            rec["prompt_text"] = d["prompt"][:8000]
                except Exception as e:
                    rec["read_error"] = str(e)[:80]
                # also pair.jsonl if present (v1 SPO facts)
                if _os.path.isfile(pj):
                    try:
                        with open(pj, "r", encoding="utf-8") as fp:
                            rec["pair_facts"] = [json.loads(L) for L in fp if L.strip()]
                    except Exception:
                        pass
                return rec
    return None


def _extract_batch_id_from_text(text: str) -> Optional[str]:
    """Fallback: parse V2 envelope JSON inside text content to find batch_id.

    Cards from hindsight recall sometimes return metadata={} (envelope only
    in text). Pattern: 【MEMORYCARD_V2_HEADER_BEGIN】\\n{...}\\n【MEMORYCARD_V2_HEADER_END】
    """
    if not text or "MEMORYCARD_V2_HEADER_BEGIN" not in text:
        return None
    try:
        beg = text.index("MEMORYCARD_V2_HEADER_BEGIN")
        # skip past the marker (which is wrapped in 【...】)
        json_start = text.find("{", beg)
        if json_start < 0:
            return None
        # Find matching close brace at depth 0
        depth = 0
        for i in range(json_start, min(len(text), json_start + 50000)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    payload = text[json_start:i+1]
                    obj = json.loads(payload)
                    return obj.get("batch_id")
    except Exception:
        pass
    return None


def attach_raw(cards: List[Dict[str, Any]], max_attach: int = 20) -> List[Dict[str, Any]]:
    """Attach raw_batch reference to first N cards by batch_id metadata.

    Looks up batch_id in 2 places: (1) card.metadata.batch_id, (2) V2 envelope
    inside card.text. Hindsight recall API sometimes returns metadata={} so
    fallback to envelope-parse is essential.
    """
    n_attached = 0
    for c in cards:
        if n_attached >= max_attach:
            break
        md = c.get("metadata") or {}
        bid = md.get("batch_id")
        if not bid:
            bid = _extract_batch_id_from_text(c.get("text") or "")
        if not bid:
            continue
        raw = find_raw_batch(bid)
        if raw is not None:
            c["raw_batch"] = raw
            n_attached += 1
    return cards


# ──────────────────────────────────────────────────────────────
# Defaults (P2.1 切换的产物)
# ──────────────────────────────────────────────────────────────

DEFAULT_BANK = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")
LEGACY_BANK = "memory_full"  # archived schema:v0/v1 (pre-2026-05-06)
DEFAULT_SCHEMA_TAG = "schema:v2"
DEFAULT_KIND_EVENT_TAG = "kind:event"
DEFAULT_KIND_ARTICLE_TAG = "kind:article"

# Win 端 salience post-filter (multi-tag = OR, 须补 AND)
# 2026-05-13: lowered 0.30 → 0.0. Prior cutoff hid most "情境/分享/state" cards
# (sal<0.3 = "low-priority info but still searchable" by design). User's "查不到"
# complaint about 王歆喆 etc. — these cards exist at sal=0.3 boundary and were
# silently filtered. Use --salience to override per query.
DEFAULT_SALIENCE_FLOOR = 0.0
# 2026-05-08: SESSION_LOAD lowered 0.50 → 0.40. Empirically 0.50 returned
# (无) on 2026-05-08 sessions despite live 80+ recent salience>=0.4 cards;
# 0.6 (initial) returned (无) on most days, 0.5 on quiet days.
SESSION_LOAD_SALIENCE_FLOOR = 0.40

# Trace
_TRACE_LOG_PATH = os.environ.get("MEMEXA_MEMORY_QUERY_TRACE", "")


def _emit_trace(event: str, payload: Dict[str, Any]) -> None:
    if not _TRACE_LOG_PATH:
        return
    try:
        rec = {"event": event, "ts": time.time(), **payload}
        with open(_TRACE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────
# Lazy client
# ──────────────────────────────────────────────────────────────

_client = None


def _get_client():
    global _client
    if _client is None:
        from memexa.core.hindsight_client import HindsightHttpClient
        _client = HindsightHttpClient()
    return _client


# ──────────────────────────────────────────────────────────────
# Internal: recall with defaults + post-filter
# ──────────────────────────────────────────────────────────────

def _post_filter(
    items: List[Dict[str, Any]],
    *,
    salience_min: Optional[float] = None,
    tier_in: Optional[Sequence[int]] = None,
    types_any: Optional[Sequence[str]] = None,
    source_in: Optional[Sequence[str]] = None,
    speaker_role_in: Optional[Sequence[str]] = None,
    schema_v_only: Optional[int] = None,
    exclude_invalidated: bool = True,
) -> List[Dict[str, Any]]:
    """Win 端 metadata 过滤 (Hindsight multi-tag OR 弱点的补强).

    每个过滤维度都从 metadata 字段读取 (因为 metadata 是 ASCII-only 严格的).

    2026-05-08 (CEO): schema_v_only default None (was 1) — v5 cards have
    schema_v=2, legacy cards have schema_v missing/0; default=1 was dropping
    EVERYTHING. Tag-level filter in _recall_raw already segregates by bank.
    """
    out: List[Dict[str, Any]] = []
    for it in items:
        md = it.get("metadata") or {}
        tags = it.get("tags") or []

        # schema 隔离 (only when caller explicitly requests a specific schema)
        if schema_v_only is not None:
            try:
                if int(md.get("schema_v", "0")) != schema_v_only:
                    continue
            except (ValueError, TypeError):
                continue

        # salience 阈值
        if salience_min is not None:
            try:
                sal = float(md.get("salience", "0"))
            except (ValueError, TypeError):
                sal = 0.0
            if sal < salience_min:
                continue

        # tier 过滤
        if tier_in is not None:
            try:
                tier = int(md.get("room_tier", "0"))
            except (ValueError, TypeError):
                tier = 0
            if tier not in tier_in:
                continue

        # types 过滤 (any 匹配 types_csv 中任一)
        if types_any is not None:
            types_csv = md.get("types_csv", "")
            card_types = set(t.strip() for t in types_csv.split(",") if t)
            if not (set(types_any) & card_types):
                continue

        # source 过滤
        if source_in is not None:
            if md.get("source", "") not in source_in:
                continue

        # speaker_role 过滤
        if speaker_role_in is not None:
            if md.get("speaker_role", "") not in speaker_role_in:
                continue

        out.append(it)

    if exclude_invalidated:
        out = _exclude_invalidated(out)

    return out


def _exclude_invalidated(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Layer 4 invalidation filter (tombstone-by-tag).

    Reuse memexa.core.invalidate.list_tombstoned() to identify chunk_ids
    that have been tombstoned; drop those from results.
    """
    try:
        from memexa.core.invalidate import list_tombstoned
        tombstoned = list_tombstoned()
    except Exception as e:
        logger.debug("invalidation filter fail-open: %s", e)
        return items

    out = []
    for it in items:
        chunk_id = it.get("chunk_id") or it.get("id") or ""
        if chunk_id and chunk_id in tombstoned:
            _emit_trace("memory_query_invalidated_filtered",
                        {"chunk_id": chunk_id})
            continue
        out.append(it)
    return out


def _recall_raw(
    query: str,
    *,
    tags: Optional[Sequence[str]] = None,
    bank: Optional[str] = None,
    budget: str = "low",
    max_tokens: int = 4096,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Direct Hindsight recall call with primary→fallback HA route.

    Routing (CEO 2026-05-07 directive — Win read-replica HA):
      1. Primary: MEMEXA_HINDSIGHT_URL (default Mac Tailscale 127.0.0.1:8888)
      2. Fallback: MEMEXA_HINDSIGHT_FALLBACK_URL (Win local 127.0.0.1:8888 when set)
      3. Both fail → empty result + warn

    Used by quick/reflect/timeline/person/project/pending internally.
    """
    import os as _os
    bank_id = bank or DEFAULT_BANK
    # 2026-05-08: legacy `memory_full` cards have NO kind:event/schema tag
    # (verified via /memories/list — most have ['probe:retain_workaround_ascii']
    # or empty tags). Forcing kind:event+schema:v1 filter drops 127→17 results
    # (-87%). For legacy bank skip tag filter; for v5/v3 keep schema-aware tags.
    if bank_id == "memory_full_v5":
        base_tags = [DEFAULT_KIND_EVENT_TAG, "schema:v2"]
    elif bank_id == "memory_full_v3":
        base_tags = [DEFAULT_KIND_EVENT_TAG, "schema:v1"]
    elif bank_id == LEGACY_BANK:
        base_tags = []  # legacy lacks tags; filter would drop most data
    else:
        base_tags = [DEFAULT_KIND_EVENT_TAG, DEFAULT_SCHEMA_TAG]
    all_tags = list(base_tags) + list(tags or [])

    body = {"query": query, "budget": budget, "max_tokens": max_tokens}
    if all_tags:
        body["tags"] = all_tags

    primary_url = _os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
    fallback_url = _os.environ.get("MEMEXA_HINDSIGHT_FALLBACK_URL", "")
    # 2026-05-11: default 60s → 15s. Under calendar_daemon fan-out (22 calls)
    # a single 60s hang blocks the whole tick. With graceful-degraded
    # hindsight (TEI fast-fail patch) recall returns ≤8s on the reranker-down
    # path, so 15s leaves margin for legit slow queries on the topk paths.
    primary_timeout = timeout if timeout else float(
        _os.environ.get("MEMEXA_HINDSIGHT_PRIMARY_TIMEOUT", "15.0"))
    fallback_timeout = float(
        _os.environ.get("MEMEXA_HINDSIGHT_FALLBACK_TIMEOUT", "10.0"))

    routes = [("primary", primary_url, primary_timeout)]
    if fallback_url:
        routes.append(("fallback", fallback_url, fallback_timeout))

    last_err: Optional[str] = None
    for label, url, t in routes:
        try:
            import httpx as _httpx  # lazy
            with _httpx.Client(base_url=url, timeout=t) as http:
                r = http.post(
                    f"/v1/default/banks/{bank_id}/memories/recall", json=body)
                # 2026-05-11: 4xx is schema/payload error — retrying same body
                # against fallback is pointless and just doubles wall time.
                # Surface as fast as 5xx server faults.
                if 400 <= r.status_code < 500:
                    last_err = (f"{label}={url}: HTTP {r.status_code} "
                                f"(client error, no retry)")
                    _emit_trace("memory_query_recall_4xx",
                                {"query": query[:80], "route": label,
                                 "status": r.status_code,
                                 "body": (r.text or "")[:200]})
                    logger.warning("memory_query recall %s 4xx: %s %s",
                                   label, r.status_code, (r.text or "")[:120])
                    return {"results": [], "error": last_err}
                r.raise_for_status()
                result = r.json()
                if label == "fallback":
                    _emit_trace("memory_query_recall_fallback_used",
                                {"query": query[:80], "url": url})
                    logger.warning("recall served by fallback %s", url)
                return result
        except Exception as e:
            last_err = f"{label}={url}: {str(e)[:120]}"
            _emit_trace("memory_query_recall_fail",
                        {"query": query[:80], "route": label,
                         "url": url, "error": str(e)[:200]})
            logger.warning("memory_query recall %s fail: %s", label, e)
            continue

    return {"results": [], "error": last_err or "all routes failed"}


# ──────────────────────────────────────────────────────────────
# Public API 1: quick — 快速 fact 召回 (replaces query_entity)
# ──────────────────────────────────────────────────────────────

def quick(
    query: str,
    *,
    source: Optional[Union[str, Sequence[str]]] = None,
    types: Optional[Sequence[str]] = None,
    tier_in: Optional[Sequence[int]] = None,
    salience_min: float = DEFAULT_SALIENCE_FLOOR,
    max_k: int = 20,
    include_legacy: bool = True,
    budget: str = "low",
) -> List[Dict[str, Any]]:
    """快速 fact 召回 + Win 端过滤.

    Args:
        query: 自然语言查询
        source: 限定 source ("wechat" | "claude_session" | ...) 单值或列表
        types: 限定 types_csv 中含任一 ("commitment" | "announcement" | ...)
        tier_in: 限定 room_tier (e.g. [1, 2] 只查私聊+班级)
        salience_min: 最低 salience (默认 0.3, SessionStart 用 0.6)
        max_k: 最大返回数
        include_legacy: True (default 2026-05-08, cutover-pending) → UNION
                        v5 + legacy memory_full results.  After cutover GA,
                        flip back to False.  Fix: previously REPLACE bug.
        budget: hindsight recall budget; "low"=fast/9s, "mid"=12s, "high"=20s.
                (Hindsight enum: low/mid/high — NOT "medium".)

    Returns:
        List of recall result items, each含 {id, text, metadata, tags, ...}
    """
    source_in = [source] if isinstance(source, str) else (
        list(source) if source else None)

    # 2026-05-10: identity manifest 集成. 如果 query 是已知 entity, 用 tag-based
    # 精准 filter (canon:<id>) 走 hindsight tag-OR 路径; 同时 BGE 召回保留 fallback.
    canon_tags = None
    try:
        from memexa.core import identity_resolver as _idr
        canon_tags = _idr.tag_filter_for(query)
    except Exception:
        pass

    raw_v5 = _recall_raw(
        query, max_tokens=max_k * 256, budget=budget,
    )
    items = list(raw_v5.get("results") or [])

    # 加 canon tag filter 的二轮召回 (跨 alias 命中)
    if canon_tags:
        try:
            raw_canon = _recall_raw(
                query, max_tokens=max_k * 256, budget=budget,
                tags=canon_tags,
            )
            items.extend(raw_canon.get("results") or [])
        except Exception:
            pass

    if include_legacy:
        try:
            raw_leg = _recall_raw(
                query, bank=LEGACY_BANK,
                max_tokens=max_k * 256, budget=budget,
            )
            items.extend(raw_leg.get("results") or [])
        except Exception:
            pass  # legacy unreachable → still return v5 results

    # Dedupe by id (cards present in both banks)
    seen: Dict[str, Dict[str, Any]] = {}
    for it in items:
        key = it.get("id") or it.get("memory_id") or it.get("text", "")[:60]
        if key not in seen:
            seen[key] = it
    items = list(seen.values())

    filtered = _post_filter(
        items,
        salience_min=salience_min,
        tier_in=tier_in,
        types_any=types,
        source_in=source_in,
    )
    _emit_trace("memory_query_quick",
                {"query": query[:80], "raw_n": len(items),
                 "filtered_n": len(filtered),
                 "source_in": source_in, "types": list(types or [])})
    return filtered[:max_k]


# ──────────────────────────────────────────────────────────────
# Public API 2: reflect — LLM 综合答案
# ──────────────────────────────────────────────────────────────

def reflect(
    query: str,
    *,
    layers: Sequence[str] = ("event", "article"),
    include_facts: bool = True,
    budget: str = "mid",
    max_tokens: int = 2048,
) -> Dict[str, Any]:
    """Hindsight reflect endpoint, daemon LLM 综合答案.

    Args:
        layers: ("event",) / ("article",) / ("event","article")
        include_facts: 是否带回 facts 列表
        budget: low | mid | high
        max_tokens: LLM 输出上限

    Returns:
        {text, based_on, structured_output, usage}
    """
    client = _get_client()
    tags = [DEFAULT_SCHEMA_TAG]
    for l in layers:
        if l == "event":
            tags.append(DEFAULT_KIND_EVENT_TAG)
        elif l == "article":
            tags.append(DEFAULT_KIND_ARTICLE_TAG)
    body = {
        "query": query,
        "tags": tags,
        "budget": budget,
        "max_tokens": max_tokens,
        "include_facts": include_facts,
    }
    try:
        r = client._http.post(
            f"/v1/default/banks/{DEFAULT_BANK}/reflect", json=body, timeout=120)
        r.raise_for_status()
        result = r.json()
        _emit_trace("memory_query_reflect",
                    {"query": query[:80], "layers": list(layers),
                     "tokens": (result.get("usage") or {}).get("total_tokens")})
        return result
    except Exception as e:
        _emit_trace("memory_query_reflect_fail",
                    {"query": query[:80], "error": str(e)[:200]})
        logger.warning("reflect fail: %s", e)
        return {"text": "", "error": str(e)[:200]}


# ──────────────────────────────────────────────────────────────
# Public API 3: timeline — 时序聚合
# ──────────────────────────────────────────────────────────────

def timeline(
    date_range: Tuple[str, str],
    *,
    room: Optional[str] = None,
    source: Optional[str] = None,
    max_k: int = 50,
) -> List[Dict[str, Any]]:
    """时序聚合 (按 metadata.when_start 排序).

    Args:
        date_range: ("2026-04-26", "2026-05-06") ISO date strings
        room: chat_room hash 或 display_name (会算 hash 比对)
        source: 限定 source
    """
    import concurrent.futures as _cf
    start_iso, end_iso = date_range

    extra_tags: List[str] = []
    if source:
        extra_tags.append(f"source:{source}")
    if room:
        if len(room) == 32 and all(c in "0123456789abcdef" for c in room):
            extra_tags.append(f"room:{room[:16]}")
        else:
            from memexa.memory_card import chat_room_hash
            extra_tags.append(f"room:{chat_room_hash(room)[:16]}")

    # 2026-05-12: fix 0-recall bug. Old impl sent single semantic query
    # "events between X and Y" — near-zero embedding similarity to actual
    # card content. Replace with broad fan-out + Win-side when_start filter.
    variants = [
        "事件 消息 通知 announcement",
        "对话 message conversation chat",
        "重要 commitment decision important",
        "邮件 email 来信 回复",
        start_iso,
        end_iso,
    ]
    if source:
        variants.append(f"{source} 最近 recent")

    def _one(q: str) -> List[Dict[str, Any]]:
        try:
            raw = _recall_raw(q, tags=extra_tags, max_tokens=8000, timeout=20.0)
            return raw.get("results") or []
        except Exception:
            return []

    seen: Dict[str, Dict[str, Any]] = {}
    with _cf.ThreadPoolExecutor(max_workers=4) as ex:
        for items in ex.map(_one, variants):
            for it in items:
                cid = it.get("id")
                if not cid or cid in seen:
                    continue
                md = it.get("metadata") or {}
                ws = md.get("when_start", "") or ""
                if not ws:
                    continue
                if start_iso <= ws[:10] <= end_iso:
                    seen[cid] = it

    out = sorted(seen.values(),
                 key=lambda x: (x.get("metadata") or {}).get("when_start", ""))
    return out[:max_k]


# ──────────────────────────────────────────────────────────────
# Public API 4: person — L1 article + recent L0 events
# ──────────────────────────────────────────────────────────────

def person(canonical_name: str, *, recent_events: int = 5) -> Dict[str, Any]:
    """身份锚定查询: L1 article (latest version) + 最近 N L0 events.

    Returns:
        {
          "article": str | None  (L1 markdown narrative if exists),
          "article_version": int | None,
          "recent_events": List[Dict],
          "first_seen": str | None,
          "last_seen": str | None,
        }
    """
    # L1: kind:article + entity:<name>
    article_raw = _recall_raw(
        f"about {canonical_name}",
        tags=[f"entity:{canonical_name}"],
        max_tokens=4096,
    )
    # 同时查 article (kind:article)
    article_extra = _recall_raw(
        canonical_name,
        tags=[DEFAULT_KIND_ARTICLE_TAG, f"entity:{canonical_name}"],
        max_tokens=4096,
    )
    article_items = (article_extra.get("results") or [])
    article_text = None
    article_version = None
    if article_items:
        latest = max(article_items,
                     key=lambda x: int((x.get("metadata") or {})
                                        .get("version", "0") or "0"))
        article_text = latest.get("text", "")
        try:
            article_version = int((latest.get("metadata") or {}).get("version", "0"))
        except (ValueError, TypeError):
            article_version = 0

    # L0 events
    events_raw = _recall_raw(
        canonical_name, max_tokens=recent_events * 300)
    events_filt = _post_filter(events_raw.get("results") or [],
                                salience_min=0.0)  # don't drop by salience for person view
    events_filt.sort(
        key=lambda x: (x.get("metadata") or {}).get("when_start", ""),
        reverse=True)

    first_seen = None
    last_seen = None
    if events_filt:
        all_when = [(it.get("metadata") or {}).get("when_start", "")
                    for it in events_filt
                    if (it.get("metadata") or {}).get("when_start")]
        if all_when:
            first_seen = min(all_when)
            last_seen = max(all_when)

    return {
        "canonical_name": canonical_name,
        "article": article_text,
        "article_version": article_version,
        "recent_events": events_filt[:recent_events],
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


# ──────────────────────────────────────────────────────────────
# Public API 5: project — 跨 source 项目视图
# ──────────────────────────────────────────────────────────────

def project(
    topic: str,
    *,
    sources: Sequence[str] = ("wechat", "claude_session", "git_commit", "email"),
    days: int = 30,
) -> Dict[str, Any]:
    """跨 source 项目视图.

    聚合一个 topic (e.g. "<topic-3> 论文") 在多个 source 中的活动.

    Returns:
        {
          "article": str | None  (L1 topic article if exists),
          "by_source": {source: [events]},
          "all_events_chronological": [events],
        }
    """
    article_raw = _recall_raw(
        f"about project {topic}",
        tags=[DEFAULT_KIND_ARTICLE_TAG, f"topic:{topic}"],
        max_tokens=4096,
    )
    article_items = article_raw.get("results") or []
    article_text = None
    if article_items:
        latest = max(article_items,
                     key=lambda x: int((x.get("metadata") or {})
                                        .get("version", "0") or "0"))
        article_text = latest.get("text", "")

    by_source: Dict[str, List[Dict]] = {}
    all_events = []
    for src in sources:
        evs = quick(topic, source=src, salience_min=0.4, max_k=10)
        by_source[src] = evs
        all_events.extend(evs)

    all_events.sort(
        key=lambda x: (x.get("metadata") or {}).get("when_start", ""))

    return {
        "topic": topic,
        "article": article_text,
        "by_source": by_source,
        "all_events_chronological": all_events,
    }


# ──────────────────────────────────────────────────────────────
# Public API 6: pending — status=pending 的承诺/问询
# ──────────────────────────────────────────────────────────────

def pending(
    *,
    types_any: Sequence[str] = ("commitment", "question"),
    max_k: int = 20,
) -> List[Dict[str, Any]]:
    """Active pending commitments — authoritative source is calendar_index.json
    (calendar_daemon's chat-extracted projection), not graph recall.

    2026-05-12 rewrite: old impl used semantic recall on "pending commitments
    and questions" which only matched cards literally mentioning those words
    (~1 noise card). The real user-facing pending TODOs live in
    data/calendar_planning/calendar_index.json with status=active.
    """
    out: List[Dict[str, Any]] = []
    try:
        idx_path = (Path(__file__).resolve().parents[2] /
                    "data" / "calendar_planning" / "calendar_index.json")
        if idx_path.exists():
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            for cid, c in data.items():
                if (c or {}).get("status") != "active":
                    continue
                due = c.get("due_iso") or ""
                out.append({
                    "id": cid,
                    "text": c.get("summary") or "",
                    "metadata": {
                        "source": "calendar_commitment",
                        "when_start": due,
                        "salience": c.get("salience_max", 0.5),
                        "types_csv": "commitment",
                        "status": "active",
                        "due_iso": due,
                        "confidence": c.get("confidence"),
                        "actor": c.get("actor"),
                        "sources_origin": ",".join(c.get("sources") or []),
                    },
                    "tags": ["status:active", "kind:commitment"],
                    "score": c.get("salience_max", 0.5),
                })
    except Exception as e:
        logger.warning("pending: calendar_index read failed: %s", e)

    # Soonest due first, ties broken by salience desc
    out.sort(key=lambda x: (
        (x.get("metadata") or {}).get("due_iso", "9999"),
        -((x.get("metadata") or {}).get("salience", 0) or 0),
    ))
    return out[:max_k]


# ──────────────────────────────────────────────────────────────
# Public API 7 (bonus): session_start_context — 默认 hook 用
# ──────────────────────────────────────────────────────────────

def session_start_context(*, max_recent: int = 15) -> Dict[str, Any]:
    """为 SessionStart hook 准备的默认 context bundle.

    2026-05-08 (CEO directive): v5-only default; v3 archived. Legacy
    memory_full bank is opt-in via include_legacy=True at quick() level.

    Returns:
        {
          "recent_high_salience": [events],     # last 7d, salience >= floor
          "pending": [events],                   # commitment/question pending
          "bank": "memory_full_v5",
          "schema": "v2",
        }
    """
    import datetime as dt
    now = dt.datetime.utcnow()
    seven_d = (now - dt.timedelta(days=7)).strftime("%Y-%m-%d")

    recent = quick(
        "recent activity",
        salience_min=SESSION_LOAD_SALIENCE_FLOOR,
        max_k=max_recent,
    )

    # Filter by 7d window
    filtered = [
        it for it in recent
        if not ((it.get("metadata") or {}).get("when_start", "") or "")
        or ((it.get("metadata") or {}).get("when_start", "") or "") >= seven_d
    ]
    filtered.sort(
        key=lambda it: -float((it.get("metadata") or {}).get("salience", 0) or 0),
    )

    pend = pending(max_k=10)
    return {
        "recent_high_salience": filtered[:max_recent],
        "pending": pend,
        "bank": DEFAULT_BANK,
        "schema": DEFAULT_SCHEMA_TAG,
    }


# ──────────────────────────────────────────────────────────────
# Public API 7b: topic — multi-variant fan-out for "tell me about X" questions
# ──────────────────────────────────────────────────────────────

# Default intent variants for topic-style queries. The pattern came from the
# 2026-05-08 mac_purchase_query baseline (11 variants × 2 banks → 212 cards),
# which empirically beat single-variant quick() by ~20× on recall surface.
# Variants cover: surface form / negation / comparison / process / source / outcome.
_TOPIC_VARIANT_TEMPLATES = [
    "{topic}",
    "{topic} 购买 价格",
    "{topic} 商家 渠道",
    "{topic} 没买 没下单",
    "{topic} 退货 退款",
    "{topic} 选 配置",
    "{topic} 决定 决策",
    "{topic} 流程 过程",
    "{topic} 问题 困难",
    "{topic} 联系人 老师",
    "考虑 {topic}",
]


def topic(
    topic: str,
    *,
    variants: Optional[Sequence[str]] = None,
    max_cards: int = 80,
    salience_min: float = 0.0,
    budget: str = "low",
    include_legacy: bool = True,
    chronological: bool = True,
) -> List[Dict[str, Any]]:
    """多变体 fan-out 召回 (replaces ad-hoc 11-variant scripts).

    与 quick() 区别: quick 发 1 query, topic 发 N variants × M banks 并 union dedup.
    与 arc() 区别: arc 是 entity/relationship 视角 (8 个 intent variants),
    topic 是 topic/事件 视角 (11 个 default variants 覆盖买卖决策流程等).

    Args:
        topic: 主题关键词 (e.g. "Mac Studio" / "项目经费报销" / "PRL 投稿")
        variants: 自定义查询变体列表; None 则用 _TOPIC_VARIANT_TEMPLATES
        max_cards: 去重后最大返回数
        salience_min: salience 阈值 (默认 0.0 即不过滤,因 fan-out 已收敛)
        budget: hindsight budget low/mid/high
        include_legacy: 同时查 memory_full bank
        chronological: True 按 when_start 升序; False 按 salience 降序

    Returns:
        List of recall cards; each含 {id, text, metadata, tags, score, ...}
    """
    import concurrent.futures as _cf
    qs = list(variants) if variants else [
        v.format(topic=topic) for v in _TOPIC_VARIANT_TEMPLATES
    ]
    banks = [DEFAULT_BANK]
    if include_legacy:
        banks.append(LEGACY_BANK)

    # Parallel fan-out: 11 variants × 2 banks = 22 calls. Sequential at
    # ~3s/call = 60s+ wall; parallel-8 caps at ~9s wall. Hindsight HTTP is
    # I/O-bound so threads beat processes here.
    tasks: List[Tuple[str, str]] = [(q, b) for q in qs for b in banks]
    seen: Dict[str, Dict[str, Any]] = {}
    n_calls = 0
    n_failed = 0

    # Per-call max_tokens stays small (each variant returns ~5-10 cards;
    # union does the aggregation). 256 tokens/card × 8 cards/variant = 2048.
    # Was max_cards*256 = 20480 — caused 60s+ timeouts on legacy bank scan.
    per_call_tokens = 2048
    # FIX 2026-05-09 (CEO LIVE measurement):
    #   Old defaults (legacy_timeout=15, max_workers=8) gave 36% v5 failure
    #   rate AND 100% legacy timeout rate on warm Mac. Real measurements:
    #   - legacy single recall = 12s typical → 15s budget left ~3s margin → fail
    #   - hindsight uvicorn workers=1 → 8 concurrent saturates → ServerDisconnect
    # FIX 2026-05-10 (Mac restart LIVE 反复 reranker 500):
    #   reranker (BGE-Reranker-v2-m3 on Metal MPS) 单实例锁, 4 并发仍触发
    #   "Server disconnected" + CLOSE_WAIT 堆积. 降到 2 并发 = 串行化 22 个
    #   variants 大约 ~60-90s wall, 但 ≥99% 成功率. ROI: 比 1× 30s × 失败重跑
    #   的总时间还短 (失败率高时 retry 成本爆炸).
    legacy_timeout = 45.0
    v5_timeout = 30.0

    def _one(qb: Tuple[str, str]):
        q, bank = qb
        try:
            t = legacy_timeout if bank == LEGACY_BANK else v5_timeout
            raw = _recall_raw(q, bank=bank, max_tokens=per_call_tokens,
                               budget=budget, timeout=t)
            return (q, bank, raw.get("results") or [], None)
        except Exception as e:
            return (q, bank, None, str(e)[:120])

    with _cf.ThreadPoolExecutor(max_workers=min(2, len(tasks))) as pool:
        for q, bank, results, err in pool.map(_one, tasks):
            if err is not None:
                n_failed += 1
                continue
            n_calls += 1
            for it in (results or []):
                key = it.get("id") or it.get("memory_id") or \
                    (it.get("text", "") or "")[:60]
                if key and key not in seen:
                    it.setdefault("query_match", q)
                    it.setdefault("bank_hit", bank)
                    seen[key] = it

    items = list(seen.values())

    if salience_min > 0:
        items = _post_filter(items, salience_min=salience_min)

    if chronological:
        items.sort(key=lambda x: (x.get("metadata") or {}).get("when_start", "")
                    or (x.get("metadata") or {}).get("when_end", ""))
    else:
        items.sort(key=lambda x: -float(
            (x.get("metadata") or {}).get("salience", "0") or 0))

    _emit_trace("memory_query_topic",
                {"topic": topic[:80], "n_variants": len(qs),
                 "n_calls": n_calls, "n_failed": n_failed,
                 "n_unique": len(items)})
    return items[:max_cards]


# ──────────────────────────────────────────────────────────────
# Public API 8: arc — chronological multi-query union for relationship/topic
# ──────────────────────────────────────────────────────────────

def arc(
    entity: str,
    *,
    max_cards: int = 80,
    budget: str = "low",
    include_legacy: bool = True,
) -> List[Tuple[str, str, str, str, str]]:
    """Pull cards mentioning `entity` across multiple intent variants and
    parse MEMORY_CARD_V2 envelopes for narratives.  Returns chronologically
    sorted (oldest first) tuples ready for "how did X evolve over time"
    questions.

    Why:
      A single `quick(entity)` returns ~20 cards but misses different
      semantic angles (first met / conflict / planning).  This helper fires
      a fixed set of intent-tagged queries (presence/relation/conflict/...)
      then unions + dedupes + chrono-sorts.

    Args:
      entity:        surface form (e.g. "Bob") OR canonical id.
      max_cards:     post-dedup ceiling.
      budget:        hindsight budget passed to each underlying recall.
      include_legacy: also UNION memory_full bank (legacy 8751 nodes).

    Returns:
      List[(ts, source, where_chat_room, speakers, narrative)] sorted by ts.
    """
    import re as _re

    # Intent-tagged variants to maximize recall surface
    variants = [
        f"{entity}",
        f"{entity} 第一次",
        f"{entity} 认识 介绍",
        f"{entity} 计划 见面",
        f"{entity} 思念 想念",
        f"{entity} 吵架 分手",
        f"Alice {entity} 朋友",
        f"{entity} 关系 相处",
    ]

    def _extract(text: str) -> Tuple[str, dict]:
        """Pull narrative + metadata from V2 envelope; fallback to raw."""
        if "MEMORYCARD_V2_HEADER_BEGIN" in text:
            m = _re.search(r"\u3011\s*({.+})\s*\u3010", text, _re.S)
            if m:
                try:
                    obj = json.loads(m.group(1))
                    speakers = ",".join(
                        (e.get("canonical_name") or "")
                        for e in (obj.get("entities") or [])
                    )
                    return (
                        obj.get("narrative", ""),
                        {
                            "speakers": speakers,
                            "where": obj.get("where_chat_room", ""),
                            "when_start": obj.get("when_start", ""),
                        },
                    )
                except Exception:
                    pass
        return text[:400], {}

    seen: Dict[str, Tuple[str, str, str, str, str]] = {}

    for variant in variants:
        banks = [DEFAULT_BANK]
        if include_legacy:
            banks.append(LEGACY_BANK)

        for bank in banks:
            try:
                raw = _recall_raw(
                    variant,
                    bank=bank,
                    max_tokens=max_cards * 256,
                    budget=budget,
                )
            except Exception:
                continue

            for r in (raw.get("results") or []):
                rid = r.get("id") or r.get("memory_id") or ""
                if not rid or rid in seen:
                    continue
                txt = r.get("text") or r.get("content") or r.get("narrative") or ""
                narrative, meta = _extract(txt)
                # Only keep if entity surface form actually appears
                if entity not in narrative and entity not in meta.get("speakers", ""):
                    continue
                md = r.get("metadata") or {}
                ts = (
                    meta.get("when_start")
                    or md.get("when_start", "")
                    or ""
                )[:19]
                src = md.get("source", "?")
                where = meta.get("where", "?")
                speakers = meta.get("speakers", "")
                seen[rid] = (ts, src, where, speakers, narrative)

    # Sort by timestamp ascending; missing ts goes to end
    out = sorted(
        seen.values(),
        key=lambda x: x[0] or "9999",
    )[:max_cards]
    return out


# ──────────────────────────────────────────────────────────────
# Advanced query layer (2026-05-13 product-grade) — exploit V2 envelope
# fields + graph links beyond the "primary" 9 subcmds.
# ──────────────────────────────────────────────────────────────

def types_query(
    type_filter: Sequence[str],
    *,
    status: Optional[str] = None,
    source: Optional[str] = None,
    days: int = 14,
    max_k: int = 50,
    salience_min: float = 0.0,
) -> List[Dict[str, Any]]:
    """Recall cards filtered by types_csv field.

    Exploits V2 envelope `types_csv` = "commitment,announcement,question" etc.
    User question example: "我所有未回复的 question" / "本周的 commitment"
    """
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    type_to_kws = {
        "commitment": ["承诺 commitment deadline due", "需要 完成 任务 提交"],
        "question": ["问题 不确定 question doubt", "为什么 怎么办 是否 能不能"],
        "announcement": ["通知 announcement 公告", "广播 发布 公示"],
        "decision": ["决定 decision 选择 拍板", "确认 同意 否决"],
        "state": ["状态 进展 status state", "正在 已经 还没"],
    }
    queries: List[str] = []
    for t in type_filter:
        queries.extend(type_to_kws.get(t.lower(), [t]))
    if source:
        queries = [f"{q} {source}" for q in queries]
    import concurrent.futures as _cf
    # 2026-05-13 v2: prior bug — `max_tokens=max_k*200` capped recall at 1200
    # tokens when max_k=6 (CLI default), returning 0 even when ≥5 real
    # commitments existed. Use FIXED 8000 token recall budget (matches arc /
    # topic). Post-filter narrows. Net: types --filter commitment --days 60
    # max-k=6 now returns matches instead of 0.
    all_items: Dict[str, Dict[str, Any]] = {}
    with _cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_recall_raw, q, budget="low",
                          max_tokens=8000): q for q in queries}
        for fut in _cf.as_completed(futs):
            try:
                r = fut.result()
                for it in r.get("results") or []:
                    key = it.get("id") or it.get("text", "")[:60]
                    if key not in all_items:
                        all_items[key] = it
            except Exception:
                continue
    type_set = set(t.lower() for t in type_filter)
    out: List[Dict[str, Any]] = []
    for it in all_items.values():
        md = it.get("metadata") or {}
        tcsv = (md.get("types_csv") or "").lower()
        if not any(t in tcsv for t in type_set):
            continue
        sal = md.get("salience")
        try:
            sal_f = float(sal) if sal is not None else 0.0
            if sal_f < salience_min:
                continue
        except (TypeError, ValueError):
            pass
        if source:
            src = (md.get("source") or "").lower()
            if src != source.lower():
                continue
        ws = md.get("when_start")
        if ws and isinstance(ws, str):
            try:
                wt = datetime.fromisoformat(ws.replace("Z", "+00:00"))
                if wt < start or wt > end + timedelta(days=1):
                    continue
            except (ValueError, TypeError):
                pass
        if status:
            st = (md.get("status") or "").lower()
            if status.lower() not in st:
                continue
        out.append(it)
    out.sort(key=lambda x: (x.get("metadata") or {}).get("when_start") or "",
              reverse=True)
    _emit_trace("memory_query_types",
                {"type_filter": list(type_filter), "n_results": len(out),
                 "days": days, "status": status, "source": source})
    return out[:max_k]


def graph_walk(
    entity: str,
    *,
    depth: int = 2,
    max_per_hop: int = 20,
    salience_min: float = 0.4,
) -> Dict[str, Any]:
    """Multi-hop graph traversal — find entities connected to X.

    User question: "X 的关联人物有谁/X 和谁经常一起出现".
    Walk: X → cards mentioning X → other entities → recurse.
    """
    from collections import Counter
    visited_cards: set = set()
    visited_entities: set = {entity.lower().strip()}
    frontier = {entity}
    hops_data: List[Dict[str, Any]] = []

    for hop in range(1, depth + 1):
        next_frontier: set = set()
        entity_counter: Counter = Counter()
        hop_cards_n = 0
        for ent in list(frontier):
            cards = quick(ent, salience_min=salience_min,
                           max_k=max_per_hop, include_legacy=False)
            for c in cards:
                cid = c.get("id") or c.get("text", "")[:60]
                if cid in visited_cards:
                    continue
                visited_cards.add(cid)
                hop_cards_n += 1
                text = c.get("text", "") or ""
                if "【MEMORYCARD_V2_HEADER_BEGIN】" in text:
                    try:
                        body = text.split("】", 1)[1] if "】" in text else text
                        body = body.split("【MEMORYCARD_V2_HEADER_END】", 1)[0]
                        env = json.loads(body)
                        for e in env.get("entities") or []:
                            name = (e.get("canonical_name") or
                                     e.get("surface_form") or "").strip()
                            if name and name.lower() not in visited_entities:
                                entity_counter[name] += 1
                    except Exception:
                        pass
        top_at_hop: List[Dict[str, Any]] = []
        for name, cnt in entity_counter.most_common(20):
            if name.lower() in visited_entities:
                continue
            top_at_hop.append({"name": name, "count": cnt})
            visited_entities.add(name.lower())
            next_frontier.add(name)
        hops_data.append({"depth": hop, "entities": top_at_hop,
                          "n_cards_inspected": hop_cards_n})
        frontier = next_frontier
        if not frontier:
            break
    _emit_trace("memory_query_graph_walk",
                {"root": entity, "depth": depth,
                 "total_entities": sum(len(h["entities"]) for h in hops_data),
                 "total_cards": len(visited_cards)})
    return {"root": entity, "hops": hops_data,
            "total_cards_seen": len(visited_cards)}


def summary(
    window_days: int = 7,
    *,
    source: Optional[str] = None,
    topic_hint: Optional[str] = None,
    budget: str = "mid",
) -> Dict[str, Any]:
    """LLM-synthesized summary — recall recent cards, your-org API synthesis.

    User question: "本周我做了什么 / 上个月学到了什么".
    Bypasses hindsight /reflect (daemon has no LLM provider set 2026-05-13);
    uses your-org qwen3.6-chat for client-side synthesis from top-N recent cards.
    """
    from datetime import datetime, timedelta, timezone

    # Step 1: pull recent cards via timeline
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=window_days)
    start_iso = start_dt.date().isoformat()
    end_iso = end_dt.date().isoformat()

    cards = timeline((start_iso, end_iso), source=source, max_k=100)
    if not cards:
        return {"text": "(no cards in window)", "error": None,
                "n_input_cards": 0}

    # Step 2: extract narrative + metadata for LLM context
    bullets: List[str] = []
    for c in cards[:60]:
        md = c.get("metadata") or {}
        ws = (md.get("when_start") or "")[:16]
        src = md.get("source", "?")
        text = c.get("text", "") or ""
        # parse V2 envelope narrative
        if "【MEMORYCARD_V2_HEADER_BEGIN】" in text:
            try:
                body = text.split("】", 1)[1].split(
                    "【MEMORYCARD_V2_HEADER_END】", 1)[0]
                env = json.loads(body)
                nar = env.get("narrative") or env.get("summary") or text[:120]
            except Exception:
                nar = text[:200]
        else:
            nar = text[:200]
        bullets.append(f"[{ws}|{src}] {nar[:200]}")

    sys_prompt = (
        "You are a concise memory synthesis assistant. Given timestamped "
        "narrative fragments from the user's personal memory bank, produce a "
        "structured Chinese summary covering: 重要事件 (events), 决策/承诺 "
        "(decisions/commitments), 未解决问题 (open questions), 学习/进展 "
        "(learning). Cite when_start (YYYY-MM-DD) for each item. Use bullet "
        "points. Maximum 400 words."
    )
    user_q = (
        f"窗口: {start_iso} → {end_iso} (过去 {window_days} 天)\n"
    )
    if source:
        user_q += f"仅 source={source}\n"
    if topic_hint:
        user_q += f"聚焦主题: {topic_hint}\n"
    user_q += f"\n卡片 narrative ({len(bullets)} 条):\n" + "\n".join(bullets)

    try:
        from memexa.extraction.ustc_llm_client import get_client as _gc
        client = _gc()
        # Use gatekeeper model (qwen3.6-chat, fast non-reasoner)
        import os as _os
        chat_model = _os.environ.get("MEMEXA_your-org_GATEKEEPER_MODEL", "qwen3.6-chat")
        resp = client.chat(model=chat_model, system=sys_prompt, user=user_q,
                            max_tokens=2048, temperature=0.3,
                            label=f"summary_{window_days}d")
        if not resp.get("ok"):
            return {"text": "", "error": resp.get("error", "synthesis failed"),
                    "n_input_cards": len(cards)}
        text = resp.get("content", "")
        _emit_trace("memory_query_summary",
                    {"window_days": window_days, "source": source,
                     "n_input_cards": len(cards), "ok": True})
        return {"text": text, "error": None,
                "n_input_cards": len(cards),
                "usage": resp.get("usage", {})}
    except Exception as e:
        _emit_trace("memory_query_summary_fail",
                    {"error": str(e)[:200]})
        return {"text": "", "error": f"{type(e).__name__}: {e}",
                "n_input_cards": len(cards)}


def trends(
    by: str = "source",
    *,
    window_days: int = 30,
    top_n: int = 15,
    salience_min: float = 0.0,
) -> Dict[str, Any]:
    """Aggregate-stat trends: who/where/what dominates a time window.

    User questions:
      "本月我跟谁交流最多" → by=sender_wxid_hash
      "本月最活跃的群" → by=where_chat_room_hash
      "本月哪些 source 流量最大" → by=source
      "本月主要话题类型" → by=types_csv

    Args:
        by: 'source' | 'sender' | 'sender_wxid_hash' | 'room' |
            'where_chat_room_hash' | 'types_csv' | 'room_tier'
        window_days: lookback window
        top_n: top-N to return
        salience_min: filter by min salience
    """
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=window_days)
    cards = timeline((start_dt.date().isoformat(),
                       end_dt.date().isoformat()), max_k=500)

    # Map shorthand to metadata field
    field_map = {
        "source": "source", "sender": "sender_wxid_hash",
        "sender_wxid_hash": "sender_wxid_hash",
        "room": "where_chat_room_hash",
        "where_chat_room_hash": "where_chat_room_hash",
        "types_csv": "types_csv", "tier": "room_tier",
        "room_tier": "room_tier",
    }
    field = field_map.get(by, by)

    counter: Counter = Counter()
    cards_inspected = 0
    for c in cards:
        md = c.get("metadata") or {}
        sal = md.get("salience")
        try:
            if sal is not None and float(sal) < salience_min:
                continue
        except (TypeError, ValueError):
            pass
        cards_inspected += 1
        v = md.get(field)
        if v is None:
            continue
        # For types_csv, split into individual types
        if field == "types_csv" and isinstance(v, str):
            for t in v.split(","):
                t = t.strip()
                if t:
                    counter[t] += 1
        else:
            counter[str(v)[:60]] += 1

    top = counter.most_common(top_n)
    _emit_trace("memory_query_trends",
                {"by": by, "field": field, "window_days": window_days,
                 "cards_inspected": cards_inspected, "n_buckets": len(counter)})
    return {"by": by, "field": field, "window_days": window_days,
            "cards_inspected": cards_inspected,
            "total_buckets": len(counter),
            "top": [{"key": k, "count": n} for k, n in top]}


def cross_source(
    query: str,
    *,
    sources: Optional[Sequence[str]] = None,
    max_per_source: int = 5,
    days: int = 30,
) -> Dict[str, Any]:
    """Same query across 6 sources, show coverage matrix.

    User question: "X 这件事真在做还是只是说？" → 多源同时出现 = 高 confidence.

    Args:
        query: the topic to search
        sources: subset of source names (default all 6)
        max_per_source: cards per source
        days: time window
    """
    from datetime import datetime, timedelta, timezone

    all_sources = sources or ("wechat", "qq", "email", "browser_session",
                                "claude_code", "audio")
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    by_source: Dict[str, List[Dict[str, Any]]] = {}
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(quick, query, source=src, max_k=max_per_source,
                            include_legacy=False, salience_min=0.0): src
                for src in all_sources}
        for fut in _cf.as_completed(futs):
            src = futs[fut]
            try:
                cards = fut.result() or []
                # Filter by date window
                kept = []
                for c in cards:
                    md = c.get("metadata") or {}
                    ws = md.get("when_start")
                    if ws and isinstance(ws, str):
                        try:
                            wt = datetime.fromisoformat(ws.replace("Z", "+00:00"))
                            if wt < start_dt:
                                continue
                        except (ValueError, TypeError):
                            pass
                    kept.append(c)
                by_source[src] = kept
            except Exception as e:
                by_source[src] = [{"error": str(e)[:120]}]

    # Compute corroboration: sources where query hit
    sources_with_hits = [s for s, cards in by_source.items()
                          if cards and not (len(cards) == 1 and "error" in cards[0])]
    confidence = (
        "high" if len(sources_with_hits) >= 3 else
        "medium" if len(sources_with_hits) == 2 else
        "low" if len(sources_with_hits) == 1 else
        "absent"
    )
    total_cards = sum(len(c) for c in by_source.values())

    _emit_trace("memory_query_cross_source",
                {"query": query[:60], "sources_with_hits": sources_with_hits,
                 "confidence": confidence, "total_cards": total_cards})
    return {"query": query, "by_source": by_source,
            "sources_with_hits": sources_with_hits,
            "confidence": confidence, "total_cards": total_cards,
            "days": days}


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def _cli(argv: List[str]) -> int:
    import argparse
    # 2026-05-12: Windows GBK 控制台撞 emoji (e.g. 📅 U+1F4C5 from calendar_index.json
    # synthesized cards in pending v2) 直接 UnicodeEncodeError. 强制 UTF-8 输出.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="memexa.core.memory_query")
    # 2026-05-16 v0.1.x: --json output mode lets AI agents (Claude Code,
    # Cursor, Cline) parse memexa output via json.loads() instead of
    # text. Subprocess CLI is the current first-class agent path;
    # native MCP server lands in v0.5.
    p.add_argument("--json", action="store_true",
                   help="emit raw result as JSON array/object for agent parsing")
    sub = p.add_subparsers(dest="cmd", required=True)

    pq = sub.add_parser("quick")
    pq.add_argument("query")
    pq.add_argument("--source")
    pq.add_argument("--types", nargs="*")
    pq.add_argument("--tier", type=int, nargs="*")
    pq.add_argument("--salience", type=float, default=DEFAULT_SALIENCE_FLOOR)
    pq.add_argument("--max-k", type=int, default=10)
    pq.add_argument("--legacy", action="store_true",
                    help="also query memory_full (schema:v0)")
    pq.add_argument("--show-raw", action="store_true",
                    help="按 batch_id 反查 raw input batch 显示原始 messages")
    pq.add_argument("--raw-max", type=int, default=5,
                    help="--show-raw 时最多 attach 几张 cards 的原文 (默认 5)")

    pr = sub.add_parser("reflect")
    pr.add_argument("query")
    pr.add_argument("--budget", default="mid")
    pr.add_argument("--no-articles", action="store_true")

    pt = sub.add_parser("timeline")
    pt.add_argument("--start", required=True)
    pt.add_argument("--end", required=True)
    pt.add_argument("--room")
    pt.add_argument("--source")

    pp = sub.add_parser("person")
    pp.add_argument("name")

    ppr = sub.add_parser("project")
    ppr.add_argument("topic")

    psu = sub.add_parser("pending")

    pss = sub.add_parser("session-context")

    # 2026-05-08: 'topic' subcommand — multi-variant fan-out for "tell me
    # about X" questions. Empirically beats single-variant quick() by ~20×
    # on recall surface (verified via mac_purchase_query baseline: 212 cards
    # vs 1 from quick).
    pt2 = sub.add_parser(
        "topic",
        help="topic-style multi-variant fan-out (replaces ad-hoc N-query scripts)",
    )
    pt2.add_argument("topic", help="主题关键词 (e.g. 'Mac Studio' / 'PRL 投稿')")
    pt2.add_argument("--variants", nargs="*",
                     help="自定义变体列表; 缺省用内置 11 变体模板")
    pt2.add_argument("--max-cards", type=int, default=80)
    pt2.add_argument("--salience", type=float, default=0.0)
    pt2.add_argument("--budget", default="low",
                     help="hindsight budget low/mid/high")
    pt2.add_argument("--no-legacy", action="store_true")
    pt2.add_argument("--by-salience", action="store_true",
                     help="按 salience 降序 (默认按 when_start 升序)")
    pt2.add_argument("--show-raw", action="store_true",
                     help="按 batch_id 反查 raw input batch 显示原文")
    pt2.add_argument("--raw-max", type=int, default=5)

    # 2026-05-08: 'arc' subcommand — multi-query union + V2 envelope narrative
    # parse + chronological sort. Replaces ad-hoc python scripts for asking
    # "tell me how X evolved over time".
    pa = sub.add_parser(
        "arc",
        help="relationship/topic chronological arc (multi-query union)",
    )
    pa.add_argument("entity", help="canonical name or surface form (e.g. Bob)")
    pa.add_argument("--max-cards", type=int, default=80)
    pa.add_argument("--budget", default="low",
                    help="hindsight budget low/mid/high")
    pa.add_argument("--no-legacy", action="store_true")
    pa.add_argument("--show-raw", action="store_true",
                    help="按 batch_id 反查 raw input batch 显示原文")
    pa.add_argument("--raw-max", type=int, default=5)

    # 2026-05-13 product-grade: advanced query layer
    ptypes = sub.add_parser(
        "types",
        help="filter by types_csv (commitment/question/announcement/decision/state)",
    )
    ptypes.add_argument("--filter", required=True, nargs="+",
                         help="任一类型匹配 (e.g. commitment question)")
    ptypes.add_argument("--status",
                         help="可选 status filter (pending/resolved)")
    ptypes.add_argument("--source",
                         help="可选 source filter (wechat/qq/audio/email/cc/browser)")
    ptypes.add_argument("--days", type=int, default=14)
    ptypes.add_argument("--max-k", type=int, default=50)
    ptypes.add_argument("--salience", type=float, default=0.0)

    pgw = sub.add_parser(
        "graph-walk",
        help="multi-hop 关系网络 (X → 关联人/物/事 → 再关联)",
    )
    pgw.add_argument("entity")
    pgw.add_argument("--depth", type=int, default=2)
    pgw.add_argument("--max-per-hop", type=int, default=20)
    pgw.add_argument("--salience", type=float, default=0.4)

    psum = sub.add_parser(
        "summary",
        help="LLM 综合时段总结 (本周做了什么 / 上月学了什么)",
    )
    psum.add_argument("--window-days", type=int, default=7)
    psum.add_argument("--source",
                       help="可选 source filter")
    psum.add_argument("--topic-hint",
                       help="可选 topic 关键词聚焦")
    psum.add_argument("--budget", default="mid")

    ptr = sub.add_parser(
        "trends",
        help="按 sender/source/room/types 聚合统计 (本月谁找我最多)",
    )
    ptr.add_argument("--by", default="source",
                      choices=["source", "sender", "sender_wxid_hash",
                               "room", "where_chat_room_hash",
                               "types_csv", "tier", "room_tier"],
                      help="聚合维度")
    ptr.add_argument("--window-days", type=int, default=30)
    ptr.add_argument("--top-n", type=int, default=15)
    ptr.add_argument("--salience", type=float, default=0.0)

    pcs = sub.add_parser(
        "cross-source",
        help="同一 query 在 6 source 的覆盖度 (X 是真做还是只说)",
    )
    pcs.add_argument("query")
    pcs.add_argument("--max-per-source", type=int, default=5)
    pcs.add_argument("--days", type=int, default=30)
    pcs.add_argument("--sources", nargs="*",
                      help="子集 (default all 6)")

    args = p.parse_args(argv[1:])
    _t_start = time.time()
    _n_results = 0
    _ok = True
    _err: Optional[str] = None
    try:
        # 2026-05-16 v0.1.x: --json mode short-circuits text rendering.
        # Same call surface, raw return value as JSON to stdout. Agents
        # invoking memexa via subprocess CLI should pass --json.
        if args.json:
            if args.cmd == "quick":
                _res = quick(args.query, source=args.source, types=args.types,
                             tier_in=args.tier, salience_min=args.salience,
                             max_k=args.max_k, include_legacy=args.legacy)
            elif args.cmd == "reflect":
                _res = reflect(args.query, budget=args.budget,
                               layers=("event",) if args.no_articles
                                       else ("event", "article"))
            elif args.cmd == "timeline":
                _res = timeline((args.start, args.end), room=args.room,
                                source=args.source)
            elif args.cmd == "person":
                _res = person(args.name)
            elif args.cmd == "project":
                _res = project(args.topic)
            elif args.cmd == "pending":
                _res = pending()
            elif args.cmd == "session-context":
                _res = session_start_context()
            elif args.cmd == "arc":
                _res = arc(args.entity, max_cards=args.max_cards,
                           budget=args.budget,
                           include_legacy=not args.no_legacy)
            elif args.cmd == "topic":
                _res = topic(args.topic, variants=args.variants,
                             max_cards=args.max_cards,
                             salience_min=args.salience,
                             budget=args.budget,
                             include_legacy=not args.no_legacy,
                             chronological=not args.by_salience)
            elif args.cmd == "types":
                _res = types_query(args.filter, status=args.status,
                                   source=args.source, days=args.days,
                                   max_k=args.max_k,
                                   salience_min=args.salience)
            elif args.cmd == "graph-walk":
                _res = graph_walk(args.entity, depth=args.depth,
                                  max_per_hop=args.max_per_hop,
                                  salience_min=args.salience)
            elif args.cmd == "summary":
                _res = summary(window_days=args.window_days,
                               source=args.source,
                               topic_hint=args.topic_hint,
                               budget=args.budget)
            elif args.cmd == "trends":
                _res = trends(by=args.by, window_days=args.window_days,
                              top_n=args.top_n,
                              salience_min=args.salience)
            elif args.cmd == "cross-source":
                _res = cross_source(args.query, sources=args.sources,
                                    max_per_source=args.max_per_source,
                                    days=args.days)
            else:
                _res = {"error": f"unknown subcommand: {args.cmd}"}
            # Count for the query log (best-effort across heterogeneous
            # return shapes).
            if isinstance(_res, list):
                _n_results = len(_res)
            elif isinstance(_res, dict):
                _n_results = (
                    len(_res.get("recent_events") or [])
                    or len(_res.get("hops") or [])
                    or len(_res.get("top") or [])
                    or sum(len(v) for v in (_res.get("by_source") or {}).values()
                           if isinstance(v, list))
                    or (1 if _res.get("text") else 0)
                )
            print(json.dumps(_res, ensure_ascii=False, default=str))
            return 0
        # Below: original text-rendering path, unchanged when --json
        # is not set.
        if args.cmd == "quick":
            res = quick(args.query, source=args.source, types=args.types,
                         tier_in=args.tier, salience_min=args.salience,
                         max_k=args.max_k, include_legacy=args.legacy)
            _n_results = len(res)
            if getattr(args, "show_raw", False):
                attach_raw(res, max_attach=args.raw_max)
            for r in res:
                md = r.get("metadata") or {}
                # 2026-05-13: legacy MEMORYCARD_V2_HEADER_BEGIN cards inline JSON;
                # new cards have plain narrative text. Display narrative either way.
                raw_text = (r.get('text','') or '')
                narrative = raw_text
                if '【MEMORYCARD_V2_HEADER_BEGIN】' in raw_text:
                    try:
                        import json as _j
                        body = raw_text.split('】', 1)[1] if '】' in raw_text else raw_text
                        body = body.split('【MEMORYCARD_V2_HEADER_END】', 1)[0]
                        parsed = _j.loads(body)
                        narrative = parsed.get('narrative') or parsed.get('summary') or body[:200]
                    except Exception:
                        narrative = raw_text[:200]
                print(f"[{md.get('source','?'):10s}] sal={md.get('salience','?'):>5} "
                      f"types={md.get('types_csv','?')[:24]:24s} "
                      f"{narrative[:180]}")
                if r.get("raw_batch"):
                    rb = r["raw_batch"]
                    print(f"  RAW from {rb.get('dir')}/{(rb.get('source_kind') or '?')}: "
                          f"{(rb.get('chat_room') or '')[:30]}")
                    msgs = rb.get("messages") or []
                    for m in msgs[:8]:
                        # 2026-05-13: some wechat raw batches have non-dict
                        # message entries (legacy malformed data: float/int/str).
                        # Skip rather than crash when .get() doesn't exist.
                        if not isinstance(m, dict):
                            print(f"    [malformed msg, type={type(m).__name__}] {repr(m)[:80]}")
                            continue
                        # 2026-05-13: m['ts'] may be float (epoch) instead of ISO str;
                        # m['sender']/m['content'] may also be non-str. Coerce.
                        ts_s = str(m.get('ts') or '')[:16]
                        sd_s = str(m.get('sender') or '?')[:8]
                        ct_s = str(m.get('content') or '')[:140]
                        print(f"    [{ts_s}] {sd_s:8s}: {ct_s}")
                    if len(msgs) > 8:
                        print(f"    ... +{len(msgs)-8} more messages in raw")
                    if rb.get("prompt_text"):
                        print(f"    PROMPT (email/browser raw): {rb['prompt_text'][:300]}")
            print(f"\nN={len(res)}")
        elif args.cmd == "reflect":
            r = reflect(args.query, budget=args.budget,
                         layers=("event",) if args.no_articles
                                 else ("event", "article"))
            _n_results = 1 if r.get("text") else 0
            print(r.get("text", "(empty)"))
        elif args.cmd == "timeline":
            res = timeline((args.start, args.end), room=args.room,
                            source=args.source)
            _n_results = len(res)
            for r in res:
                md = r.get("metadata") or {}
                print(f"{md.get('when_start','?')[:19]} "
                      f"[{md.get('source','?')}] "
                      f"{(r.get('text','') or '')[:100]}")
        elif args.cmd == "person":
            r = person(args.name)
            _n_results = len(r.get("recent_events") or [])
            print(f"=== {args.name} ===")
            print(f"first_seen: {r['first_seen']}")
            print(f"last_seen:  {r['last_seen']}")
            if r['article']:
                print(f"\n--- L1 article (v{r['article_version']}) ---")
                print(r['article'][:500])
            print(f"\n--- recent events ({len(r['recent_events'])}) ---")
            for ev in r['recent_events']:
                md = ev.get("metadata") or {}
                print(f"  {md.get('when_start','?')[:19]} "
                      f"{(ev.get('text','') or '')[:100]}")
        elif args.cmd == "project":
            r = project(args.topic)
            _n_results = sum(len(v) for v in (r.get('by_source') or {}).values())
            for src, evs in r['by_source'].items():
                print(f"--- {src} ({len(evs)}) ---")
                for ev in evs[:3]:
                    print(f"  {(ev.get('text','') or '')[:100]}")
        elif args.cmd == "pending":
            res = pending()
            _n_results = len(res)
            print(f"PENDING ({len(res)}):")
            for r in res:
                md = r.get("metadata") or {}
                print(f"  [{md.get('types_csv','?')}] "
                      f"{(r.get('text','') or '')[:120]}")
        elif args.cmd == "session-context":
            r = session_start_context()
            _n_results = len(r.get('recent_high_salience') or []) + len(r.get('pending') or [])
            print(json.dumps({
                "recent_n": len(r['recent_high_salience']),
                "pending_n": len(r['pending']),
                "bank": r['bank'],
            }, indent=2))
            for it in r['recent_high_salience'][:5]:
                md = it.get("metadata") or {}
                print(f"  [{md.get('source','?')}] sal={md.get('salience')} "
                      f"{(it.get('text','') or '')[:100]}")
        elif args.cmd == "arc":
            res = arc(
                args.entity,
                max_cards=args.max_cards,
                budget=args.budget,
                include_legacy=not args.no_legacy,
            )
            _n_results = len(res)
            for ts, src, where, speakers, narrative in res:
                print(f"[{ts}] src={src} where={where[:30]}")
                if speakers:
                    print(f"  speakers: {speakers[:80]}")
                print(f"  {narrative[:400]}")
                print()
            print(f"\nN={len(res)} cards")
            if getattr(args, "show_raw", False):
                # arc 返 tuple, 没 metadata.batch_id 直接可见; 仅在 quick/topic 用 --show-raw 较稳
                print("(--show-raw 在 arc 命令下当前不支持; 用 quick/topic 替代)")
        elif args.cmd == "topic":
            res = topic(
                args.topic,
                variants=args.variants,
                max_cards=args.max_cards,
                salience_min=args.salience,
                budget=args.budget,
                include_legacy=not args.no_legacy,
                chronological=not args.by_salience,
            )
            _n_results = len(res)
            if getattr(args, "show_raw", False):
                attach_raw(res, max_attach=args.raw_max)
            for r in res:
                md = r.get("metadata") or {}
                ts = (md.get("when_start", "") or md.get("when_end", "") or "?")[:19]
                src = md.get("source", "?")
                sal = md.get("salience", "?")
                qm = r.get("query_match", "?")
                txt = (r.get("text", "") or "")[:200].replace("\n", " ")
                print(f"[{ts}] src={src:14s} sal={sal} via='{qm[:24]:24s}' {txt}")
                if r.get("raw_batch"):
                    rb = r["raw_batch"]
                    msgs = rb.get("messages") or []
                    print(f"  RAW [{rb.get('source_kind','?')}] "
                          f"{(rb.get('chat_room') or '')[:30]} ({len(msgs)} msgs):")
                    for m in msgs[:6]:
                        print(f"    [{(m.get('ts') or '')[:16]}] "
                              f"{(m.get('sender') or '?'):8s}: "
                              f"{(m.get('content') or '')[:130]}")
                    if rb.get("prompt_text"):
                        print(f"    PROMPT_RAW: {rb['prompt_text'][:200]}")
            print(f"\nN={len(res)} cards (topic={args.topic!r})")
        elif args.cmd == "types":
            res = types_query(
                args.filter, status=args.status, source=args.source,
                days=args.days, max_k=args.max_k,
                salience_min=args.salience,
            )
            _n_results = len(res)
            print(f"TYPES filter={args.filter} status={args.status} "
                  f"source={args.source} days={args.days} N={len(res)}:")
            for r in res:
                md = r.get("metadata") or {}
                ts = (md.get("when_start", "") or "?")[:19]
                src = md.get("source", "?")
                tcsv = (md.get("types_csv", "") or "")[:30]
                sal = md.get("salience", "?")
                txt = (r.get("text", "") or "")[:160].replace("\n", " ")
                # parse V2 envelope narrative for display
                if "【MEMORYCARD_V2_HEADER_BEGIN】" in txt:
                    try:
                        import json as _j
                        body = (r.get("text","") or "").split("】", 1)[1].split(
                            "【MEMORYCARD_V2_HEADER_END】", 1)[0]
                        parsed = _j.loads(body)
                        txt = (parsed.get("narrative") or
                               parsed.get("summary") or txt)[:160]
                    except Exception:
                        pass
                print(f"  [{ts}] {src:10s} sal={sal} types={tcsv:30s} {txt}")
        elif args.cmd == "graph-walk":
            r = graph_walk(args.entity, depth=args.depth,
                            max_per_hop=args.max_per_hop,
                            salience_min=args.salience)
            _n_results = sum(len(h["entities"]) for h in r["hops"])
            print(f"=== GRAPH-WALK root={r['root']!r} depth={args.depth} ===")
            print(f"total_cards_seen: {r['total_cards_seen']}")
            for h in r["hops"]:
                print(f"\n-- hop {h['depth']} ({len(h['entities'])} entities, "
                      f"{h['n_cards_inspected']} cards) --")
                for e in h["entities"][:15]:
                    print(f"  {e['count']:>3}× {e['name']}")
        elif args.cmd == "summary":
            r = summary(window_days=args.window_days, source=args.source,
                         topic_hint=args.topic_hint, budget=args.budget)
            _n_results = 1 if r.get("text") else 0
            print(f"=== SUMMARY window={args.window_days}d "
                  f"source={args.source} topic={args.topic_hint} ===\n")
            print(r.get("text", "(empty)"))
            usage = r.get("usage") or {}
            if usage:
                print(f"\n[reflect tokens: in={usage.get('prompt_tokens','?')} "
                      f"out={usage.get('completion_tokens','?')}]")
        elif args.cmd == "trends":
            r = trends(by=args.by, window_days=args.window_days,
                        top_n=args.top_n, salience_min=args.salience)
            _n_results = len(r["top"])
            print(f"=== TRENDS by={args.by} (field={r['field']}) "
                  f"window={args.window_days}d ===")
            print(f"cards_inspected={r['cards_inspected']} "
                  f"total_buckets={r['total_buckets']}\n")
            for i, item in enumerate(r["top"], 1):
                key = item["key"][:50]
                bar = "█" * min(40, item["count"])
                print(f"  {i:>2}. {item['count']:>4}× {bar} {key}")
        elif args.cmd == "cross-source":
            r = cross_source(args.query, sources=args.sources,
                              max_per_source=args.max_per_source,
                              days=args.days)
            _n_results = r["total_cards"]
            print(f"=== CROSS-SOURCE query={args.query!r} days={args.days} ===")
            print(f"confidence: {r['confidence']} "
                  f"(hits in {len(r['sources_with_hits'])} sources: "
                  f"{', '.join(r['sources_with_hits'])})")
            print(f"total_cards: {r['total_cards']}\n")
            for src, cards in r["by_source"].items():
                if not cards or (len(cards) == 1 and "error" in cards[0]):
                    print(f"  [{src:14s}] (no hits)")
                    continue
                print(f"  [{src:14s}] {len(cards)} cards:")
                for c in cards[:3]:
                    md = c.get("metadata") or {}
                    ws = (md.get("when_start", "") or "?")[:19]
                    txt = (c.get("text", "") or "")[:120].replace("\n", " ")
                    # parse narrative if envelope
                    if "【MEMORYCARD_V2_HEADER_BEGIN】" in txt:
                        try:
                            import json as _j
                            body = (c.get("text","") or "").split("】", 1)[1].split(
                                "【MEMORYCARD_V2_HEADER_END】", 1)[0]
                            parsed = _j.loads(body)
                            txt = (parsed.get("narrative") or txt)[:120]
                        except Exception:
                            pass
                    print(f"      [{ws}] {txt}")
        return 0
    except Exception as e:
        _ok = False
        _err = f"{type(e).__name__}: {e}"
        import traceback
        traceback.print_exc()
        return 2
    finally:
        # 2026-05-12: always log invocation regardless of outcome
        try:
            _query_field = (
                getattr(args, "query", None)
                or getattr(args, "topic", None)
                or getattr(args, "name", None)
                or getattr(args, "entity", None)
                or ""
            )
            _params = {k: v for k, v in vars(args).items()
                       if k != "cmd" and v is not None and v != ""}
            _append_query_log({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(_t_start)),
                "subcmd": args.cmd,
                "query": _query_field,
                "params": _params,
                "n_results": _n_results,
                "latency_ms": int((time.time() - _t_start) * 1000),
                "ok": _ok,
                "error": _err,
            })
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv))
