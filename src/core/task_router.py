"""
Task Router -- 任务分类 + 工作流模板选择 + 多轮循环调度

解决4个GAP:
1. 覆盖 plan 模式: 自动生成 plan 文件，plan -> execute -> verify 链路
2. 多轮大循环: 自动重跑 fix->verify 直到通过 (max 3 rounds)
3. 泛化能力: 根据任务类型选择不同工作流模板
4. 严谨工作流: 每个模板都有 plan->execute->review->verify 闭环

5 种任务类型 x 5 种工作流模板:
  RESEARCH   -> scope_validate -> research -> report -> extract_patterns
  DEVELOP    -> plan -> scope_validate -> implement -> test -> review -> fix_loop -> release
  FIX        -> diagnose -> fix -> test -> verify_loop
  REVIEW     -> prime_kb -> review -> report -> extract_patterns
  QUALITY    -> bigloop Q0-Q7 (existing)

调用方式:
  from src.core.task_router import classify_task, get_workflow
  task_type = classify_task("帮我调研量子纠错的最新进展")
  workflow = get_workflow(task_type)
"""

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

_DATA_DIR = Path(__file__).parent.parent / "data"
_PLANS_DIR = Path(__file__).parent.parent.parent.parent / ".claude" / "plans"


class TaskType(Enum):
    RESEARCH = "research"      # 调研/分析/对比
    DEVELOP = "develop"        # 新功能/重构/架构
    FIX = "fix"                # bug修复/问题诊断
    REVIEW = "review"          # 代码审查/质量检查
    QUALITY = "quality"        # 完整大循环 Q0-Q7
    THEORY = "theory"          # 科研: 想法→论证→计算→审查


@dataclass
class WorkflowStep:
    name: str                  # 步骤名
    description: str           # 做什么
    agent: Optional[str]       # 用哪个 agent (None = 主 agent 自己做)
    auto: bool = True          # 是否自动执行 (False = 需要用户确认)
    verify: Optional[str] = None  # 验证命令 (None = 不验证)


@dataclass
class Workflow:
    task_type: TaskType
    steps: List[WorkflowStep]
    max_fix_rounds: int = 3    # 最大修复轮数
    require_plan: bool = True  # 是否需要先生成 plan


# ── 任务分类 ──

_TASK_PATTERNS = {
    TaskType.THEORY: [
        r"想法|idea|假设|hypothesis|猜想|conjecture",
        r"论证|prove|证明|demonstrate|验证.*理论",
        r"推导|derive|derivation|公式",
        r"物理.*计算|numerical.*verif|数值验证",
        r"适用条件|applicability|regime|边界条件",
        r"示意图|schematic|能级图|相图|phase diagram",
    ],
    TaskType.RESEARCH: [
        r"调研|研究|分析|对比|评估|survey|research|investigate|compare",
        r"了解|学习|探索|搜索|查找",
        r"论文|paper|arxiv|文献",
        r"最佳实践|best practice|前沿|state.of.the.art",
        r"现状|趋势|进展|方向|生态|landscape",
    ],
    TaskType.DEVELOP: [
        r"开发|实现|构建|创建|新增|添加|develop|implement|build|create",
        r"重构|迁移|升级|refactor|migrate|upgrade",
        r"功能|feature|模块|module|系统|system",
        r"写一个|做一个|设计|编写|生成代码",
    ],
    TaskType.FIX: [
        r"修复|修改|解决|fix|bug|error|问题|issue",
        r"崩溃|crash|失败|fail|broken",
        r"不工作|not working|报错",
    ],
    TaskType.REVIEW: [
        r"审查|检查|review|audit|inspect",
        r"代码质量|code quality|安全审计|security",
        r"测试覆盖|test coverage",
    ],
    TaskType.QUALITY: [
        r"大循环|bigloop|quality cycle|质量循环",
        r"完整检查|全面检查|quality audit",
    ],
}


def classify_task(prompt: str, *, override_type: Optional[TaskType] = None) -> TaskType:
    """
    根据用户输入自动分类任务类型。
    QUALITY 优先级最高（"全自动"直接匹配），其余按分数。

    TU-2 (plan v2 2026-04-22): `override_type` kwarg — keyword-only (the `*,`
    separator forces it) so positional-arg confusion cannot inject from user
    text. CLI wrappers / SKILL.md can pass `override_type=TaskType.DEVELOP`
    when CEO explicitly says so; prompt-body overrides like "[TYPE:develop]"
    are NEVER honored — they're detected + logged by _detect_injection_attempt
    (TU-5) and then ignored (OWASP LLM01 instruction-hierarchy compliance).

    Returns:
        TaskType enum value
    """
    # TU-5: audit prompt-body override attempts (does not change routing)
    _detect_injection_attempt(prompt)

    # TU-2: trusted CLI override wins over classification
    if override_type is not None and isinstance(override_type, TaskType):
        return override_type

    # 剥离触发词后分类 ("全自动"是模式触发词，不是任务类型)
    cleaned = re.sub(r"全自动|自主完成|autopilot|一条龙|从头到尾", "", prompt).strip()
    if not cleaned:
        return TaskType.QUALITY  # 纯"全自动"无具体任务 -> QUALITY

    # QUALITY 只在明确提到大循环/质量检查时触发
    for pattern in _TASK_PATTERNS[TaskType.QUALITY]:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return TaskType.QUALITY

    scores = {t: 0 for t in TaskType if t != TaskType.QUALITY}

    for task_type, patterns in _TASK_PATTERNS.items():
        if task_type == TaskType.QUALITY:
            continue
        for pattern in patterns:
            matches = re.findall(pattern, cleaned, re.IGNORECASE)
            scores[task_type] += len(matches) * 2

    # 歧义解决
    # THEORY vs RESEARCH: "推导/论证/假设/计算验证" -> THEORY 优先
    if scores[TaskType.THEORY] > 0 and scores[TaskType.RESEARCH] > 0:
        if re.search(r"推导|论证|假设|hypothesis|derive|prove|计算验证", cleaned, re.IGNORECASE):
            scores[TaskType.THEORY] += 5
        else:
            scores[TaskType.RESEARCH] += 3

    # DEVELOP vs RESEARCH: "调研" 类关键词出现时 research 优先
    if scores[TaskType.DEVELOP] > 0 and scores[TaskType.RESEARCH] > 0:
        if re.search(r"调研|研究|分析|对比|survey|compare", cleaned, re.IGNORECASE):
            scores[TaskType.RESEARCH] += 5
        else:
            scores[TaskType.DEVELOP] += 3

    # TU-3 (plan v2 2026-04-22): FIX vs DEVELOP disambiguator.
    # "解决/修改 N 条/件/个" OR "解决 + 新增/添加/create" reads as DEVELOP.
    # BUT: bug/error/漏洞 markers negate — those are genuine FIX.
    #
    # LOG-R1-HIGH-1 (2026-04-22): +5 wasn't enough — a dense-FIX prompt with
    # 3 fix-keywords (6 pts) beat DEVELOP-after-bump (5 pts). Fix:
    # post-bump, guarantee DEVELOP > FIX by at least 1 point when the
    # disambiguator triggers. This makes the "when fire → always wins"
    # contract explicit. We do NOT subtract from FIX (preserves audit
    # of why FIX scored that high).
    #
    # Examples:
    #   "解决 4 条断链"            → DEVELOP wins (bumped above FIX)
    #   "修改 3 条规则"            → DEVELOP wins
    #   "解决 1 个 bug"            → FIX (bug marker negates bump)
    #   "修复 SQL 注入漏洞"         → FIX (漏洞 marker)
    #   "解决 修复 修改 3 条断链"   → DEVELOP wins (dense FIX, but bumped high)
    if scores[TaskType.FIX] > 0 and not _FIX_BUG_MARKER_RE.search(cleaned):
        if _DEVELOP_CREATION_RE.search(cleaned) or _DIGIT_COUNT_RE.search(cleaned):
            # Guarantee DEVELOP > FIX when bump fires
            scores[TaskType.DEVELOP] = max(
                scores[TaskType.DEVELOP] + 5,
                scores[TaskType.FIX] + 1,
            )

    # TU-4 (plan v2 2026-04-22): imperative marker nudge. CEO "请用 develop"
    # adds +3 to matching type score — breaks ties without unilateral override.
    imp_match = _IMPERATIVE_MARKER_RE.search(cleaned)
    if imp_match:
        target = imp_match.group(1).lower()
        nudge_map = {
            "develop": TaskType.DEVELOP, "开发": TaskType.DEVELOP,
            "complex": TaskType.DEVELOP,  # "complex" in imperative → DEVELOP semantics
            "research": TaskType.RESEARCH, "调研": TaskType.RESEARCH,
            "fix": TaskType.FIX, "修复": TaskType.FIX,
            "review": TaskType.REVIEW, "审查": TaskType.REVIEW,
            "theory": TaskType.THEORY, "复杂": TaskType.DEVELOP,
        }
        nudge_target = nudge_map.get(target)
        if nudge_target is not None and nudge_target in scores:
            scores[nudge_target] += 3

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return TaskType.DEVELOP

    return best


# ── 复杂度评估 ──

