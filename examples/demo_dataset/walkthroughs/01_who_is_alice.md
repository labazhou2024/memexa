# Walkthrough 01 · Who is Alice?

**English** · [中文](01_who_is_alice.zh.md)

> **30-second TL;DR**: For people, always use `arc` first, never `topic`.
> `topic` has built-in shopping-decision variants that contaminate person
> queries. `arc` runs 8 relationship-aware variants in parallel. Then `quick`
> picks up the most-recent week that `arc` underweights.

## The scenario

You're about to meet Alice tomorrow. You haven't talked to her in a week.
Question: *who is she to me, what's the open thread, and what landmines
should I avoid?*

```
┌─────────────────────────────────────────────────────────────────────┐
│  You at 11pm:                                                       │
│  "wait — what was Alice working on again? did I owe her anything?"  │
│                                                                     │
│                          memex arc "Alice" → relationship baseline  │
│                          memex quick "Alice 1月" → most-recent week │
│                          → 5-min mental model rebuilt               │
└─────────────────────────────────────────────────────────────────────┘
```

## Step 1 — relationship baseline

```bash
memex arc "Alice" --max-cards 60
```

`arc` is the relationship-aware subcommand. It fans out **8 semantic
variants** (history / relationship / interactions / arc / chronological /
together / shared / first-met) and unions the results. For a person, this
is the right tool.

⚠️ Don't reach for `topic "Alice"` — `topic` is tuned for things and projects;
its default variants include "X 购买 价格" / "X 商家 渠道" / "X 退货" which
drag in irrelevant cards.

Expected output structure:

```
=== arc("Alice") ──── 18 cards across wechat+qq, 2024-01-05 → 2024-01-22 ───

📅 2024-01-05  qq   demo-1on1-alice
   Alice 推荐 DDIA 第 5 章 (replication / leaderless)

📅 2024-01-08  wechat  demo-study-group
   Alice 把组会改到周三下午三点

📅 2024-01-12  qq   demo-1on1-alice
   Alice ppt 已做完, 等 Bob 数据合稿

📅 2024-01-15  wechat  demo-study-group
   Alice 14:00 准时开始组会

📅 2024-01-22  wechat  demo-study-group
   Alice 通知组会因节假日改到 1-30
```

Five rows. You now know: Alice runs the study group, she's a reader (DDIA),
she's the schedule-keeper.

## Step 2 — the most-recent week

`arc` weights breadth; if Alice texted you yesterday it might still be
buried under the 2024-01-05 first-met card. Patch with `quick`:

```bash
memex quick "Alice 1月" --max-k 20
```

```
=== quick("Alice 1月") ──── 6 cards in 7-day window ───

📅 2024-01-22  wechat  Alice → group   "下周组会因节假日改到 2024-01-30"
📅 2024-01-15  wechat  Alice → group   "组会 14:00 准时开始"
📅 2024-01-12  qq      Alice → me      "做好了，等 Bob 数据合稿"
...
```

## Why this two-step pattern matters

| Step | Function | What you'd miss without it |
|---|---|---|
| 1. `arc`  | Full relationship arc (weeks ago first-met → today) | Recency bias — only see latest texts, miss the baseline |
| 2. `quick` (with name + month) | Last 1-2 weeks, no semantic re-ranking | Coverage bias — `arc` 's 60-card cap can squeeze out yesterday |

```
       arc:  ●─────●───────●──────●──────●──────●
             relationship breadth, time-balanced

     quick:                          ●●●●●●
                                     last-week recency
```

The two together = full picture in **under 30 seconds of wall time**.

## Adapt to your own data

The moment you ingest your own WeChat / QQ history, the *same* two commands
just work. Replace `Alice` with anyone you talk to regularly:

```bash
memex arc "<your-friend>" --max-cards 60
memex quick "<your-friend> $(date +%Y)年$(date +%m)月" --max-k 20
```

Most people end up wrapping this as a 2-line shell function called `who`.

## See also

- [04_what_did_advisor_say.md](04_what_did_advisor_say.md) — when the person
  is a counterparty (advisor, client, ops), use `person` instead of `arc`.
- [docs/case_studies/02_meeting_brief_pattern.md](../../../docs/case_studies/02_meeting_brief_pattern.md)
  — the same two-step pattern, stitched into a 4-section brief template.
