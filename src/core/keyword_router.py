"""
Keyword Router -- oh-my-claudecode Magic Keyword 模式的 Python 实现

借鉴 oh-my-claudecode 的 keyword-detector hook:
- 从用户自然语言输入中检测关键词
- 自动路由到对应 agent/skill/workflow
- 支持优先级冲突解决
- 支持任务规模过滤 (防止简单任务触发重型模式)

作为 UserPromptSubmit hook 调用:
  python memex/memex/core/keyword_router.py

从 stdin 读取 JSON:
  {"prompt": "用户输入文本", "session_id": "..."}

输出到 stdout (Claude Code 读取):
  检测到的关键词 + 推荐 agent + 上下文注入
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class KeywordRule:
    """单条关键词规则"""
    name: str                    # 规则标识
    priority: int                # 优先级 (越高越优先)
    patterns: List[str]          # 正则模式列表
    agent: Optional[str]         # 推荐的 agent
    skill: Optional[str]         # 推荐的 skill/command
    description: str             # 给 Claude 的行为指令
    min_words: int = 0           # 最少字数要求 (防止简单任务触发重型模式)
    require_specificity: bool = False  # 是否要求具体性 (文件名/函数名)


# ── 关键词规则表 (按优先级降序) ──

KEYWORD_RULES: List[KeywordRule] = [
    # P100: 取消类 (最高优先级)
    KeywordRule(
        name="cancel",
        priority=100,
        patterns=[r"取消", r"停止", r"cancel", r"stop\s+(?:all|everything)"],
        agent=None,
        skill=None,
        description="用户要求停止当前操作。立即停止所有进行中的 agent 和任务。",
    ),

    # P90: 全自主模式
    KeywordRule(
        name="autopilot",
        priority=90,
        patterns=[
            r"全自动", r"自主完成", r"autopilot",
            r"帮我.*(?:从头到尾|完整)", r"一条龙",
        ],
        agent=None,
        skill=None,
        description="AUTOPILOT_STATE_BOARD",  # Sentinel; format_output builds dynamic state board
        min_words=3,  # lowered from 10: "autopilot" is explicit intent, Chinese prompts have fewer "words"
        require_specificity=False,
    ),

    # P80: 持续模式 (Ralph)
    KeywordRule(
        name="persistent",
        priority=80,
        patterns=[
            r"不要停", r"做完为止", r"必须完成", r"don'?t\s+stop",
            r"ralph", r"持续执行",
        ],
        agent=None,
        skill=None,
        description=(
            "用户请求持续模式。任务未完成前不要停下来。"
            "每完成一个子步骤后继续下一个，直到验证通过。"
        ),
        min_words=3,  # lowered: "做完为止 修复X" is only ~5 words in Chinese
    ),

    # P70: 并行模式
    KeywordRule(
        name="parallel",
        priority=70,
        patterns=[r"并行", r"同时", r"parallel", r"ultrawork"],
        agent=None,
        skill=None,
        description=(
            "用户请求并行执行。将任务分解为独立子任务，"
            "在一条 response 中同时启动多个 Agent (最多 5 个)。"
            "使用 isolation: worktree 避免文件冲突。"
        ),
        min_words=5,  # lowered: "并行 实现三个模块" is ~7 words
    ),

    # P60: 调研类
    KeywordRule(
        name="research",
        priority=60,
        patterns=[
            r"调研", r"研究一下", r"(?:找|搜).*(?:方案|项目|论文|最佳实践)",
            r"research", r"survey", r"对比.*方案",
        ],
        agent="chief-researcher",
        skill=None,
        description="启动 chief-researcher agent 进行行业调研。",
    ),

    # P58: 科研理论论证类 (高于 paper_writing, 因为 "想法→论证" 比 "写论文" 更上游)
    KeywordRule(
        name="theory",
        priority=58,
        patterns=[
            r"(?:我有|有个|一个).*(?:想法|idea|假设|hypothesis|猜想)",
            r"(?:论证|prove|证明|验证).*(?:想法|idea|理论|假设)",
            r"(?:推导|derive|derivation).*(?:公式|方程|哈密顿|Hamiltonian)",
            r"(?:数值|numerical).*(?:验证|verify|计算|calculate)",
            r"适用条件|applicability|regime",
        ],
        agent=None,
        skill=None,
        description=(
            "THEORY 模式: 科研想法→严格论证→计算验证→审查。"
            "14 步工作流: formulation→derivation_review→compute→statistics→"
            "visualization→applicability→synthesis→technical/narrative/consistency review。"
            "用户只提供想法, 其余全自动。"
        ),
        min_words=3,
    ),

    # P55: 论文写作类
    KeywordRule(
        name="paper_writing",
        priority=55,
        patterns=[
            r"(?:写|改|修).*(?:论文|paper|manuscript|draft)",
            r"PRL|PRB|Nature|Science",
            r"rewrite.*(?:abstract|introduction|conclusion)",
        ],
        agent="prl-writing-master",
        skill=None,
        description="启动论文写作 agent。根据目标期刊选择 prl-writing-master 或 nature-writing-master。",
    ),

    # P50: 实验报告类
    KeywordRule(
        name="lab_report",
        priority=50,
        patterns=[
            r"(?:大物|物理).*(?:实验|报告|lab)",
            r"实验报告", r"lab\s*report",
        ],
        agent="physics-lab-report",
        skill=None,
        description="启动 physics-lab-report agent 生成大物实验报告。",
    ),

    # P45: 组会/汇报类
    KeywordRule(
        name="meeting",
        priority=45,
        patterns=[
            r"组会", r"meeting", r"汇报", r"PPT", r"slides",
        ],
        agent=None,
        skill="meeting-prep",
        description="使用 /meeting-prep skill 准备组会材料和 PPT。",
    ),

    # P40: 日程/DDL 类
    KeywordRule(
        name="schedule",
        priority=40,
        patterns=[
            r"DDL", r"deadline", r"日程", r"待办", r"作业",
            r"(?:今天|明天|这周).*(?:安排|任务|事项)",
        ],
        agent=None,
        skill="academic-hub",
        description="使用 /academic-hub skill 汇总 DDL 和学业任务。",
    ),

    # P35: 微信消息处理
    KeywordRule(
        name="wechat",
        priority=35,
        patterns=[r"微信", r"wechat", r"消息导入"],
        agent=None,
        skill="wechat-processor",
        description="使用 /wechat-processor skill 处理微信消息。",
    ),

    # P30: QQ 消息处理
    KeywordRule(
        name="qq",
        priority=30,
        patterns=[r"QQ消息", r"QQ群", r"(?:读|看).*QQ"],
        agent=None,
        skill="qq-processor",
        description="使用 /qq-processor skill 处理 QQ 消息。",
    ),

    # P25: 代码审查
    KeywordRule(
        name="code_review",
        priority=25,
        patterns=[
            r"(?:代码)?审查", r"code\s*review", r"review.*code",
            r"检查.*代码", r"质量检查",
        ],
        agent="code-reviewer",
        skill=None,
        description="启动 code-reviewer agent 进行代码审查。",
    ),

    # P20: 论文分析
    KeywordRule(
        name="paper_analyze",
        priority=20,
        patterns=[
            r"(?:分析|读).*(?:论文|paper|arxiv)",
            r"arxiv.*\d{4}\.\d{4,5}",
        ],
        agent=None,
        skill="paper-analyzer",
        description="使用 /paper-analyzer skill 分析论文。",
    ),

    # P15: 邮件
    KeywordRule(
        name="email",
        priority=15,
        patterns=[r"邮件", r"邮箱", r"email", r"qq邮箱"],
        agent=None,
        skill="qq-email",
        description="使用 /qq-email skill 读写 QQ 邮箱。",
    ),
]


# TU-5 (autopilot 20260428_070949_daemon_watch): fail-loud helper.
# Per HARD RULE feedback_partial_fix_explicit_unknown_state — graph
# unavailability MUST emit explicit `status=unknown_state` 三态, NOT silent [].
# Stage4 security-iter1 MED-1 fix: widen sk- pattern to include `-` and `_` so
# hyphenated keys (`sk-proj-*`, `sk-real-key-XYZ`) don't partial-leak suffix.
_GRAPH_REDACT_RE = re.compile(
    r"(api_key|apikey|access_token|bearer|token|secret)=[^\s&]+|sk-[A-Za-z0-9_\-]+",
    re.IGNORECASE,
)


def _safe(exc: object) -> str:
    """Strip API keys / tokens / bearer headers from exception text.

    Used by both keyword_router graph-failure layers (inner L~745 + outer
    L~769) and the [GRAPH UNAVAILABLE: ...] emit string. logic-iter1-6 fix:
    asserts in tests verify substrings stripped from result; the print-site
    string is the source of `status=unknown_state`, NOT this helper.
    """
    s = str(exc)
    return _GRAPH_REDACT_RE.sub("[REDACTED]", s)[:200]


def _clean_input(text: str) -> str:
    """清理输入: 去掉代码块、URL、XML 标签"""
    # 去掉代码块
    text = re.sub(r"```[\s\S]*?```", "", text)
    # 去掉 URL
    text = re.sub(r"https?://\S+", "", text)
    # 去掉 XML 标签
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _count_words(text: str) -> int:
    """中英文混合字数统计"""
    # 英文按空格分词
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    # 中文按字符数
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    return english_words + chinese_chars


def _has_specificity(text: str) -> bool:
    """检查用户输入是否足够具体 (包含文件名、函数名、编号步骤、项目名等)"""
    signals = [
        r"[\w/]+\.\w{1,4}",           # 文件路径 (e.g., src/main.py)
        r"(?:def|class|function)\s+\w+",  # 函数/类名
        r"^\s*\d+[.)]\s",             # 编号步骤
        r"```",                        # 代码块
        r"#\d+",                       # Issue 引用
        r"force:",                     # 强制跳过检查
        r"(?i)memex|memex",          # 项目名
        r"(?:测试|安全|审查|修复|质量|重构)",  # 具体行为动词
        r"(?:所有|全部|完整|整个)",      # 全量操作指示
    ]
    return any(re.search(p, text, re.MULTILINE) for p in signals)


def _is_help_question(text: str, keyword: str) -> bool:
    """检测是否是询问关键词用法而非激活意图"""
    help_patterns = [
        rf"(?:什么是|如何使用|怎么用).*{keyword}",
        rf"{keyword}.*(?:是什么|怎么用|怎么回事)",
        rf"(?:how|what).*{keyword}",
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in help_patterns)


def detect_keywords(prompt: str) -> List[Tuple[KeywordRule, int]]:
    """
    检测用户输入中的关键词，返回匹配的规则列表 (按优先级降序)。

    Returns:
        List of (rule, priority) tuples, sorted by priority descending
    """
    cleaned = _clean_input(prompt)
    word_count = _count_words(cleaned)
    matches = []

    for rule in KEYWORD_RULES:
        # 检查是否匹配任一 pattern
        matched = False
        for pattern in rule.patterns:
            if re.search(pattern, cleaned, re.IGNORECASE):
                matched = True
                break

        if not matched:
            continue

        # 过滤: 帮助问题
        if _is_help_question(cleaned, rule.name):
            continue

        # 过滤: 最少字数
        if rule.min_words > 0 and word_count < rule.min_words:
            continue

        # 过滤: 具体性要求 (类似 oh-my-claudecode 的 Ralplan Gate)
        if rule.require_specificity and not _has_specificity(prompt):
            # 降级: 建议先规划而不是直接执行
            matches.append((KeywordRule(
                name=f"{rule.name}_needs_planning",
                priority=rule.priority - 5,
                patterns=rule.patterns,
                agent=None,
                skill=None,
                description=(
                    f"用户请求了 {rule.name} 模式，但输入不够具体。"
                    "请先询问用户的具体需求 (哪些文件/函数/模块)，"
                    "制定计划后再执行。"
                ),
            ), rule.priority - 5))
            continue

        matches.append((rule, rule.priority))

    # 按优先级降序排序
    matches.sort(key=lambda x: x[1], reverse=True)

    # 冲突解决: cancel 压制所有
    if matches and matches[0][0].name == "cancel":
        return [matches[0]]

    return matches


# 需要前置 scope validation 的关键词 (大任务/架构变更)
_SCOPE_VALIDATION_TRIGGERS = {"autopilot", "parallel", "research", "paper_writing", "theory"}

_SCOPE_VALIDATION_PROMPT = """
MANDATORY SCOPE VALIDATION (Phase -1, gstack /office-hours pattern):
Before proceeding, you MUST answer these 4 questions in your response:
1. What specific problem does this task solve?
2. What happens if we don't do it?
3. Is there a simpler alternative?
4. What is the minimum viable implementation?

