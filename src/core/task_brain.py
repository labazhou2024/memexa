"""
[DEPRECATED 2026-05-12] KAIROS self-evolution frozen since 2026-04-04
(last evo_run / last kairos heartbeat both ~5 weeks stale). 0 production
callers. Kept for archive/tests only. New code: use src.core.memory_query.

Task Brain — KAIROS 的战略决策层。

把感知（auto_trigger）、质量循环（big_loop）、进化（orchestrator）、
执行（KAIROS daemon）串联成闭环。

职责：
  1. SENSE: 从 6 个数据源收集全局状态
  2. JUDGE: 跨维度优先级排序 + 时间预算裁剪
  3. ACT:   生成可执行 Project 列表喂给 KAIROS
  4. REACT: 项目完成后链式触发（修复→测试→大循环→进化）

调用方式：
  brain = TaskBrain()
  projects = brain.generate_projects(duration_minutes=180)  # departure 时
  projects = brain.on_queue_empty()                          # KAIROS 队列空时
  next_proj = brain.on_project_complete(result)              # 项目完成后链式反应
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_MEMEX_ROOT = Path(__file__).parent.parent.parent
_WORKSPACE = _MEMEX_ROOT.parent
_DATA = Path(__file__).parent.parent / "data"

# WORKFLOW_SUFFIX is now in kairos_daemon.py, applied per mode at submission time

# R5: Smart model routing keywords (borrowed from Hermes Agent + oh-my-claudecode)
_SIMPLE_KEYWORDS = {"metrics", "collect", "snapshot", "status", "check", "report", "log", "list"}
_COMPLEX_KEYWORDS = {"refactor", "architect", "redesign", "migrate", "investigate", "research",
                     "bigloop", "multi-file", "rewrite", "security", "audit"}


def _route_model(project: Dict) -> tuple:
    """Auto-select model, max_turns, budget based on task complexity.

    Returns: (model, max_turns, max_budget_usd)
    - Simple (metrics/logs/checks): sonnet, 10 turns, $2
    - Medium (single-file fix/test): sonnet, 30 turns, $5
    - Complex (multi-file/architecture): opus, 50 turns, $10
    """
    title = (project.get("title", "") + " " + project.get("prompt", "")[:200]).lower()
    mode = project.get("mode", "workflow")

    # Quick mode always uses sonnet
    if mode == "quick":
        return ("sonnet", 10, 2.0)

    # Check keywords
    has_simple = any(kw in title for kw in _SIMPLE_KEYWORDS)
    has_complex = any(kw in title for kw in _COMPLEX_KEYWORDS)

    if has_complex and not has_simple:
        return ("opus", 50, 10.0)
    elif has_simple and not has_complex:
        return ("sonnet", 15, 3.0)
    else:
        # Default medium
        return ("sonnet", 30, 5.0)


class TaskBrain:
    """KAIROS 的大脑。负责决定做什么、何时做、如何做。"""

    def __init__(self):
        self._completed_since_last_loop = 0

    # ==============================================================
    # SENSE: 从 6 个数据源收集状态
    # ==============================================================

    def _sense_triggers(self) -> List[Dict]:
        """数据源 1: auto_trigger 检测结果。"""
        try:
            from .auto_trigger import check_triggers
            return check_triggers()
        except Exception as e:
            logger.warning("Brain: auto_trigger failed: %s", e)
            return []

    def _sense_pending_specs(self) -> List[Dict]:
        """数据源 2: big_loop 待执行的 agent specs。"""
        specs_file = _DATA / "pending_agent_specs.json"
        if specs_file.exists():
            try:
                return json.loads(specs_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _sense_evolution(self) -> Dict:
        """数据源 3: evolution_metrics 退化检测。"""
        metrics_file = _DATA / "evolution_metrics.json"
        if not metrics_file.exists():
            return {"health": 0, "trend": "unknown", "degraded": False}
        try:
            data = json.loads(metrics_file.read_text(encoding="utf-8"))
            snapshots = data.get("snapshots", [])
            if not snapshots:
                return {"health": 0, "trend": "unknown", "degraded": False}
            latest = snapshots[-1]
            health = latest.get("health_score", 0)
            # Trend: compare last 2
            if len(snapshots) >= 2:
                prev = snapshots[-2].get("health_score", 0)
                trend = "improving" if health > prev + 0.02 else ("declining" if health < prev - 0.02 else "stable")
            else:
                trend = "unknown"
            return {
                "health": health,
                "trend": trend,
                "degraded": health < 0.5 or trend == "declining",
                "test_pass_rate": latest.get("test_pass_rate", 1.0),
                "pattern_count": latest.get("pattern_count", 0),
            }
        except Exception:
            return {"health": 0, "trend": "unknown", "degraded": False}

    def _sense_errors(self) -> List[Dict]:
        """数据源 4: events.jsonl 错误模式分析。"""
        events_file = _DATA / "events.jsonl"
        if not events_file.exists():
            return []
        try:
            lines = events_file.read_text(encoding="utf-8").strip().splitlines()
            errors = []
            for line in lines[-200:]:
                try:
                    e = json.loads(line)
                    if "fail" in e.get("type", "").lower() or "error" in e.get("type", "").lower():
                        errors.append(e)
                except json.JSONDecodeError:
                    continue
            return errors
        except Exception:
            return []

    def _sense_approvals(self) -> List[Dict]:
        """数据源 5: CEO 已审批项 → 转化为可执行任务。

        Reads ALL approved items (L1, L2, L3) and converts actionable ones
        to KAIROS projects. Marks consumed items as 'actioned' to avoid re-processing.
        """
        file = _DATA / "pending_approvals.json"
        if not file.exists():
            return []
        try:
            items = json.loads(file.read_text(encoding="utf-8"))
            approved = [i for i in items if i.get("status") == "approved"
                        and not i.get("actioned")]
            if not approved:
                return approved

            # Mark approved items as actioned so they're not re-processed
            changed = False
            for item in items:
                if item.get("status") == "approved" and not item.get("actioned"):
                    item["actioned"] = True
                    item["actioned_at"] = datetime.utcnow().isoformat() + "Z"
                    changed = True
            if changed:
                file.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                logger.info("Brain: marked %d approved items as actioned", len(approved))

            return approved
        except Exception as e:
            logger.warning("Brain: _sense_approvals failed: %s", e)
            return []

    def _sense_meta_health(self) -> List[Dict]:
        """数据源 7: 元健康自诊断 — 检测 KAIROS 自身的数据一致性问题。

        不同于其他 _sense_* 方法检测外部问题（测试失败、错误率），
        这个方法检测 KAIROS 系统自身的内部问题：
          - evolution_metrics 计数器与实际运行不一致
          - harness_state 过时
          - feedback 数据缺失
          - heartbeat 中断
        """
        issues = []

        # 1. evolution_metrics.total_attempts 与 evolution_runs.jsonl 条目数不一致
        metrics = self._load_json(_DATA / "evolution_metrics.json") or {}
        runs_file = _DATA / "evolution_runs.jsonl"
        runs_count = 0
        if runs_file.exists():
            try:
                runs_count = sum(1 for l in runs_file.read_text(encoding="utf-8").strip().splitlines() if l.strip())
            except Exception:
                pass
        recorded_attempts = metrics.get("total_attempts", 0)
        if runs_count > 0 and recorded_attempts == 0:
            issues.append({
                "type": "metrics_counter_bug",
                "detail": f"evolution_metrics.total_attempts={recorded_attempts} 但 evolution_runs 有 {runs_count} 条记录",
                "fix_hint": "evolution_metrics.json 的 total_attempts/successful_deploys 计数器未被正确递增，需要修复 evolution_orchestrator 或 evolution_metrics 的记录逻辑",
            })

        # 2. harness_state 新鲜度
        harness_file = _WORKSPACE / ".claude" / "config" / "harness_state.json"
        harness = self._load_json(harness_file) or {}
        updated_at = harness.get("updated_at", "")
        if updated_at:
            try:
                from datetime import timezone
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
                if age_hours > 48:
                    issues.append({
                        "type": "stale_harness",
                        "detail": f"harness_state.json 已 {age_hours:.0f} 小时未更新",
                        "fix_hint": "heartbeat 或 session_exit_gate 应在每次运行后更新 harness_state",
                    })
            except Exception:
                pass

        # 3. heartbeat 文件缺失或过旧
        hb = self._load_json(_DATA / "kairos_heartbeat.json")
        if not hb:
            issues.append({
                "type": "missing_heartbeat",
                "detail": "kairos_heartbeat.json 不存在",
                "fix_hint": "Windows Task Scheduler 的 heartbeat 任务可能未注册或未运行",
            })
        elif hb.get("ts"):
            try:
                from datetime import timezone
                hb_time = datetime.fromisoformat(hb["ts"].replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - hb_time).total_seconds() / 60
                if age_min > 120:
                    issues.append({
                        "type": "stale_heartbeat",
                        "detail": f"heartbeat 已 {age_min:.0f} 分钟未响应（阈值 120 分钟）",
                        "fix_hint": "检查 Windows Task Scheduler 中 memex-Heartbeat 任务是否正常",
                    })
            except Exception:
                pass

        # 4. kairos_feedback 条目数 vs pending_projects 已完成数
        feedback_count = 0
        feedback_file = _DATA / "kairos_feedback.jsonl"
        if feedback_file.exists():
            try:
                feedback_count = sum(1 for l in feedback_file.read_text(encoding="utf-8").strip().splitlines() if l.strip())
            except Exception:
                pass
        projects = self._load_json(_DATA / "pending_projects.json") or []
        if isinstance(projects, list):
            completed_count = sum(1 for p in projects if p.get("status") == "completed")
            if completed_count > 0 and feedback_count == 0:
                issues.append({
                    "type": "missing_feedback",
                    "detail": f"{completed_count} 个项目已完成但 feedback 为空",
                    "fix_hint": "feedback_collector.collect_feedback() 可能未被调用",
                })

        return issues

    @staticmethod
    def _load_json(path: Path):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def _sense_context(self) -> Dict:
        """数据源 6: 项目状态（pytest、bugs、coverage）。"""
        ctx = {}
        # Pytest (shared cache — no redundant subprocess)
        try:
            from .pytest_cache import get_test_results
            test_results = get_test_results()
            ctx["tests_pass"] = test_results.get("success", True)
            ctx["test_output"] = test_results.get("output", "")
        except Exception:
            ctx["tests_pass"] = True
            ctx["test_output"] = ""

        # Q2 bugs
        bugs_file = _DATA / "q2_bugs.json"
        if bugs_file.exists():
            try:
                bugs = json.loads(bugs_file.read_text(encoding="utf-8"))
                ctx["bugs"] = [b for b in bugs if b.get("severity") in ("CRITICAL", "HIGH")]
            except Exception:
                ctx["bugs"] = []
        else:
            ctx["bugs"] = []

        # Untested modules
        core = _MEMEX_ROOT / "memex" / "core"
        tests = _MEMEX_ROOT / "tests"
        if core.exists() and tests.exists():
            tested = {f.stem.replace("test_", "") for f in tests.glob("test_*.py")}
            ctx["untested"] = [f.stem for f in sorted(core.glob("*.py"))
                               if f.name != "__init__.py" and f.stem not in tested]
        else:
            ctx["untested"] = []

        return ctx

    # ==============================================================
    # JUDGE: 跨维度优先级排序
    # ==============================================================

    def _prioritize(self, candidates: List[Dict]) -> List[Dict]:
        """按优先级排序 + 去重。"""
        # Dedup by title
        seen = set()
        unique = []
        for c in candidates:
            title = c.get("title", "")
            if title not in seen:
                seen.add(title)
                unique.append(c)
        # Sort by priority descending
        unique.sort(key=lambda c: -c.get("priority", 3))
        return unique

    def _fit_to_budget(self, projects: List[Dict], budget_min: int) -> List[Dict]:
        """裁剪到时间预算内。"""
        fitted = []
        used = 0
        for p in projects:
            est = p.get("estimate_min", 10)
            if used + est <= budget_min:
                fitted.append(p)
                used += est
        return fitted

    # ==============================================================
    # ACT: 生成 Project 列表
    # ==============================================================

    def _get_pattern_context(self, task_description: str, project: Dict = None) -> str:
        """W3: Retrieve relevant semantic patterns for prompt injection.

        If project dict is provided, stores injected pattern IDs in
        project["injected_pattern_ids"] for later outcome tracking.
        """
        try:
            from .semantic_memory import get_semantic_memory
            sm = get_semantic_memory()
            patterns = sm.retrieve(task_description, top_k=3)
            if not patterns:
                return ""
            if project is not None:
                project["injected_pattern_ids"] = [p.pattern_id for p in patterns]
            lines = ["[Historical patterns] (by confidence)"]
            for p in patterns:
                lines.append(f"- [{p.confidence:.0%}] {p.rule}")
            return f"\n\n" + "\n".join(lines) + "\n"
        except Exception as e:
            logger.debug("W3 pattern injection skipped: %s", e)
        return ""

    def _get_evolved_agent_prompt(self, agent_name: str) -> str:
        """W6: Read full evolved agent .md prompt (if it exists).

        Returns the agent definition body (up to 4000 chars) with YAML
        frontmatter stripped.  The full content is critical — truncating
        loses Phase definitions, output formats, and behavioral rules
        that the agent needs to operate correctly.
        """
        if "/" in agent_name or ".." in agent_name or "\\" in agent_name:
            return ""
        agent_file = _WORKSPACE / ".claude" / "agents" / f"{agent_name}.md"
        if not agent_file.exists():
            return ""
        try:
            content = agent_file.read_text(encoding="utf-8")
            # Strip YAML frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()[:4000]
            return content[:4000]
        except Exception:
            return ""

    def _spec_to_project(self, spec: Dict) -> Dict:
        """把 big_loop agent_spec 翻译成 KAIROS project。W3+W6 integrated.

        All BigLoop agent tasks go through workflow mode (multi-agent pipeline).
        """
        agent = spec.get("agent", "sonnet-executor")
        prompt = spec.get("prompt", "")

        # All BigLoop tasks use workflow mode with Opus orchestration
        # Opus will naturally dispatch Sonnet sub-agents as needed
        model = "opus"
        budget = 5.00
        turns = 50

        # W6: Use evolved agent prompt if available (inject FULL definition)
        evolved_context = self._get_evolved_agent_prompt(agent)
        agent_intro = f"You are acting as the {agent} agent."
        if evolved_context:
            agent_intro += f"\n\nYour specialized instructions:\n{evolved_context[:3000]}"

        project = {
            "title": f"[BigLoop {agent}] {prompt[:50]}",
            "prompt": "",  # filled below
            "priority": 4,
            "model": model,
            "mode": "workflow",
            "max_budget_usd": budget,
            "max_turns": turns,
            "estimate_min": 15 if agent in ("qa-director", "strategic-advisor") else 8,
        }

        # W3: Inject relevant patterns (stores IDs in project dict)
        pattern_context = self._get_pattern_context(prompt, project=project)
        project["prompt"] = f"{agent_intro}\n\n{prompt}{pattern_context}"

        return project

    def generate_projects(self, duration_minutes: int = 60) -> List[Dict]:
        """核心方法：智能生成项目队列。

        整合 6 个数据源，按优先级排序，裁剪到时间预算。
        替代 departure.py 的硬编码模板。
        """
        candidates = []
        budget = int(duration_minutes * 0.7)

        # --- 数据源 1: auto_trigger 紧急项（workflow mode, 最高优先级）---
        triggers = self._sense_triggers()
        for t in triggers:
            if t["type"] == "auto_fix_tests":
                candidates.append({
                    "title": "FIX: failing tests",
                    "prompt": f"Tests are failing. Fix them.\n\nTest output:\n{t['data'].get('output', '')[-800:]}",
                    "priority": 5, "estimate_min": 15, "mode": "workflow",
                })
            elif t["type"] == "auto_postmortem":
                candidates.append({
                    "title": "INVESTIGATE: high error rate in events",
                    "prompt": f"Error rate is {t['data'].get('error_count', 0)} errors in recent 100 events. "
                              f"Read memex/data/events.jsonl, analyze patterns, fix root causes.",
                    "priority": 4, "estimate_min": 15, "mode": "workflow",
                })
            elif t["type"] == "consolidate_memory":
                candidates.append({
                    "title": "EVOLVE: consolidate semantic memory",
                    "prompt": "Run semantic memory consolidation and report results:\n"
                              "python -c \"import asyncio; from src.core.auto_dream import AutoDream; "
                              "ad = AutoDream(); r = asyncio.run(ad.run()); print(f'Patterns: {r.patterns_after}')\"",
                    "priority": 2, "estimate_min": 5, "mode": "quick",
                })

        # --- 数据源 2: big_loop pending agent specs → workflow mode ---
        specs = self._sense_pending_specs()
        for spec in specs:
            candidates.append(self._spec_to_project(spec))
        if specs:
            specs_file = _DATA / "pending_agent_specs.json"
            specs_file.write_text("[]", encoding="utf-8")

        # --- 数据源 3: evolution 退化 → quick (read-only) ---
        evo = self._sense_evolution()
        if evo["degraded"]:
            candidates.append({
                "title": "EVOLVE: health declining, run evolution cycle",
                "prompt": "System health is declining. Run evolution orchestrator and metrics collection.",
                "priority": 3, "estimate_min": 8, "mode": "quick",
            })

        # --- 数据源 4: error patterns → workflow (may need code fixes) ---
        errors = self._sense_errors()
        if len(errors) >= 5:
            error_types = {}
            for e in errors:
                t = e.get("type", "unknown")
                error_types[t] = error_types.get(t, 0) + 1
            top_errors = sorted(error_types.items(), key=lambda x: -x[1])[:3]
            candidates.append({
                "title": f"INVESTIGATE: top error patterns ({top_errors[0][0]})",
                "prompt": f"Recent error patterns in events.jsonl:\n"
                          + "\n".join(f"  {t}: {c} occurrences" for t, c in top_errors)
                          + "\n\nAnalyze root causes and fix if possible.",
                "priority": 3, "estimate_min": 15, "mode": "workflow",
            })

        # --- 数据源 5: CEO approved items → convert to executable tasks ---
        approved = self._sense_approvals()
        for item in approved:
            category = item.get("category", "")
            proposal = item.get("proposal", "")
            context = item.get("context", "")
            ceo_notes = item.get("ceo_response", "")
            title = item.get("title", "Approved task")

            if category == "kairos_failure":
                # CEO approved retry of a failed project
                candidates.append({
                    "title": f"[CEO-APPROVED] Retry: {title[:50]}",
                    "prompt": f"CEO approved retrying this task.\nOriginal context: {context[:500]}\n"
                              f"CEO notes: {ceo_notes}\nProposal: {proposal}\n\n"
                              f"Please fix the underlying issue and complete the task.",
                    "priority": 5, "estimate_min": 15, "mode": "workflow",
                })
            elif category == "strategic_review":
                # CEO approved a strategic recommendation
                candidates.append({
                    "title": f"[CEO-APPROVED] {title[:50]}",
                    "prompt": f"CEO approved this strategic recommendation.\n"
                              f"Context: {context[:500]}\n"
                              f"Proposal: {proposal}\n"
                              f"CEO notes: {ceo_notes}\n\n"
                              f"Implement the approved recommendation.",
                    "priority": 4, "estimate_min": 15, "mode": "workflow",
                })
            else:
                # Generic approved item
                candidates.append({
                    "title": f"[CEO-APPROVED] {title[:50]}",
                    "prompt": f"CEO approved: {title}\nContext: {context[:500]}\n"
                              f"Proposal: {proposal}\nCEO notes: {ceo_notes}",
                    "priority": 4, "estimate_min": 10, "mode": "workflow",
                })

        # --- 数据源 6+7: bugs (workflow), tests (workflow), review (workflow) ---
        ctx = self._sense_context()

        if ctx.get("bugs"):
            bug_details = "\n".join(
                f"- {b['id']}: {b['file']}:{b.get('line','?')} — {b['description']} (hint: {b.get('fix_hint','')})"
                for b in ctx["bugs"][:5]
            )
            candidates.append({
                "title": f"FIX: {len(ctx['bugs'][:5])} HIGH/CRITICAL bugs",
                "prompt": f"Fix these bugs:\n\n{bug_details}",
                "priority": 4, "estimate_min": 15, "mode": "workflow",
            })

        if ctx.get("untested"):
            mods = ctx["untested"][:3]
            candidates.append({
                "title": f"TEST: write tests for {', '.join(mods)}",
                "prompt": f"Write unit tests for: {', '.join(mods)}.\n"
                          f"Follow tests/test_semantic_memory.py pattern.\n"
                          f"5-8 tests per module, use tempfile+mocks.",
                "priority": 3, "estimate_min": 15, "mode": "workflow",
            })

        # --- 数据源 7: 元健康自诊断 → workflow (自修复) ---
        meta_issues = self._sense_meta_health()
        for issue in meta_issues:
            candidates.append({
                "title": f"SELF-FIX: {issue['type']}",
                "prompt": (
                    f"KAIROS 自诊断发现内部数据一致性问题：\n\n"
                    f"问题: {issue['detail']}\n"
                    f"修复方向: {issue['fix_hint']}\n\n"
                    f"请诊断根因并修复。修复后验证数据一致。"
                ),
                "priority": 4, "estimate_min": 10, "mode": "workflow",
            })

        # Always: health check (quick mode, low priority filler)
        candidates.append({
            "title": "METRICS: collect evolution snapshot",
            "prompt": "Run evolution metrics collection and report results.",
            "priority": 1, "estimate_min": 3, "mode": "quick",
        })

        # W3: Inject patterns into each candidate's prompt (stores IDs in project)
        for c in candidates:
            pattern_ctx = self._get_pattern_context(c.get("title", ""), project=c)
            if pattern_ctx:
                c["prompt"] = c["prompt"] + pattern_ctx

        # R5: Smart model routing — auto-select model/turns/budget by task complexity
        for c in candidates:
            if "model" not in c:
                c["model"], c["max_turns"], c["max_budget_usd"] = _route_model(c)

        # Prioritize + budget fit
        prioritized = self._prioritize(candidates)
        fitted = self._fit_to_budget(prioritized, budget)

        logger.info("Brain generated %d projects from %d candidates (budget: %dmin)",
                     len(fitted), len(candidates), budget)
        return fitted

    # ==============================================================
    # REACT: 链式反应
    # ==============================================================

    def _sense_agent_performance(self) -> Dict[str, Dict]:
        """Aggregate agent performance stats from kairos_feedback.jsonl."""
        agents: Dict[str, Dict] = {}
        ff = _DATA / "kairos_feedback.jsonl"
        if not ff.exists():
            return agents

        try:
            for line in ff.read_text(encoding="utf-8").strip().splitlines():
                try:
                    entry = json.loads(line)
                    role = entry.get("agent_role", "unknown")
                    if role not in agents:
                        agents[role] = {"total": 0, "success": 0, "total_quality": 0,
                                        "total_cost": 0, "titles": []}
                    agents[role]["total"] += 1
                    if entry.get("success"):
                        agents[role]["success"] += 1
                    agents[role]["total_quality"] += entry.get("quality_score", 0)
                    agents[role]["total_cost"] += entry.get("cost_usd", 0)
                    agents[role]["titles"].append(entry.get("title", "")[:60])
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

        # Compute averages
        for role, data in agents.items():
            data["avg_quality"] = round(data["total_quality"] / max(data["total"], 1), 2)
            data["success_rate"] = round(data["success"] / max(data["total"], 1), 2)
            data["avg_cost"] = round(data["total_cost"] / max(data["total"], 1), 3)

        return agents

    def _sense_roi(self) -> List[Dict]:
        """Calculate ROI by task category from kairos_feedback.jsonl."""
        roi: Dict[str, Dict] = {}
        ff = _DATA / "kairos_feedback.jsonl"
        if not ff.exists():
            return []

        try:
            for line in ff.read_text(encoding="utf-8").strip().splitlines():
                try:
                    entry = json.loads(line)
                    title = entry.get("title", "")
                    if "FIX:" in title:
                        cat = "bug_fix"
                    elif "TEST:" in title:
                        cat = "test_writing"
                    elif "DASHBOARD:" in title:
                        cat = "dashboard"
                    elif "BigLoop" in title:
                        cat = "quality_cycle"
                    elif "EVOLVE:" in title:
                        cat = "evolution"
                    else:
                        cat = "other"

                    if cat not in roi:
                        roi[cat] = {"count": 0, "total_quality": 0, "total_cost": 0,
                                    "successes": 0}
                    roi[cat]["count"] += 1
                    roi[cat]["total_quality"] += entry.get("quality_score", 0)
                    roi[cat]["total_cost"] += entry.get("cost_usd", 0)
                    if entry.get("success"):
                        roi[cat]["successes"] += 1
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

        results = []
        for cat, data in roi.items():
            avg_q = data["total_quality"] / max(data["count"], 1)
            avg_c = data["total_cost"] / max(data["count"], 1)
            roi_score = avg_q / max(avg_c, 0.01)
            results.append({
                "category": cat,
                "count": data["count"],
                "avg_quality": round(avg_q, 2),
                "avg_cost": round(avg_c, 3),
                "success_rate": round(data["successes"] / max(data["count"], 1), 2),
                "roi_score": round(roi_score, 2),
            })

        results.sort(key=lambda x: -x["roi_score"])
        return results

    def on_queue_empty(self) -> List[Dict]:
        """KAIROS queue empty - generate new defensive tasks."""
        return self.generate_projects(duration_minutes=60)

    def on_project_complete(self, result: Dict) -> Optional[Dict]:
        """项目完成后的链式反应。

        Returns:
            New project dict if chain reaction needed, None otherwise.
        """
        if not result.get("success"):
            return None

        self._completed_since_last_loop += 1

        # 每 5 个完成项目，触发 mini big_loop
        if self._completed_since_last_loop >= 5:
            self._completed_since_last_loop = 0
            logger.info("Brain: 5 projects done, triggering mini big-loop")
            return {
                "title": "[BigLoop] Q1+Q4 regression check",
                "prompt": "Run big loop automated stages:\n"
                          "python -c \"\nimport asyncio\n"
                          "from src.core.big_loop import BigLoop\n"
                          "from pathlib import Path\n"
                          "loop = BigLoop(project_root=Path('.'))\n"
                          "q1 = asyncio.run(loop.q1_test_baseline())\n"
                          "print(f'Q1: {q1[\"passed_count\"]} passed, {q1[\"failed_count\"]} failed')\n"
                          "q4 = asyncio.run(loop.q4_regression_check())\n"
                          "print(f'Q4: {len(q4[\"regressions\"])} regressions')\n\"",
                "priority": 4, "estimate_min": 5, "model": "sonnet", "max_turns": 10,
            }

        return None


# Singleton
_instance: Optional[TaskBrain] = None

def get_task_brain() -> TaskBrain:
    global _instance
    if _instance is None:
        _instance = TaskBrain()
    return _instance
