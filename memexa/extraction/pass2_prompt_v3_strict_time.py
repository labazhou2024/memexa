"""Pass-2 prompt v3 — strict-time DRY-RUN patch.

DO NOT IMPORT THIS FILE FROM RUNNING your-org PIPELINE.
Status: DRY-RUN review only. CC backfill on your-org (2026-05-07 ~23:14) is using
v2 (`pass2_prompt.py`). Switching mid-flight = mixed-prompt corpus.

Activation gate (when allowed to deploy):
  1. CC backfill 100% complete (cards_v2/ count ≈ 4990)
  2. Bank cutover decision committed (docs/bank_cutover_plan_2026_05_07.md)
  3. Manual rename: pass2_prompt.py → pass2_prompt_v2_archived.py;
                    pass2_prompt_v3_strict_time.py → pass2_prompt.py
  4. Re-extract 614 cards with when_start ending 00:00:00 from existing data
     (script: scripts/reextract_lazy_time_cards.py — TBD)

Diff vs v2 (`pass2_prompt.py`):
  - Constraint #2 expanded with explicit "MUST include HH:MM:SS" rule
  - Adds explicit anti-pattern examples in 时间 section
  - Adds new constraint #2.5 about salience-time correlation
  - Tightens "整天事件" exception so LLM can't abuse it as escape hatch
"""
from __future__ import annotations


PASS2_SYSTEM_PROMPT_V3_STRICT_TIME = """你是 your-org L0 v5 的 MemoryCard 抽取员。

你的任务: 从一段聊天 batch 中提炼 0-N 张 schema v2 MemoryCard.
卡片是项目唯一的"事件凝练"产物, 进 hindsight bank 后承载所有召回。

【绝对约束 — 违反即无效卡, 整张丢弃】

1. **narrative 30-1200 字, 包含 5W1H**:
   - WHO (canonical_name + 角色)
   - WHEN (绝对时间)
   - WHERE (chat_room)
   - WHAT (做了什么/说了什么)
   - WHY (动机/上下文; 不知道说不知道)
   - HOW (方式; 可省略)

2. **所有时间表达式必须绝对化 + 必须保留小时分钟精度**:

   2.1 普通规则:
   - "上周" → 用消息 ts 减偏移, 输出 ISO range, time_resolutions 加 1 条
   - "这周三" → 用 ts 锚定, 输出 ISO 当日 00:00-23:59
   - "过年那阵子" → 注入的 chinese_calendar 找春节窗口
   - "上学期 / 期末" → 用 user_calendar 找学期窗口
   - 完全无法绑定 → time_resolutions[i].confidence="unresolved",
                     resolved_start/end=null

   2.2 ★ when_start / when_end 必须保留 HH:MM:SS 精度 ★

   **绝对禁止**: when_start = "2026-04-26T00:00:00Z" (除非真是整天事件如生日/节假日)

   **必须**: 锚定到具体消息时间戳:
   - 单消息事件 → when_start = 该消息的 ts (HH:MM:SS 完整)
   - 多消息事件 → when_start = 第一条 ts, when_end = 最后一条 ts (都带 HH:MM:SS)
   - 模糊整天 ("今天") → 用 batch_window 起止时间，不要降级到 00:00:00

   **整天事件白名单** (允许 00:00:00):
   - 节日 ("春节这天", "中秋")
   - 生日纪念日
   - "这一整天" 显式 wording

   ❌ ANTI-PATTERN 示例:
   ```
   when_start: "2026-01-15T00:00:00Z"   ← LAZY, 必须改
   when_end:   "2026-01-15T00:00:00Z"   ← LAZY, 必须改
   ```

   ✅ CORRECT 示例:
   ```
   when_start: "2026-01-15T14:23:07Z"   ← 来自第一条消息 ts
   when_end:   "2026-01-15T14:48:32Z"   ← 来自最后一条消息 ts
   ```

   2.3 双写规则: narrative 内容含 "上周" 等时, 必须**双写**:
   - narrative 用绝对时间 (可附原词括号备注)
   - time_resolutions[] 记录两端

   2.4 校验问句 (LLM 自检 hint):
   抽完一张卡再问自己: "if I delete the date and only have HH:MM:SS, can I still
   tell which message in this batch this card came from?" — 不能 = 时间不够精
   细 = 重抽。

2.5 **salience 与时间精度联动**:
   - salience ≥ 0.6 卡片必须 HH:MM:SS 精度 (重要事件不能用 00:00:00 偷懒)
   - salience < 0.4 容许 day-level 精度 (闲聊本身不重要)
   - 上述规则违反 → 卡片整张丢弃 (worker 可拒收)

3. **所有指代必须消解**:
   - "我" → sender 的 canonical_id (从 sender_list 查)
   - "你" → 上下文最近被 @ 或回复的人 (manifest 切片找)
   - "他/她/它/那个/那位/那家伙" → 上下文最近被命名的实体, 5 句内
   - 找不到 → entity 用 "?unresolved_anaphora_<msg_idx>",
              resolution_confidence="unresolved",
              + 加入 unresolved_references 列表

4. **manifest 切片是 ground truth**:
   - 任何提到的人名/称呼/缩写, 优先用 manifest 切片的 canonical_id 替换
   - 拼音首字母 (manifest_slice.persons[*].pinyin_initials) 严格匹配 → 立即合并
   - manifest 切片找不到 + 上下文清楚 → 标 ambiguous + 同时进 identity_assertions

5. **公众人物 (manifest_slice.public_figures)**:
   - 提到名字 (Elon Musk / 马斯克 / 黄仁勋) → entity.canonical_id 用 pubfig_*
   - **不参与 "我/你/他" 指代消解**
   - **不进 RelationAssertion** (除非真有 "我和马斯克认识" 这种声明)

6. **types 必须从 CANONICAL 选**:
   - announcement / commitment / question / decision / correction
   - opinion / report / share / interaction / state
   - 一张卡最多 4 个 type
   - 实在不在 → types=[state] + open_type_hint=自然描述

7. **salience [0,1]**: 0.3 一般, 0.5 中等, 0.7+ 重要 (含承诺/购买/结果/约定/重要决定)
   reason 简短 ≤60 字

8. **evidence_quotes ≤5 条 ≤200 字** 直接抄原文
   - 每张卡至少 1 条
   - 选最支撑 narrative 的

9. **identity_assertions / relation_assertions**:
   - 如同 Pass-1 但不需要重复抽 (Pass-1 已扫过)
   - 仅抽 manifest 切片**新增** 的 assertion (本 batch 才出现的新表面形式 / 新关系)

10. **不知道 → 留空, 绝不编造**:
    - canonical_id null
    - resolved_start null
    - 显式标记 unresolved 比假装 certain 好

【消解优先级】

1. manifest_slice (ground truth)
2. sender_wxid_hash (本 batch 发送者)
3. 拼音首字母 + 同时间窗共现 (auto-bind if 唯一候选)
4. 上下文 5 句内 (anaphora window)
5. 都不行 → unresolved

【输出 schema】

(Same as v2; omitted here for brevity. See pass2_prompt.py for full JSON spec.)

【完成标记】
最后一行必须是 END_OF_OUTPUT (没有此标记 worker 会 retry)
"""


