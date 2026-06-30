# Demo dataset

**English** · [中文](README.zh.md)

> A small, fully synthetic Chinese conversation corpus used by `memexa
> demo`. **No real-person data.**
>
> The corpus is hand-crafted, not derived from any chat export. It
> mimics the *shape* of a personal six-source memory (group chat,
> private chat, email, browser history, AI conversation, voice memo)
> while remaining entirely fictional. The people in it (Alice, Bob,
> Carol, demo_user) are invented.

## Files

| File | Source |
|---|---|
| `wechat_demo.json` | Synthetic group chat about a course study group |
| `qq_demo.json` | Synthetic 1-on-1 chat between two demo users |
| `email_demo.json` | Synthetic course-announcement email thread |
| `browser_demo.json` | Synthetic browser-history entries (titles + URLs) |
| `claude_demo.jsonl` | Synthetic AI-assistant conversation transcripts |
| `audio_demo_transcript.json` | Synthetic ASR output from a fictional memo |

## Schema

Each source has its own JSON schema:

- `wechat_demo.json` — array of `{room, sender, send_time, content}`.
- `qq_demo.json` — same shape; `room` is a synthetic chat-id.
- `email_demo.json` — array of `{from, to, subject, sent_at, body}`.
- `browser_demo.json` — array of `{visit_time, url, title}`.
- `claude_demo.jsonl` — one JSON per line: `{ts, role, content}`.
- `audio_demo_transcript.json` — `{session_id, started_at, speakers,
  utterances: [{speaker_id, start_ms, end_ms, text}]}`.

## Run it

The open demo ingests this corpus with a stub extractor and runs a few
sample queries against the result, entirely in memory — no backend, no
LLM key, no configuration:

```bash
memexa demo
# or, equivalently, the ingester directly:
python -m examples.demo_dataset.ingest --dry-run
```

## License

The synthetic data here is released under CC0 1.0 (public domain) —
reuse it for any purpose without attribution. The ingestion script
itself is Apache-2.0.
