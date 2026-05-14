"""
CTO Briefing — 中文自然语言汇报生成器。

给 CEO 看的不是 metrics 表格，而是一段话：
"你离开这段时间，KAIROS 做了什么、结果如何、发现了什么问题、需要你决定什么。"

Usage:
    from src.core.cto_briefing import generate_briefing
    print(generate_briefing())
"""

import json
import logging
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MEMEX_ROOT = Path(__file__).parent.parent.parent
_WORKSPACE = _MEMEX_ROOT.parent
_DATA = Path(__file__).parent.parent / "data"
_HARNESS = _WORKSPACE / ".claude" / "config" / "harness_state.json"


def _load_json(path: Path) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _load_jsonl(path: Path, last_n: int = 0) -> List[Dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        if last_n:
            lines = lines[-last_n:]
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except Exception:
        return []


# ================================================================
# 数据采集 (纯读取，不触发任何副作用)
# ================================================================

def _collect_kairos_stats() -> Dict:
    """KAIROS 项目执行统计。"""
    entries = _load_jsonl(_DATA / "kairos_feedback.jsonl")
    if not entries:
        return {"total": 0}

    total = len(entries)
    successes = sum(1 for e in entries if e.get("success"))
    cost = sum(e.get("cost_usd", 0) for e in entries)
    qualities = [e.get("quality_score", 0) for e in entries if e.get("quality_score")]
    avg_q = sum(qualities) / len(qualities) if qualities else 0

    # 按 agent 角色统计
    roles = Counter(e.get("agent_role", "unknown") for e in entries)
    top_roles = roles.most_common(5)

    # 最近 20 条的趋势
    recent = entries[-20:]
    recent_q = [e.get("quality_score", 0) for e in recent if e.get("quality_score")]
    recent_avg = sum(recent_q) / len(recent_q) if recent_q else 0

    return {
        "total": total,
        "successes": successes,
        "fail_count": total - successes,
        "cost": cost,
        "avg_quality": avg_q,
        "recent_avg_quality": recent_avg,
        "top_roles": top_roles,
    }


def _collect_git_commits() -> List[str]:
    """今日 git commits。"""
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "--since=midnight", "--format=%s"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(_MEMEX_ROOT), timeout=5,
        )
        return [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    except Exception:
        return []


def _collect_evolution_state() -> Dict:
    """进化引擎状态：metrics + runs + prompts。"""
    metrics = _load_json(_DATA / "evolution_metrics.json") or {}
    runs = _load_jsonl(_DATA / "evolution_runs.jsonl")
    prompt_evo = _load_json(_DATA / "prompt_evolution.json") or {}

    snapshots = metrics.get("snapshots", [])
    latest = snapshots[-1] if snapshots else {}

    # 从 prompt_evolution.json 获取真实部署数
    history = prompt_evo.get("history", [])
    deployed = [h for h in history if h.get("deployed")]
    rejected = [h for h in history if not h.get("deployed")]

    return {
        "runs_count": len(runs),
        "health_score": latest.get("health_score", 0),
        "health_status": latest.get("health_status", "unknown"),
        "test_pass_rate": latest.get("test_pass_rate", 0),
        "pattern_count": latest.get("pattern_count", 0),
        "avg_confidence": latest.get("avg_confidence", 0),
        "deployed_prompts": len(deployed),
        "rejected_prompts": len(rejected),
        "deployed_names": [h.get("agent_name", "?") for h in deployed],
        "latest_run_time": runs[-1].get("timestamp", "") if runs else "",
    }


def _collect_patterns() -> Dict:
    """语义 patterns 状态。"""
    data = _load_json(_DATA / "semantic_patterns.json") or {}
    raw = data.get("patterns", {})
    # patterns 可能是 dict(id→pattern) 或 list
    if isinstance(raw, dict):
        patterns = list(raw.values())
    elif isinstance(raw, list):
        patterns = raw
    else:
        patterns = []
    return {
        "count": len(patterns),
        "avg_confidence": sum(p.get("confidence", 0) for p in patterns if isinstance(p, dict)) / max(len(patterns), 1),
        "total_retrievals": sum(p.get("times_retrieved", 0) for p in patterns if isinstance(p, dict)),
        "top_patterns": sorted([p for p in patterns if isinstance(p, dict)],
                                key=lambda p: -p.get("times_retrieved", 0))[:3],
    }


def _collect_approvals() -> Dict:
    """CEO 审批队列。"""
    items = _load_json(_DATA / "pending_approvals.json") or []
    pending = [a for a in items if a.get("status") == "pending"]
    approved = [a for a in items if a.get("status") == "approved"]
    rejected = [a for a in items if a.get("status") == "rejected"]
    return {
        "pending": pending,
        "approved_count": len(approved),
        "rejected_count": len(rejected),
    }


def _collect_lessons() -> List[Dict]:
    """经验教训库。"""
    data = _load_json(_DATA / "lessons.json") or {}
    return data.get("lessons", []) if isinstance(data, dict) else []


def _collect_meta_health() -> List[str]:
    """元健康检查：数据一致性问题。"""
    issues = []

    # 1. evolution_metrics vs evolution_runs 计数是否一致
    metrics = _load_json(_DATA / "evolution_metrics.json") or {}
    runs = _load_jsonl(_DATA / "evolution_runs.jsonl")
    snapshot_count = len(metrics.get("snapshots", []))
    if runs and abs(snapshot_count - len(runs)) > 2:
        issues.append(f"进化 metrics 快照({snapshot_count})与实际运行({len(runs)})不一致")

    # 2. metrics 中的 total_attempts 是否被正确递增
    if metrics.get("total_attempts", 0) == 0 and len(runs) > 0:
        issues.append(f"metrics 的 total_attempts=0 但实际已运行 {len(runs)} 轮（计数器 bug）")

    # 3. harness_state 新鲜度
    harness = _load_json(_HARNESS) or {}
    updated_at = harness.get("updated_at", "")
    if updated_at:
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
            if age_hours > 24:
                issues.append(f"harness_state.json 已 {age_hours:.0f} 小时未更新")
        except Exception:
            pass

    # 4. heartbeat 是否活跃
    hb = _load_json(_DATA / "kairos_heartbeat.json")
    if hb:
        ts = hb.get("ts", "")
        if ts:
            try:
                hb_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - hb_time).total_seconds() / 60
                if age_min > 120:
                    issues.append(f"Heartbeat 已 {age_min:.0f} 分钟未响应（阈值 120 分钟）")
            except Exception:
                pass
    else:
        issues.append("Heartbeat 文件不存在")

    return issues


