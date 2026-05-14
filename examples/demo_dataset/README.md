# Demo dataset

**English** · [中文](README.zh.md)

> A small, fully synthetic Chinese conversation corpus used by `make
> demo-ingest` and `make smoke`. **No real-person data.**
>
> The corpus is hand-crafted, not derived from any chat export. It
> mimics the *shape* of a personal six-source memory (group chat,
> private chat, email, browser history, AI conversation, voice memo)
> while remaining entirely fictional.

## Files

| File                                    | Lines | Source              |
|-----------------------------------------|-------|---------------------|
| `wechat_demo.json`                      | 120   | Synthetic group chat about a course study group |
| `qq_demo.json`                          | 80    | Synthetic 1-on-1 chat between two demo users    |
| `email_demo.json`                       | 25    | Synthetic course-announcement email thread       |
| `browser_demo.json`                     | 40    | Synthetic browser-history entries (titles + URLs) |
| `claude_demo.jsonl`                     | 60    | Synthetic Claude conversation transcripts       |
| `audio_demo_transcript.json`            | 15    | Synthetic ASR output from a fictional 3-min memo |

All file names use `_demo` suffix to make them grep-detectable.

## Schema

Each source has its own JSON schema; the schemas mirror what the real
ingestion builders expect:

- `wechat_demo.json` — array of message objects with `{room, sender,
  send_time, content}`.
- `qq_demo.json` — same shape; `room` is a synthetic chat-id.
- `email_demo.json` — array with `{from, to, subject, sent_at, body}`.
- `browser_demo.json` — array with `{visit_time, url, title}`.
- `claude_demo.jsonl` — one JSON per line with `{ts, role, content}`.
- `audio_demo_transcript.json` — `{session_id, started_at, speakers,
  utterances: [{speaker_id, start_ms, end_ms, text}]}`.

## Ingestion

```bash
python -m examples.demo_dataset.ingest
```

This script:

1. Reads each demo file.
2. Calls the matching builder in `src/ingestion/` and `src/extraction/`
   to produce per-source batches under `data/demo/<source>/batches/`.
3. Runs the two-LLM extraction (gate + extract). **By default this
   uses a stub LLM** that emits deterministic synthetic V2 envelopes
   so the smoke test does not need a real LLM endpoint. Set
   `MEMEX_REMOTE_LLM_BASE_URL` to use a real model.
4. POSTs the resulting cards to the local Hindsight daemon at
   `http://127.0.0.1:8888`.

After ingestion you should see ~80-120 cards in the
`memory_full_v5_demo` bank (separate from any real bank you may have).

## Querying

```bash
python -m src.core.memory_query topic "studying" --bank memory_full_v5_demo
python -m src.core.memory_query timeline --start 2024-01-01 --end 2024-02-01 --bank memory_full_v5_demo
python -m src.core.memory_query arc "Alice" --bank memory_full_v5_demo
```

## License

The synthetic data here is released under CC0 1.0 (public domain).
You may reuse it for any purpose without attribution. The ingestion
script itself is Apache 2.0.

## Why synthetic and not LCCC

The Large-scale Chinese Conversation (LCCC) corpus and similar public
datasets are excellent for chatbot training but have two drawbacks
for our smoke test:

1. They are large (>1 GB), unsuitable for CI.
2. They lack the *six-source diversity* we need to exercise every
   builder.

The synthetic corpus is ~30 KB, fully covered by the smoke test in
<30 seconds.