_COMPLEXITY_SIGNALS = {
    "complex": [
        r"全自动", r"autopilot", r"架构", r"architecture",
        r"新模块", r"new module", r"系统", r"system",
        r"从零", r"from scratch", r"重写", r"rewrite",
        r"进化", r"evolve", r"深度", r"论文", r"paper",
        r"想法", r"idea", r"hypothesis", r"假设",
        r"推导", r"derive", r"论证", r"prove",
        # TU-1 (plan v2 2026-04-22): literal complexity words previously missing
        # (CEO-observed bug: "complex难度" was not matched on 2026-04-21/22)
        r"\bcomplex\b", r"复杂", r"高难度", r"困难",
        r"全面", r"整体", r"彻底", r"系统性", r"联合",
        r"comprehensive", r"thorough",
    ],
    "medium": [
        r"新增功能", r"new feature", r"重构", r"refactor",
        r"迁移", r"migrate", r"添加", r"add",
        r"多文件", r"multiple files",
    ],
}

# TU-5 (plan v2 2026-04-22): prompt-body tokens that LOOK like CLI override.
# Detect + audit + ignore (OWASP LLM01 instruction-hierarchy). Only CLI kwargs
# `override_type=...` are trusted, never prompt body.
_INJECTION_PATTERNS = [
    re.compile(r"\[TYPE\s*[:=]\s*(\w+)\]", re.IGNORECASE),
    re.compile(r"\[COMPLEXITY\s*[:=]\s*(\w+)\]", re.IGNORECASE),
    re.compile(r"--task-type\s*[:=]?\s*(\w+)", re.IGNORECASE),
    re.compile(r"<override>(.*?)</override>", re.IGNORECASE | re.DOTALL),
]

# TU-4 (plan v2 2026-04-22): imperative markers that nudge classification when
# CEO explicitly signals a task-type preference. NEVER overrides — adds +3 to
# the target-type score so it breaks score ties toward CEO intent without
# unilateral takeover (matches research Framing-3 recommendation).
_IMPERATIVE_MARKER_RE = re.compile(
    r"(?:请|我明确|我要|必须|务必|一定|specify|explicit)"
    r"\s*[说要用采走调做]?[:：]?\s*"
    r"(develop|complex|research|fix|review|theory|"
    r"开发|调研|修复|审查|复杂)",
    re.IGNORECASE,
)

# TU-3 (plan v2 2026-04-22): FIX-vs-DEVELOP disambiguator helpers.
# "解决 N 条/件/个" without bug-markers reads as DEVELOP (new feature count),
# not FIX. Bug-markers negate the heuristic.
_DEVELOP_CREATION_RE = re.compile(r"新增|添加|create|implement", re.IGNORECASE)
_DIGIT_COUNT_RE = re.compile(r"\d+\s*(?:条|件|个|处)")
_FIX_BUG_MARKER_RE = re.compile(
    r"bug|error|漏洞|问题|crash|broken|不工作|not working",
    re.IGNORECASE,
)


def _detect_injection_attempt(prompt: str) -> None:
    """TU-5: log prompt-body override attempts (OWASP LLM01 audit).

    Never modifies routing; purely records to trace_sink for post-hoc review.
    Emits `classify_injection_attempt` event with SHA256(prompt)[:12] + the
    matched token span[:60] (no raw prompt body per LLM02).

    SEC-R1-MED-1 (2026-04-22): the <override>...</override> pattern captures
    arbitrary inner content; if the user embeds a secret in that tag, the
    first 30 chars would leak to traces.jsonl. For this specific pattern,
    redact matched_arg to "<redacted>" — the event still fires for audit,
    but the payload carries no potentially-sensitive content.
    """
    # SEC-R1-LOW-4: guard against DoS on huge prompts; truncate before regex
    if len(prompt) > 10_000:
        prompt_for_scan = prompt[:10_000]
    else:
        prompt_for_scan = prompt
    for idx, rgx in enumerate(_INJECTION_PATTERNS):
        m = rgx.search(prompt_for_scan)
        if m is None:
            continue
        # SEC-R1-MED-1: <override>...</override> is index 3; captures free text
        is_override_tag = idx == 3
        try:
            from src.core.trace_sink import write_trace_event
            # SEC-R1-LOW-3: SHA256 > SHA1 for hygiene; 12 hex chars still
            sha = hashlib.sha256(
                prompt.encode("utf-8", errors="replace")
            ).hexdigest()[:12]
            if is_override_tag:
                matched_arg = "<redacted>"
                # SEC-R1-MED-1: matched_token also wraps the secret; redact too
                matched_token = "<override>...</override>"
            else:
                matched_arg = (m.group(1) if m.lastindex else "")[:30]
                matched_token = m.group(0)[:60]
            write_trace_event(
                "classify_injection_attempt",
                {
                    "prompt_sha256_12": sha,
                    "matched_token": matched_token,
                    "matched_arg": matched_arg,
                    "source": "task_router.classify_task",
                },
            )
        except Exception as e:
            # SEC-R1-MED-2: was silent pass, now log — lets operator notice
            # trace_sink breakage (file locked, import error) instead of
            # forensic coverage silently dropping to zero.
            try:
                import logging as _log
                _log.getLogger(__name__).warning(
                    "task_router: classify_injection_attempt audit failed: %s", e
                )
            except Exception:
                pass  # inner exception swallowed to guarantee routing proceeds
        break  # one event per classify call, first match wins


def assess_complexity(prompt: str, *, override_complexity: Optional[str] = None) -> str:
    """Assess task complexity: simple / medium / complex.

    Complex tasks get full 5-phase pipeline with 3 hard gates.
    Medium tasks get Phase 0 (plan) + implementation + BigLoop.
    Simple tasks get direct execution with hook protection only.

    TU-2 (plan v2 2026-04-22): `override_complexity` kwarg — keyword-only so
    positional-arg injection cannot reach it. Trusted CLI channel only.
    """
    if override_complexity in ("simple", "medium", "complex"):
        return override_complexity

    cleaned = re.sub(r"全自动|自主完成|autopilot|一条龙", "", prompt).strip()
    word_count = len(re.findall(r"[\u4e00-\u9fff]", prompt)) + len(re.findall(r"[a-zA-Z]+", prompt))

    # Explicit "全自动" trigger always means complex
    if re.search(r"全自动|autopilot|一条龙", prompt, re.IGNORECASE):
        return "complex"

    # Check complex signals
    for pattern in _COMPLEXITY_SIGNALS["complex"]:
        if re.search(pattern, prompt, re.IGNORECASE):
            return "complex"

    # Check medium signals
    for pattern in _COMPLEXITY_SIGNALS["medium"]:
        if re.search(pattern, prompt, re.IGNORECASE):
            return "medium"

    # Short prompts are simple
    if word_count < 15:
        return "simple"

    return "medium"


# ── 工作流模板 ──