# ================================================================
# 中文叙事生成
# ================================================================

def _narrate_kairos(stats: Dict) -> str:
    """KAIROS 执行情况叙事。"""
    if stats["total"] == 0:
        return "KAIROS 尚未执行任何项目。"

    parts = []
    parts.append(
        f"KAIROS 累计执行了 {stats['total']} 个项目，"
        f"其中 {stats['successes']} 个成功"
    )
    if stats["fail_count"]:
        parts.append(f"，{stats['fail_count']} 个失败")
    parts.append(f"。平均质量 {stats['avg_quality']:.1f}/5，总花费 ${stats['cost']:.2f}。")

    # 趋势
    if stats["recent_avg_quality"] > stats["avg_quality"] + 0.2:
        parts.append("最近 20 个项目质量有所提升。")
    elif stats["recent_avg_quality"] < stats["avg_quality"] - 0.2:
        parts.append("注意：最近 20 个项目质量有所下降。")

    # 主力 agent
    if stats["top_roles"]:
        role_strs = [f"{role}({count}次)" for role, count in stats["top_roles"][:3]]
        parts.append(f"出力最多的 agent: {', '.join(role_strs)}。")

    return "".join(parts)


def _narrate_evolution(evo: Dict) -> str:
    """进化引擎叙事。"""
    parts = []

    if evo["runs_count"] == 0:
        return "进化引擎尚未运行过。"

    parts.append(f"进化引擎已运行 {evo['runs_count']} 轮。")

    if evo["deployed_prompts"]:
        names = "、".join(evo["deployed_names"])
        parts.append(f"成功部署了 {evo['deployed_prompts']} 个优化 prompt（{names}）。")
    else:
        parts.append("尚未成功部署任何优化 prompt。")

    if evo["rejected_prompts"]:
        parts.append(f"{evo['rejected_prompts']} 次优化尝试被拒绝（改进不达标）。")

    parts.append(f"系统健康分 {evo['health_score']:.3f}（{evo['health_status']}）。")

    return "".join(parts)


def _narrate_patterns(pat: Dict) -> str:
    """语义 patterns 叙事。"""
    if pat["count"] == 0:
        return "语义记忆中尚无 pattern。"

    parts = [
        f"语义记忆中有 {pat['count']} 个活跃 pattern，"
        f"平均置信度 {pat['avg_confidence']:.2f}，"
        f"累计被检索 {pat['total_retrievals']} 次。"
    ]

    if pat["top_patterns"]:
        top = pat["top_patterns"][0]
        parts.append(f"最常用的 pattern: \"{top.get('rule', '?')[:50]}\"（检索 {top.get('times_retrieved', 0)} 次）。")

    return "".join(parts)


