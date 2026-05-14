# Usage guide

**English** · [中文](usage_guide.zh.md)

> Every query subcommand, when to use it, what to expect back. Read
> [architecture.md](architecture.md) first if you want to understand
> what is happening behind each call.

## Decision table

| User question pattern                                                | Subcommand                                          | Why                                                       |
|----------------------------------------------------------------------|-----------------------------------------------------|-----------------------------------------------------------|
| "Who is X / what did X do / how I met X / my relationship with X"    | `arc "X"`                                           | 8 intent variants ordered by timestamp                     |
| "The whole arc of X" (X = thing / project / not-a-person)            | `topic "X"`                                         | 11 variants fanned out across two banks                    |
| "What happened between date A and B"                                 | `timeline --start A --end B`                        | Multi-variant fan-out with `when_start` filter              |
| "How is Y the teacher / classmate doing"                             | `person "Y"`                                        | Per-person dossier (article + events)                      |
| "What is the latest on project Z across all sources"                 | `project "Z"`                                       | Aggregates wechat / qq / email / browser                   |
| "What commitments / unanswered questions do I have"                  | `pending`                                           | Reads `calendar_index.json:status=active`                   |
| "Did I drop course Y" / state question                               | **Five-phase workflow** (see below)                 | Single recall cannot triangulate                            |
| "Give me a synthesized answer to question Q"                         | `reflect "Q"`                                       | LLM-summarised over recalled cards                         |

## Hard rule: queries for a person never use `topic`

`topic` has 11 built-in variants that are tuned for *purchase / decision
exploration* (e.g. "X price", "X vendor", "X return"). When `X` is a
person, BGE-M3 cosine retrieval picks up shopping noise and returns ~0
relevant cards. **Always use `arc` for people.**

Empirical validation: `arc("<person-A>")` returned 6/6 hits;
`topic("<person-A>")` returned 0/100 hits.

## Five-phase state inference

Use this protocol when the question is *"is X yes or no"* and a single
semantic recall is insufficient.

```
Phase A  Seed
  quick("X")         → entity surface forms
  arc("X")           → relationship lineage

Phase B  Entity expansion
  For each person in Phase A:
    arc("<person>")  → infer their role (peer / TA / advisor / friend)

Phase C  Five orthogonal signals
  1 user-speaks    → quick("X") where speaker_role=self
  2 user-silence   → timeline(room=<X>) ∩ user not seen
  3 boundary       → quick("X deadline") / quick("X cutoff")
  4 peer           → arc(<peer-from-Phase-B>) ∩ X
  5 private        → quick("X notes") where speaker_role=self

Phase D  Inference chain
  Combine the five signals into the most likely current state.

Phase E  Counter-evidence
  Actively search for cards that would falsify Phase D.
  If counter-evidence is empty → conclusion stands.
```

A complete worked example is in [5_phase_query.md](5_phase_query.md).

## Per-subcommand options

### `quick`

```
python -m memexa.core.memory_query quick "X" [--max-k 30] [--salience 0.0]
```

- `--max-k` raises the recall budget (default 10). Pair with
  `--salience 0.0` to inspect the full distribution.

### `topic`

```
python -m memexa.core.memory_query topic "X" [--max-cards 100] [--by-salience]
```

- Fan-outs 11 variants in parallel (`ThreadPoolExecutor(max_workers=4)`).
- Wall time is dominated by Hindsight server (15-60 s typical).
- Do **not** wrap external parallelism around it — internal fan-out
  already saturates the daemon's BGE worker.

### `arc`

```
python -m memexa.core.memory_query arc "X" [--max-cards 80]
```

- 8 intent variants run **serially** (each variant's results inform the
  next).
- Returns cards in chronological order so the first row is the earliest
  surfaced interaction.

### `timeline`

```
python -m memexa.core.memory_query timeline --start ISO --end ISO [--source S] [--room R]
```

- Multi-variant fan-out (event / message / important / email / etc.) →
  union → `when_start` filter → sort.

### `person`

```
python -m memexa.core.memory_query person "Y"
```

- Returns a synthesised article-card + the underlying event cards.

### `project`

```
python -m memexa.core.memory_query project "Z"
```

- Runs `quick` against each of (wechat, qq, email, browser_session,
  browser_search, claude_code) and presents the union grouped by source.

### `pending`

```
python -m memexa.core.memory_query pending
```

- Reads the calendar index. Returns active commitments sorted by
  `due_iso` ascending, with `salience` as tie-breaker.

### `reflect`

```
python -m memexa.core.memory_query reflect "Q"
```

- Server-side LLM synthesis. Slower (10-60 s) and requires the daemon
  to have an LLM provider configured.

## Common pitfalls

- **Tags are OR, not AND.** Hindsight's recall API treats `tags=[...]`
  as a disjunction. Post-filter on the client. See
  [lessons_learned/01_tags_are_or.md](lessons_learned/01_tags_are_or.md).
- **`budget="medium"` is rejected.** The enum is `low / mid / high`,
  not `medium`. See `memexa/core/hindsight_client.py`.
- **`max_tokens` too small returns 1 card.** Average V2 envelope card
  weighs ~866 tokens. The default `max_tokens=1024` packs only one.
- **0 cards on legacy bank.** The legacy `memory_full` bank has no
  `schema:v2` tag policy; force-querying with `tags=[kind:event,schema:v2]`
  drops everything. Query legacy banks with empty tags.

## Troubleshooting

`Q1 — query returns 0 cards`

```bash
# diagnose
python -m memexa.core.memory_query quick "X" --salience 0.0 --max-k 50
# still 0 → try a topic (different fan-out)
python -m memexa.core.memory_query topic "X" --salience 0.0 --max-cards 100
# still 0 → check the daemon is reachable
curl -s http://127.0.0.1:8888/healthz
```

`Q2 — query times out (>60 s)`

- BGE-M3 sidecar cold start — wait 30 s, retry.
- Daemon memory pressure — see lifecycle docs.
- `--max-k 50` is the sweet spot; higher values risk timeout.

`Q3 — UnicodeEncodeError on Windows`

- Set `PYTHONIOENCODING=utf-8` or run inside Windows Terminal.