WORKFLOWS: Dict[TaskType, Workflow] = {
    TaskType.RESEARCH: Workflow(
        task_type=TaskType.RESEARCH,
        require_plan=False,
        max_fix_rounds=1,
        steps=[
            WorkflowStep("scope_validate", "回答 4 问: 解决什么问题/不做会怎样/更简单替代/MVP", agent="scope-validator"),
            WorkflowStep("prime_kb", "加载知识库中的相关 pattern", agent=None,
                         verify="python -m src.core.pattern_extractor prime --keywords TASK_KEYWORDS"),
            WorkflowStep("research_plan", "制定调研计划: 假设列表, 搜索策略, 预期产出格式", agent=None),
            WorkflowStep("research", "执行调研: WebSearch + chub + Agent-Reach + Crawl4AI", agent="chief-researcher"),
            WorkflowStep("verify_claims", "交叉验证关键发现: 检查数据来源, 确认引用准确性", agent=None,
                         verify="python -m src.core.pattern_extractor prime --keywords TASK_KEYWORDS"),
            WorkflowStep("review", "独立挑战调研结论: 遗漏了什么? 有没有反例?", agent="code-reviewer"),
            WorkflowStep("report", "输出结构化调研报告", agent=None),
            WorkflowStep("extract_patterns", "从调研结果提取 pattern 到知识库", agent=None,
                         verify="python -m src.core.pattern_extractor stats"),
        ],
    ),

    TaskType.DEVELOP: Workflow(
        task_type=TaskType.DEVELOP,
        require_plan=True,  # 开发必须先生成 plan
        max_fix_rounds=3,
        steps=[
            WorkflowStep("scope_validate", "回答 4 问", agent="scope-validator"),
            WorkflowStep("research", "Industry benchmark: search GitHub/papers for existing solutions",
                         agent="chief-researcher"),
            WorkflowStep("plan", "生成实现计划，保存到 .claude/plans/", agent=None),
            WorkflowStep("implement", "按 plan 实现，Phase A(分解) -> B(编码)", agent=None),
            WorkflowStep("test", "运行 pytest 验证实现", agent=None,
                         verify="python -m pytest tests/ -q --tb=short"),
            WorkflowStep("review_security", "安全审查: 注入/XSS/路径穿越/密钥泄露", agent="security-reviewer"),
            WorkflowStep("review_logic", "逻辑审查: 边界/空值/API契约/状态管理", agent="logic-reviewer"),
            WorkflowStep("review_coverage", "覆盖审查: 测试覆盖/边缘用例/Mock卫生", agent="coverage-reviewer"),
            WorkflowStep("fix_loop", "修复审查 findings (retry_tracker 3 次限制)", agent="fix-agent"),
            WorkflowStep("security", "安全扫描", agent=None,
                         verify="python memex/core/security_scanner.py memex/core/"),
            WorkflowStep("regression", "回归测试 (对比 Q1 基线)", agent=None,
                         verify="python -m pytest tests/ -q --tb=short"),
            WorkflowStep("extract_patterns", "提取本次开发的 pattern", agent=None,
                         verify="python -m src.core.pattern_extractor stats"),
        ],
    ),

    TaskType.FIX: Workflow(
        task_type=TaskType.FIX,
        require_plan=False,
        max_fix_rounds=3,
        steps=[
            WorkflowStep("scope_validate", "确认修复目标: 是根因还是症状? 是否有更深层问题?", agent="scope-validator"),
            WorkflowStep("prime_kb", "加载相关 pattern (历史修复经验)", agent=None,
                         verify="python -m src.core.pattern_extractor prime --keywords TASK_KEYWORDS"),
            WorkflowStep("diagnose", "根因分析: 读代码 + grep + 日志 (Iron Law 2: 先根因后修复)", agent="investigator"),
            WorkflowStep("root_cause_doc", "文档化根因: 记录到 commit message 或 pattern KB", agent=None),
            WorkflowStep("fix", "应用最小修复", agent="fix-agent"),
            WorkflowStep("test", "验证修复", agent=None,
                         verify="python -m pytest tests/ -q --tb=short"),
            WorkflowStep("review", "独立审查修复: 是否引入新问题?", agent="code-reviewer"),
            WorkflowStep("security", "安全扫描: 修复是否引入新漏洞", agent=None,
                         verify="python memex/core/security_scanner.py memex/core/"),
            WorkflowStep("regression", "回归测试: 确认无副作用", agent=None,
                         verify="python -m pytest tests/ -q --tb=short"),
            WorkflowStep("extract_patterns", "提取根因和修复经验到知识库", agent=None,
                         verify="python -m src.core.pattern_extractor stats"),
        ],
    ),

    TaskType.REVIEW: Workflow(
        task_type=TaskType.REVIEW,
        require_plan=False,
        max_fix_rounds=2,
        steps=[
            WorkflowStep("scope_validate", "明确审查范围: 哪些文件/模块, 审查标准是什么", agent="scope-validator"),
            WorkflowStep("prime_kb", "加载审查相关 pattern (历史 bug 类别)", agent=None,
                         verify="python -m src.core.pattern_extractor prime --keywords TASK_KEYWORDS"),
            WorkflowStep("review_plan", "制定审查计划: 重点区域, 检查清单, 严重度标准", agent=None),
            WorkflowStep("review", "全量代码审查", agent="qa-director"),
            WorkflowStep("security", "安全扫描", agent=None,
                         verify="python memex/core/security_scanner.py memex/core/"),
            WorkflowStep("cross_review", "第二视角挑战: 审查审查者的发现, 是否有遗漏或误报", agent="code-reviewer"),
            WorkflowStep("report", "输出 Bug JSON 清单 (合并两轮审查)", agent=None),
            WorkflowStep("fix_loop", "修复发现的问题", agent="fix-agent"),
            WorkflowStep("regression", "回归测试", agent=None,
                         verify="python -m pytest tests/ -q --tb=short"),
            WorkflowStep("extract_patterns", "提取审查发现到知识库", agent=None,
                         verify="python -m src.core.pattern_extractor stats"),
        ],
    ),

    TaskType.QUALITY: Workflow(
        task_type=TaskType.QUALITY,
        require_plan=False,
        max_fix_rounds=2,
        steps=[
            WorkflowStep("prime_kb", "加载知识库 pattern (上次质量循环的经验)", agent=None,
                         verify="python -m src.core.pattern_extractor prime --keywords TASK_KEYWORDS"),
            WorkflowStep("baseline", "记录当前测试/安全基线状态", agent=None,
                         verify="python -m pytest tests/ --collect-only -q"),
            WorkflowStep("bigloop", "执行完整 BigLoop Q0-Q7", agent=None,
                         verify="python -c \"from src.core.big_loop import run_big_loop_sync; print(run_big_loop_sync())\""),
            WorkflowStep("review_findings", "挑战 BigLoop 发现: 是否有误报? 优先级正确吗?", agent="code-reviewer"),
            WorkflowStep("extract_patterns", "提取本轮质量循环的 pattern", agent=None,
                         verify="python -m src.core.pattern_extractor stats"),
        ],
    ),

    TaskType.THEORY: Workflow(
        task_type=TaskType.THEORY,
        require_plan=False,  # THEORY uses formulation, not plan file
        max_fix_rounds=2,
        steps=[
            # Phase 1: Formulate (steps 1-4, ends with derivation review gate)
            WorkflowStep("scope_validate", "明确核心断言: 精确陈述 claim, 什么能证伪它?",
                         agent="scope-validator",
                         verify="python -c \"print('scope_validate: agent output required')\""),
            WorkflowStep("literature_survey", "文献定位: 前人工作, 空白点, 本想法的新颖性",
                         agent="research-assistant",
                         verify="python -c \"print('T1: literature survey agent output required')\""),
            WorkflowStep("formulation", "严格数学表述: 哈密顿量/方程/假设条件, 逐步推导", agent=None,
                         verify="python -c \"from pathlib import Path; files=list(Path('.').rglob('derivation*')); assert files, 'No derivation file found (derivation.md or derivation.tex)'\""),
            WorkflowStep("derivation_review", "独立审查推导: 数学正确性, 遗漏步骤, 单位一致性",
                         agent="reviewer-technical",
                         verify="python -c \"print('T3: derivation_review agent output required')\""),
            # Phase 2: Compute (steps 5-8)
            WorkflowStep("computation_plan", "计算方案: 参数空间, 预期结果, 收敛判据", agent=None,
                         verify="python -c \"from pathlib import Path; files=list(Path('.').rglob('params.*')) + list(Path('.').rglob('computation_plan*')); assert files, 'No computation plan found'\""),
            WorkflowStep("compute", "数值计算: Python/<your-stack>, checkpoint, 并行", agent="compute-runner",
                         verify="python -c \"from pathlib import Path; assert any(Path('.').rglob('*.npz')) or any(Path('.').rglob('*.csv')), 'No computation output found'\""),
            WorkflowStep("statistics", "统计分析: bootstrap CI, 不确定度预算, 收敛曲线",
                         agent="statistics-specialist",
                         verify="python -c \"from pathlib import Path; assert any(Path('.').rglob('stats_report*')), 'No stats_report found'\""),
            WorkflowStep("visualization", "作图: |psi|^2叠加势场, 能级图, 相图, 示意图", agent=None,
                         verify="python -c \"from pathlib import Path; figs=list(Path('.').rglob('fig_*.p*')); print(f'{len(figs)} figures found'); assert figs, 'No figures generated'\""),
            # Phase 3: Contextualize (steps 9-11)
            WorkflowStep("applicability", "适用条件: 参数范围, 极限行为, 近似何时破坏", agent=None,
                         verify="python -c \"print('T8: applicability analysis must be documented in draft')\""),
            WorkflowStep("landscape_comparison", "横向对比: 本结果 vs 已发表数据, 定位新颖贡献, 识别未覆盖空白",
                         agent="research-assistant",
                         verify="python -c \"print('T9: landscape comparison agent output required')\""),
            WorkflowStep("synthesis", "综合叙述: 将推导+计算+对比+图合为连贯论述", agent=None,
                         verify="python -c \"from pathlib import Path; drafts=list(Path('.').rglob('draft*')); assert drafts, 'No draft file found'\""),
            # Phase 4: Rigorous review (steps 12-15, iterative cross-review)
            WorkflowStep("technical_review", "Referee 1: 推导精度, 数值可靠性, claim-evidence 对齐",
                         agent="reviewer-technical",
                         verify="python -c \"print('T11: technical_review agent output required')\""),
            WorkflowStep("narrative_review", "Referee 2: 重要性定位, 清晰度, 可读性, 开头 hook",
                         agent="reviewer-narrative",
                         verify="python -c \"print('T12: narrative_review agent output required')\""),
            WorkflowStep("consistency_check", "跨文件一致性: 符号/数值/图文/引用/术语",
                         agent="consistency-auditor",
                         verify="python -c \"print('T13: consistency_check agent output required')\""),
            WorkflowStep("extract_patterns", "提取方法论和发现到知识库", agent=None,
                         verify="python -m src.core.pattern_extractor stats"),
        ],
    ),
}


# ── Per-type review routing ──

@dataclass
class ReviewConfig:
    """Stage 4 review configuration per task type."""
    mode: str                          # "iterative" | "single" | "workflow"
    reviewers: List[str] = field(default_factory=list)   # agent names for iterative mode
    max_iterations: int = 1            # max cross-review iterations
    note: str = ""                     # explanation for workflow mode


