# Walkthrough 04 · 导师/老板要啥?

[English](04_what_did_advisor_say.md) · **中文**

> **30 秒 TL;DR**: 对**对手方** (导师/老板/客户/运维) — 给你下指令的那种 ——
> 用 `person`。它会拉出他们针对你的所有发言, 按"指令强度"而不是日期排序。

## 场景

```
   advisor@example.com 一周发了 3 封邮件。
   还在群里提了 2 次。
   问: 他到底要我做什么?
```

如果你翻邮箱, 3 封长邮件得读一遍。用 `person`, 10 秒拿到**指令蒸馏列表**。

## 为啥不用 `arc`?

`arc` 是给**双向关系** (你 ↔ 朋友, 多年) 用的。你和导师的关系是
**单向的**: 他说话, 你执行。"first-met" 语义变体是浪费; 你要的是
"requirements / directives / deadline" 这些变体。

`person` 跑:
- L1 文章式总结 (如有)
- L0 事件级召回, 偏向 `types_csv=announcement/decision/directive`
- 按 salience × recency 排序

## Step 1 — `person` 查询

```bash
memexa person "advisor@example.com" --window-days 30
```

(你也可以用名字: `memexa person "Dr. Smith"`, 前提是 aliases.yaml 配好了。
不确定时用邮箱形式最稳。)

预期输出:

```
=== person("advisor@example.com") ──── 6 条事件来自 emails+claude ───

🎯 DIRECTIVES (salience ≥ 0.7)

  2024-01-08 09:00  email  📧 "中期报告 1-16 23:59 前提交"
                    types: deadline, announcement
                    where: course notification

  2024-01-08 14:05  email  📧 "至少要包含实验数据, 不能只是文献综述"
                    types: directive, requirement
                    salience: 0.85 — 这是实质要求

📋 OPEN COMMITMENTS (你说过会做的事)

  2024-01-08 11:32  email  📧 me → advisor:
                    "本周内会发您看一下大纲"
                    → 完成 2024-01-11 22:00 (大纲 email)  ✅ closed

🗓️ CALENDAR-AFFECTING

  2024-01-16 23:59       deadline 来自 1-08 directive
```

输出是**行动导向**的。前 3 行后可以停 — 一共就 2 条 directive + 1 条已闭环
commitment。

## Step 2 — 抽出隐含合同

从上面的输出, 写下来 (或粘贴到 todo):

```
欠 advisor 2024-01-16 23:59 之前交:
  ✅ 大纲 (1-11 已发)
  ☐  完整报告
       └── 必须含实验数据 (per 2024-01-08 14:05)
       └── 不只是文献综述 (明确)
```

"必须含实验数据" 那一行如果你快速扫邮件可能会漏。`person` 把它顶上来,
因为:
- `types_csv` 含 `requirement`
- `body_text` 含 "不能只是" — 强约束标记
- 与 deadline directive 同发件人 + 同主题 → 主题强化

## 为啥这个比搜邮箱强

| 搜邮箱 | `memexa person` |
|---|---|
| 3 封长邮件全文 | 2 行高信号 |
| 容易漏 "必须含 X" 那个限定语 | 限定语排序在 deadline 之上 |
| 按日期线性 | 按 salience 排 |
| 每次都得重读 | 一条命令, 输出粘到 chat / todo |

## 用到你自己的数据

```bash
# 模式: 单个对手方
memexa person "<boss@company.com>"          --window-days 14
memexa person "<professor-name>"            --window-days 90
memexa person "<client-contact>"            --window-days 30
```

频繁对手方 (经理 / 导师) — 设个日刷新 alias:

```bash
# 在 ~/.bashrc 或 .zshrc
alias say-what='memexa person "boss@company.com" --window-days 7'
```

## 何时不用 `person`

- 双向关系 (朋友 / 伴侣) → 用 `arc`
- "我这周欠多少人多少东西" (多对手方) → 用 `pending`
- "X 是不是说过 XXX" (你已经有关键词) → 用 `quick "<X> + keyword"`

## 相关

- [01_who_is_alice.zh.md](01_who_is_alice.zh.md) — 双向关系怎么查
- [05_my_pending_actions.zh.md](05_my_pending_actions.zh.md) — 多对手方的
  待办面板视图
- [docs/case_studies/02_meeting_brief_pattern.zh.md](../../../docs/case_studies/02_meeting_brief_pattern.zh.md)
  — `person` 输出作为 4 段会议 brief 的输入之一
