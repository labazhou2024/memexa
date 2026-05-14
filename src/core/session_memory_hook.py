"""SessionStart memory hook (Phase 2.4, 2026-05-06).

Replaces grep-based memory/*.md loading with graph-backed recall.

Flow:
  1. (skip Tier-0 static files; loaded by CLAUDE.md auto-memory section)
  2. quick("recent activity", salience>=0.6, max_k=15) — high-signal events
  3. pending() — outstanding commitments/questions
  4. Print as <system-reminder> context bundle for Claude SessionStart

Token budget: ~6k tokens total injected.

Triggered by:
  .claude/config/settings.json hooks.SessionStart entry
  command: python memex/memex/core/session_memory_hook.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure utf-8 stdout
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Add memex root to sys.path so `import memex.*` works regardless of cwd
_MEMEX_ROOT = Path(__file__).resolve().parents[2]
if str(_MEMEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEMEX_ROOT))


def _format_event_brief(it: Dict[str, Any]) -> str:
    md = it.get("metadata") or {}
    text = (it.get("text", "") or "")[:240]
    where_part = ""
    # daemon-rewritten text usually contains "| When: ... | Involving: ..."
    # so we mostly extract from text
    return f"  [tier{md.get('room_tier','?')}|{md.get('source','?')}|sal{md.get('salience','?')}] {text}"


def _kick_legacy_warmup() -> None:
    """Fire-and-forget warmup of legacy memory_full bank.

    2026-05-08 (CEO): session_start_context only queries v5 → legacy bank
    PG cache + MPS embeddings stay cold. First user query against legacy
    pays full 20s+ cold-start. This thread fires a tiny dummy recall so
    BGE-M3 + reranker tensors are pre-loaded by the time user sends a prompt.
    """
    try:
        from src.core.memory_query import _recall_raw
        # Cheap query: budget=low + tiny tokens; we discard result.
        _recall_raw("warmup", bank="memory_full", max_tokens=512, budget="low")
    except Exception:
        pass  # silent; warmup is best-effort


def main() -> int:
    # Pre-warm legacy bank in background BEFORE primary recall, so the
    # PG/embedding/reranker caches finish loading concurrently with v5.
    import threading
    _t_warm = threading.Thread(target=_kick_legacy_warmup, daemon=True)
    _t_warm.start()

    # Lazy-import in case Hindsight unavailable; never block session start
    try:
        from src.core.memory_query import session_start_context
        ctx = session_start_context(max_recent=12)
    except Exception as e:
        # Fail-open: emit minimal context, never block
        print(f"<!-- session_memory_hook fallback (memory_query unavailable: {type(e).__name__}: {str(e)[:120]}) -->")
        _emit_query_cheatsheet()
        return 0

    recent = ctx.get("recent_high_salience", [])
    pend = ctx.get("pending", [])

    print("<!-- session_memory_hook P2.4 -->")
    print(f"<!-- bank: {ctx.get('bank')} schema: {ctx.get('schema')} -->")
    print()
    print("===== 近 7 天高显著性事件 (graph-recall, salience≥0.4) =====")
    if recent:
        for it in recent:
            print(_format_event_brief(it))
    else:
        print("  (无)")
    print()
    print("===== 待办承诺/未回应问询 (status=pending) =====")
    if pend:
        for it in pend[:6]:
            md = it.get("metadata") or {}
            text = (it.get("text", "") or "")[:200]
            print(f"  [{md.get('types_csv','?')[:30]}] {text}")
    else:
        print("  (无)")
    print()
    _emit_query_cheatsheet()
    return 0


def _emit_query_cheatsheet() -> None:
    """选 query 类型决定召回质量. 错选 → 1 条乱码; 选对 → 80–200+ cards.

    2026-05-08 post-mortem: graph_memory_v2 query (single-variant + legacy
    schema) recalls 1 garbage card on 'Mac Studio purchase' while
    memory_query topic (11-variant fan-out) recalls 200+. CLAUDE.md §7.1
    + this cheatsheet are the two SessionStart-loaded surfaces telling
    future-Claude to choose right.
    """
    print("===== 选对查询命令 (2026-05-08 升级) =====")
    print("用户问 X 的全过程/演变/购买/项目  → memory_query topic 'X'      (11 变体, 80–200+ cards)")
    print("用户问 X 是谁/X 干了什么            → memory_query quick 'X'      (单 query 点查)")
    print("用户问 X 和 Y 的关系/认识过程       → memory_query arc 'X'        (8 关系 variants)")
    print("用户问 X 这段时间发生了什么         → memory_query timeline --start --end")
    print("用户问 X 项目跨 source 视图         → memory_query project 'X'")
    print("用户问 Y 老师/同学最近说什么        → memory_query person 'Y'")
    print("用户问我有哪些待办                  → memory_query pending")
    print()
    print("❌ DEPRECATED: graph_memory_v2 query  (2026-05-08, CLI 会 stderr banner)")
    print("❌ DEPRECATED: graph_memory query     (2026-04-26)")
    print("❌ FORBIDDEN:  grep memory/*.md       (跳过图查询)")


if __name__ == "__main__":
    sys.exit(main())
