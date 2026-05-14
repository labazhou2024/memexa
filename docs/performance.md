# Performance

**English** · [中文](performance.zh.md)

What latency / throughput to expect and where the bottlenecks live.
Numbers are from the maintainer's deployment (M4 Max Mac Studio + remote
GPU box for extraction) and are reproducible if you match the hardware.

## End-to-end ingestion

For one 6-hour cron cycle ingesting ~50 fresh batches across all six
sources:

| Stage          | Wall time      | Bottleneck                                |
|----------------|----------------|-------------------------------------------|
| Builders       | 10–60 s        | I/O (reading raw exports)                 |
| Stage A gate   | 90–180 s       | LLM provider latency × concurrency        |
| Stage B extract| 180–600 s      | LLM provider latency (large model + thinking) |
| Stage C arbiter| 10–30 s        | DeepSeek API or local LLM                 |
| Stage D POST   | 20–60 s        | BGE-M3 embedding + PG insert              |
| **Total**      | **5–15 min**   |                                           |

The 6-hour schedule has ~5.7 h of slack at this rate. If you sustain
>1 000 batches/day you'll run out and need to either (a) tune the LLM
provider for higher throughput or (b) accept growing backlog.

## Query latency

Tested against a 16 k-card bank with cold BGE-M3 sidecar:

| Subcommand           | Wall time      | API calls       | Notes                          |
|----------------------|----------------|-----------------|--------------------------------|
| `quick`              | 3–10 s         | 2               | Bank A + bank B union          |
| `topic`              | 100–200 s      | 22              | 11 variants × 2 banks fan-out  |
| `arc`                | 30–90 s        | 16              | 8 intent variants × 2 banks    |
| `timeline`           | 5–30 s         | 6               | Multi-variant fan-out          |
| `person`             | 5–15 s         | 2               |                                |
| `project`            | 15–40 s        | 6               | 6 sources × `quick`            |
| `pending`            | <1 s           | 0               | Reads `calendar_index.json`    |
| `reflect`            | 10–60 s        | 1 + LLM         | Server-side synthesis          |
| `summary`            | 10–60 s        | 1 + LLM         | Client-side synthesis          |
| `cross-source`       | 15–40 s        | 6               | 6 source `quick` in parallel   |

`topic` and `arc` are deliberately slow — they trade wall-time for recall
breadth. Do *not* wrap external parallelism around them: their internal
fan-out already saturates the daemon's BGE worker pool.

## Throughput knobs

### LLM concurrency

`MEMEXA_EXTRACT_CONCURRENT` controls how many extraction requests a single
driver fires in parallel. Sweet spot for most providers is 5:

| Setting | Batches / min (extractor) | Notes                              |
|---------|---------------------------|------------------------------------|
| 1       | 0.9                       | sequential, leaves throughput on table |
| 3       | 2.1                       | linear scaling                     |
| **5**   | **3.4**                   | **default — sweet spot**           |
| 7       | 3.0                       | queue saturates, slower than 5     |
| 9       | (varies)                  | many providers return 429 here     |

Validate your provider's curve before bumping past 5.

### BGE-M3 sidecar

The sidecar handles all embedding work for both retain and recall. It
processes one request at a time on Apple Silicon (Metal MPS context is
not re-entrant) and ~8 in parallel on CUDA. Calls beyond that queue
inside the sidecar — the daemon does not load-balance across sidecars.

If your recall latency is dominated by "BGE-M3 sidecar queued"
(visible in trace logs), the fix is to give the daemon more sidecar
replicas, not to add client-side parallelism.

### PostgreSQL

The `memory_units` table grows linearly with card count. Indexes that
matter:

- `(bank_id, metadata->>'source')` — used by `pg_bid_cache`
- `(bank_id, metadata->>'when_start')` — used by `timeline`
- `embedding ivfflat` — used by recall

Reindex every ~50 k new cards to keep ivfflat efficient:

```sql
REINDEX INDEX CONCURRENTLY memory_units_embedding_idx;
```

## When you outgrow this stack

The architecture is sized for "one user, six years of personal data",
roughly 100 k–500 k cards. Beyond that you'll hit:

- Postgres ivfflat ANN quality degrades; switch to HNSW or pgvector
  0.7+ with the new `hnsw` index type.
- BGE-M3 sidecar becomes the recall bottleneck on single-machine
  deploys; shard the bank or run multiple sidecars behind a load
  balancer.
- Cron orchestrator's per-driver subprocess overhead becomes
  noticeable; consider rewriting the orchestrator as a long-running
  daemon with an in-process worker pool.

None of those are on the v0.x roadmap. Open an issue if you actually
hit one — patches very welcome.

## Profiling

```bash
MEMEXA_HINDSIGHT_TRACE_LOG=/tmp/memexa_trace.jsonl \
  python -m memexa.core.memory_query topic "<query>" --max-cards 100

jq '. | select(.event=="recall_request") | {ts, duration_ms, query, n_results}' \
  /tmp/memexa_trace.jsonl
```

Per-stage breakdowns live in
[`docs/lessons_learned/`](lessons_learned/) — read those before adding
new performance optimizations; the project has already learned several
"obviously parallel"-looking traps the hard way.