def _narrate_approvals(appr: Dict) -> str:
    """审批队列叙事。"""
    pending = appr["pending"]
    if not pending:
        return ""

    parts = [f"审批队列中有 {len(pending)} 项待你决定："]
    for a in pending[:5]:
        level = a.get("level", "?")
        title = a.get("title", "?")[:60]
        parts.append(f"  - [{level}] {title}")

    return "\n".join(parts)


def _narrate_meta_health(issues: List[str]) -> str:
    """元健康问题叙事。"""
    if not issues:
        return ""

    parts = ["系统自检发现以下数据一致性问题："]
    for issue in issues:
        parts.append(f"  - {issue}")
    parts.append("这些问题会导致监控数据不准确，建议优先修复。")
    return "\n".join(parts)


# ================================================================
# 主函数
# ================================================================

def generate_briefing() -> str:
    """生成中文自然语言 CEO 汇报。

    输出一段完整的中文叙事，像 CTO 给 CEO 的口头汇报。
    """
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # 采集所有数据
    kairos = _collect_kairos_stats()
    commits = _collect_git_commits()
    evo = _collect_evolution_state()
    patterns = _collect_patterns()
    approvals = _collect_approvals()
    lessons = _collect_lessons()
    meta_issues = _collect_meta_health()

    # 组装叙事
    sections = []

    # 开头
    sections.append(f"# KAIROS 系统汇报 — {now}\n")

    # 一句话总结
    health_emoji = {"HEALTHY": "运行良好", "DEGRADED": "需要关注", "CRITICAL": "状态危急"}
    status_cn = health_emoji.get(evo["health_status"], evo["health_status"])
    sections.append(f"**一句话**: 系统{status_cn}，健康分 {evo['health_score']:.2f}/1.0，"
                    f"已执行 {kairos['total']} 个项目，{evo['deployed_prompts']} 个 prompt 完成进化部署。\n")

    # KAIROS 执行
    sections.append("## KAIROS 做了什么\n")
    sections.append(_narrate_kairos(kairos))
    sections.append("")

    # 今日 commits
    if commits:
        sections.append(f"今天提交了 {len(commits)} 个 commit：")
        for c in commits[:8]:
            sections.append(f"  - {c[:70]}")
        sections.append("")

    # 进化引擎
    sections.append("## 进化引擎状态\n")
    sections.append(_narrate_evolution(evo))
    sections.append("")

    # 语义记忆
    sections.append("## 语义记忆\n")
    sections.append(_narrate_patterns(patterns))
    sections.append("")

    # 经验教训
    if lessons:
        top_lesson = max(lessons, key=lambda l: l.get("times_applied", 0))
        lesson_name = top_lesson.get("trigger", top_lesson.get("title", "?"))[:40]
        applied = top_lesson.get("times_applied", 0)
        helped = top_lesson.get("times_helped", 0)
        sections.append(f"经验库共 {len(lessons)} 条。最常应用的是"
                        f"「{lesson_name}」"
                        f"（应用 {applied} 次，帮助率 {helped}/{max(applied, 1)}）。")
        sections.append("")

    # 需要你决定的事
    approval_text = _narrate_approvals(approvals)
    if approval_text:
        sections.append("## 需要你决定\n")
        sections.append(approval_text)
        sections.append("")

    # 元健康问题
    meta_text = _narrate_meta_health(meta_issues)
    if meta_text:
        sections.append("## 系统自检问题\n")
        sections.append(meta_text)
        sections.append("")

    # 结尾建议
    sections.append("---")
    if meta_issues:
        sections.append("建议：先处理系统自检问题（数据准确性），再推进进化。")
    elif evo["deployed_prompts"] < 3:
        sections.append(f"建议：继续积累执行数据，推动 Phase 2 gate（需 3+ prompt 部署，当前 {evo['deployed_prompts']}）。")
    else:
        sections.append("建议：系统运行正常，可以开始观察进化效果。")

    # 保存
    briefing_text = "\n".join(sections)
    _DATA.mkdir(parents=True, exist_ok=True)
    (_DATA / "latest_briefing.md").write_text(briefing_text, encoding="utf-8")

    return briefing_text


# ================================================================
# U19: briefing schema v2 (structured dict, schema-locked)
# ================================================================