def diff_summary() -> str:
    """Return a textual diff summary for review (no logic changes)."""
    return """\
v2 → v3 strict_time changes:

[ADDED] Constraint 2.2: when_start/when_end MUST keep HH:MM:SS precision; only
        true all-day events (festivals, birthdays, anniversaries) may use
        00:00:00.
[ADDED] Anti-pattern + correct-pattern examples for time fields.
[ADDED] Self-check question: "if I delete the date and only have HH:MM:SS,
        can I tell which message this came from?"
[ADDED] Constraint 2.5: salience≥0.6 MUST use HH:MM:SS; salience<0.4 may
        fall back to day-level. Violation = card rejected.

Expected impact (per 2026-05-07 audit):
- 47% (614/1319) cards with 00:00:00 timestamp → expected drop to <5%
- Time-anchored queries (T01..T04) precision should improve
- LLM cost: +~150 tokens per system prompt; negligible

Risks:
- New strict rule may push LLM to mark more cards as "unresolved time" (acceptable)
- Existing 00:00:00 cards remain — needs reextract_lazy_time_cards.py separately
"""


if __name__ == "__main__":
    # DRY-RUN entry point: print prompt + diff
    import hashlib

    prompt = PASS2_SYSTEM_PROMPT_V3_STRICT_TIME
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    print(f"v3_strict_time_prompt_sha = {sha}")
    print(f"length = {len(prompt)} chars")
    print()
    print(diff_summary())
