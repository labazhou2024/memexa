# 给 AI agent 用 memex

[English](for_agents.md) · **中文**

> **本文是什么**: 一份协议文档。memex 是为 AI agent 设计的查询系统 ——
> Claude Code, Cursor, Cline, 或你自己包的任何 agent — 替人类用户跑
> 查询。本页是每个 agent 必须遵守的合同, 否则查不出有用结果。
>
> **人类用户**: 请去看 [usage_guide.zh.md](usage_guide.zh.md)。本页更密,
> 更规则化, 默认读者 (你) 是一个 LLM, 有快扫 + 严格执行的循环。

## 0. 这个文档存在的原因

memex 的 14 个子命令看起来是 14 种查图谱的方式。它们实际是一个小协议,
有可预测的失败模式。大多数 agent 在 memex 上失败都是这 5-6 种:

1. 对人名调 `topic` (返回购物噪声)
2. 用单次 recall 回答状态题 ("X 发生了吗")
3. 在外层套并发包 `topic` / `arc` (饱和 daemon)
4. 把 `pending` 当语义 recall (它是 calendar-index 直读)
5. 跳过 pre-task SOP, 每轮重新推导上下文
6. 忽略 "广召回 → 精炼" 组合模式

本文档就是上述失败的浓缩版。

---

## 1. Cold-start checklist

每个 session **只跑一次**, 按顺序, 在任何查询前:

```
1. README.md            — memex 是什么, 不是什么
2. usage_guide.md       — 14 子命令 + 决策表
3. for_agents.md        — 本文件
4. (可选) 用户的 MEMORY.md 或等价物 — 他们跨 session 让你记住的事
```

如果你的环境自动加载这些 (Claude Code 配 `CLAUDE.md` 就会), 1-3 跳过。
通过不重读就能描述 `arc` vs `topic` 的差别来验证已加载。

## 2. 硬规则

### HR-1 — 永远不要对人名调 `topic`

