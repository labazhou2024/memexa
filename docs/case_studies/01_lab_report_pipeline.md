# Case Study 01 · Late-bound deliverable pipeline

**English** · [中文](01_lab_report_pipeline.zh.md)

> **The problem in one sentence**: You missed an experiment / class / meeting,
> and the recovery path is a written deliverable due in N days. You don't
> remember the spec. Your memexa bank does.
>
> **The pipeline takes you from "ambiguous task" to "9-page PDF" in 20 min.**

## The audience

Two real situations this fits:

1. **科研/学生 (knowledge worker)** — missed a lab session, must submit a
   pre-class report before the make-up window closes.
2. **办事人 (knowledge worker, ops flavor)** — missed a quarterly review,
   must submit a written status doc to compensate.

Both look the same to memexa: **a deadline + a deliverable + scattered
context across 4-6 sources**.

## The 7-step pipeline

```
                          ┌─────────────────────────────────┐
                          │  1. cold-start                  │
                          │     read docs + recent context  │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  2. pending direct query         │
                          │     memexa pending → hit the row  │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  3. task-brief 5-step SOP        │
                          │     memexa quick + memexa person   │
                          │     → spec + counterparty asks   │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  4. external seed (the web)      │
                          │     WebSearch + curl peer refs   │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  5. authoritative source         │
                          │     curl official handout/spec   │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  6. LaTeX render                 │
                          │     fill template → xelatex × 2  │
                          │     → PDF                         │
                          └────────────┬────────────────────┘
                                       │
                                       ▼
                          ┌─────────────────────────────────┐
                          │  7. action card + boundary       │
                          │     write checklist; do NOT      │
                          │     auto-send / auto-schedule    │
                          └─────────────────────────────────┘
```

Every box is a real command or a real human decision. No magic.

## Walk-through using `demo_dataset`

We'll pretend Alice's study group missed the in-class midterm presentation
and needs to compensate with a written report.

### Step 1 — cold start (3 min)

```bash
memexa pending
```

Output (abridged):

```
🟠 due in 1-7 days
   2024-01-16 23:59  📧 midterm report submission
                     ↳ advisor email 2024-01-08
                     ↳ sub-asks: experimental data, not just literature
```

Hit. You now know **what** is due, **when**, and **whose ask** it traces to.

### Step 2 — recover the spec (3 min)

```bash
memexa person "advisor@example.com" --window-days 14
```

Pulls the **directive list**:

- "Submit by 2024-01-16 23:59"
- "Must include experimental data — not just a literature review"

That's the contract. Two requirements, no ambiguity.

### Step 3 — recover the in-flight work (3 min)

```bash
memexa topic "midterm report"
memexa quick "experimental data section" --max-k 8
```

Two queries → you find that Carol already delivered the experimental data
(2024-01-09) and you wrote section 3 (2024-01-10). The report exists in
pieces; assembly is the missing step.

### Step 4 — external seed via WebSearch (5 min)

```bash
# Use whatever search tool you have wired up. The principle:
# - find a 2-3 page reference report in the same topic area
# - extract the section structure (intro/method/result/discussion)
```

For this demo, picture Alice grabbing a 2023 example report from a peer
and using it for structural reference (not content).

### Step 5 — authoritative source (3 min)

If a course handout / RFP / SOP exists, fetch it:

```bash
curl -o /tmp/handout.pdf "https://example.com/handout-q1.pdf"
```

This is the **authoritative spec** — the report must match it section
for section.

### Step 6 — render (3 min)

```bash
cd ~/reports/midterm
$EDITOR midterm_report.tex      # fill template using step 3 + 5 outputs
xelatex midterm_report.tex
xelatex midterm_report.tex      # second pass for refs
ls -lh midterm_report.pdf       # 9 pages, ~250 KB
```

### Step 7 — action card + boundary (1 min)

Write a one-page **action card** for tomorrow:

```
Tomorrow (2024-01-16):
  - [ ] 18:00 final read-through, fix typos
  - [ ] 22:00 email to advisor with PDF attached
  - [ ] 23:00 backup to git, post to study group

Don't:
  - send the PDF anywhere except advisor
  - announce on study group before sending to advisor
  - schedule a follow-up before getting feedback
```

The boundary is as important as the action. memexa is a query system, not
an autonomous agent. **Doing** is up to you.

## Key principle: the 2-step "broad-then-narrow" recall

You'll notice steps 2-3 follow a recurring pattern:

```
broad recall (person / topic / pending)  →  raw events / asks
                                            │
narrow refine (quick + keyword)            │
                                            ▼
                                     concrete next action
```

This pattern shows up in every memexa workflow. Once you internalize it,
the 14 subcommands collapse to a single mental model:

- "Who / what is the source of this task?"  → broad recall
- "What's the specific phrasing / number / line I need?"  → narrow refine

## Time budget honesty

The 20-min number assumes:
- Your bank already has the relevant data (no fresh ingestion)
- The external authoritative source exists and is fetchable
- LaTeX template exists from previous reports (you keep one)

First-time setup is 1-2 hours (template + LaTeX install). After that, every
recovery is 20 min flat.

## Adapt to your own data

Replace the four placeholders:

| Step | Placeholder | Your value |
|---|---|---|
| Step 1 | `memexa pending` row about midterm | Whatever your missed deliverable was |
| Step 2 | `advisor@example.com` | The actual counterparty's email/name |
| Step 4 | midterm peer report URL | Any prior reference (peer / colleague / template repo) |
| Step 5 | course handout URL | Official spec source (RFP, SOP, syllabus) |

The pipeline itself doesn't change.

## When this pipeline doesn't fit

- **No prior data in the bank** — you must ingest before querying. Cold-bank
  pipelines need a different doc (TODO: v0.2).
- **No external authoritative source exists** — you'd be inventing the spec,
  not recovering it. This is a different kind of writing task.
- **Counterparty hasn't communicated the requirement** — you need to ask
  first. memexa won't conjure requirements that were never expressed.

## See also

- [02_meeting_brief_pattern.md](02_meeting_brief_pattern.md) — same
  "broad-then-narrow" pattern, applied to meetings instead of deliverables.
- [examples/demo_dataset/walkthroughs/05_my_pending_actions.md](../../examples/demo_dataset/walkthroughs/05_my_pending_actions.md)
  — just the entry step, in isolation.
- [docs/5_phase_query.md](../5_phase_query.md) — the "is X done?" yes/no
  state inference pattern, useful in step 3.
