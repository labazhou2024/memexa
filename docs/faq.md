# Frequently asked questions

**English** · [中文](faq.zh.md)

Curated from the issues the maintainer has answered most often. If your
question is not here, search Discussions before opening an issue.

## Setup

### Q: Does this work without a GPU?

Yes for the **query path** (BGE-M3 runs on CPU, slower but correct).
No for the **ingestion path** if you want Mandarin-quality extraction —
the extractor LLM (Gemma-31B / Qwen-32B class) needs a GPU or a hosted
endpoint. A small model (Qwen-7B class on CPU) works for proof-of-concept
but extracts ~30 % fewer cards per batch.

### Q: Can I use OpenAI / Claude / Gemini as the extractor?

Yes. Set `MEMEXA_REMOTE_LLM_BASE_URL` to any OpenAI-compatible endpoint.
Confirmed working: vLLM, Ollama, LiteLLM proxy, DeepSeek API, OpenRouter,
OneAPI. The extractor prompt is in
[`memexa/extraction/pass2_prompt.py`](../src/extraction/pass2_prompt.py)
and is provider-agnostic.

### Q: My Hindsight container won't start.

Three usual suspects:

1. Port 8888 is already taken. `lsof -i :8888` to confirm.
2. The Postgres init script can't write `data/pg/`. Check the volume mount.
3. The `pgvector` extension is missing. The bundled image includes it;
   if you BYO Postgres, run `CREATE EXTENSION IF NOT EXISTS vector` in
   the target database.

### Q: How big is the on-disk footprint?

- ~30 GB after 1 year of intensive ingestion (6 sources, ~50 batches/day).
- ~5 GB for BGE-M3 model + sidecar weights.
- ~2 GB for Postgres indices.
- ~10 GB for raw chat archives (compressed JSON).

## Ingestion

### Q: My WeChat export fails. The builder says "no messages found".

The builder expects WeChatMsg-style JSON. Other exporters use different
schemas. If you used a different tool, write a 50-line converter that
turns its output into the canonical envelope schema
(`memexa/ingestion/v5_wechat_batch_builder.py` shows the target shape).

### Q: Why does Stage B return 0 cards on HIGH-verdict batches?

Most common cause: the extractor model is too small / too quantized.
Gemma-31B 4-bit and Qwen-32B 4-bit return well-formed JSON ~99 % of the
time; Qwen-7B 4-bit returns malformed JSON ~30 % of the time. Switch the
extractor model, or accept the false-negative rate.

Second most common cause: the gatekeeper called HIGH but the batch
genuinely has no extractable facts (small talk + emoji). HIGH is a
recall threshold, not a precision threshold.

### Q: The driver keeps re-extracting the same batches every cron run.

You hit the PG-no-marker drift case. Run:

```bash
python -m memexa.core.pg_bid_cache <source> --force-refresh
```

This rebuilds the local "already in PG" cache from Postgres truth and
the next driver run skips those batches.

### Q: How do I add a new source?

1. Write a builder that converts the raw export to the canonical envelope
   schema. Use `memexa/ingestion/v5_email_batch_builder.py` as the template.
2. Write a driver that wires the builder into the 6-hour cycle. Use
   `memexa/drivers/backfill_v5_email_driver.py` as the template.
3. Register the driver in `data/cron_manifest.yaml`.
4. Run `python -m memexa.core.cron_orchestrator validate-manifest`.
5. Open a PR.

## Query

### Q: Why does `topic` return shopping cards when I search for a person?

By design — `topic` fans out 11 variants that include "X price",
"X vendor", "X return" because the most common topic question is about
purchases. Use `arc` for people. See the
[hard rule](usage_guide.md#hard-rule-queries-for-a-person-never-use-topic).

### Q: My query returns 0 cards. The data is in there.

Walk down the diagnostic ladder:

```bash
# 1. broaden recall
python -m memexa.core.memory_query quick "X" --salience 0.0 --max-k 50
# 2. try the topic fan-out
python -m memexa.core.memory_query topic "X" --salience 0.0 --max-cards 100
# 3. check backend health
memexa doctor
# 4. check the bank has cards
curl -s http://127.0.0.1:8888/v1/default/banks/memory_full_v5/stats | jq .
```

If step 4 shows `nodes: 0`, the ingestion path is broken, not the query
path. Look at the cron logs and run `make demo-ingest` to confirm a
clean baseline works.

### Q: `reflect` returns empty / nonsense.

`reflect` needs a daemon-side LLM provider configured. If yours is
unconfigured (the env var is empty), `reflect` falls back to a stub and
returns garbage. Configure the daemon LLM or switch to `summary` (which
runs the synthesis client-side using `MEMEXA_REMOTE_LLM_BASE_URL`).

### Q: How do I phrase a state question ("did I drop class Y")?

Use the 5-phase workflow documented in
[5_phase_query.md](5_phase_query.md). A single semantic recall cannot
triangulate state — you need to combine 5 orthogonal signals.

## Privacy

### Q: Does this send my chat history anywhere?

Only to the LLM provider you configured. The Hindsight daemon and
Postgres are local. The dashboard server is local. The cron orchestrator
shells out to local Python only.

The LLM provider sees one batch at a time. If you set
`MEMEXA_REMOTE_LLM_BASE_URL=http://127.0.0.1:8000` and run vLLM / Ollama
locally, no data leaves your machine.

### Q: How do I delete a specific person from the graph?

There is no built-in "right to be forgotten" CLI yet. Manual recipe:

```sql
DELETE FROM memory_units
WHERE bank_id = 'memory_full_v5'
  AND metadata->>'persons' LIKE '%<canonical-id>%';
DELETE FROM memory_units
WHERE bank_id = 'memory_full_v5'
  AND text LIKE '%<surface-form>%';
```

Then run `python -m memexa.core.pg_bid_cache all --force-refresh` so the
drivers rebuild their pending set. Open an issue if you want this
automated.

### Q: Can I run this in a Docker container?

Yes — see [deployment/docker-compose.md](deployment/docker-compose.md).
The CLI / ingester / dashboard each run fine in containers; the only
host-coupled bit is the audio recorder watch script (it polls a USB
mount), which you'd run on the host.

## Project

### Q: Why Chinese first?

Existing Chinese exhaust (WeChat, QQ) has no good open-source memory
tool. Tools like Mem.ai or Reflect store markdown and assume English
prose. Mem0 / Letta / Zep are SDKs without a Chinese-native extractor.
The maintainer started this because nothing else worked for the obvious
input format.

The pipeline does extract English fine — Stage B handles mixed-language
batches without special-casing. There's just no documented English-only
path because no one has asked for one.

### Q: Will there be a hosted version?

No. Personal memory is the kind of thing where "self-hosted" is the
product, not a tier.

### Q: Where is the v0.1.0 release?

Cut after the fresh-clone smoke test passes on Win + macOS + Linux. See
[ROADMAP.md](../ROADMAP.md).