`topic` 默认 11 变体调的是 *购买 / 决策* 探索 ("X 价格", "X 商家", "X
退货")。人名查询会被购物噪声淹没。实测: `arc("<人>")` 命中 6/6,
`topic("<人>")` 命中 0/100。

```bash
# 错
memex topic "Alice"

# 对
memex arc "Alice"
```

### HR-2 — 状态题需要 5 阶段工作流

用户问 *yes/no* 关于他们已经离开社交语境的事 ("我退掉了 X 课吗",
"Y 项目还在跑吗"), 单次 recall 三角不出。用
[docs/5_phase_query.zh.md](5_phase_query.zh.md) 的 5 阶段工作流:
Seed → Expand → 5 信号 → Chain → Counter。

### HR-3 — `pending` 读 calendar index, 不走 recall API

```bash
# 错 — 对 "pending" 这个词做语义 recall 返回乱码
memex topic "我的待办"

# 对 — 直接读, 返结构化 commitment 行
memex pending
```

### HR-4 — Tags 是 OR 不是 AND

Hindsight 的 recall API 把 `tags=[a, b]` 当析取。需要 AND 语义时,
client 端 post-filter。memex 内部 helper `_post_filter()` 做这事;
直接调 API 时自己复制。

### HR-5 — 不要在外层套并发包 `topic` / `arc`

这俩子命令**内部**已用 `ThreadPoolExecutor(max_workers=4)` 扇出。
你 agent 在外层 3 个并发, 饱和 daemon 的 BGE-M3 worker pool — 拿到的
是 queue 饥饿, 不是加速。串行调。

### HR-6 — `max_tokens` < 2000 截断 V2 envelope

V2 envelope 卡平均 ~866 tokens。默认 `max_tokens=1024` 只返 1 张。直接
调 recall 时 `max_tokens` 至少设 `2000 + 900 * max_cards`。

### HR-7 — Legacy bank `memory_full` 没 tag 政策

对 v3 legacy bank 用 `tags=["kind:event", "schema:v2"]` 查会拿 0 结果。
对 legacy bank, 传空 tag。memex 的 bank wrapper 自动处理; 直接调 API
要自己处理。

---

## 3. 决策表

| 用户自然语言提问 | 选 | 为什么 |
|---|---|---|
| "X 是谁?" / "我怎么认识 X" / "我和 X 关系" | `arc "X"` | 8 个关系语义变体, 时间排序 |
| "X 干了什么?" (X = 人) | `arc "X"` 然后 `quick "X <月份>"` | arc 广度 + quick 近因 |
| "X 的全过程" (X = 事 / 项目 / 不是人) | `topic "X"` | 11 个语义变体 |
| "A 到 B 之间发生了啥?" | `timeline --start A --end B` | when_start 过滤 |
| "<对手方> 要我做啥?" | `person "<对手方>"` | directive 加权的人物档 |
| "Z 项目跨源动态" | `project "Z"` 然后 `timeline` | 6 源并集 + 排序读 |
| "我有哪些待办?" / "我欠人啥" | `pending` 然后每行 `quick` | calendar 直读 + 上下文补 |
| "上周谁最活跃" | `trends --by sender --window-days 7` | 聚合, 不 recall |
| "我退掉 X 课了吗?" / 状态 yes-no | **5 阶段工作流** | 单 recall 不够 |
| "总结我上周" | `summary --window-days 7` 或 `reflect` | 在召回卡上 LLM 综合 |
| "X 在多源都被讨论了吗?" | `cross-source "X"` | 6 源覆盖矩阵 |
| "找 commitment / question / decision 类型的卡" | `types --filter <type>` | types_csv 直接过滤 |
| "找跟 X 图谱相关的实体" | `graph-walk "X"` | 多跳关系遍历 |

---

## 4. Pre-task SOP

用户给非平凡任务前**必跑**:

```
Step 1 — 重读用户 MEMORY.md / 偏好
         (找会改答案的引用)

Step 2 — 查 `memex pending`
         (抓任何跟用户请求重叠的 active commitment)

Step 3 — 从上面决策表选子命令
         (不要默认 quick; quick 是点查用的)

Step 4 — 先发**一条**广召回命令
         (arc / topic / project / person / pending)

Step 5 — 用 `quick` + 从 step 4 输出抽出的关键词精炼
         (这是 "广召回 → 精炼" 模式; 见 §5)
```

跳过 Step 2 是最常见错误。用户经常不会主动提他在该领域已经有
commitment; `pending` 帮你找到。

## 5. 组合模式

### 5.1 广召回 → 精炼 (最常见, 80% 工作流用这个)

```
广召回 (arc / topic / project / person / pending)
    │
    │ → 30-60 张原始卡 / 结构化行
    │
精炼 (quick "X <具体关键词>")
    │
    │ → 3-8 张高信号卡
    │
人类可读答案
```

为啥有效: 广召回 recall 高但排序质量低。精炼对具体 token precision 高。
叠起来既有覆盖又有焦点。

### 5.2 5 阶段状态推理 (仅 yes/no 题)

```
A. Seed         在主题上跑 quick + arc
B. Expand       在 A 浮现的每个 named actor 上跑 arc
C. 5 信号       user-speaks / user-silence / boundary / peer / private
D. Chain        5 个信号合成最可能状态
E. Counter      主动搜可证伪 D 的卡
```

每阶段 1-3 条命令。总: ~10-15 条命令一个状态题。慢但答案唯一。

完整 worked example: [5_phase_query.zh.md](5_phase_query.zh.md)。

### 5.3 跨源覆盖 (claim 验证)

用户问 "X 是 *真做了* 还是只是嘴上说说":

```bash
memex cross-source "X" --days 90 --max-per-source 10
```

返回覆盖矩阵: 每 6 个 source 里有多少卡提到 X。阈值规则: `>= 3 source`
= 强证据, `== 1 source` = 弱, `== 0` = 不存在。

### 5.4 人物档组合

```bash
memex person "Y" --window-days 30        # 指令 + commitment
memex arc "Y" --max-cards 60             # 关系弧
memex quick "Y <最近关键词>" --max-k 15   # 最近 7 天
```

这 3 个加起来 = 30 秒内一个对手方完整画像。

---

## 6. 常见坑 (agent KB)

| # | 症状 | 原因 | 修法 |
|---|---|---|---|
| 1 | `topic "Alice"` 返购物卡 | HR-1 | 用 `arc` |
| 2 | `topic "我的待办"` 返一张乱码卡 | HR-3 | 用 `pending` |
| 3 | 单 recall 回答不了 "X 发生了吗" | HR-2 | 5 阶段 |
| 4 | 你要 wechat-only 却返 350 张跨源卡 | HR-4 (tags OR) | post-filter |
| 5 | `topic` 60s 超时 | 默认 `MEMEX_HINDSIGHT_TIMEOUT=180` 够; 仍超时 = daemon 冷启 BGE-M3 | 30s 后重试一次 |
| 6 | Windows 上 UnicodeEncodeError | 终端是 GBK | 设 `PYTHONIOENCODING=utf-8` |
| 7 | 已知主题查询返 0 卡 | bank 错 | 检查 `MEMEX_HINDSIGHT_BANK`; 默认 `memory_full_v5` |
| 8 | calendar `pending` 返过期日期 | calendar 协调延迟 | `--refresh` 或重跑 cron |
| 9 | `reflect` 返胡说 | daemon 侧 LLM 没配 | 改用 `summary` (client 侧) |
| 10 | `arc` 返 60 张但漏昨天 | arc 加权广度不是近因 | 叠 `quick "<名> <月>"` |
| 11 | 在 `topic` 外面套 `asyncio.gather` 反而变慢 | HR-5 | 外层串行 |
| 12 | 卡体显示 `MEMORYCARD_V2_HEADER_BEGIN` 原文 | 没解析 V2 envelope | 用 wrapper, 不要走 raw recall |

撞到不在本表的症状, **先 grep 本文 HR 列表**再加新搜索变体。大多数
看起来新的失败是老失败换了衣服。

---

## 7. 不在范围

memex 不会:

- **生成用户没说过的文本** — `reflect` 综合现有卡; 不编造。用户问
  "我会怎么跟 X 说" 是 roleplay 请求, 不是 memex 查询
- **写入用户刚刚打的新事实** — memex 从 6 个 source 流走 cron 摄入。
  没有 `memex remember "..."` 写 API (v0.x 可能加; 现在没)
- **排序用户没目睹的事件** — memex 数据是用户的 exhaust。用户不在场,
  就没卡
- **回答未来** — 每张卡 `when_start` 在过去。"X 什么时候发生" 只在带
  `due_iso` 在未来的 commitment 卡存在时有效; `pending` 是入口

agent 任务撞到这些时, 显式向用户说明限制, 不要编造。

---

## 8. 速查卡

存进 agent 的持久 prompt:

```
memex 查询协议 — 速查

人:                arc "X" + quick "X <月份>"
项目:              project "X" + timeline --start --end
主题 / 事物:        topic "X"
对手方诉求:        person "X"
待办:              pending + 每行 quick
状态 (yes/no):     5 阶段 (Seed/Expand/Signals/Chain/Counter)
近期活动:          trends --by sender --window-days 7

硬规则:
  - 人名用 arc 不用 topic (HR-1)
  - pending 直读, 不走 topic (HR-3)
  - 状态题用 5 阶段 (HR-2)
  - tags 是 OR; post-filter (HR-4)
  - topic/arc 不要外层并发 (HR-5)

Pre-task SOP:
  1. 用户偏好    2. pending 检查    3. 选子命令
  4. 广召回      5. 精炼

组合:
  广召回 → 精炼 (默认)
  5 阶段 (状态题)
  cross-source (claim 验证)
```

---

## 相关

- [usage_guide.zh.md](usage_guide.zh.md) — 同内容人类视角
- [5_phase_query.zh.md](5_phase_query.zh.md) — 完整 worked example
- [case_studies/01_lab_report_pipeline.zh.md](case_studies/01_lab_report_pipeline.zh.md) — 多命令 agent 工作流
- [case_studies/02_meeting_brief_pattern.zh.md](case_studies/02_meeting_brief_pattern.zh.md) — 3 查询组合
- [examples/demo_dataset/walkthroughs/](../examples/demo_dataset/walkthroughs/) — 5 个可复现场景 + 期望 output 结构