After answering, state your scope recommendation:
- Expansion / Selective Expansion / Hold Scope / Reduction

Only then proceed with the actual task.
""".strip()


def format_output(matches: List[Tuple[KeywordRule, int]], prompt: str) -> str:
    """格式化输出给 Claude Code 的上下文注入"""
    if not matches:
        return ""

    top_match = matches[0][0]
    lines = []
    lines.append(f"Keyword detected: {top_match.name} (priority {matches[0][1]})")

    if top_match.agent:
        lines.append(f"Recommended agent: {top_match.agent}")
    if top_match.skill:
        lines.append(f"Recommended skill: {top_match.skill}")

    lines.append(f"Action: {top_match.description}")

    if len(matches) > 1:
        others = ", ".join(f"{m[0].name}(P{m[1]})" for m in matches[1:3])
        lines.append(f"Also detected: {others}")

    # Skill 强制评估 (Superpowers pattern: 激活率 ~20% -> ~84%)
    # 仅对非空匹配且有 skill 推荐时注入
    if top_match.skill and top_match.name not in ("cancel",):
        lines.append(f"\nACTION REQUIRED: Use the Skill tool to invoke /{top_match.skill} BEFORE doing anything else.")

    # 大任务自动注入 scope validation + 写状态文件
    if top_match.name in _SCOPE_VALIDATION_TRIGGERS:
        lines.append("")
        lines.append(_SCOPE_VALIDATION_PROMPT)
        try:
            from pathlib import Path as _P
            scope_flag = _P(__file__).parent.parent / "data" / "scope_validation_pending.flag"
            scope_flag.parent.mkdir(parents=True, exist_ok=True)
            scope_flag.write_text(top_match.name, encoding="utf-8")
        except Exception:
            pass

    # autopilot 模式: 精简状态板 + task_spec 创建
    if top_match.name == "autopilot":
        try:
            import sys as _s
            from pathlib import Path as _P2
            _jr = str(_P2(__file__).parent.parent.parent)
            if _jr not in _s.path:
                _s.path.insert(0, _jr)
            from src.core.task_router import (
                classify_task, generate_task_spec, assess_complexity,
            )
            task_type = classify_task(prompt)
            complexity = assess_complexity(prompt)

            # Create task_spec (this activates PLAN/REVIEW/RELEASE gates)
            spec = generate_task_spec(task_type, prompt)

            # Build compact state board (replaces 100-line template)
            lines.append("")
            lines.append("=== AUTOPILOT STATE BOARD ===")
            lines.append(f"TASK: {task_type.value} | {complexity}")

            # Show gate status from criteria
            criteria = spec.get("acceptance_criteria", [])
            gate_items = []
            for c in criteria:
                mark = "DONE" if c.get("verified") else "BLOCKED"
                gate_items.append(f"{c['id']}={mark}")
            if gate_items:
                lines.append(f"GATES: {' | '.join(gate_items)}")

            # Show step tracker status
            tracker = spec.get("step_tracker", {})
            completed_steps = tracker.get("completed_steps", [])
            current = tracker.get("current_step")
            required = tracker.get("required_for_completion", [])
            if current:
                lines.append(f"CURRENT STEP: {current}")
                done_count = len(completed_steps)
                total = len(tracker.get("all_steps", []))
                lines.append(f"PROGRESS: {done_count}/{total} steps done")
                missing_required = [s for s in required if s not in completed_steps]
                if missing_required:
                    lines.append(f"REQUIRED REMAINING: {', '.join(missing_required[:5])}")

            if complexity == "complex":
                lines.append("HARD GATES (hook-enforced, not prompt):")
                lines.append("  PLAN GATE: .py writes denied until plan_approved (pretool_gate)")
                lines.append("  REVIEW GATE: commit denied until review_approved (session_gate)")
                lines.append("  STEP GATE: commit denied if review/security steps skipped (session_gate)")
                lines.append("  RELEASE GATE: exit denied until all criteria met (persistent_mode)")
                lines.append("  mark_completed() HARD REJECTS if any criterion unmet")
            lines.append("PIPELINE: research -> plan -> implement -> test -> review -> commit -> sync -> report")
            lines.append("AFTER EACH STEP: mark_step_completed('step_name') to record progress")
            lines.append("RULES: persistent ON | quality > speed | rtk prefix | never skip review")
            lines.append("=== END STATE BOARD ===")
        except Exception as _e:
            import traceback as _tb
            print(f"[keyword-router] autopilot state board error: {_e}", file=sys.stderr)
            _tb.print_exc(file=sys.stderr)

    return "\n".join(lines)


# ── 用户纠正检测 (metaswarm conversation mining) ──

_CORRECTION_PATTERNS = [
    # 中文纠正 (扩展 2026-04-18, L1 Phase 1)
    # ⚠️ 顺序重要: future_rule 必须在 prohibition 之前,因为 "以后不要" 含 "不要"
    (r"以后记住|以后注意|以后不|以后别|以后要|下次不要|下次记得|下次别|下次要|"
     r"务必|必须要|一定要|请确保|确保以后|请记住|记住别|请记得|记得|记住", "future_rule"),
    (r"应该先|必须先|一定要先|得先|需要先", "order_correction"),
    (r"不要|不应该|禁止|别再|不许|严禁|不准|请别|请不要|不再", "prohibition"),
    (r"你错了|做错了|搞错了|弄错了|错了|说错了", "correction"),
    (r"这不对|不是这样|不是这个|不对|错的", "factual_correction"),
    (r"我说的是|我的意思是|我是说", "clarification"),
    (r"太慢了|效率太低|浪费|别磨蹭", "efficiency"),
    (r"读文档|看文档|查文档|RTFM|看说明|读官方|先查", "docs_first"),
    # 英文纠正 (扩展) — future_rule 先于 prohibition
    (r"(?:remember|next time|from now on|going forward|in the future)", "future_rule"),
    (r"(?:should|must)\s+(?:first|always|read|never)", "order_correction"),
    (r"(?:don'?t|never|stop|avoid)\s+\w+", "prohibition"),
    (r"(?:wrong|incorrect|mistake|error|no,)", "correction"),
    (r"(?:actually|let me rephrase|what i meant)", "clarification"),
    (r"(?:read the docs|rtfm|check the docs)", "docs_first"),
]

# 强信号关键词 — 即使消息较短 (< 5 字) 也视为纠正
_STRONG_SIGNAL_REGEX = re.compile(
    r"(不要|不再|不许|不准|严禁|禁止|别再|以后不|以后别|以后要|"
    r"务必|必须|记住|记得|请确保|下次|"
    r"don'?t|never|stop|avoid|rtfm|mistake|must)",
    re.IGNORECASE,
)


def detect_user_correction(prompt: str) -> Optional[Tuple[str, str]]:
    """
    检测用户消息中的纠正/反馈模式。

    Returns:
        (correction_type, matched_text) or None

    [L1 Phase 1 改动 2026-04-18]:
    - 正则从 12 条扩展到 20+ 条,覆盖"以后不/不再/务必/记得"等常见变体
    - 字数阈值由 5 放宽:若消息含强信号关键词,>=3 字即可
    """
    cleaned = _clean_input(prompt)
    word_count = _count_words(cleaned)

    # 放宽阈值: 含强信号 >=3 字; 否则仍 >=5 字
    has_strong_signal = bool(_STRONG_SIGNAL_REGEX.search(cleaned))
    min_words = 3 if has_strong_signal else 5
    if word_count < min_words:
        return None

    for pattern, corr_type in _CORRECTION_PATTERNS:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            return corr_type, cleaned

    return None


def _auto_extract_correction(prompt: str, correction_type: str) -> None:
    """自动将用户纠正提取为 pattern 写入知识库"""
    try:
        import sys as _sys
        from pathlib import Path as _Path
        # 确保 memex 包在 sys.path 中 (subprocess 调用时可能不在)
        _jarvis_root = str(_Path(__file__).parent.parent.parent)
        if _jarvis_root not in _sys.path:
            _sys.path.insert(0, _jarvis_root)
        from src.core.pattern_extractor import (
            PatternEntry, save_patterns, _sanitize_correction_text,
        )

        type_map = {
            "prohibition": "anti_pattern",
            "correction": "gotcha",
            "order_correction": "pattern",
            "future_rule": "pattern",
            "factual_correction": "gotcha",
            "clarification": "gotcha",
            "efficiency": "performance",
            "docs_first": "anti_pattern",
        }

        # [SEC-6] Sanitize user input before storing as high-confidence pattern
        safe_fact = _sanitize_correction_text(prompt, max_len=300)
        entry = PatternEntry(
            type=type_map.get(correction_type, "gotcha"),
            fact=safe_fact,
            recommendation="User correction. Follow this guidance in future interactions.",
            confidence="high",
            tags=["user_correction", correction_type],
            affected_files=[],
            provenance=[{
                "source": "user_correction",
                "reference": f"UserPromptSubmit hook auto-capture",
                "date": __import__("datetime").datetime.now().isoformat(),
            }],
        )
        save_patterns([entry])

        # [V-2] Cross-reference correction with recently-primed patterns.
        # Strict matching (≥5 token overlap, 1-hour window, stopwords excluded).
        # Demoted patterns get +1 to outdated_reports + full audit trail.
        try:
            from src.core.pattern_extractor import (
                find_patterns_for_correction,
                record_pattern_outdated,
            )
            demotable = find_patterns_for_correction(
                prompt, min_overlap=5, max_age_sec=3600,
            )
            if demotable:
                record_pattern_outdated(
                    demotable,
                    reason=f"user_correction:{correction_type}",
                    correction_text=prompt,
                )
        except Exception:
            pass  # non-blocking
    except Exception:
        pass  # non-blocking


def main():
    """
    Hook 入口点。从 stdin 读取 hook 输入 JSON，
    输出关键词检测结果到 stdout。

    两个功能:
    1. 关键词路由 (agent/skill 推荐)
    2. 用户纠正自动捕获 (写入知识库)
    """
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return

        data = json.loads(raw)
        prompt = data.get("prompt", data.get("user_prompt", ""))

        if not prompt:
            return

        # 功能 1: 关键词路由
        matches = detect_keywords(prompt)

        if matches:
            top = matches[0][0]

            # 自动激活 persistent mode (Ralph pattern)
            if top.name in ("persistent", "autopilot"):
                try:
                    import sys as _sys
                    from pathlib import Path as _Path
                    _jr = str(_Path(__file__).parent.parent.parent)
                    if _jr not in _sys.path:
                        _sys.path.insert(0, _jr)
                    from src.core.persistent_mode import activate
                    # autopilot 模式: 小时级运行，20 次 reinforcement
                    # persistent 模式: 中等任务，5 次 reinforcement
                    max_r = 20 if top.name == "autopilot" else 5
                    activate(prompt[:200], top.name, max_reinforcements=max_r)
                except Exception:
                    pass  # non-blocking

            # cancel 自动解除 persistent mode
            if top.name == "cancel":
                try:
                    import sys as _sys
                    from pathlib import Path as _Path
                    _jr = str(_Path(__file__).parent.parent.parent)
                    if _jr not in _sys.path:
                        _sys.path.insert(0, _jr)
                    from src.core.persistent_mode import deactivate
                    import os as _os2
                    _os2.environ["MEMEX_HOOK_CALLER"] = "cli"
                    try:
                        deactivate("cancel")
                    finally:
                        _os2.environ.pop("MEMEX_HOOK_CALLER", None)
                except Exception:
                    pass
            output = format_output(matches, prompt)
            print(output)

        # 功能 1b: CEO 反馈信号捕获 (2026-04-21 TU-1 wiring)
        # Fire-and-forget: ceo_feedback.hook_user_prompt_submit NEVER raises.
        # Only records strong signals (confidence >= 0.4), so empty prompts
        # and neutral text are silently skipped.
        # Env kill-switch MEMEX_CEO_FEEDBACK_HOOK=0 disables without code change.
        if os.environ.get("MEMEX_CEO_FEEDBACK_HOOK", "1") == "1":
            try:
                from src.core.ceo_feedback import hook_user_prompt_submit
                hook_user_prompt_submit(prompt)
            except Exception:
                pass  # non-blocking by contract

        # 功能 1c: U2 query-type log instrumentation (chat-graph plan v3 P0).
        # Non-blocking; env kill-switch MEMEX_QUERY_TYPE_LOG=0 disables.
        # Log feeds CEO visibility (no plan gating). Privacy: only sha256
        # of prompt + sha256 of session_id are persisted, never raw text.
        if os.environ.get("MEMEX_QUERY_TYPE_LOG", "1") == "1":
            try:
                # sys.path defense: subprocess context (no PYTHONPATH set)
                # would silently drop the import; mirrors lines 648-650 pattern.
                import sys as _sys
                from pathlib import Path as _Path
                _jr = str(_Path(__file__).parent.parent.parent)
                if _jr not in _sys.path:
                    _sys.path.insert(0, _jr)
                from src.core.query_type_classifier import log_query_type
                log_query_type(prompt, data.get("session_id", ""))
            except Exception:
                pass  # non-blocking by contract

        # 功能 2: 用户纠正自动捕获 (无论是否匹配关键词都检测)
        correction = detect_user_correction(prompt)
        if correction:
            corr_type, _ = correction
            _auto_extract_correction(prompt, corr_type)
        else:
            # [L2 Phase 2 2026-04-18] Soft-signal fallback: L1 miss → Haiku classifier
            # 只在含软信号关键词时调,省钱. PII scrub + 预算守护 在 classifier 内部做.
            try:
                from src.core.soft_signal_classifier import classify_soft_signal
                result = classify_soft_signal(prompt)
                if result and result.is_feedback:
                    # Use rule_text (normalized) or fallback to raw prompt
                    effective_text = result.rule_text or prompt
                    _auto_extract_correction(effective_text, result.rule_type)
            except Exception as _l2e:
                # non-blocking: L2 failure must not break the hook
                print(f"[keyword-router] L2 soft-signal error: {_l2e}", file=sys.stderr)

        # 功能 3: 知识库 pattern 自动注入 (闭环学习)
        # 2026-04-30 daemon repair: smart_prime → semantic_kb → graph_memory_v2
        # → Hindsight recall (60s on Win CPU). Wrap in daemon-thread budget
        # so UserPromptSubmit returns ≤10s even when daemon busy.
        # MEMEX_HOOK_PRIME_BUDGET_S env override; 0 disables priming.
        # 2026-05-08 (CEO): bench measured query_entity cold-start 33-48s
        # for entities that hit both v5 + legacy union path; 25s timed out
        # half the time. Bumped to 50s. Daemon thread keeps hook non-blocking
        # even if exceeded — slow path falls back to PRIME SLOW notice.
        _PRIME_BUDGET_S = float(
            os.environ.get("MEMEX_HOOK_PRIME_BUDGET_S", "50.0"))
        if _PRIME_BUDGET_S <= 0:
            return  # priming disabled by env
        try:
            from pathlib import Path as _P3
            _jr3 = str(_P3(__file__).parent.parent.parent)
            if _jr3 not in sys.path:
                sys.path.insert(0, _jr3)
            from src.core.pattern_extractor import smart_prime, format_prime_output
            # Extract keywords: Chinese 2+ chars OR English 3+ chars
            # Also include common tags that match patterns in KB
            prime_kw = list(set(re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z_]{3,}', prompt)))[:10]
            # Sanitize: pipe encoding may produce non-CJK garbled chars
            prime_kw = [kw for kw in prime_kw if kw.isascii() or all(ord(c) < 0xD800 for c in kw)]
            if prime_kw:
                # 2026-04-30 daemon-thread budget around smart_prime
                # (semantic_kb path → Hindsight recall on CPU = 60s+).
                import threading as _thr3
                import time as _t_pr
                _result = {"primed": None}
                def _smart_prime_runner(kws=prime_kw, h=_result):
                    try:
                        h["primed"] = smart_prime(keywords=kws, limit=5)
                    except Exception:
                        h["primed"] = None
                t_pr = _thr3.Thread(target=_smart_prime_runner, daemon=True)
                t_pr.start()
                t_pr.join(timeout=_PRIME_BUDGET_S)
                if t_pr.is_alive():
                    print(
                        f"# [PRIME SLOW: budget {_PRIME_BUDGET_S}s exhausted; "
                        f"status=unknown_state reason=daemon_busy_or_cold_start]"
                    )
                else:
                    primed = _result["primed"]
                    if primed:
                        out = format_prime_output(primed)
                        if "No relevant" not in out:
                            print("")
                            print(out)
        except Exception as _pe:
            print(f"[keyword-router] pattern priming error: {_pe}", file=sys.stderr)

        # 功能 4: Graph 直查 (TU-P1 2026-04-21)
        # Surface raw facts about canonical entities mentioned in the
        # prompt. Distinct from pattern priming (which returns KB
        # patterns) and from semantic_search_boosted (which boosts
        # patterns by graph signal). This is the user-visible [GRAPH]
        # context surface — top facts by confidence, capped to 5.
        # Kill-switch: MEMEX_GRAPH_RETRIEVE=0 disables.
        if os.environ.get("MEMEX_GRAPH_RETRIEVE", "1") != "0":
            try:
                from src.core.canonicalizer import (
                    canonicalize_entity, _build_entity_map, _normalize,
                )
                # v2 facade (Hindsight); replaces legacy Neo4j-based graph_memory.
                # Per CLAUDE.md §7.1.7 Tier-1 protocol post 2026-04-30 daemon repair.
                from src.core.graph_memory_v2 import query_entity
                # Reuse semantic_kb's tokenization heuristic
                tokens = [prompt] + [
                    t for t in re.split(r"[\s,;:/\\|()\[\]]+", prompt) if t
                ]
                try:
                    entity_map = _build_entity_map()
                except Exception:
                    entity_map = {}
                q_canon: list = []
                seen: set = set()
                for tok in tokens:
                    try:
                        key = _normalize(str(tok))
                        if key in entity_map and entity_map[key] not in seen:
                            seen.add(entity_map[key])
                            q_canon.append(entity_map[key])
                    except Exception:
                        continue
                # Cap to top 3 entities to bound Hindsight round trips (was Neo4j)
                q_canon = q_canon[:3]
                # 2026-04-30 daemon repair: hook-side budget via daemon=True
                # threading.Thread (NOT ThreadPoolExecutor — its workers are
                # non-daemon so process won't exit until they finish, defeating
                # the budget). Each query_entity call has 180s default timeout
                # (CPU rerank); without budget, hook would block user 540s+.
                # Per HARD RULE feedback_partial_fix_explicit_unknown_state.
                import threading
                # 2026-05-08 (CEO): query_entity 实测 33-48s (cold-start MPS
                # + cross-bank UNION). 25s 仍丢一半. Bumped 50s. Pre-warm at
                # SessionStart should bring 2nd+ query down to 10-20s anyway.
                _GRAPH_HOOK_BUDGET_S = float(
                    os.environ.get("MEMEX_GRAPH_HOOK_BUDGET_S", "50.0"))
                if q_canon:
                    facts: list = []
                    _t_budget_start = __import__("time").monotonic()
                    for canon in q_canon:
                        remaining = _GRAPH_HOOK_BUDGET_S - (
                            __import__("time").monotonic() - _t_budget_start)
                        if remaining <= 0.5:
                            print(
                                f"# [GRAPH SLOW: budget {_GRAPH_HOOK_BUDGET_S}s "
                                f"exhausted; skipped {canon}; status=unknown_state "
                                f"reason=daemon_busy_or_cold_start]"
                            )
                            break
                        _result_holder = {"rows": [], "err": None}
                        def _gq_runner(c=canon, h=_result_holder):
                            try:
                                # 2026-05-08: bigger limit + medium budget + legacy union
                                # so cold-start UserPromptSubmit hook surfaces 8-12 cards
                                # spanning v5 + memory_full instead of 1-card cliff.
                                h["rows"] = query_entity(
                                    c, limit=10, budget="mid",
                                    include_legacy=True,
                                )
                            except Exception as e:
                                h["err"] = e
                        t = threading.Thread(target=_gq_runner, daemon=True)
                        t.start()
                        t.join(timeout=remaining)
                        if t.is_alive():
                            print(
                                f"# [GRAPH SLOW: entity={canon} t>{remaining:.1f}s; "
                                f"status=unknown_state reason=hook_budget_exceeded]"
                            )
                            rows = []
                        elif _result_holder["err"] is not None:
                            print(
                                f"# [GRAPH UNAVAILABLE: entity={canon}: "
                                f"status=unknown_state reason={_safe(_result_holder['err'])}]"
                            )
                            rows = []
                        else:
                            rows = _result_holder["rows"]
                        for row in rows:
                            facts.append(row)
                    # Sort by confidence desc, dedup by fact_id
                    seen_ids: set = set()
                    uniq = []
                    for f in sorted(facts, key=lambda r: -getattr(r, "confidence", 0.0)):
                        fid = getattr(f, "fact_id", "")
                        if fid and fid not in seen_ids:
                            seen_ids.add(fid)
                            uniq.append(f)
                        if len(uniq) >= 5:
                            break
                    if uniq:
                        print("")
                        print(f"# [GRAPH] {len(uniq)} fact(s) for {q_canon}:")
                        for row in uniq:
                            try:
                                print("#   " + row.fmt())
                            except Exception:
                                continue
            except Exception as _ge:
                # TU-5 fail-loud OUTER layer (logic-iter1-6 + cov-iter2-3):
                # whole-block failure ALSO emits to stdout (not just stderr) so
                # main session sees `status=unknown_state` 三态 marker.
                # Per HARD RULE feedback_partial_fix_explicit_unknown_state.
                print(
                    f"# [GRAPH UNAVAILABLE: status=unknown_state "
                    f"reason={_safe(_ge)}]"
                )
                print(f"[keyword-router] graph retrieve skipped: {_safe(_ge)}",
                      file=sys.stderr)

    except json.JSONDecodeError:
        # 非 JSON 输入 (可能是纯文本 prompt)
        prompt = raw
        matches = detect_keywords(prompt)
        if matches:
            print(format_output(matches, prompt))
        correction = detect_user_correction(prompt)
        if correction:
            _auto_extract_correction(prompt, correction[0])
    except Exception as e:
        # Hook 不应阻塞, 静默失败
        print(f"[keyword-router] error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
