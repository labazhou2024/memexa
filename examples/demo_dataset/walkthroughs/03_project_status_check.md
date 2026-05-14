# Walkthrough 03 · Midterm report — where are we?

**English** · [中文](03_project_status_check.zh.md)

> **30-second TL;DR**: `project` is purpose-built for "give me everything
> across all 6 sources for this project". `timeline` reorders by event time,
> not relevance, so you read the story in the order it happened.

## The scenario

```
  Tuesday morning, you open the laptop and ask:
  "OK so where is the midterm report? what's done, what's left, who's blocking?"
```

This is the **project rollup** pattern — you want a single ordered narrative,
not a thematic dump.

## The pipeline

```
                          ┌───────────────────────────┐
                          │  memexa project "midterm"  │
                          │  → 22 raw cards           │
                          │  cross-source (6 sources) │
                          └─────────────┬─────────────┘
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │  memexa timeline --start ..│
                          │  → same cards, ordered    │
                          │     by when_start         │
                          └─────────────┬─────────────┘
                                        │
                                        ▼
                          ┌───────────────────────────┐
                          │  Read top→bottom as story │
                          │  Spot gaps & blockers     │
                          └───────────────────────────┘
```

## Step 1 — pull everything for the project

```bash
memexa project "midterm report" --max-cards 40
```

`project` runs source-aware variants in parallel (it knows to look for
"中期报告" in wechat/qq, RFC2822 subject in email, page titles in browser,
filename mentions in claude logs). Output is **union of 6 source channels**.

```
=== project("midterm report") ──── 22 cards / 6 sources ───

source breakdown:
  email     ▮▮▮▮             4
  wechat    ▮▮▮▮▮▮▮          7
  qq        ▮▮▮              3
  browser   ▮▮▮              3
  claude    ▮▮               2
  audio     ▮                1
  ────────  ────             ──
  total                      22
```

## Step 2 — order it as a story

```bash
memexa timeline --start 2024-01-08 --end 2024-01-16 \
  --filter "topic:midterm report"
```

`timeline` sorts by `when_start`. Read top-to-bottom and the story emerges:

```
2024-01-08 09:00  📧 email  advisor: "中期报告 1-16 23:59 前提交"
2024-01-08 11:32  📧 email  me → advisor: "本周内会发您看一下大纲"
2024-01-08 14:05  📧 email  advisor: "必须含实验数据, 不能只是综述"
2024-01-08 10:14  💬 wechat Alice: "组会改到周三下午三点"
2024-01-09 11:00  🤖 claude me ↔ AI: "实验部分该怎么组织？"
2024-01-09 14:20  🌐 browser FastAPI deps doc        ← side research
2024-01-09 18:02  💬 wechat Bob: "ppt 第 5 页, 明天交"
2024-01-09 18:03  💬 wechat Carol: "实验数据明早交"
2024-01-10 09:30  💬 wechat me: "第 3 部分写完"
2024-01-11 08:32  🎤 audio  me (voice memo): "明早 Bob/Carol 交付齐"
2024-01-11 22:00  📧 email  me → group: "大纲整理好, 四部分"
2024-01-12 15:02  💬 qq     Alice: "ppt 做好, 等 Bob 合稿"
2024-01-15 09:45  🌐 browser report template
2024-01-15 14:00  💬 wechat Alice: "组会 14:00 准时开始"
2024-01-16 22:00  🌐 browser "late submission 怎么办"       ← ⚠️ signal!
```

## Reading the timeline

Three things jump out from the **order**:

1. **Day 1 (1-08)** — advisor sets the deadline + emphasis on experiments
2. **Day 2 (1-09)** — execution day: Bob, Carol, Alice deliver in parallel
3. **Day 5 (1-12)** — Alice waits on Bob's data for final stitching
4. **Day 8 (1-15)** — you search for templates → meeting same day
5. **Day 9 (1-16)** — late-night browser search "late submission" → ⚠️ might miss deadline

You wouldn't catch #5 from `project` alone, because relevance ranking puts
the deadline email at the top. **Time order is what surfaces the late-night
panic search.**

## When to use `project` vs `topic`

| | `project "X"` | `topic "X"` |
|---|---|---|
| Cross-source aggregation | ✅ explicit per-source variants | ⚠️ generic semantic variants |
| Time window | ✅ defaults to full project history | ⚠️ needs `--window-days` |
| Reading mode | Story (with `timeline` follow-up) | Theme |
| Best for | Project / initiative / multi-month effort | Anything with a clear noun-phrase name |

## When to add `timeline`

Whenever the question is **"where ARE we"** (not "what is this"). Status,
blockers, root-cause of delay — all sequential questions.

## Adapt to your own data

```bash
memexa project "<project-name>" --max-cards 40
memexa timeline --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
  --filter "topic:<project-keyword>"
```

A common variant — narrow to one source if a project lives mostly in one place:

```bash
memexa project "<project>" --source wechat
```

## See also

- [02_weekly_team_summary.md](02_weekly_team_summary.md) — when the window
  is fixed (last 7 days) and you want sender attribution.
- [docs/5_phase_query.md](../../../docs/5_phase_query.md) — when "where are
  we?" needs a yes/no answer ("Is it done?"), use the 5-phase pattern.