BRIEFING_V2_SCHEMA = {
    "goal": str,
    "actions_taken": list,
    "live_evidence": dict,
    "gates_status": dict,
    "out_of_scope": list,
    "permanent_lesson_candidates": list,
    # B-5 (2026-05-04): symptoms_addressed list captures pre/post state
    # for each AC the task aimed to heal. Reads ac_verifier evidence.jsonl
    # entries to compute {symptom, pre_state, post_state, healed bool}.
    # Pre-fix: briefing listed actions but never claimed "症状是否治了".
    "symptoms_addressed": list,
}


def validate_briefing_v2_schema(d: Dict[str, Any]) -> List[str]:
    """Validate dict against BRIEFING_V2_SCHEMA. Returns list of error strings; empty = pass."""
    errors: List[str] = []
    if not isinstance(d, dict):
        return [f"top-level must be dict, got {type(d).__name__}"]
    for key, expected in BRIEFING_V2_SCHEMA.items():
        if key not in d:
            errors.append(f"missing key: {key}")
            continue
        if not isinstance(d[key], expected):
            errors.append(
                f"key {key} expected {expected.__name__}, got {type(d[key]).__name__}"
            )
    if "actions_taken" in d and isinstance(d["actions_taken"], list) and not d["actions_taken"]:
        errors.append("actions_taken must be non-empty")
    if "live_evidence" in d and isinstance(d["live_evidence"], dict) and not d["live_evidence"]:
        errors.append("live_evidence must be non-empty")
    return errors


