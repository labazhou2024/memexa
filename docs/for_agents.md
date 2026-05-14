# Using memexa from an AI agent

**English** · [中文](for_agents.zh.md)

> **What this is**: a protocol document. memexa is built to be queried by
> AI agents — Claude Code, Cursor, Cline, or any agent you wrap yourself —
> on behalf of a human user. This doc is the contract every agent must
> honor to return useful answers.
>
> **Human users**: read [usage_guide.md](usage_guide.md) instead. This page
> is denser, more rule-oriented, and assumes you (the reader) are an LLM
> with a fast scan / strict-execution loop.

## 0. Why this document exists

memexa's 14 subcommands look like 14 ways to query a graph. They are
actually a small protocol with predictable failure modes. Most agents
that fail at memexa queries fail in the same 5-6 ways:

1. Calling `topic` on a person's name (returns shopping noise)
2. Trying to answer state questions ("did X happen?") with a single recall
3. Wrapping external parallelism around `topic` / `arc` (saturates daemon)
4. Treating `pending` as a semantic recall (it is a calendar-index read)
5. Skipping the pre-task SOP and re-deriving context every turn
6. Ignoring the "broad-then-narrow" composition pattern

This document is the short version of all those failures.

---

## 1. Cold-start checklist

Run these reads **once per session**, in order, before any query:

```
1. README.md            — what memexa is, what it isn't
2. usage_guide.md       — 14 subcommands + decision table
3. for_agents.md        — this file
4. (optional) the user's MEMORY.md or equivalent — what they've told you
                          to remember across sessions
```

Skip 1-3 if your environment already auto-loads them (Claude Code with
`CLAUDE.md` does). Confirm by checking that you can describe the
purpose of `arc` vs `topic` without re-reading.

## 2. Hard rules

### HR-1 — Never call `topic` on a person's name

`topic` fans out 11 semantic variants tuned for *purchase / decision
exploration* ("X price", "X vendor", "X return"). Names get drowned in
shopping noise. Empirically: `arc("<person>")` returns 6/6 hits,
`topic("<person>")` returns 0/100.

```bash
# WRONG
memexa topic "Alice"

# RIGHT
memexa arc "Alice"
```

### HR-2 — State questions require the 5-phase workflow

If the user is asking *yes/no* about something they have left the
social context of ("did I drop course X", "is project Y still going"),
a single recall cannot triangulate. Use the 5-phase workflow from
[docs/5_phase_query.md](5_phase_query.md): Seed → Expand → 5 signals →
Chain → Counter.

### HR-3 — `pending` reads the calendar index, not the recall API

```bash
# WRONG — semantic recall on the word "pending" returns garbage
memexa topic "我的待办"

# RIGHT — direct read, returns structured commitment rows
memexa pending
```

### HR-4 — Tags are OR, not AND

Hindsight's recall API treats `tags=[a, b]` as a disjunction. If you
need AND semantics, post-filter client-side. memexa's internal helper
`_post_filter()` does this; if you call the API directly, replicate it.

### HR-5 — Do not wrap external parallelism around `topic` / `arc`

These subcommands fan out **internally** via `ThreadPoolExecutor(max_workers=4)`.
Calling 3 of them in parallel from your agent saturates the daemon's
BGE-M3 worker pool — you get queue starvation, not speedup. Sequence them.

### HR-6 — `max_tokens` < 2000 truncates V2 envelopes

The average V2 envelope card weighs ~866 tokens. Default `max_tokens=1024`
returns one card. Set `max_tokens` to at least `2000 + 900 * max_cards`
when you call recall directly.

### HR-7 — Legacy bank `memory_full` has no tag policy

If you query the v3 legacy bank with `tags=["kind:event", "schema:v2"]`
you get zero results. For legacy banks, pass empty tags. memexa's bank
wrappers handle this; raw API callers must.

---

## 3. Decision table

| User natural-language question | Pick | Why |
|---|---|---|
| "Who is X?" / "how do I know X?" / "my relationship with X" | `arc "X"` | 8 relationship-intent variants, time-ordered |
| "What did X do?" (X = person) | `arc "X"` then `quick "X <month>"` | arc breadth + quick recency |
| "The whole story of X" (X = thing / project / not-a-person) | `topic "X"` | 11 semantic variants |
| "What happened between A and B?" | `timeline --start A --end B` | when_start filter |
| "What did <counterparty> ask of me?" | `person "<counterparty>"` | Directive-weighted dossier |
| "Status of project Z across all sources" | `project "Z"` then `timeline` | 6-source union + ordered read |
| "What's on my plate?" / "my commitments" | `pending` then `quick` per row | Direct calendar read + context fill |
| "Last 7 days who was most active" | `trends --by sender --window-days 7` | Aggregate, no recall |
| "Did I drop course X?" / state yes-no | **5-phase workflow** | Single recall insufficient |
| "Summarize my last week" | `summary --window-days 7` or `reflect` | LLM synthesis on recalled cards |
| "Is topic X discussed across multiple sources?" | `cross-source "X"` | 6-source coverage matrix |
| "Find cards tagged commitment / question / decision" | `types --filter <type>` | types_csv direct filter |
| "Find related entities to X via graph" | `graph-walk "X"` | Multi-hop relationship walk |

---

## 4. Pre-task SOP

Run this **before any non-trivial task** the user asks:

```
Step 1 — re-read the user's MEMORY.md / preferences
         (find any references that change the answer)

Step 2 — check `memexa pending`
         (catch any active commitment that overlaps the user's request)

Step 3 — pick a subcommand from the decision table above
         (do NOT default to quick; quick is for point lookups)

Step 4 — issue ONE broad-recall command first
         (arc / topic / project / person / pending)

Step 5 — refine with `quick` + keyword(s) extracted from step-4 output
         (this is the "broad-then-narrow" pattern; see §5)
```

