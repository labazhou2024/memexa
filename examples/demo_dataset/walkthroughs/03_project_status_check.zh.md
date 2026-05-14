# Walkthrough 03 · 中期报告到哪了?

[English](03_project_status_check.md) · **中文**

> **30 秒 TL;DR**: `project` 专门用来"把这个项目跨 6 个 source 都拉出来"。
> `timeline` 按 `when_start` 排序而不按相关度, 让你按事情发生的顺序读故事。

## 场景

```
  周二早上, 打开电脑:
  "OK 中期报告现在到哪了? 哪些做完了, 哪些没做, 卡在谁那?"
```

这是**项目 rollup** 模式 — 要的不是主题汇总, 而是一条按时间排好的叙事。

## 流水线

```
                          ┌───────────────────────────┐
                          │  memexa project "midterm"  │
                          │  → 22 张原始卡            │
                          │  跨源 (6 source)          │
                          └─────────────┬─────────────┘
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │  memexa timeline --start ..│
                          │  → 同一批卡, 按           │
                          │     when_start 重排       │
                          └─────────────┬─────────────┘
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │  从上到下当故事读         │
                          │  找出缺口和阻塞点         │
                          └───────────────────────────┘
```

## Step 1 — 把项目相关的全拉出来

```bash
memexa project "midterm report" --max-cards 40
```

`project` 跑 source 定制的并发变体 (它知道在 wechat/qq 里找"中期报告",
在 email 里看 RFC2822 subject, 在 browser 里看 page title, 在 claude log
里看 filename mentions)。输出是 **6 个 source 的并集**。

```
=== project("midterm report") ──── 22 张卡 / 6 source ───

source 分布:
  email     ▮▮▮▮             4
  wechat    ▮▮▮▮▮▮▮          7
  qq        ▮▮▮               3
  browser   ▮▮▮               3
  claude    ▮▮                2
  audio     ▮                 1
  ────────  ────              ──
  总计                        22
```

## Step 2 — 按故事排序

```bash
memexa timeline --start 2024-01-08 --end 2024-01-16 \
  --filter "topic:midterm report"
```

`timeline` 按 `when_start` 排序。从上到下读, 故事自然浮现:

```
2024-01-08 09:00  📧 email  导师: "中期报告 1-16 23:59 前提交"
2024-01-08 11:32  📧 email  me → 导师: "本周内会发您看一下大纲"
2024-01-08 14:05  📧 email  导师: "必须含实验数据, 不能只是综述"
2024-01-08 10:14  💬 wechat Alice: "组会改到周三下午三点"
2024-01-09 11:00  🤖 claude me ↔ AI: "实验部分该怎么组织？"
2024-01-09 14:20  🌐 browser FastAPI deps doc        ← 副研究
2024-01-09 18:02  💬 wechat Bob: "ppt 第 5 页, 明天交"
2024-01-09 18:03  💬 wechat Carol: "实验数据明早交"
2024-01-10 09:30  💬 wechat me: "第 3 部分写完"
2024-01-11 08:32  🎤 audio  me (voice memo): "明早 Bob/Carol 交付齐"
2024-01-11 22:00  📧 email  me → group: "大纲整理好, 四部分"
2024-01-12 15:02  💬 qq     Alice: "ppt 做好, 等 Bob 合稿"
2024-01-15 09:45  🌐 browser report template
2024-01-15 14:00  💬 wechat Alice: "组会 14:00 准时开始"
2024-01-16 22:00  🌐 browser "late submission 怎么办"       ← ⚠️ 信号!
```

## 读时间线

按**顺序**, 3 件事跳出来:

1. **第 1 天 (1-08)** — 导师定 deadline + 强调实验数据
2. **第 2 天 (1-09)** — 执行日: Bob, Carol, Alice 并行交付
3. **第 5 天 (1-12)** — Alice 在等 Bob 数据合稿
4. **第 8 天 (1-15)** — 你在搜模板 → 同一天有组会
5. **第 9 天 (1-16)** — 深夜搜 "late submission" → ⚠️ 可能错过 deadline

第 5 条从 `project` 单独看不出来, 因为相关度排序会把 deadline 邮件顶到前面。
**只有按时间排, 才会看到深夜的恐慌搜索。**

## 何时用 `project` 何时用 `topic`

| | `project "X"` | `topic "X"` |
|---|---|---|
| 跨源汇总 | ✅ source 定制变体 | ⚠️ 通用语义变体 |
| 时间窗口 | ✅ 默认整个项目史 | ⚠️ 需 `--window-days` |
| 阅读模式 | 故事 (配 `timeline` 跟上) | 主题 |
| 适合 | 项目 / 长期努力 / 多月议程 | 任何有清晰名词短语的事 |

## 何时加 `timeline`

只要问题是**"现在到哪了"** (不是"这是啥") 都加。状态 / 阻塞 / 拖延根因 ——
都是"按顺序的问题"。

## 用到你自己的数据

```bash
memexa project "<你项目名>" --max-cards 40
memexa timeline --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
  --filter "topic:<项目关键词>"
```

常见变体 — 限定单一 source (如果项目主要发生在某个 source):

```bash
memexa project "<项目>" --source wechat
```

## 相关

- [02_weekly_team_summary.zh.md](02_weekly_team_summary.zh.md) — 窗口固定
  (最近 7 天) + 需要 sender 归因 时用。
- [docs/5_phase_query.zh.md](../../../docs/5_phase_query.zh.md) — "X 是否
  完成?" 这种 yes/no 问题, 用 5-phase 模式。
