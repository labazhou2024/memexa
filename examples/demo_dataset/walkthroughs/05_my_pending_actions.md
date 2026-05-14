# Walkthrough 05 · What's on my plate this week?

**English** · [中文](05_my_pending_actions.zh.md)

> **30-second TL;DR**: `pending` is the only subcommand that reads
> `calendar_index.json` directly (not a semantic recall). It returns
> structured commitment rows sorted by `due_iso`. Then `quick` for each
> to add the context you need to act.

## The scenario

```
  Monday 9am, coffee in hand. You ask: "what do I actually have to do this week?"
```

This is the **outstanding-commitments dashboard** pattern. You want:
1. A flat ordered list of "things I owe people / myself"
2. For each: enough context to triage *right now* (do / defer / decline)

Two commands, 3 minutes.

## The pipeline

```
   ┌──────────────────────────┐
   │  memex pending           │   ← reads calendar_index.json directly
   │  → ordered list of 4-8   │   NO semantic recall (not contaminated
   │    structured rows       │   by junk cards)
   └────────────┬─────────────┘
                │
                │ for each row that needs context:
                ▼
   ┌──────────────────────────┐
   │  memex quick "<topic>"    │   ← pull 3-5 recent cards
   │  --max-k 8                │     to recall the original ask
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │  Triage / batch / draft  │   ← human work
   └──────────────────────────┘
```

## Step 1 — the plate

```bash
memex pending
```

This reads `calendar_index.json` directly (it's the **only** subcommand that
does — every other one goes through hindsight recall). The reason: in v5
calendar entries are extracted from raw cards into a structured commitment
ledger with `status=active`, `due_iso`, `actor`, etc. `pending` returns the
ledger filtered to `status=active`, sorted by `due_iso` ascending.

Expected output:

```
=== pending ──── 4 active commitments ───

🔴 due today / overdue (1)
   2024-01-08 14:05  📧 advisor's "must include experimental data" caveat
                     ↳ derives from 2024-01-16 23:59 midterm deadline
                     ↳ status: active   sal: 0.85

🟠 due in 1-7 days (2)
   2024-01-16 23:59  📧 midterm report submission
                     ↳ advisor email 2024-01-08 09:00
                     ↳ sub-asks: experimental data, not just literature
                     ↳ status: active   sal: 0.90

   2024-01-15 14:00  💬 study group meeting (Alice scheduled)
                     ↳ wechat 2024-01-08 10:14
                     ↳ recurring? no — one-shot
                     ↳ status: active   sal: 0.50

🟡 future / recurring (1)
   2024-01-30 14:00  💬 next study group (moved due to holiday)
                     ↳ wechat 2024-01-22 11:00
                     ↳ status: active   sal: 0.40
```

The format is intentionally **dense + scannable**. Triage in 30 seconds.

## Step 2 — context for the messy one

```bash
memex quick "midterm experimental data" --max-k 8
```

You spotted that the top row ("must include experimental data") is
ambiguous — is that satisfied yet? Pull context:

```
=== quick("midterm experimental data") ──── 5 cards ───

[wechat]  2024-01-09 18:03  Carol → group  "实验数据明早可以交, 已跑完"
[wechat]  2024-01-10 09:30  me → group     "第 3 部分写完, 含数据图"
[email]   2024-01-11 22:00  me → group     "大纲第三部分 = 实验"
[audio]   2024-01-11 08:32  voice memo     "Carol OK, Bob 数据明早齐"
[email]   2024-01-08 14:05  advisor        "不能只是综述"
```

Now you can triage:
- "experimental data" → ✅ Carol delivered 1-09, you wrote section 3 with figs
- Open risk: did the *final assembled report* actually include section 3?
  → that's the 1-16 deadline item, not closed

## Step 3 — drafted next action

Output of the above:

```
Today's first move:
  Confirm Alice's stitched ppt has section 3 (yours, includes experimental data).
  If yes → relax until 1-16.
  If no → message Alice now.
```

## Why `pending` beats `topic "todos"`

Tried in early development:

```bash
memex topic "我的待办" / "pending commitments" / "things to do"
```

Returns one garbled card containing the literal string "MEMORYCARD_V2_HEADER_BEGIN".
Semantic recall fails because **commitments aren't textually labeled** in
chat data — they emerge from extraction. The calendar index is the structured
ground truth; `pending` reads it directly.

| Approach | Cards returned | Useful? |
|---|---|---|
| `topic "pending"` | 1 garbled card | ❌ |
| `quick "todo this week"` | Random matches on the word "todo" | ❌ |
| `pending` | Structured commitment rows | ✅ |

This is why `pending` is its own subcommand and not a `topic` variant.

## Adapt to your own data

```bash
# weekly Monday-morning ritual
memex pending

# for each row that's not self-evident:
memex quick "<keyword>" --max-k 8
```

Many users set this as a startup hook (LaunchAgent / Scheduled Task) that
prints `pending` output to a daily Markdown file.

## See also

- [02_weekly_team_summary.md](02_weekly_team_summary.md) — when you also
  want to know *who else* is on the hook
- [docs/case_studies/01_lab_report_pipeline.md](../../../docs/case_studies/01_lab_report_pipeline.md)
  — `pending` is the entrypoint to the "ddl → PDF" pipeline
