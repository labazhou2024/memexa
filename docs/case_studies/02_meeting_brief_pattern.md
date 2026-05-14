# Case Study 02 · 5-minute meeting brief pattern

**English** · [中文](02_meeting_brief_pattern.zh.md)

> **The problem in one sentence**: You'll see X in 30 minutes. What's the
> baseline? When did you last talk? What's the open thread? What's the
> landmine? Most people skim chat history — slow and error-prone. memexa
> turns it into 4 sections in 5 minutes.

## The audience

- **办事人 (knowledge worker, ops)** — pre-meeting / pre-1:1 brief
- **科研/学生** — before seeing advisor, collaborator, lab-mate
- **PM / sales** — before any customer call

Same pattern, different vocabulary.

## The 4-section brief structure

```
   ┌────────────────────────────────────────────────────────┐
   │ §1  BASELINE                                           │
   │     who they are, how you know them, key time anchors  │
   │     "one-line portrait"                                │
   ├────────────────────────────────────────────────────────┤
   │ §2  LAST CONTACT                                       │
   │     when, what about, what was the tone                │
   │     "what they remember of you"                        │
   ├────────────────────────────────────────────────────────┤
   │ §3  OPEN THREADS                                       │
   │     promises pending, questions outstanding            │
   │     "what they expect you to bring up"                 │
   ├────────────────────────────────────────────────────────┤
   │ §4  LANDMINES                                          │
   │     touchy topics, things to avoid mentioning          │
   │     "what NOT to say"                                  │
   └────────────────────────────────────────────────────────┘
```

This template is **prescriptive**. Don't add sections. Don't drop sections.
The whole point is that you can fill it in 5 min and use it cold.

## The 3-command extraction pipeline

```
                      ┌────────────────────────────┐
                      │ INPUT: person name / handle│
                      └─────────────┬──────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
       ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
       │  memexa arc   │     │ memexa quick  │     │ memexa quick  │
       │  (60 cards,  │     │ "<name>      │     │ "<name>      │
       │  relationship│     │  this week"  │     │  question"   │
       │  history)    │     │  (recency)   │     │  (asks)      │
       └──────┬───────┘     └──────┬───────┘     └──────┬───────┘
              │                    │                    │
              └────────────────────┼────────────────────┘
                                   │
                                   ▼
                      ┌────────────────────────────┐
                      │  HUMAN SYNTHESIS           │
                      │  (4 min, you fill template)│
                      └────────────────────────────┘
```

## Demo: brief for Alice (from `demo_dataset`)

You're meeting Alice tomorrow morning. Three queries:

### Query 1 — `arc` for relationship breadth

```bash
memexa arc "Alice" --max-cards 60
```

You read top→bottom and extract:

- Alice is the study-group lead (recurring "组会改到..." messages)
- DDIA-reading partner (recommended Ch.5 on replication, 2024-01-05)
- She did the ppt; you did section 3; Bob/Carol did data
- Communication mostly QQ 1-on-1 + WeChat group

→ §1 BASELINE is now full.

### Query 2 — `quick` for the freshest 7 days

```bash
memexa quick "Alice 1月" --max-k 20
```

- 2024-01-22: she rescheduled study group to 2024-01-30 (holiday)
- 2024-01-15: she ran the 14:00 meeting on time
- 2024-01-12: she said "ppt 做好，等 Bob 数据合稿"

→ §2 LAST CONTACT is the rescheduling (2024-01-22).

### Query 3 — `quick` for outstanding asks

```bash
memexa quick "Alice 等 待 问" --max-k 15
```

This catches "waiting for" / "still need" / "could you" language.

- 2024-01-12 qq: Alice said "等 Bob 数据合稿" — but Bob's data was actually
  delivered 2024-01-09 18:03. **Open thread: confirm she got Bob's data.**

→ §3 OPEN THREADS = "confirm she has Bob's final data; otherwise the
   midterm assembly is blocked".

§4 LANDMINES — anything notably tense? Scan `arc` output for negative
sentiment markers. In the demo data, no major friction. (In real data,
look for: previous canceled plans, criticism, money topics.)

