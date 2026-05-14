"""
L7 SessionStart Transparency Dashboard (Phase 5, 2026-04-18)

Read-only. Renders ≤15-line brief shown at session start so the CEO knows the
current self-evolution state at a glance. Silent failure (never blocks hook).

Sections:
  - L1/L2 捕获: 今日捕获数、7d 累积、L2 Haiku 调用数 + 成本
  - L4 语义 KB: 覆盖率、索引状态、不可用降级标志
  - L5 pattern 健康: 总数、活跃/死库存、待 prune
  - L6 prompt 进化: 距下次触发 N session / 上次进化 agent+版本
  - L7 Env flag 状态: 哪些层开/关
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

__all__ = ["render_brief", "render_full"]


_DATA_DIR = Path(__file__).parent.parent / "data"
_PATTERNS_FILE = _DATA_DIR / "improvement_patterns.jsonl"
_BUDGET_FILE = _DATA_DIR / "haiku_budget_usage.json"
_SEMANTIC_UNAVAIL = _DATA_DIR / "embeddings" / "semantic_unavailable.flag"
_EMB_META = _DATA_DIR / "embeddings" / "patterns_meta.json"
_PRIMED_LOG = _DATA_DIR / "primed_patterns_session.jsonl"


def _safe_load_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default if default is not None else {}


def _count_recent_primed(hours: int = 24) -> int:
    """How many prime events in last N hours.

    [LOGIC-LOW Round1 fix 2026-04-18] Bounded tail scan — only read last
    2000 lines to prevent O(N) startup latency as log grows.
    """
    if not _PRIMED_LOG.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    count = 0
    try:
        with open(_PRIMED_LOG, "r", encoding="utf-8", errors="replace") as f:
            # Read last 2000 lines only (bounded scan)
            tail = f.readlines()[-2000:]
        for line in tail:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                # P2 (2026-04-23): use centralized event_ts utility
                from src.core._hook_utils import event_ts
                ts = event_ts(ev)
                if not ts:
                    continue
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t >= cutoff:
                    count += 1
            except Exception:
                continue
    except Exception:
        pass
    return count


def _pattern_health() -> dict:
    """Stats on pattern KB."""
    stats = {"total": 0, "active_7d": 0, "never_used": 0, "auto_generated": 0}
    if not _PATTERNS_FILE.exists():
        return stats
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        with open(_PATTERNS_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    p = json.loads(line)
                    stats["total"] += 1
                    if p.get("usage_count", 0) == 0:
                        stats["never_used"] += 1
                    if p.get("auto_generated"):
                        stats["auto_generated"] += 1
                    lp = p.get("last_primed")
                    if lp:
                        try:
                            t = datetime.fromisoformat(lp.replace("Z", "+00:00"))
                            if t.tzinfo is None:
                                t = t.replace(tzinfo=timezone.utc)
                            if t >= cutoff:
                                stats["active_7d"] += 1
                        except Exception:
                            pass
                except Exception:
                    continue
    except Exception:
        pass
    return stats


def _haiku_cost_today() -> float:
    data = _safe_load_json(_BUDGET_FILE, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return float(data.get(today, 0.0))


def _env_flag(name: str, default: str) -> str:
    val = os.environ.get(name, default)
    return "ON" if val == "1" else "OFF"


def _semantic_status() -> str:
    if _SEMANTIC_UNAVAIL.exists():
        return "DEGRADED (offline)"
    meta = _safe_load_json(_EMB_META, {})
    count = meta.get("count", 0)
    if count == 0:
        return "uninitialized"
    return f"{count} patterns"


def _l6_status() -> str:
    """Prompt evolution trigger status, from evolution_trigger.get_counter_status.

    [B4 fix 2026-04-18] Surfaces in_progress (stuck claim), eligible agents
    and last evolve results so the CEO can diagnose without reading harness_state.
    """
    try:
        from src.core.evolution_trigger import get_counter_status
        status = get_counter_status()
        count = status.get("count", 0)
        threshold = status.get("threshold", 10)
        remaining = status.get("sessions_until_trigger", threshold)
        enabled = status.get("enabled", False)
        pending = status.get("pending_approval", False)
        in_progress = status.get("in_progress", False)
        eligible = status.get("eligible_agents") or []
        last_results = status.get("last_results") or []

        parts = [f"{count}/{threshold} sessions"]
        if in_progress:
            since = status.get("in_progress_since", "")[:19]
            parts.append(f"IN-PROGRESS since {since}")
        if pending:
            who = ",".join(eligible) if eligible else "?"
            parts.append(f"approval pending ({who})")
        if not enabled:
            parts.append("disabled")
        elif remaining == 0 and not in_progress and not pending:
            parts.append("READY to trigger")
        elif not pending and not in_progress:
            parts.append(f"{remaining} to go")
        if last_results:
            # Compact last-results summary: "code-reviewer=no_data,fix-agent=rejected"
            brief = ",".join(
                f"{r.get('agent','?')}={(r.get('status','?') or '').split(':',1)[0]}"
                for r in last_results[:3]
            )
            parts.append(f"last: {brief}")
        return " | ".join(parts)
    except Exception:
        return "unavailable"


def render_brief() -> str:
    """Render compact dashboard. Returns multi-line string (≤15 lines)."""
    try:
        health = _pattern_health()
        primed_24h = _count_recent_primed(hours=24)
        cost_today = _haiku_cost_today()
        sem = _semantic_status()
        l6 = _l6_status()

        lines = [
            "=== Self-Evolution Dashboard ===",
            f"Pattern KB: {health['total']} total | "
            f"{health['active_7d']} active (7d) | "
            f"{health['never_used']} never used",
            f"Last 24h: {primed_24h} prime events | "
            f"Haiku cost today: ${cost_today:.3f}",
            f"Semantic index (L4): {sem}",
            f"Prompt evolution (L6): {l6}",
            f"Layers: L1=ON  L2={_env_flag('MEMEXA_L2_SOFT_SIGNAL', '1')}  "
            f"L3={_env_flag('MEMEXA_L3_REFLECTION', '1')}  "
            f"L4={_env_flag('MEMEXA_L4_SEMANTIC_KB', '1')}  "
            f"L6={_env_flag('MEMEXA_L6_EVOLUTION', '0')}  "
            f"L7={_env_flag('MEMEXA_L7_DASHBOARD', '1')}",
            "================================",
        ]
        return "\n".join(lines)
    except Exception:
        return ""  # silent degrade — never block SessionStart


def _l3_status() -> str:
    """[P4 2026-04-18] Status of L3 reflections: recent log count + last verdict."""
    logs_dir = _DATA_DIR / "logs"
    if not logs_dir.exists():
        return "L3: no reflections yet"
    try:
        refl_logs = sorted(logs_dir.glob("reflection_*.log"))
        if not refl_logs:
            return "L3: no reflections yet"
        total = len(refl_logs)
        # Peek most-recent for last-line verdict
        last_log = refl_logs[-1]
        try:
            content = last_log.read_text(encoding="utf-8", errors="replace")
            tail = content.strip().splitlines()[-1] if content.strip() else "(empty)"
        except Exception:
            tail = "(read failed)"
        return f"L3: {total} reflections | last: {tail[:80]}"
    except Exception:
        return "L3: unavailable"


def _outcome_status() -> str:
    """[P4 2026-04-18] Status of B3 outcome capture: records by agent_role."""
    kf_file = _DATA_DIR / "kairos_feedback.jsonl"
    if not kf_file.exists():
        return "B3: no kairos_feedback.jsonl yet"
    try:
        from collections import Counter
        counts = Counter()
        posthook = 0
        with open(kf_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[-5000:]:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    counts[r.get("agent_role", "?")] += 1
                    if r.get("source") == "postooluse_hook":
                        posthook += 1
                except Exception:
                    continue
        top = counts.most_common(3)
        top_str = " ".join(f"{k}={v}" for k, v in top)
        return f"B3: {posthook} post-hook records | top roles: {top_str or '(none)'}"
    except Exception:
        return "B3: unavailable"


def render_full() -> str:
    """More detailed version for CLI use.

    [P4 2026-04-18] Appends L3 reflection status + B3 outcome-capture status.
    """
    brief = render_brief()
    extras = []
    try:
        health = _pattern_health()
        extras.append(
            f"Auto-generated patterns: {health['auto_generated']}/"
            f"{health['total']} ({100*health['auto_generated']/max(1,health['total']):.0f}%)"
        )
        # P4: L3 + B3 visibility
        extras.append(_l3_status())
        extras.append(_outcome_status())

        budget_data = _safe_load_json(_BUDGET_FILE, {})
        if budget_data:
            week = sorted(budget_data.items(), reverse=True)[:7]
            extras.append("Haiku spend 7d: " + " ".join(
                f"{d[-5:]}=${v:.3f}" for d, v in week
            ))
    except Exception:
        pass
    if extras:
        return brief + "\n" + "\n".join(extras)
    return brief


def is_enabled() -> bool:
    return os.environ.get("MEMEXA_L7_DASHBOARD", "1") == "1"


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "full":
        print(render_full())
    else:
        print(render_brief())


if __name__ == "__main__":
    main()
