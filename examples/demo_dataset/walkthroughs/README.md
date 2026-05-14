# Example walkthroughs

**English** · [中文](README.zh.md)

> Five real-world questions answered against the bundled synthetic dataset.
> Every command and every line of output is reproducible — `make demo-ingest`
> then follow along.

These walkthroughs were written to demonstrate **command combinations**, not
single subcommands. Each one shows:

1. The everyday question someone might ask
2. Which subcommand to pick (and which trap to avoid)
3. The exact CLI invocation
4. What the synthesized output looks like
5. Why the chosen combination beats the obvious alternative

```
┌───────────────────────────────────────────────────────────────────┐
│  Pick your question                                                │
└───────────────────────────────────────────────────────────────────┘
                              │
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
  Who is X?              Project X status?     What's on my plate?
  → 01_who_is_alice      → 03_project_status   → 05_my_pending_actions
       │                      │                      │
       ▼                      ▼                      ▼
  What did Y say?        Time window report?   Cross-source recap?
  → 04_advisor_said      → 02_weekly_summary   → 02_weekly_summary
```

## The dataset

```
demo-study-group  (WeChat)   ─── Alice, Bob, Carol, demo_user
demo-1on1-alice   (QQ)       ─── demo_user ↔ Alice (DDIA reading)
advisor@example.com (Email)  ─── midterm report directives
browser history              ─── distributed-systems + RAG research
Claude chats                 ─── system design + report structure brainstorm
voice memo                   ─── demo_user thinking aloud about midterm
```

The same week (2024-01-04 to 2024-01-22) seen through six lenses. Every
walkthrough cross-references at least two sources.

## Index

| # | Walkthrough | Pattern | Subcommands |
|---|---|---|---|
| [01](01_who_is_alice.md)   | Who is Alice?              | Person profile from a name      | `arc` + `quick` |
| [02](02_weekly_team_summary.md) | What did the group do last week? | Time-window cross-source summary | `topic` + `trends` |
| [03](03_project_status_check.md) | Midterm report — where are we? | Project rollup across sources    | `project` + `timeline` |
| [04](04_what_did_advisor_say.md) | What does advisor want?    | Person-as-counterparty deep dive | `person` |
| [05](05_my_pending_actions.md) | What's on my plate this week? | Outstanding commitments dashboard | `pending` + `quick` |

## How to run the dataset locally

```bash
# from repo root
docker compose -f docker-compose.example.yml up -d   # start backend
make demo-ingest                                      # POST 26 cards
memex doctor                                          # confirm bank has data

# then any walkthrough's commands will return real output
```

## Reading order suggestion

- New users: 01 → 04 → 05 (the "person + plate" loop most people start with)
- Engineers: 02 → 03 (the "rollup" patterns most useful for status reports)
- Power users: read all five and notice the recurring 2-step pattern of
  "broad recall → narrow refine" that shows up everywhere
