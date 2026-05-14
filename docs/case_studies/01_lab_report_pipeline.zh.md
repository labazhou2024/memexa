# Case Study 01 · 错过 ddl 补救流水线

[English](01_lab_report_pipeline.md) · **中文**

> **问题一句话**: 你错过了一次实验 / 课程 / 会议, 补救路径是 N 天内
> 提交一份书面交付物。你不记得具体要求。**你的 memexa bank 记得**。
>
> **这条流水线 20 分钟把你从"模糊任务"带到"9 页 PDF"。**

## 受众

两种真实场景:

1. **科研/学生** — 错过实验课, 必须在补做窗口关闭前提交一份预习报告。
2. **办事人 (运维风)** — 错过季度回顾, 必须提交一份书面状态报告补救。

对 memexa 来说两者一样: **deadline + 交付物 + 散落在 4-6 source 的 context**。

## 7 步流水线

```
                          ┌─────────────────────────────────┐
                          │  1. cold-start                   │
                          │     读 docs + 近期 context       │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  2. pending 直查                  │
                          │     memexa pending → 命中那一行    │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  3. task-brief 5 步 SOP           │
                          │     memexa quick + memexa person    │
                          │     → spec + 对手方诉求           │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  4. 外网种子 (上网搜)             │
                          │     WebSearch + curl 同行参考     │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  5. 抓权威源                      │
                          │     curl 官方讲义/SOP             │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  6. LaTeX 渲染                    │
                          │     填模板 → xelatex × 2 → PDF    │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  7. 行动卡 + 边界                 │
                          │     写 checklist; **不要**        │
                          │     自动发 / 自动排期             │
                          └─────────────────────────────────┘
```

每个 box 都是真命令或真人工决定。没有魔法。

## 用 `demo_dataset` 走一遍

我们假设 Alice 的学习小组错过了课堂中期报告展示, 需要书面报告补救。

### Step 1 — cold start (3 min)

```bash
memexa pending
```

输出 (节选):

```
🟠 1-7 天内到期
   2024-01-16 23:59  📧 中期报告提交
                     ↳ advisor email 2024-01-08
                     ↳ sub-asks: 实验数据, 不只是综述
```

命中。你现在知道**什么**要交, **什么时候**交, **谁要的**。

### Step 2 — 回忆 spec (3 min)

```bash
memexa person "advisor@example.com" --window-days 14
```

拉出**指令清单**:

- "2024-01-16 23:59 前提交"
- "必须含实验数据 — 不只是文献综述"

这是合同。两条要求, 无歧义。

### Step 3 — 回忆当前进度 (3 min)

```bash
memexa topic "midterm report"
memexa quick "experimental data section" --max-k 8
```

两条查询 → 你发现 Carol 1-09 已交实验数据, 你 1-10 写了第 3 部分。
报告各部分已经存在; 缺的是合稿这一步。

### Step 4 — 外网 WebSearch 种子 (5 min)

```bash
# 用任何你接的搜索工具。原则:
# - 找一份 2-3 页的同主题参考报告
# - 抽出 section 结构 (intro / method / result / discussion)
```

本 demo 里, 想象 Alice 从去年同学那拿到一份 2023 的样本报告作为结构参考
(不是内容参考)。

### Step 5 — 抓权威源 (3 min)

如果有课程讲义 / RFP / SOP, 抓下来:

```bash
curl -o /tmp/handout.pdf "https://example.com/handout-q1.pdf"
```

这是**权威 spec** — 报告必须 section-by-section 对得上。

### Step 6 — 渲染 (3 min)

```bash
cd ~/reports/midterm
$EDITOR midterm_report.tex      # 用 step 3 + 5 输出填模板
xelatex midterm_report.tex
xelatex midterm_report.tex      # 第二遍解 refs
ls -lh midterm_report.pdf       # 9 页, ~250 KB
```

### Step 7 — 行动卡 + 边界 (1 min)

写一页**行动卡** for 明天:

```
明天 (2024-01-16):
  - [ ] 18:00 最后通读, 改 typo
  - [ ] 22:00 发邮件给 advisor, 附 PDF
  - [ ] 23:00 备份到 git, 发学习群

不要做:
  - 把 PDF 发到 advisor 之外的地方
  - 在发 advisor 之前就发学习群
  - 在拿到反馈前先排 follow-up 会
```

边界跟动作一样重要。memexa 是查询系统, 不是自主 agent。**做事**这一步
归你。

## 核心原则: "先广召回 → 再精炼" 2 步

注意 step 2-3 是同一个模式:

```
广召回 (person / topic / pending)  →  原始事件 / 诉求
                                        │
精炼 (quick + keyword)                  │
                                        ▼
                                 具体下一步行动
```

这个模式在每个 memexa 工作流里都会出现。一旦内化, 14 个子命令塌缩成
一个心智模型:

- "这事的源头/任务派出方是谁?"  → 广召回
- "我需要的具体那一句/数字是?"  → 精炼

## 时间预算诚实说

20 分钟假设:
- bank 里已经有相关数据 (不需要现去 ingestion)
- 外部权威源存在且能 curl
- LaTeX 模板已有 (你保留了一份前次报告的模板)

第一次配置要 1-2 小时 (模板 + LaTeX 装环境)。之后每次补救都是 20 分钟。

## 用到你自己的数据

替换 4 个占位:

| Step | 占位 | 你的值 |
|---|---|---|
| Step 1 | `memexa pending` 里中期报告那行 | 你错过的那个交付物 |
| Step 2 | `advisor@example.com` | 对手方真实邮箱/姓名 |
| Step 4 | 中期报告同行参考 URL | 任何参考 (同行 / 同事 / 模板仓库) |
| Step 5 | 课程讲义 URL | 官方 spec 源 (RFP, SOP, syllabus) |

流水线本身不变。

## 什么时候这个不适用

- **bank 里没相关数据** — 必须先 ingest 才能 query。冷 bank 流水线另一个
  文档处理 (TODO: v0.2)。
- **没有外部权威源** — 你在创造 spec, 不是回忆 spec。这是不同的写作任务。
- **对手方没明确表达过要求** — 你得先去问。memexa 不会凭空造出从未表达
  过的要求。

## 相关

- [02_meeting_brief_pattern.zh.md](02_meeting_brief_pattern.zh.md) — 同样
  "先广召回 → 再精炼" 2 步, 应用到会议而不是交付物。
- [examples/demo_dataset/walkthroughs/05_my_pending_actions.zh.md](../../examples/demo_dataset/walkthroughs/05_my_pending_actions.zh.md)
  — 单看 entry step。
- [docs/5_phase_query.zh.md](../5_phase_query.zh.md) — "X 是否完成" 这种
  yes/no 状态推理模式, step 3 里有用。