@dataclass
class CouncilConfig:
    """Stage 1.2 planning-council configuration.

    2026-04-21 plan_feedback_loop bug-2 fix: when research_brief.md already
    covers the ground the council would re-explore, downgrade to 3-expert
    mini council to cut the ~40% redundancy seen in the memory_syste task.
    """
    mode: str = "full"                 # "full" (5-expert) | "mini" (3-expert)
    experts: List[str] = field(default_factory=list)
    rationale: str = ""                # why this mode was chosen
    brief_bytes: int = 0               # research_brief.md size at decision time


_FULL_COUNCIL_EXPERTS = [
    "architect", "chief-researcher", "verifier",
    "logic-reviewer", "coverage-reviewer",
]

_MINI_COUNCIL_EXPERTS = [
    "architect", "verifier", "coverage-reviewer",
]

# Thresholds for mini-mode eligibility. Tuned against the actual
# memory_syste research_brief.md (~5.5 KB, 7 sections, §1-§7).
_MINI_COUNCIL_MIN_BRIEF_BYTES = 2000
_MINI_COUNCIL_REQUIRED_HEADINGS = ("## § 1", "## § 2", "## § 3")


def decide_council_mode(
    research_brief_path: Optional[str],
    complexity: Optional[str] = None,
) -> CouncilConfig:
    """Decide whether to run full 5-expert / mini 3-expert / skip council.

    TU-3 (2026-04-22): `complexity` param routes by task difficulty. Values:
      - "simple": skip council entirely (0 expert, 0 wall-time)
      - "medium" / "complex" / None: fall through to brief-based mini/full decision
    Accepts None for backward compat with pre-TU-3 callers.

    Mini mode fires iff ALL conditions hold (for medium/complex only):
      - research_brief_path exists and is readable
      - file size >= 2000 bytes
      - contains at least 3 top-level '## § N' headings (standard template)

    Rationale: council's highest-ROI output is conflict surfacing. If
    research_brief.md already covers contamination root cause + gap
    inventory + alternatives, the 4 of 5 experts that would re-describe
    the problem space produce 60% overlap (measured on memory_syste
    2026-04-21). Mini keeps (architect: approach design) +
    (verifier: worst-case challenge) + (coverage-reviewer: AC oracle)
    which are the 3 non-duplicated roles.

    Simple tasks (classify_task == FIX with low LoC estimate or trivial
    documentation edits) never justify council cost and skip entirely.
    """
    import os as _os

    # TU-3 (2026-04-22) complexity routing — short-circuit for simple.
    if complexity == "simple":
        return CouncilConfig(
            mode="skip",
            experts=[],
            rationale=(
                "complexity='simple': no council (cost > expected gain at "
                "this task size)"
            ),
            brief_bytes=0,
        )

    if not research_brief_path:
        return CouncilConfig(
            mode="full",
            experts=list(_FULL_COUNCIL_EXPERTS),
            rationale="no research_brief_path provided",
            brief_bytes=0,
        )
    if not _os.path.isfile(research_brief_path):
        return CouncilConfig(
            mode="full",
            experts=list(_FULL_COUNCIL_EXPERTS),
            rationale=f"research_brief not found at {research_brief_path}",
            brief_bytes=0,
        )
    try:
        size = _os.path.getsize(research_brief_path)
    except Exception:
        return CouncilConfig(
            mode="full",
            experts=list(_FULL_COUNCIL_EXPERTS),
            rationale="research_brief stat() failed; defaulting to full",
            brief_bytes=0,
        )

    if size < _MINI_COUNCIL_MIN_BRIEF_BYTES:
        return CouncilConfig(
            mode="full",
            experts=list(_FULL_COUNCIL_EXPERTS),
            rationale=(
                f"research_brief {size}B < {_MINI_COUNCIL_MIN_BRIEF_BYTES}B "
                "threshold"
            ),
            brief_bytes=size,
        )

    # Check heading coverage
    try:
        with open(research_brief_path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return CouncilConfig(
            mode="full",
            experts=list(_FULL_COUNCIL_EXPERTS),
            rationale="research_brief read() failed",
            brief_bytes=size,
        )

    headings_found = sum(
        1 for h in _MINI_COUNCIL_REQUIRED_HEADINGS if h in text
    )
    if headings_found < len(_MINI_COUNCIL_REQUIRED_HEADINGS):
        return CouncilConfig(
            mode="full",
            experts=list(_FULL_COUNCIL_EXPERTS),
            rationale=(
                f"research_brief has {headings_found}/"
                f"{len(_MINI_COUNCIL_REQUIRED_HEADINGS)} required "
                "headings; needs full council to fill gaps"
            ),
            brief_bytes=size,
        )

    # All preconditions satisfied → mini mode
    return CouncilConfig(
        mode="mini",
        experts=list(_MINI_COUNCIL_EXPERTS),
        rationale=(
            f"research_brief {size}B covers §1-§3; "
            "mini council sufficient (3 non-duplicated roles)"
        ),
        brief_bytes=size,
    )


REVIEW_CONFIGS: Dict[TaskType, ReviewConfig] = {
    TaskType.DEVELOP: ReviewConfig(
        mode="iterative",
        reviewers=["security-reviewer", "logic-reviewer", "coverage-reviewer"],
        max_iterations=3,
    ),
    TaskType.THEORY: ReviewConfig(
        mode="iterative",
        reviewers=["reviewer-technical", "reviewer-narrative", "consistency-auditor"],
        max_iterations=2,
    ),
    TaskType.FIX: ReviewConfig(
        mode="single",
        reviewers=["code-reviewer"],
        max_iterations=1,
    ),
    TaskType.RESEARCH: ReviewConfig(
        mode="workflow",
        note="review step already in workflow (step 6: code-reviewer challenges conclusions)",
    ),
    TaskType.REVIEW: ReviewConfig(
        mode="workflow",
        note="cross_review step already in workflow (qa-director + code-reviewer)",
    ),
    TaskType.QUALITY: ReviewConfig(
        mode="workflow",
        note="BigLoop Q2 (qa-director) handles review internally",
    ),
}


def get_review_config(task_type: TaskType) -> ReviewConfig:
    """Get the Stage 4 review configuration for a task type."""
    return REVIEW_CONFIGS.get(task_type, ReviewConfig(mode="single", reviewers=["code-reviewer"]))


# ---- U9: chunked review (BL-1) ----
# Tight regex per logic-iter1-7: three-hash + space + literal TU- + ID
# where ID = digits + optional trailing letter (TU-3a / TU-3b sub-IDs).
# Rejects false-positives like "TU-ple example" (non-numeric prefix).
_TU_HEADING_RE = re.compile(r"^#{3}\s+TU-([0-9]+(?:[a-z])?)\b", re.MULTILINE)


def chunk_for_review(plan_text: str, max_chunk_tu: int = 16) -> List[List[str]]:
    """U9 (long_term_plan_v2 BL-1): greedy bin-pack TU IDs into chunks.

    Solves 80 TU x 3 reviewer x 3 findings = 720 entry OOM in Stage 4 review:
    each chunk gets its own 3-reviewer Mode-A iter, then a cross-cluster-auditor
    consolidates. NOT deps-aware (greedy by sequential TU index; deps-aware
    deferred to v3).

    Args:
        plan_text: full plan_v<N>.md content
        max_chunk_tu: max TUs per chunk (1-N; default 16)

    Returns:
        List of TU-id-string lists, each sublist <= max_chunk_tu.

    Raises:
        ValueError: if max_chunk_tu < 1, or no TU headings found in plan.

    Idempotent: same input bytes -> byte-identical chunks (no random).
    Round-trip: sorted(flatten(chunks)) == sorted(all_tu_ids).
    """
    if not isinstance(max_chunk_tu, int) or max_chunk_tu < 1:
        raise ValueError(f"max_chunk_tu must be int >= 1, got {max_chunk_tu!r}")

    tu_ids = [f"TU-{m.group(1)}" for m in _TU_HEADING_RE.finditer(plan_text)]
    if not tu_ids:
        raise ValueError("plan §3 missing or empty (no '### TU-N' headings found)")

    chunks = [tu_ids[i:i + max_chunk_tu]
              for i in range(0, len(tu_ids), max_chunk_tu)]
    return chunks


def get_workflow(task_type: TaskType) -> Workflow:
    """获取对应任务类型的工作流模板"""
    return WORKFLOWS[task_type]


def generate_verification_commands(task_type: TaskType) -> str:
    """
    生成每种工作流的验证命令块。
    所有工作流都包含 BigLoop 级别的验证能力。
    """
    # 通用验证命令 (所有非 RESEARCH 工作流都需要)
    common_verify = """
VERIFICATION COMMANDS (run these to verify acceptance criteria):
```python
import asyncio
from pathlib import Path

# V1: pytest
import subprocess
r = subprocess.run(['python', '-m', 'pytest', 'tests/', '-q', '--tb=short'],
    capture_output=True, text=True, timeout=180)
print(f"PYTEST: {r.stdout.strip().split(chr(10))[-1]}")

# V2: security scan
from src.core.security_scanner import scan_directory, format_report
findings = scan_directory(Path('memex/core'))
print(format_report(findings, 'memex/core/'))

# V3: knowledge base prime
from src.core.pattern_extractor import prime, format_prime_output
results = prime(keywords=['TASK_KEYWORDS'], work_type='WORK_TYPE')
print(format_prime_output(results))
```

After each verification, mark criteria:
```python
from src.core.task_router import verify_criteria
verify_criteria('CRITERION_ID', 'evidence text')
```

Check if all done:
```python
from src.core.task_router import check_all_criteria_met
all_met, summary = check_all_criteria_met()
print(f"All criteria met: {all_met} -- {summary}")
```
"""

    if task_type == TaskType.RESEARCH:
        return """
VERIFICATION: Before completing, ensure:
1. Report has 3+ reference links (verify manually)
2. Clear recommendation with rationale
3. Patterns extracted:
```python
from src.core.task_router import verify_criteria, check_all_criteria_met
verify_criteria('R1', 'links: ...')
verify_criteria('R2', 'recommendation: ...')
verify_criteria('R3', 'patterns extracted')
print(check_all_criteria_met())
```
"""
    return common_verify


def generate_plan_prompt(task_type: TaskType, user_prompt: str) -> str:
    """生成用于注入 Claude context 的工作流执行指令.

    self_evolution_reconnect TU-A1 (2026-05-04):
    在 prompt 头部注入 §0.1 INHERITED LESSONS (top-3 by helpful_count + recency),
    过滤 current task_id 防循环, hard cap 300 token.
    """
    wf = get_workflow(task_type)

    # TU-A1: inject inherited lessons (fail-soft; never blocks plan generation)
    inherited_lessons_block = ""
    try:
        import os as _os
        from src.core.improvement_pattern_injector import (
            load_patterns, filter_by_task_type, format_inherited_lessons_section,
        )
        current_tid = _os.environ.get("MEMEX_ACTIVE_TASK_ID", "")
        all_patterns = load_patterns()
        # Filter by task_type + exclude current task_id (anti-loop)
        filtered = filter_by_task_type(
            all_patterns, task_type.value, top_n=3, byte_cap_utf8=4096,
        )
        if current_tid:
            filtered = [p for p in filtered
                        if str(p.get("task_id", "")) != current_tid]
            filtered = filtered[:3]
        if filtered:
            # format_inherited_lessons_section already includes header
            inherited_lessons_block = format_inherited_lessons_section(filtered) + "\n"
            try:
                from src.core.trace_sink import write_trace_event
                # use existing allowlisted event name (improvement_pattern_injected)
                write_trace_event("improvement_pattern_injected", {
                    "task_type": task_type.value,
                    "current_tid": current_tid,
                    "patterns_count": len(filtered),
                    "byte_size": len(inherited_lessons_block.encode("utf-8")),
                })
            except Exception:  # pragma: no cover
                pass
    except Exception as _e:  # pragma: no cover
        # never block plan generation on injector failure
        inherited_lessons_block = ""

    lines = [
        f"TASK CLASSIFIED AS: {task_type.value.upper()}",
        f"WORKFLOW: {len(wf.steps)} steps, max {wf.max_fix_rounds} fix rounds",
        "",
    ]
    if inherited_lessons_block:
        lines.append(inherited_lessons_block.rstrip())
        lines.append("")
    lines.append("EXECUTION PLAN:")

    for i, step in enumerate(wf.steps, 1):
        agent_info = f" (agent: {step.agent})" if step.agent else ""
        verify_info = f" [verify: {step.verify[:40]}...]" if step.verify else ""
        lines.append(f"  Step {i}: {step.name} -- {step.description}{agent_info}{verify_info}")

    if wf.require_plan:
        # Find the actual step number for 'implement' (dynamic, not hardcoded)
        impl_step = next((i for i, s in enumerate(wf.steps, 1) if s.name == "implement"), "?")
        lines.append("")
        lines.append(f"PLAN FILE REQUIRED: Before Step {impl_step} (implement), you MUST create a plan file at")
        lines.append(f"  .claude/plans/<descriptive-name>.md")
        lines.append("  Include: context, approach, files to modify, verification method.")

    lines.append("")
    lines.append(f"FIX LOOP: If test/review fails, retry fix->test (max {wf.max_fix_rounds} rounds)")
    lines.append("PERSISTENT MODE: Active. Do NOT stop between steps.")
    lines.append("QUALITY FIRST: Do not rush. Each step must complete properly.")
    lines.append("")

    # 注入验证命令和 task_spec
    lines.append("ACCEPTANCE CRITERIA (from task_spec.json):")
    lines.append("Generate task_spec at start:")
    lines.append("```python")
    lines.append("from src.core.task_router import generate_task_spec, TaskType")
    lines.append(f"spec = generate_task_spec(TaskType.{task_type.name}, '''USER_PROMPT''')")
    lines.append("for c in spec['acceptance_criteria']:")
    lines.append("    print(f\"  {c['id']}: {c['criterion']}\")")
    lines.append("```")
    lines.append("")
    lines.append("After each step, verify and mark criteria:")
    lines.append("```python")
    lines.append("from src.core.task_router import verify_criteria, check_all_criteria_met")
    lines.append("verify_criteria('XX', 'evidence')")
    lines.append("all_met, summary = check_all_criteria_met()")
    lines.append("if not all_met: print(f'UNMET: {summary}')")
    lines.append("```")
    lines.append("")
    lines.append("DO NOT call mark_completed() until check_all_criteria_met() returns True.")
    lines.append("The Stop hook will show unmet criteria if you try to exit early.")

    # 验证命令块
    verify_cmds = generate_verification_commands(task_type)
    lines.append("")
    lines.append(verify_cmds)

    return "\n".join(lines)


def generate_task_spec(task_type: TaskType, user_prompt: str) -> dict:
    """
    生成 task_spec.json (PRD-driven, 借鉴 oh-my-claudecode Ralph 模式)

    每个任务开始时生成，包含:
    - acceptance_criteria: 可客观验证的完成条件列表
    - 循环直到所有 criteria 有新鲜证据 (不依赖 LLM 主观判断)
    """
    wf = get_workflow(task_type)

    # 根据任务类型生成默认 acceptance criteria
    criteria_templates = {
        TaskType.RESEARCH: [
            {"id": "R1", "criterion": "调研报告包含至少 3 个参考链接", "verified": False},
            {"id": "R2", "criterion": "给出明确推荐方案及理由", "verified": False},
            {"id": "R3", "criterion": "pattern 已提取到知识库", "verified": False},
        ],
        TaskType.DEVELOP: [
            {"id": "D1", "criterion": "所有新代码通过 ast.parse 语法检查", "verified": False},
            {"id": "D2", "criterion": "pytest 全部通过 (0 failures)", "verified": False},
            {"id": "D3", "criterion": "安全扫描通过 (0 CRITICAL/HIGH)", "verified": False},
            {"id": "D4a", "criterion": "security-reviewer 审查通过", "verified": False},
            {"id": "D4b", "criterion": "logic-reviewer 审查通过", "verified": False},
            {"id": "D4c", "criterion": "coverage-reviewer 审查通过", "verified": False},
            {"id": "D5", "criterion": "回归测试通过 (0 regressions)", "verified": False},
            {"id": "D6", "criterion": "plan 文件已创建在 .claude/plans/", "verified": False},
        ],
        TaskType.FIX: [
            {"id": "F1", "criterion": "根因已识别并记录", "verified": False},
            {"id": "F2", "criterion": "修复后相关测试通过", "verified": False},
            {"id": "F3", "criterion": "无新引入的回归", "verified": False},
            {"id": "F4", "criterion": "修复经验已提取到知识库", "verified": False},
        ],
        TaskType.REVIEW: [
            {"id": "V1", "criterion": "所有源文件已审查", "verified": False},
            {"id": "V2", "criterion": "Bug 清单已输出 (JSON)", "verified": False},
            {"id": "V3", "criterion": "安全扫描已完成", "verified": False},
            {"id": "V4", "criterion": "审查发现已提取到知识库", "verified": False},
        ],
        TaskType.QUALITY: [
            {"id": "Q1", "criterion": "BigLoop Q0-Q7 全部执行", "verified": False},
            {"id": "Q2", "criterion": "0 regressions in Q4", "verified": False},
            {"id": "Q3", "criterion": "所有 agent specs 已排队", "verified": False},
        ],
        TaskType.THEORY: [
            {"id": "T1", "criterion": "文献定位完成 (>= 3 relevant references)", "verified": False},
            {"id": "T2", "criterion": "数学推导完成 (derivation.md 存在且 > 500 chars)", "verified": False},
            {"id": "T3", "criterion": "推导通过 reviewer-technical 审查", "verified": False},
            {"id": "T4", "criterion": "计算方案已制定 (params.json 或 computation_plan 记录)", "verified": False},
            {"id": "T5", "criterion": "数值计算完成 (结果文件存在)", "verified": False},
            {"id": "T6", "criterion": "统计分析完成 (误差棒/置信区间)", "verified": False},
            {"id": "T7", "criterion": "图已生成 (>= 1 figure file)", "verified": False},
            {"id": "T8", "criterion": "适用条件已明确记录", "verified": False},
            {"id": "T9", "criterion": "横向对比完成 (本结果 vs 已发表数据)", "verified": False},
            {"id": "T10", "criterion": "综合叙述完成 (draft.md 存在)", "verified": False},
            {"id": "T11", "criterion": "technical_review PASS", "verified": False},
            {"id": "T12", "criterion": "narrative_review PASS", "verified": False},
            {"id": "T13", "criterion": "consistency_check PASS", "verified": False},
        ],
    }

    complexity = assess_complexity(user_prompt)

    # For complex tasks, add per-type gate criteria
    # Each gate only appears if the workflow has a corresponding step
    if complexity == "complex":
        # All gates available
        _ALL_GATES = {
            "research_completed": {"id": "research_completed", "criterion": "Stage 1.1 industry research completed", "verified": False},
            "plan_approved": {"id": "plan_approved", "criterion": "Phase 0 plan created and approved", "verified": False},
            "verifier_passed": {"id": "verifier_passed", "criterion": "Stage 1.3 independent verifier challenge passed", "verified": False},
            "review_approved": {"id": "review_approved", "criterion": "Phase 4 independent review passed", "verified": False},
            "state_synced": {"id": "state_synced", "criterion": "Stage 5 knowledge-manager sync completed", "verified": False},
            "report_completed": {"id": "report_completed", "criterion": "Phase 5 briefing report generated", "verified": False},
        }
        # Per-type gate selection: only gates that match workflow steps
        _TYPE_GATES = {
            TaskType.DEVELOP:  ["research_completed", "plan_approved", "verifier_passed", "review_approved", "state_synced", "report_completed"],
            TaskType.RESEARCH: ["research_completed", "verifier_passed", "review_approved", "state_synced", "report_completed"],
            TaskType.FIX:      ["review_approved", "state_synced", "report_completed"],
            TaskType.REVIEW:   ["review_approved", "state_synced", "report_completed"],
            TaskType.QUALITY:  ["state_synced", "report_completed"],
            TaskType.THEORY:   ["research_completed", "verifier_passed", "review_approved", "state_synced", "report_completed"],
        }
        gate_ids = _TYPE_GATES.get(task_type, ["state_synced", "report_completed"])
        all_criteria = [_ALL_GATES[g] for g in gate_ids] + criteria_templates.get(task_type, [])
    else:
        all_criteria = criteria_templates.get(task_type, [])

    # Sanitize: stdin pipe on Windows can produce surrogate chars from CJK encoding mismatch
    clean_prompt = user_prompt[:500].encode("utf-8", errors="replace").decode("utf-8")

    # Build step tracker: which steps exist, which are required for completion
    step_names = [s.name for s in wf.steps]
    # Steps that have agents or verify commands are "required" -- they produce artifacts
    required_steps = [s.name for s in wf.steps if s.agent or s.verify]

    spec = {
        "task_type": task_type.value,
        "complexity": complexity,
        "user_prompt": clean_prompt,
        "created_at": datetime.now().isoformat(),
        "workflow_steps": len(wf.steps),
        "max_fix_rounds": wf.max_fix_rounds,
        "acceptance_criteria": all_criteria,
        "step_tracker": {
            "all_steps": step_names,
            "completed_steps": [],
            "current_step": step_names[0] if step_names else None,
            "required_for_completion": required_steps,
        },
        "current_round": 1,
        "status": "in_progress",
        # A3 (plan v2, 2026-04-21): TaskUnit-level scheduling fields.
        # Optional — legacy readers use spec.get(...) so missing fields are harmless.
        "task_id": None,            # foreign key into .claude/harness/tasks/<id>/
        "units": [],                # populated by task_unit_scheduler.initialize_units
        "current_phase": None,
        "current_unit_idx": -1,
    }

    # 保存 task_spec.json (A3: atomic via _atomic_state, Alt-4 reuse)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    spec_file = _DATA_DIR / "task_spec.json"
    try:
        from src.core._atomic_state import atomic_update_json
        atomic_update_json(spec_file, lambda _: spec)
    except ImportError:
        # Fallback if _atomic_state unavailable for any reason
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    return spec


# =====================================================================
# A3 (plan v2 2026-04-21): TaskUnit helpers on task_spec.json
# =====================================================================

def advance_to_unit(unit_idx: int) -> bool:
    """Update task_spec.json current_unit_idx. Bounds-validated (verifier R2 MED-2).

    Returns False for out-of-bounds idx (does NOT write) or missing spec file;
    True on successful write.
    """
    spec_file = _DATA_DIR / "task_spec.json"
    if not spec_file.exists():
        return False
    # Bounds check BEFORE dispatching to atomic_update_json
    try:
        existing = json.loads(spec_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    units = existing.get("units", [])
    if unit_idx < 0 or unit_idx >= len(units):
        return False
    try:
        from src.core._atomic_state import atomic_update_json
    except ImportError:
        return False

    def _set(state):
        state["current_unit_idx"] = unit_idx
        if 0 <= unit_idx < len(state.get("units", [])):
            state["current_phase"] = state["units"][unit_idx].get("phase", "")
        return state
    return atomic_update_json(spec_file, _set)


def current_unit():
    """Return the current TaskUnit dict (from current_unit_idx) or None."""
    spec_file = _DATA_DIR / "task_spec.json"
    if not spec_file.exists():
        return None
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    idx = spec.get("current_unit_idx", -1)
    units = spec.get("units", [])
    if 0 <= idx < len(units):
        return units[idx]
    return None


def _check_artifact(filename: str, required_fields: list, max_age_sec: int = 3600) -> tuple:
    """Check if an artifact file exists, is fresh, has required fields,
    and has sufficient content depth (not a trivially faked file).

    Content depth check: the file must have at least 2 top-level keys
    beyond the required fields, OR a 'findings'/'issues' list, OR a
    'summary' field with >= 50 chars. This prevents minimal
    `{"verdict":"PASS"}` from passing.

    Returns:
        (ok: bool, message: str, data: dict or None)
    """
    artifact = _DATA_DIR / filename
    if not artifact.exists():
        return False, f"{filename} not found. Run the corresponding agent first.", None

    import os as _os
    import time as _time
    age = _time.time() - _os.path.getmtime(str(artifact))
    if age > max_age_sec:
        return False, f"{filename} is {age/60:.0f}min old (max {max_age_sec//60}min)", None

    # OOM defense: reject artifacts >512KB (signals abuse / runaway agent)
    try:
        if artifact.stat().st_size > 512 * 1024:
            return False, (
                f"{filename} too large ({artifact.stat().st_size//1024}KB, max 512KB). "
                f"Re-run reviewer with hard finding cap."
            ), None
    except OSError:
        return False, f"{filename} is unreadable (stat failed)", None

    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False, f"{filename} is corrupt or unreadable", None

    for field in required_fields:
        if field not in data or data[field] is None:
            return False, f"{filename} missing required field: {field}", None

    # Content depth check: prevent trivially faked artifacts.
    # Require EITHER a non-empty findings/issues list OR a summary >= 50 chars.
    # Empty lists don't count (trivially fakeable). Whitespace doesn't count either.
    findings_list = data.get("findings") or data.get("issues")
    # Type contract: findings must be a list (not a dict, not a string)
    if findings_list is not None and not isinstance(findings_list, list):
        return False, (
            f"{filename} findings/issues must be a list, "
            f"got {type(findings_list).__name__}. Reviewer wrote wrong type."
        ), None
    # Findings must contain at least one substantive (non-whitespace, non-empty) entry
    def _is_substantive(item):
        if isinstance(item, dict):
            # Dict must have at least one non-empty string field with >= 10 chars after strip
            return any(
                isinstance(v, str) and len(v.strip()) >= 10
                for v in item.values()
            )
        if isinstance(item, str):
            return len(item.strip()) >= 10
        return False
    has_findings = (
        isinstance(findings_list, list)
        and len(findings_list) > 0
        and any(_is_substantive(item) for item in findings_list)
    )
    # Summary must be substantive AFTER stripping whitespace
    summary = data.get("summary", "")
    has_summary = isinstance(summary, str) and len(summary.strip()) >= 50

    if not (has_findings or has_summary):
        return False, (
            f"{filename} lacks content depth. Need: non-empty findings list with "
            f"substantive entries (>=10 chars each) OR summary >= 50 chars (after strip). "
            f"Got findings={type(findings_list).__name__}({len(findings_list) if isinstance(findings_list, list) else 'N/A'}), "
            f"summary={len(summary.strip())}chars."
        ), None

    return True, "OK", data


# Artifact requirements for per-type criteria.
# Maps criteria_id -> (artifact_filename, required_fields, max_age_seconds)
_ARTIFACT_REQUIREMENTS = {
    # THEORY review criteria (3 separate reviewers)
    "T11": ("last_review_technical.json", ["verdict"], 7200),
    "T12": ("last_review_narrative.json", ["verdict"], 7200),
    "T13": ("last_review_consistency.json", ["verdict"], 7200),
    # DEVELOP review criteria (3 separate reviewers)
    "D4a": ("last_review_security.json", ["verdict"], 3600),
    "D4b": ("last_review_logic.json", ["verdict"], 3600),
    "D4c": ("last_review_coverage.json", ["verdict"], 3600),
    # DEVELOP test/security criteria
    "D2": ("last_pytest_result.json", ["passed"], 3600),
    "D3": ("last_security_scan.json", ["total_findings"], 3600),
}

# Criteria that accept string evidence but require minimum length
_MIN_EVIDENCE_LENGTH = 20


def verify_criteria(criteria_id: str, evidence: str = "") -> bool:
    """标记某个 acceptance criterion 为已验证.

    Enforcement levels:
    1. Gate criteria (plan_approved, review_approved, etc.): require specific file evidence
    2. Artifact criteria (T11-T13, D2-D4): require agent output JSON files
    3. Other criteria: require evidence string with minimum length
    """
    spec_file = _DATA_DIR / "task_spec.json"
    if not spec_file.exists():
        return False
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"[verify_criteria] ERROR: task_spec.json is corrupt or unreadable")
        return False

    # Level 2: Artifact-backed per-type criteria
    if criteria_id in _ARTIFACT_REQUIREMENTS:
        filename, fields, max_age = _ARTIFACT_REQUIREMENTS[criteria_id]
        ok, msg, data = _check_artifact(filename, fields, max_age)
        if not ok:
            print(f"[verify_criteria] REJECTED ({criteria_id}): {msg}")
            return False
        evidence = f"{filename}: {', '.join(f'{k}={data.get(k)}' for k in fields)}"

    # Level 1: Hard evidence checks for gate criteria
    if criteria_id == "plan_approved":
        if not evidence:
            print("[verify_criteria] REJECTED: plan_approved requires evidence=path_to_plan_file")
            return False
        plan_path = _PLANS_DIR / Path(evidence).name
        if not plan_path.exists():
            print(f"[verify_criteria] REJECTED: plan file not found: {evidence}")
            return False
        content = plan_path.read_text(encoding="utf-8", errors="replace")
        if len(content) < 200:
            print("[verify_criteria] REJECTED: plan too short (< 200 chars)")
            return False
        quality_markers = ["approach", "risk", "step", "file", "tradeoff",
                           "方案", "风险", "步骤", "文件"]
        if not any(m.lower() in content.lower() for m in quality_markers):
            print("[verify_criteria] REJECTED: plan lacks quality markers (approach/risk/step/file)")
            return False
        evidence = f"plan={plan_path.name} ({len(content)} chars)"

    elif criteria_id == "review_approved":
        review_file = _DATA_DIR / "last_review.json"
        if not review_file.exists():
            print("[verify_criteria] REJECTED: last_review.json not found. Run code-reviewer first.")
            return False
        import os as _os, time as _time
        age = _time.time() - _os.path.getmtime(str(review_file))
        if age > 1800:
            print(f"[verify_criteria] REJECTED: last_review.json is {age/60:.0f}min old (max 30min)")
            return False
        try:
            review_data = json.loads(review_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[verify_criteria] REJECTED: last_review.json is corrupt or unreadable")
            return False
        if review_data.get("verdict") != "APPROVED":
            print(f"[verify_criteria] REJECTED: verdict is '{review_data.get('verdict')}', not APPROVED")
            return False
        # Must have real review content (not just {"verdict":"APPROVED"})
        if "findings" not in review_data and "summary" not in review_data:
            print("[verify_criteria] REJECTED: last_review.json lacks findings/summary fields")
            return False

        # PROCESS ENFORCEMENT: For iterative review modes, require per-reviewer verdicts.
        # Determine task type from task_spec to get required reviewers.
        try:
            spec_data = json.loads((_DATA_DIR / "task_spec.json").read_text(encoding="utf-8"))
            task_type_str = spec_data.get("task_type", "")
            try:
                tt = TaskType(task_type_str)
                rc = get_review_config(tt)
            except ValueError:
                rc = None

            if rc and rc.mode == "iterative" and rc.reviewers:
                reviewers_data = review_data.get("reviewers", {})
                missing = [r for r in rc.reviewers if r not in reviewers_data]
                if missing:
                    print(
                        f"[verify_criteria] REJECTED: iterative review requires verdicts from "
                        f"ALL reviewers. Missing: {', '.join(missing)}. "
                        f"Got: {list(reviewers_data.keys())}"
                    )
                    return False
                # Each reviewer must have a verdict
                for rname, rdata in reviewers_data.items():
                    if rname in rc.reviewers and not rdata.get("verdict"):
                        print(
                            f"[verify_criteria] REJECTED: reviewer '{rname}' has no verdict"
                        )
                        return False
        except (json.JSONDecodeError, OSError) as e:
            # Spec file corrupt/missing -- can't determine task type for per-reviewer check.
            # Log warning but continue (the basic verdict+findings/summary check above
            # already enforces minimum quality).
            print(f"[verify_criteria] WARN: cannot load task_spec for per-reviewer check: {e}")
        # NOTE: do NOT swallow other exceptions silently. If a different error occurs,
        # let it propagate so it surfaces in the gate output.

        evidence = f"review APPROVED (score={review_data.get('score', '?')})"

    elif criteria_id == "report_completed":
        briefing_file = _DATA_DIR / "last_briefing.json"
        if not briefing_file.exists():
            print("[verify_criteria] REJECTED: last_briefing.json not found")
            return False
        import os as _os, time as _time
        age = _time.time() - _os.path.getmtime(str(briefing_file))
        if age > 7200:
            print(f"[verify_criteria] REJECTED: last_briefing.json is {age/3600:.1f}h old (max 2h)")
            return False
        try:
            briefing_data = json.loads(briefing_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[verify_criteria] REJECTED: last_briefing.json is corrupt or unreadable")
            return False
        # Must have real content: session_id + summary + at least one of commits/findings
        required = ["session_id", "summary"]
        missing = [k for k in required if not briefing_data.get(k)]
        if missing:
            print(f"[verify_criteria] REJECTED: last_briefing.json missing fields: {missing}")
            return False
        if len(briefing_data.get("summary", "")) < 20:
            print("[verify_criteria] REJECTED: briefing summary too short (< 20 chars)")
            return False
        evidence = f"briefing: {briefing_data.get('summary', '')[:80]}"

    elif criteria_id == "verifier_passed":
        verifier_file = _DATA_DIR / "last_verifier.json"
        if not verifier_file.exists():
            print("[verify_criteria] REJECTED: last_verifier.json not found. Run verifier agent first.")
            return False
        import os as _os, time as _time
        age = _time.time() - _os.path.getmtime(str(verifier_file))
        if age > 3600:
            print(f"[verify_criteria] REJECTED: last_verifier.json is {age/60:.0f}min old (max 60min)")
            return False
        try:
            verifier_data = json.loads(verifier_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[verify_criteria] REJECTED: last_verifier.json is corrupt or unreadable")
            return False
        verdict = verifier_data.get("verdict", "")
        if verdict != "PROCEED":
            print(f"[verify_criteria] REJECTED: verdict is '{verdict}', expected PROCEED")
            return False
        if "concerns" not in verifier_data:
            print("[verify_criteria] REJECTED: last_verifier.json missing 'concerns' field")
            return False
        evidence = f"verifier {verdict} ({len(verifier_data.get('concerns', []))} concerns)"

    elif criteria_id == "research_completed":
        research_file = _DATA_DIR / "last_research.json"
        if not research_file.exists():
            print("[verify_criteria] REJECTED: last_research.json not found. Run chief-researcher first.")
            return False
        import os as _os, time as _time
        age = _time.time() - _os.path.getmtime(str(research_file))
        if age > 7200:
            print(f"[verify_criteria] REJECTED: last_research.json is {age/3600:.1f}h old (max 2h)")
            return False
        try:
            research_data = json.loads(research_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[verify_criteria] REJECTED: last_research.json is corrupt or unreadable")
            return False
        has_content = (
            research_data.get("findings")
            or research_data.get("recommendations")
            or research_data.get("summary")
        )
        if not has_content:
            print("[verify_criteria] REJECTED: last_research.json has no findings/recommendations/summary")
            return False
        content_str = json.dumps(has_content, ensure_ascii=False) if isinstance(has_content, (list, dict)) else str(has_content)
        if len(content_str) < 100:
            print("[verify_criteria] REJECTED: research content too short (< 100 chars)")
            return False
        evidence = f"research: {content_str[:80]}"

    elif criteria_id == "state_synced":
        sync_file = _DATA_DIR / "last_sync.json"
        if not sync_file.exists():
            print("[verify_criteria] REJECTED: last_sync.json not found. Run knowledge-manager first.")
            return False
        import os as _os, time as _time
        age = _time.time() - _os.path.getmtime(str(sync_file))
        if age > 1800:  # 30 minutes (aligned with review_approved TTL)
            print(f"[verify_criteria] REJECTED: last_sync.json is {age/60:.0f}min old (max 30min)")
            return False
        try:
            sync_data = json.loads(sync_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[verify_criteria] REJECTED: last_sync.json is corrupt or unreadable")
            return False
        if not sync_data.get("harness_updated"):
            print("[verify_criteria] REJECTED: harness_updated is false in last_sync.json")
            return False
        evidence = f"sync: harness={sync_data.get('harness_updated')}, orphans={sync_data.get('memory_orphans_fixed', '?')}"

    # Level 3: String evidence with minimum length for non-gate, non-artifact criteria
    gate_ids = {"plan_approved", "review_approved", "report_completed",
                "verifier_passed", "research_completed", "state_synced"}
    if criteria_id not in gate_ids and criteria_id not in _ARTIFACT_REQUIREMENTS:
        if not evidence or len(evidence.strip()) < _MIN_EVIDENCE_LENGTH:
            print(
                f"[verify_criteria] REJECTED ({criteria_id}): evidence too short "
                f"({len(evidence.strip()) if evidence else 0} chars, min {_MIN_EVIDENCE_LENGTH}). "
                f"Provide meaningful evidence describing what was done."
            )
            return False

    # Mark verified
    found = False
    for c in spec.get("acceptance_criteria", []):
        if c["id"] == criteria_id:
            c["verified"] = True
            c["evidence"] = evidence[:200]
            c["verified_at"] = datetime.now().isoformat()
            found = True
    if not found:
        print(f"[verify_criteria] WARNING: criteria_id '{criteria_id}' not found in task_spec")
        return False
    # LOG-R2 #4: atomic write via _atomic_state (non-atomic write_text leaves
    # truncated file on crash → permanent deadlock of all future criteria checks)
    try:
        from src.core._atomic_state import atomic_update_json
        return atomic_update_json(spec_file, lambda _: spec)
    except ImportError:
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        return True


# Maps step names to artifact files they should produce.
# If a step has an entry here, mark_step_completed() will verify the artifact exists.
_STEP_ARTIFACT_MAP = {
    # THEORY workflow
    "derivation_review": "last_review_technical.json",
    "technical_review": "last_review_technical.json",
    "narrative_review": "last_review_narrative.json",
    "consistency_check": "last_review_consistency.json",
    # DEVELOP workflow (3 separate reviewers)
    "review_security": "last_review_security.json",
    "review_logic": "last_review_logic.json",
    "review_coverage": "last_review_coverage.json",
    # FIX/REVIEW workflow (single reviewer)
    "review": "last_review.json",
    # Common
    "cross_review": "last_review.json",
}


def mark_step_completed(step_name: str) -> bool:
    """Record a workflow step as completed in task_spec.json.

    Enforces:
    1. Sequential order: prior required steps must be done
    2. Artifact existence: steps with known artifacts must have produced them

    Returns:
        True if step was recorded, False on error or out-of-order.
    """
    spec_file = _DATA_DIR / "task_spec.json"
    if not spec_file.exists():
        return False
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    tracker = spec.get("step_tracker")
    if not tracker:
        return False

    all_steps = tracker.get("all_steps", [])
    completed = tracker.get("completed_steps", [])

    if step_name not in all_steps:
        print(f"[step-tracker] WARNING: '{step_name}' not in workflow steps")
        return False

    if step_name in completed:
        return True  # already done, idempotent

    # Sequential order check: all prior required steps must be completed
    step_idx = all_steps.index(step_name)
    required = set(tracker.get("required_for_completion", []))
    for i in range(step_idx):
        prior = all_steps[i]
        if prior in required and prior not in completed:
            print(
                f"[step-tracker] BLOCKED: cannot complete '{step_name}' "
                f"before required step '{prior}'"
            )
            return False

    # Artifact existence check: steps with known artifacts must have them
    if step_name in _STEP_ARTIFACT_MAP:
        artifact_file = _DATA_DIR / _STEP_ARTIFACT_MAP[step_name]
        if not artifact_file.exists():
            print(
                f"[step-tracker] BLOCKED: step '{step_name}' requires artifact "
                f"'{_STEP_ARTIFACT_MAP[step_name]}' but file not found. "
                f"Run the agent first."
            )
            return False

    completed.append(step_name)
    tracker["completed_steps"] = completed
    # Advance current_step to next uncompleted step
    for s in all_steps:
        if s not in completed:
            tracker["current_step"] = s
            break
    else:
        tracker["current_step"] = None  # all done

    # LOG-R2 #4: atomic write via _atomic_state (same rationale as verify_criteria)
    try:
        from src.core._atomic_state import atomic_update_json
        return atomic_update_json(spec_file, lambda _: spec)
    except ImportError:
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        return True


def get_next_step() -> Optional[str]:
    """Return the next uncompleted workflow step, or None if all done."""
    spec_file = _DATA_DIR / "task_spec.json"
    if not spec_file.exists():
        return None
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    tracker = spec.get("step_tracker", {})
    return tracker.get("current_step")


def check_required_steps_done() -> tuple:
    """Check if all required workflow steps have been completed.

    Returns:
        (all_done: bool, summary: str)
    """
    spec_file = _DATA_DIR / "task_spec.json"
    if not spec_file.exists():
        try:
            from .persistent_mode import _load_state
            state = _load_state()
            if state and state.get("active") and state.get("mode") == "autopilot":
                return False, "task_spec.json missing during autopilot"
        except Exception:
            pass
        return True, "No task_spec.json"
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False, "task_spec.json corrupt or unreadable"

    tracker = spec.get("step_tracker")
    if not tracker:
        return True, "No step_tracker"

    required = set(tracker.get("required_for_completion", []))
    completed = set(tracker.get("completed_steps", []))
    missing = required - completed

    if missing:
        return False, f"{len(missing)} required steps not done: {', '.join(sorted(missing))}"
    return True, f"All {len(required)} required steps completed"


def check_all_criteria_met() -> tuple:
    """检查是否所有 acceptance criteria 都已验证 AND 所有 required steps 完成。

    When task_spec.json is missing during an active persistent mode session,
    returns False (not vacuous True). This prevents escape via file deletion.

    Returns:
        (all_met: bool, summary: str)
    """
    spec_file = _DATA_DIR / "task_spec.json"
    if not spec_file.exists():
        # If autopilot mode is active, missing spec = cannot verify = block.
        # This prevents escape by deleting task_spec.json.
        # Only blocks for autopilot mode (not plain persistent mode).
        try:
            from .persistent_mode import _load_state
            state = _load_state()
            if state and state.get("active") and state.get("mode") == "autopilot":
                return False, "task_spec.json missing during autopilot -- cannot verify completion"
            return True, "No task_spec.json (no active autopilot)"
        except Exception as e:
            # ESCAPE-PROOF: Cannot determine autopilot status -> fail CLOSED.
            # A broken guard is not the same as an inactive guard.
            # If persistent_mode is unimportable (e.g., concurrent fix-agent edit
            # broke syntax), block exit until it's fixed.
            return False, (
                f"persistent_mode unreadable ({type(e).__name__}: {e}). "
                f"Cannot verify autopilot status -- failing closed."
            )
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False, "task_spec.json corrupt or unreadable"

    issues = []

    # Check acceptance criteria
    criteria = spec.get("acceptance_criteria", [])
    if criteria:
        unmet = [c for c in criteria if not c.get("verified", False)]
        if unmet:
            issues.append(f"{len(unmet)}/{len(criteria)} criteria unmet: " + ", ".join(c["id"] for c in unmet))

    # Check required steps
    tracker = spec.get("step_tracker")
    if tracker:
        required = set(tracker.get("required_for_completion", []))
        completed = set(tracker.get("completed_steps", []))
        missing = required - completed
        if missing:
            issues.append(f"{len(missing)} steps skipped: {', '.join(sorted(missing))}")

    if issues:
        return False, " | ".join(issues)

    total = len(criteria) if criteria else 0
    return True, f"All {total} criteria verified, all steps completed"


def save_execution_state(task_type: TaskType, current_step: int,
                          round_num: int = 1, status: str = "running") -> None:
    """保存当前执行状态 (用于 PostCompact 恢复)"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "task_type": task_type.value,
        "current_step": current_step,
        "round_num": round_num,
        "status": status,
        "updated_at": datetime.now().isoformat(),
    }
    state_file = _DATA_DIR / "task_execution_state.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_execution_state() -> Optional[dict]:
    """加载执行状态 (PostCompact 后恢复用)"""
    state_file = _DATA_DIR / "task_execution_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def clear_execution_state() -> None:
    """清除执行状态 + scope validation flag"""
    state_file = _DATA_DIR / "task_execution_state.json"
    if state_file.exists():
        state_file.unlink()
    # Auto-clear scope validation flag when task completes
    scope_flag = _DATA_DIR / "scope_validation_pending.flag"
    if scope_flag.exists():
        try:
            scope_flag.unlink()
        except OSError:
            pass


def main():
    """CLI: classify / workflow / plan"""
    if len(sys.argv) < 2:
        print("Usage: task_router.py [classify|workflow|plan] <prompt>")
        return

    cmd = sys.argv[1]
    prompt = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""

    if cmd == "classify":
        task_type = classify_task(prompt)
        print(f"Task type: {task_type.value}")

    elif cmd == "workflow":
        task_type = classify_task(prompt)
        wf = get_workflow(task_type)
        print(f"Type: {task_type.value}, Steps: {len(wf.steps)}, Max fix rounds: {wf.max_fix_rounds}")
        for i, step in enumerate(wf.steps, 1):
            print(f"  {i}. {step.name}: {step.description}")

    elif cmd == "plan":
        task_type = classify_task(prompt)
        print(generate_plan_prompt(task_type, prompt))

    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
