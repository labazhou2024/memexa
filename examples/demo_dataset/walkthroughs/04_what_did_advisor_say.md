# Walkthrough 04 · What does my advisor actually want?

**English** · [中文](04_what_did_advisor_say.zh.md)

> **30-second TL;DR**: For *counterparties* (advisor, boss, client, ops) —
> people who give you directives — use `person`. It pulls every utterance
> they aimed at you and sorts by `requirement strength`, not date.

## The scenario

```
   advisor@example.com sent 3 emails in a week.
   Plus mentioned things twice in passing.
   Question:  what exactly do they want from me?
```

If you eyeball the inbox you'll re-read 3 long emails. If you use `person`,
you get the **distilled requirement list** in 10 seconds.

## Why not `arc`?

`arc` is for **two-sided relationships** (you ↔ friend over years). Your
advisor relationship is **one-sided**: they say things, you execute. The
"first-met" semantic variant is wasted; the "requirements / directives /
deadline" variants are what you want.

`person` runs:
- L1 article-style summarization (if available)
- L0 event-level recall biased toward `types_csv=announcement/decision/directive`
- Sorts by salience × recency

## Step 1 — the `person` query

```bash
memexa person "advisor@example.com" --window-days 30
```

(You can also use a name: `memexa person "Dr. Smith"` once your aliases.yaml
is set up. The email form is unambiguous when you're not sure.)

Expected output:

```
=== person("advisor@example.com") ──── 6 events from emails+claude ───

🎯 DIRECTIVES (salience ≥ 0.7)

  2024-01-08 09:00  email  📧 "中期报告 1-16 23:59 前提交"
                    types: deadline, announcement
                    where: course notification

  2024-01-08 14:05  email  📧 "至少要包含实验数据, 不能只是文献综述"
                    types: directive, requirement
                    salience: 0.85 — this is the substantive ask

📋 OPEN COMMITMENTS (you said you'd do this)

  2024-01-08 11:32  email  📧 me → advisor:
                    "本周内会发您看一下大纲"
                    → done 2024-01-11 22:00 (大纲 email)  ✅ closed

🗓️ CALENDAR-AFFECTING

  2024-01-16 23:59       deadline derived from 1-08 directive
```

The output is **action-oriented**. You can stop reading after the first 3
rows — there are exactly two directives and one closed commitment.

## Step 2 — extract the implicit contract

From the output above, write down (or paste into your todo):

```
Owed to advisor by 2024-01-16 23:59:
  ✅ outline (sent 2024-01-11)
  ☐  full report
       └── MUST include experimental data (per 2024-01-08 14:05)
       └── NOT a literature review (explicit)
```

The "must include experimental data" line is the one you'd miss if you
skim-read. `person` upweights it because:
- `types_csv` contains `requirement`
- `body_text` contains 不能只是 ("not only") — a strong constraint marker
- Same sender + topic as the deadline directive → topical reinforcement

## Why this beats searching your inbox

| Searching inbox | `memexa person` |
|---|---|
| You see all 3 emails in full | You see 2 high-signal lines |
| Easy to miss the "must include X" caveat | Caveat is ranked above the deadline itself |
| Linear by date | Sorted by salience |
| Re-reading the full thread each time | One command, paste output to chat / todo |

## Adapt to your own data

```bash
# Pattern: any single counterparty
memexa person "<boss@company.com>"          --window-days 14
memexa person "<professor-name>"            --window-days 90
memexa person "<client-contact>"            --window-days 30
```

For *frequent* counterparties (manager, advisor) — pin a daily refresh:

```bash
# in ~/.bashrc or .zshrc
alias say-what='memexa person "boss@company.com" --window-days 7'
```

## When to NOT use `person`

- Two-sided relationship (friend / partner) → use `arc`
- "What do I owe people this week" (multiple counterparties) → use `pending`
- "Did X say something specific?" (you have the keyword) → use `quick "<X> + keyword"`

## See also

- [01_who_is_alice.md](01_who_is_alice.md) — for two-sided relationships
- [05_my_pending_actions.md](05_my_pending_actions.md) — for the
  multi-counterparty plate-of-todos view
- [docs/case_studies/02_meeting_brief_pattern.md](../../../docs/case_studies/02_meeting_brief_pattern.md)
  — `person` output as one of four inputs into a meeting brief
