# Walkthrough 05 · 我这周要做啥?

[English](05_my_pending_actions.md) · **中文**

> **30 秒 TL;DR**: `pending` 是**唯一**直接读 `calendar_index.json` 的子
> 命令 (不走语义召回)。它返回结构化 commitment 行, 按 `due_iso` 排序。
> 然后每条用 `quick` 补 context。

## 场景

```
  周一 9 点, 端着咖啡: "我这周到底要干啥?"
```

这是**未完成 commitment 面板**模式。你要:
1. 一个扁平有序的"我欠人/欠自己的事"列表
2. 每条带足够上下文, **立刻**能 triage (做/推/拒)

两条命令, 3 分钟。

## 流水线

```
   ┌──────────────────────────┐
   │  memex pending           │   ← 直接读 calendar_index.json
   │  → 4-8 条结构化行         │     不走语义召回 (不会被
   │     按 due 排序           │     垃圾卡污染)
   └────────────┬─────────────┘
                │
                │ 每一条需要 context 的:
                ▼
   ┌──────────────────────────┐
   │  memex quick "<topic>"    │   ← 拉 3-5 张最近卡
   │  --max-k 8                │     回忆原始 ask
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │  triage / batch / draft  │   ← 人工做的部分
   └──────────────────────────┘
```

## Step 1 — 看面板

```bash
memex pending
```

这条**直接读** `calendar_index.json` (它是**唯一**这么干的子命令 — 其他
全走 hindsight recall)。原因: v5 里 calendar entry 已经从原始卡抽成结构化
commitment 表, 含 `status=active`, `due_iso`, `actor` 等字段。`pending` 返回
`status=active` 的子集, 按 `due_iso` 升序。

预期输出:

```
=== pending ──── 4 条 active commitment ───

🔴 今天到期 / 已过期 (1)
   2024-01-08 14:05  📧 advisor 的 "必须含实验数据" 限定
                     ↳ 派生自 2024-01-16 23:59 中期报告 deadline
                     ↳ status: active   sal: 0.85

🟠 1-7 天内到期 (2)
   2024-01-16 23:59  📧 中期报告提交
                     ↳ advisor 邮件 2024-01-08 09:00
                     ↳ sub-asks: 实验数据, 不只综述
                     ↳ status: active   sal: 0.90

   2024-01-15 14:00  💬 组会 (Alice 排期)
                     ↳ wechat 2024-01-08 10:14
                     ↳ recurring? no — 一次性
                     ↳ status: active   sal: 0.50

🟡 未来 / 周期性 (1)
   2024-01-30 14:00  💬 下次组会 (节假日延后)
                     ↳ wechat 2024-01-22 11:00
                     ↳ status: active   sal: 0.40
```

格式刻意做得**密 + 可扫**。30 秒 triage。

## Step 2 — 补乱的那条

```bash
memex quick "midterm experimental data" --max-k 8
```

你注意到第一行 ("必须含实验数据") 模糊 — 这事是不是已经搞定了? 拉 context:

```
=== quick("midterm experimental data") ──── 5 张卡 ───

[wechat]  2024-01-09 18:03  Carol → group  "实验数据明早可以交, 已跑完"
[wechat]  2024-01-10 09:30  me → group     "第 3 部分写完, 含数据图"
[email]   2024-01-11 22:00  me → group     "大纲第三部分 = 实验"
[audio]   2024-01-11 08:32  voice memo     "Carol OK, Bob 数据明早齐"
[email]   2024-01-08 14:05  advisor        "不能只是综述"
```

现在能 triage:
- "实验数据" → ✅ Carol 1-09 交了, 你 1-10 写了第 3 部分含数据图
- 开放风险: **最终合稿的报告**到底有没有把第 3 部分塞进去?
  → 那才是 1-16 deadline 项, 未闭环

## Step 3 — 起草下一步行动

上面输出的总结:

```
今天第一件事:
  确认 Alice 合稿的 ppt 含第 3 部分 (你写的, 有实验数据)。
  如果是 → 放心到 1-16。
  如果不是 → 立刻找 Alice。
```

## 为啥 `pending` 比 `topic "todos"` 强

开发早期试过:

```bash
memex topic "我的待办" / "pending commitments" / "things to do"
```

返回一张乱码卡 "MEMORYCARD_V2_HEADER_BEGIN..."。语义召回失败因为
**commitment 在聊天数据里没有字面标签** — 它是抽取出来的派生属性。
calendar_index 是结构化 ground truth, `pending` 直接读它。

| 方案 | 召回 | 有用? |
|---|---|---|
| `topic "pending"` | 1 张乱码 | ❌ |
| `quick "todo this week"` | 字面匹配 "todo" 的随机卡 | ❌ |
| `pending` | 结构化 commitment 行 | ✅ |

这就是为啥 `pending` 是独立子命令而不是 `topic` 的变体。

## 用到你自己的数据

```bash
# 周一早 ritual
memex pending

# 每条需要 context 的:
memex quick "<keyword>" --max-k 8
```

很多人会把它做成开机 hook (LaunchAgent / Scheduled Task), 把 `pending`
输出存成每日 Markdown 文件。

## 相关

- [02_weekly_team_summary.zh.md](02_weekly_team_summary.zh.md) — 想同时知道
  "其他人也欠了啥" 时用
- [docs/case_studies/01_lab_report_pipeline.zh.md](../../../docs/case_studies/01_lab_report_pipeline.zh.md)
  — `pending` 是"ddl → PDF" 流水线的入口