Skipping Step 2 is the most common error. The user often does not
mention they already have a commitment in this area; `pending` finds it.

## 5. Composition patterns

### 5.1 Broad-then-narrow (most common, used in 80% of workflows)

```
broad recall (arc / topic / project / person / pending)
    │
    │ → 30-60 raw cards / structured rows
    │
narrow refine (quick "X <specific-keyword>")
    │
    │ → 3-8 high-signal cards
    │
human-readable answer
```

Why it works: broad calls have high recall but low ranking quality.
Narrow refines have high precision on the specific token. Stacked, you
get coverage AND focus.

### 5.2 Five-phase state inference (yes/no questions only)

```
A. Seed         quick + arc on the topic
B. Expand       arc on each named actor surfaced in A
C. 5 signals    user-speaks / user-silence / boundary / peer / private
D. Chain        combine 5 signals into one most-likely state
E. Counter      actively search for cards that falsify D
```

Each phase is 1-3 commands. Total: ~10-15 commands per state question.
Slow but unique-answer.

Full worked example: [5_phase_query.md](5_phase_query.md).

### 5.3 Cross-source coverage (claim verification)

When the user asks "is X *real* or just something I said once":

```bash
memexa cross-source "X" --days 90 --max-per-source 10
```

Returns a coverage matrix: how many cards mention X in each of 6
sources. Threshold rule: `>= 3 sources` = strong evidence, `== 1
source` = weak, `== 0` = absent.

### 5.4 Person profile composition

```bash
memexa person "Y" --window-days 30        # directives + commitments
memexa arc "Y" --max-cards 60             # relationship arc
memexa quick "Y <recent-keyword>" --max-k 15  # last 7 days
```

These three together = a complete profile of one counterparty in
under 30 seconds.

---

## 6. Common pitfalls (agent KB)

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `topic "Alice"` returns shopping cards | HR-1 | Use `arc` |
| 2 | `topic "我的待办"` returns one garbled card | HR-3 | Use `pending` |
| 3 | Single recall doesn't answer "did X happen" | HR-2 | 5-phase |
| 4 | 350 cards returned when you asked for wechat-only | HR-4 (tags OR) | Post-filter |
| 5 | `topic` query times out at 60s | Default `MEMEXA_HINDSIGHT_TIMEOUT=180` is enough; if still timing out, daemon is cold-starting BGE-M3 | Retry once after 30s |
| 6 | UnicodeEncodeError on Windows | Terminal is GBK | Set `PYTHONIOENCODING=utf-8` |
| 7 | Query returns 0 cards on a known topic | Wrong bank | Check `MEMEXA_HINDSIGHT_BANK`; default is `memory_full_v5` |
| 8 | Calendar `pending` returns stale dates | Calendar reconciliation lag | `--refresh` flag or re-run cron |
| 9 | `reflect` returns nonsense | Daemon-side LLM not configured | Use `summary` (client-side) instead |
| 10 | `arc` returns 60 cards but missing yesterday | arc weights breadth not recency | Stack with `quick "<name> <month>"` |
| 11 | Wrapping `topic` in `asyncio.gather` slows it down | HR-5 | Sequence externally |
| 12 | Card body shows `MEMORYCARD_V2_HEADER_BEGIN` raw text | You forgot to parse the V2 envelope | Use the wrapper, not the raw recall |

When you hit a symptom not in this table, **grep this file's HR list
first** before adding new search variants. Most novel-looking failures
are old failures wearing new disguise.

---

## 7. Out of scope

memexa won't:

- **Generate text the user did not say** — `reflect` summarises existing
  cards; it does not fabricate. If the user asks "what would I say to X"
  that's a roleplay request, not a memexa query.
- **Persist new facts the user just typed** — memexa ingests from 6
  source streams on a cron. There is no `memexa remember "..."` write API.
  (v0.x may add one; not yet.)
- **Order events the user never witnessed** — memexa's data is the user's
  exhaust. If the user wasn't in the room, there's no card.
- **Answer about the future** — every card has a `when_start` in the
  past. "When is X happening" only works if a commitment-tagged card
  with a future `due_iso` exists; `pending` is the right entry point.

When an agent task hits one of these, return to the user with the
limitation explicitly, do not fabricate.

---

## 8. Quick reference card

Save this in your agent's persistent prompt:

```
memexa query protocol — quick reference

Person:           arc "X" + quick "X <month>"
Project:          project "X" + timeline --start --end
Topic / thing:    topic "X"
Counterparty ask: person "X"
Plate:            pending + quick per row
State (yes/no):   5-phase (Seed/Expand/Signals/Chain/Counter)
Recent activity:  trends --by sender --window-days 7

Hard rules:
  - arc not topic for people (HR-1)
  - pending direct, not via topic (HR-3)
  - state questions need 5-phase (HR-2)
  - tags are OR; post-filter (HR-4)
  - no external parallelism on topic/arc (HR-5)

Pre-task SOP:
  1. user prefs    2. pending check    3. pick subcmd
  4. broad recall  5. narrow refine

Composition:
  broad-then-narrow (default)
  5-phase (state questions)
  cross-source (claim verification)
```

---

## See also

- [usage_guide.md](usage_guide.md) — same content from a human perspective
- [5_phase_query.md](5_phase_query.md) — full worked example
- [case_studies/01_lab_report_pipeline.md](case_studies/01_lab_report_pipeline.md) — multi-command agent workflow
- [case_studies/02_meeting_brief_pattern.md](case_studies/02_meeting_brief_pattern.md) — 3-query composition
- [examples/demo_dataset/walkthroughs/](../examples/demo_dataset/walkthroughs/) — 5 reproducible scenarios with expected output structure