def _read_json_safe(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _fallback_actions_taken(task_id: str) -> List[str]:
    try:
        r = subprocess.run(
            ["git", "log", f"--grep={task_id}", "--oneline", "-n", "10"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(_MEMEX_ROOT), timeout=10,
        )
        out = (r.stdout or "").strip().splitlines()
        return out if out else ["[no actions captured; missing both last_briefing.json and matching git log]"]
    except (subprocess.TimeoutExpired, OSError):
        return ["[git log fallback timeout]"]


import re as _re_static  # static import (clean local_reviewer MEDIUM finding)
_TASK_ID_RE = _re_static.compile(r"^[A-Za-z0-9_-]{1,128}$")


def generate_briefing_v2(task_id: str, task_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Schema-locked briefing dict. Reads <task_dir>/last_briefing.json if present;
    falls back per logic-iter1-7 mapping; merges partial last_briefing per logic-iter1-1 fix."""
    if not _TASK_ID_RE.match(task_id or ""):
        # security-iter1-5 fix: task_id format validation (avoid git log option-injection)
        raise ValueError(f"invalid task_id format: {task_id!r}")
    if task_dir is None:
        task_dir = _WORKSPACE / ".claude" / "harness" / "tasks" / task_id
    task_dir = Path(task_dir).resolve()
    # security-iter1-4 fix: workspace boundary on task_dir
    workspace_resolved = _WORKSPACE.resolve()
    try:
        task_dir.relative_to(workspace_resolved)
    except ValueError:
        import tempfile as _tf
        try:
            task_dir.relative_to(Path(_tf.gettempdir()).resolve())
        except ValueError:
            raise ValueError(f"task_dir traversal blocked: {task_dir}")
    last_briefing = _read_json_safe(task_dir / "last_briefing.json")

    task_spec = _read_json_safe(task_dir / "task_spec.json") or {}
    state = _read_json_safe(task_dir / "state.json") or {}
    harness = _read_json_safe(_HARNESS) or {}

    goal = (
        (task_spec.get("ceo_directives") or [None])[0]
        or state.get("plan_unit")
        or "[goal not captured]"
    )
    actions_taken = _fallback_actions_taken(task_id)
    live_evidence: Dict[str, Any] = {
        "evidence_jsonl_present": (task_dir / "evidence.jsonl").exists(),
        "trace_jsonl_present": (task_dir / "trace.jsonl").exists(),
        "stage": state.get("stage", "unknown"),
    }
    gates_status: Dict[str, Any] = {
        "gate_coverage": state.get("gate_coverage")
        or harness.get("git_repos", {}).get("memex", {}).get("last_commit_summary", "")[-200:],
        "last_commit_hash": state.get("commit_hash") or harness.get("last_commit_hash", ""),
    }
    out_of_scope: List[str] = []
    plan_path = task_dir / "plan_v_latest.md"
    if plan_path.exists():
        text = plan_path.read_text(encoding="utf-8", errors="replace")
        import re as _re
        m = _re.search(r"^## Out-of-scope\s*\n([\s\S]*?)(?=^## |\Z)", text, _re.MULTILINE)
        if m:
            out_of_scope = [
                ln.strip("- ").strip() for ln in m.group(1).splitlines()
                if ln.strip().startswith("-")
            ]
    permanent_lesson_candidates = list(task_spec.get("permanent_lessons_approved") or [])

    # B-5 (2026-05-04): symptoms_addressed — read evidence.jsonl + plan AC
    # pre/post fields, list which symptoms got healed by this task.
    symptoms_addressed = _compute_symptoms_addressed(task_dir)

    fallback = {
        "goal": goal,
        "actions_taken": actions_taken,
        "live_evidence": live_evidence,
        "gates_status": gates_status,
        "out_of_scope": out_of_scope,
        "permanent_lesson_candidates": permanent_lesson_candidates,
        "symptoms_addressed": symptoms_addressed,
    }
    # logic-iter1-1 fix: merge partial last_briefing fields rather than discard
    if isinstance(last_briefing, dict):
        for k in BRIEFING_V2_SCHEMA:
            if k in last_briefing and isinstance(last_briefing[k], BRIEFING_V2_SCHEMA[k]):
                fallback[k] = last_briefing[k]
    return fallback


def _compute_symptoms_addressed(task_dir: Path) -> List[Dict[str, Any]]:
    """B-5: derive [{symptom, pre, post, healed}] from evidence.jsonl + plan.

    Reads the latest plan_v<N>.md for AC blocks declaring `pre_state:` and
    `post_state:` fields (B-1 contract upgrade). For each AC, look up the
    matching evidence.jsonl entry by ac_id and check exit_code==0 → healed.
    Empty list if no plan/evidence — not an error, just nothing to claim.
    """
    out: List[Dict[str, Any]] = []
    try:
        ev_path = task_dir / "evidence.jsonl"
        if not ev_path.exists():
            return out
        # Find the latest plan_v<N>.md
        import re as _re
        plan_files = sorted(task_dir.glob("plan_v*.md"),
                            key=lambda p: int(_re.search(r"plan_v(\d+)", p.name).group(1))
                            if _re.search(r"plan_v(\d+)", p.name) else 0)
        if not plan_files:
            return out
        plan_text = plan_files[-1].read_text(encoding="utf-8", errors="replace")
        # Parse AC blocks: **AC-N**: <symptom>\n  pre_state: <x>\n  post_state: <y>
        ac_specs: Dict[str, Dict[str, str]] = {}
        for m in _re.finditer(
            r"\*\*AC-(\d+\w*)\*\*\s*:\s*([^\n]+)(?:\n[^\n]*?(?:pre_state|pre)\s*:\s*([^\n]+))?(?:\n[^\n]*?(?:post_state|post)\s*:\s*([^\n]+))?",
            plan_text,
        ):
            ac_id = f"AC-{m.group(1)}"
            ac_specs[ac_id] = {
                "symptom": (m.group(2) or "").strip()[:200],
                "pre_state": (m.group(3) or "").strip()[:200],
                "post_state": (m.group(4) or "").strip()[:200],
            }
        # Read evidence
        ev_by_ac: Dict[str, Dict[str, Any]] = {}
        for line in ev_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if rec.get("ac_id"):
                    # Keep latest entry per ac_id (overwrite on later)
                    ev_by_ac[rec["ac_id"]] = rec
            except Exception:
                continue
        # Compose
        for ac_id, spec in ac_specs.items():
            ev = ev_by_ac.get(ac_id)
            healed = bool(ev and ev.get("exit_code") == 0)
            out.append({
                "ac_id": ac_id,
                "symptom": spec["symptom"],
                "pre_state": spec["pre_state"],
                "post_state": spec["post_state"],
                "healed": healed,
                "evidence_exit_code": ev.get("exit_code") if ev else None,
            })
    except Exception:
        # fail-soft; briefing is observability, not gate
        pass
    return out


def emit_briefing_schema_v2(task_id: str, briefing_dict: Dict[str, Any]) -> bool:
    errors = validate_briefing_v2_schema(briefing_dict)
    payload = {
        "key_count": len(briefing_dict) if isinstance(briefing_dict, dict) else 0,
        "schema_ok": not errors,
        "errors_count": len(errors),
    }
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("briefing_schema_v2", payload)
        return True
    except Exception:
        try:
            from src.core.task_dir_layout import append_trace
            return append_trace(task_id, "briefing_schema_v2", payload)
        except Exception:
            return False
