# Architecture

**English** · [中文](architecture.zh.md)

> Design rationale + module boundaries + data flow. For a quick install
> guide go to [quickstart.md](quickstart.md). For each query subcommand
> read [usage_guide.md](usage_guide.md).

## 1. Goal

Take six categories of personal Chinese-language exhaust — chat history,
email, browser activity, AI conversations, voice memos — and turn them
into a single queryable memory graph that can answer:

- Entity questions (*"who is X"*) — point lookup.
- Topic questions (*"the whole arc of X"*) — fan-out semantic recall.
- Relationship questions (*"how I met X"*) — ordered-by-timestamp lineage.
- State questions (*"did I drop course Y"*) — five-phase inference combining
  five orthogonal signals.

## 2. Layers

```
                ┌────────────────────────────────────────┐
   Layer 0      │  User-private raw exports              │
   (data)       │   wechat dump / qq nt_msg.db / IMAP    │
                │   / browser sqlite / claude transcripts │
                │   / wav recordings                      │
                └────────────────────────────────────────┘
                                  │
                ┌────────────────────────────────────────┐
   Layer 1      │  Per-source batch builders             │
   (ingestion)  │   memexa/ingestion/v5_*_batch_builder.py  │
                │   + memexa/extraction/qq/*                 │
                │   + memexa/extraction/claude_code_to_v5*  │
                │   Normalise to a single                 │
                │   ``messages_envelope.json`` schema.    │
                └────────────────────────────────────────┘
                                  │
                ┌────────────────────────────────────────┐
   Layer 2      │  Extraction pipeline (per batch)       │
   (extraction) │                                         │
                │   Stage A: gatekeeper LLM               │
                │     verdict ∈ {HIGH, MEDIUM, LOW}       │
                │   Stage B: extractor LLM                │
                │     emit V2 envelope JSON with          │
                │     entities, predicates, evidence,     │
                │     time resolutions                     │
                │   Stage C: BGE-M3 cosine quorum         │
                │     + DeepSeek arbiter on disagreement  │
                │   Stage D: POST to memory_full_v5       │
                │     via streaming_post_v5.py            │
                └────────────────────────────────────────┘
                                  │
                ┌────────────────────────────────────────┐
   Layer 3      │  Hindsight FastAPI daemon               │
   (storage)    │   PostgreSQL + pgvector + BGE-M3        │
                │   embeddings + temporal links           │
                │   3-API contract: retain / recall /     │
                │   reflect                                │
                └────────────────────────────────────────┘
                                  │
                ┌────────────────────────────────────────┐
   Layer 4      │  Query CLI + 5-phase state inference   │
   (query)      │   memexa/core/memory_query.py              │
                │   8 subcommands +                        │
                │   dashboard live progress panel         │
                └────────────────────────────────────────┘
```

## 3. Two-LLM gate-extract

The reason for splitting *gatekeeper* and *extractor* roles:

- Cost — a cheap chat-tier model (Qwen-14B 4-bit, Qwen3.6-chat API, or
  similar) filters out the ~40-60 % of batches that contain no extractable
  facts. Sending those to the slow extractor would burn GPU/$.
- Quality — the extractor model needs a higher reasoning budget. On Macs
  it is Gemma-31B with thinking enabled; on remote GPUs it is a 30 B+
  reasoning-tuned model. Letting it focus on HIGH/MEDIUM batches keeps
  output schema-valid.
- Failure isolation — when one stage hangs or returns bad JSON, the
  other stage's output is preserved.

Both stages run via an OpenAI-compatible base URL. Plug in vLLM, Ollama,
OneAPI, LiteLLM proxy, or any commercial endpoint that ships
`/v1/chat/completions`.

## 4. V2 envelope schema (canonical card)

Every card in the memory graph is a single PostgreSQL row whose `text`
column carries a JSON envelope wrapped in sentinel tokens:

```text
【MEMORYCARD_V2_HEADER_BEGIN】
{
  "entities": [...],
  "predicates": [...],
  "evidence_quotes": [...],
  "time_resolutions": {...},
  "narrative": "...",
  "source": "wechat",
  "when_start": "2026-04-13T10:42:00+08:00",
  "salience": 0.71,
  "types_csv": "decision,announcement"
}
【MEMORYCARD_V2_HEADER_END】
```

Schema specification is canonical in `memexa/extraction/pass2_prompt.py`.
A non-V2 row in `memory_full_v5` is **a bug** (see ingestion guard in
`memexa/extraction/streaming_post_v5.py`).

## 5. PG-aware pending tracking

A naive backfill driver keeps local `*.posted` marker files to track
which batches have been ingested. This drifts:

- *Ghost markers* — marker present, no corresponding row in PostgreSQL
  (failed POST that wrote the marker too early).
- *PG-no-marker* — row present in PostgreSQL, no local marker (worker
  was killed before writing the sidecar file).

`memexa/core/pg_bid_cache.py` adds a 1-hour LRU over the PostgreSQL truth.
Drivers query it first; markers become a fast-path hint, not the source
of truth. See [lessons_learned/03_pg_aware_pending.md](lessons_learned/03_pg_aware_pending.md).

## 6. Five-phase semantic state inference

A single semantic recall cannot answer *"did I drop course Y"*. The
top-K from BGE-M3 returns posts about the course but cannot
*triangulate* whether you are still attending. The five-phase workflow:

1. **Seed** — `quick` + `arc` on the course name.
2. **Entity expansion** — find peers, TAs, instructor surfaces.
3. **Five orthogonal signals** — user-speaks, user-silence, boundary,
   peer-triangulation, private-notes.
4. **Inference chain** — combine signals into a most-likely state.
5. **Counter-evidence** — actively search for posts that would falsify
   the inference.

Read [usage_guide.md#5-phase-state-inference](usage_guide.md#5-phase-state-inference)
for the complete protocol with template queries.

## 7. Cron orchestrator

`memexa/cron/cron_orchestrator.py` owns the 6-hour incremental cycle:

1. Per-source driver `list_pending_batches()` (PG-aware).
2. Build new batches if input data has grown.
3. Run extraction pipeline for each pending batch.
4. POST results, write `.posted` marker on success or `.dead` on
   exhausted retry budget.
5. Save cursor.

Drivers run in series within a single orchestrator invocation. Audio is
the exception — it has its own scheduler because the ASR step is
long-running. See [deployment/macos.md](deployment/macos.md) and
[deployment/windows.md](deployment/windows.md).

## 8. Dashboard

`memexa/dashboard/sys_monitor` is a FastAPI + vanilla-JS dashboard that
polls live state on a 2-second tick:

- Cron health table (last result, runtime, scheduled-vs-actual).
- Live activity panel (which driver is currently running, queue
  depth, items processed in the last cycle).
- Memory query log (last 25 invocations with subcommand, query, result
  count, latency).
- Per-source PG card counts.
- Six-source pending backlog table.

## 9. Failure semantics

- Stage A/B/C output JSON parse failure → mark batch as `.dead`,
  preserve raw response under `data/dead_letters/`. Recover with
  `tools/recover_phaseB_dead_letter.py`.
- Stage D 500/422 → exponential backoff (0/30/300/1800/14400 s) up to
  5 retries; final failure writes `.dead`.
- Hindsight daemon unreachable → fall back to `MEMEXA_HINDSIGHT_FALLBACK_URL`
  if set; otherwise propagate to caller.
- Subprocess timeout (Windows) → Job Object guarantees grandchild
  cleanup. See [lessons_learned/04_win_job_subprocess.md](lessons_learned/04_win_job_subprocess.md).
