# Walkthrough 02 · What did our group do last week?

**English** · [中文](02_weekly_team_summary.zh.md)

> **30-second TL;DR**: `topic` fans out 11 variants for a project name —
> good for breadth. `trends` aggregates over a time window by source/sender —
> good for "who did most of the work". Stack them.

## The scenario

Sunday night. You need to send a one-paragraph status update to your advisor:
*what did our study group actually accomplish between Mon and Fri?*

```
                       last 5 days
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
    WeChat group        QQ 1-on-1           AI chats
   (15 messages)        (Alice / DDIA)    (system design)
        │                   │                   │
        └─────────┐         │         ┌─────────┘
                  ▼         ▼         ▼
              memexa topic "midterm report"
              memexa trends --by sender --window-days 7
                            │
                            ▼
                ┌────────────────────────┐
                │  One-paragraph update  │
                │  + per-person credit   │
                └────────────────────────┘
```

## Step 1 — what was the project's heartbeat this week?

```bash
memexa topic "midterm report" --window-days 7
```

`topic` fans out **11 semantic variants** by default — for a project name
this is exactly what you want: it catches "中期报告" / "报告大纲" / "实验
部分" / "演示文稿" / "提交截止" / etc. as separate phrasings.

Expected output:

```
=== topic("midterm report") ──── 14 cards / 4 sources / 7-day window ───

[wechat]  2024-01-08 10:14  Alice → group   "组会改到周三下午三点"
[wechat]  2024-01-09 18:02  Bob → group     "演示文稿做到第 5 页, 明天交"
[wechat]  2024-01-09 18:03  Carol → group   "实验数据明早可以交, 已跑完"
[wechat]  2024-01-10 09:30  me → group      "第 3 部分写完了"
[qq]      2024-01-12 15:02  Alice → me      "ppt 做好了, 等 Bob 数据合稿"
[email]   2024-01-08 09:00  advisor → me    "中期报告 1-16 23:59 前提交"
[email]   2024-01-08 14:05  advisor → me    "至少包含实验数据, 不能只是综述"
[email]   2024-01-11 22:00  me → group      "整理了一份大纲, 四部分..."
[browser] 2024-01-09 14:20  fastapi-deps doc      
[browser] 2024-01-15 09:45  midterm-report-template
[claude]  2024-01-09 11:00  user → claude   "实验部分该怎么组织？"
[claude]  2024-01-09 11:00  claude          "三段式：设置/结果/讨论"
[audio]   2024-01-11 08:32  voice memo      "Bob 数据明早齐, Carol OK"
...
```

## Step 2 — who actually did what?

```bash
memexa trends --by sender --window-days 7 --filter "topic:midterm"
```

`trends` aggregates by sender/source/room/type. For "credit assignment" — i.e.
*who showed up* — sender is the right axis.

```
=== trends by sender, last 7 days, midterm-tagged cards ───

       sender       │ cards │ bar
       ─────────────┼───────┼─────────────────────────────
       Alice        │   5   │ ████████████  schedule + ppt
       me           │   4   │ ██████████    section 3 + outline
       Bob          │   2   │ █████         data / ppt page 5
       Carol        │   2   │ █████         experiment data
       advisor      │   2   │ █████         requirements + reminders
       (claude AI)  │   1   │ ██▌           structure brainstorm
```

## Compose the one-paragraph update

You now have everything to write three honest sentences:

```
本周组会推进中期报告。Alice 把组会改到周三 14:00（1-15）并合稿 ppt；
Bob 完成 ppt 第 5 页 + Carol 跑完实验数据（均 1-09 交付）；
我（demo_user）写完第 3 部分并提交大纲（1-10 / 1-11）。
导师 1-08 邮件强调"必须含实验数据"，符合当前进度。
下一里程碑：1-16 23:59 提交完整报告。
```

## Why `topic + trends` beats `topic` alone

| Just `topic` | `topic + trends` |
|---|---|
| 14 raw cards, you eyeball them | 14 raw cards **plus** a one-row-per-person breakdown |
| Have to count per-person manually | Bar chart literally shows you the rank |
| Easy to over-credit the loud one (Alice has most cards) | trends shows Carol's 2 cards are full-merit (data delivered) |

## Adapt to your own data

```bash
memexa topic "<project-name>" --window-days 7
memexa trends --by sender --window-days 7 --filter "topic:<project-keyword>"
```

If you'd rather see *which source* dominated (e.g. "are we mostly meeting on
WeChat or email lately?"):

```bash
memexa trends --by source --window-days 7
```

## See also

- [03_project_status_check.md](03_project_status_check.md) — when you need
  the full timeline (not just last 7 days), swap `topic` for `project + timeline`.
- [docs/usage_guide.md#trends](../../../docs/usage_guide.md) — full `trends` options.