## The filled brief

```markdown
# Meeting brief: Alice — 2024-01-23 morning

## §1 Baseline (who is she)
- Study-group lead; DDIA reading partner
- Owns: schedule + final ppt stitching
- We split: Alice=ppt, me=section 3, Bob=ppt page 5, Carol=experiment data
- Comm channels: QQ 1-on-1 + WeChat group

## §2 Last contact
- 2024-01-22 — she pushed study group to 2024-01-30 (holiday)
- Before that: 2024-01-15 in-person meeting, normal cadence

## §3 Open threads
- Did she ever get Bob's final data? She said 1-12 "等 Bob 数据合稿"
  but Bob delivered 1-09. Might be a sync issue.
- Midterm report assembly status

## §4 Landmines
- None spotted in chat history
- Note: don't bring up holiday plans (rescheduling was her call)
```

5 minutes from "memexa arc Alice" to filled brief. You walk into the meeting
having scanned the relationship in a way that would otherwise take 30
minutes of scrolling.

## Why this template, exactly?

Each section answers one question the other person silently asks:

| Section | The implicit question | Failure mode if you skip |
|---|---|---|
| §1 Baseline | "Do you actually know me?" | You make a beginner mistake (forgetting their role) |
| §2 Last contact | "Do you remember what we said?" | You re-litigate something you already agreed on |
| §3 Open threads | "Are you addressing what I asked?" | They feel ignored |
| §4 Landmines | "Are you reading the room?" | You step on a sore spot |

If the brief skips any one, you're going in half-blind.

## When to skip a step (vs follow all 3)

| Situation | Need all 3? |
|---|---|
| Daily coworker, just saw them yesterday | No. `quick "<name> 今天"` is enough |
| Weekly 1-on-1 you've had for months | Skip §1. Run `arc` once, save baseline; just refresh §2-§4 |
| Cold meeting with someone you barely know | Yes. Run all 3 + spend extra time on §1 |
| Re-engaging after 3+ months silence | Yes, double-check §4 — relationships drift |

## Pre-canned scripts

Once the pattern clicks, wrap it:

```bash
# in ~/.bashrc or PowerShell profile
function brief {
  echo "=== §1 BASELINE ==="
  memexa arc "$1" --max-cards 60
  echo ""
  echo "=== §2 LAST 7 DAYS ==="
  memexa quick "$1 最近" --max-k 15
  echo ""
  echo "=== §3 OPEN THREADS ==="
  memexa quick "$1 等 待 问 还没" --max-k 15
}

# usage
brief "Alice" > today_alice.md
```

Then in the meeting you have today_alice.md open in a side pane.

## Adapt to your own data

The template is universal. The queries swap one word:

```bash
brief "<your-counterparty>"
```

Some users version-control these briefs (git in a private repo) so they
can look back at "what was my read on X in 2024 Q3?".

## When this doesn't fit

- **You've never talked to them before** — memexa has no data. Use LinkedIn /
  their CV instead.
- **The meeting is purely informational (lecture, presentation)** — no
  relationship layer needed. Skip §3-§4, use [03_project_status_check.md](../../examples/demo_dataset/walkthroughs/03_project_status_check.md)
  for the topic prep.
- **Adversarial / negotiation** — the 4-section template is too friendly.
  Add §5 "their interests" and §6 "BATNA" (out of scope here).

## See also

- [examples/demo_dataset/walkthroughs/01_who_is_alice.md](../../examples/demo_dataset/walkthroughs/01_who_is_alice.md)
  — the `arc + quick` 2-step in isolation
- [examples/demo_dataset/walkthroughs/04_what_did_advisor_say.md](../../examples/demo_dataset/walkthroughs/04_what_did_advisor_say.md)
  — when the counterparty is a *one-sided* relationship (boss / advisor),
  swap `arc` for `person`
- [01_lab_report_pipeline.md](01_lab_report_pipeline.md) — when the
  meeting output is a deliverable (not a conversation)
