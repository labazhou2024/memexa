# Quickstart

**English** · [中文](quickstart.zh.md)

> Get to your first query in ≈ 30 minutes on a clean Win or macOS box.
> Linux users follow [deployment/docker-compose.md](deployment/docker-compose.md).

## 0. Prerequisites

- Python **3.10+**
- Docker (for the Hindsight memory backend) **or** a local PostgreSQL
  16+ with pgvector installed.
- An OpenAI-compatible chat-completions endpoint (vLLM / Ollama /
  LiteLLM proxy / DeepSeek API / 通义 / Moonshot / OpenRouter / etc).
- ~8 GB free RAM during ingestion (LLM extractor lives in a separate
  process; this is for BGE-M3 + your code).

## 1. Install

```bash
git clone https://github.com/labazhou2024/memexa.git memexa
cd memexa
python -m venv .venv
. .venv/bin/activate     # PowerShell: .venv\Scripts\Activate.ps1
pip install -e .[dev]
```

## 2. Configure

```bash
# environment (loaded by docker-compose + Makefile, not the Python code)
cp .env.example .env
$EDITOR .env

# user config (read by the Python code at runtime)
mkdir -p ~/.memexa
cp config/aliases.example.yaml   ~/.memexa/aliases.yaml
cp config/identity.example.yaml  ~/.memexa/identity.yaml
$EDITOR ~/.memexa/aliases.yaml
$EDITOR ~/.memexa/identity.yaml
```

The bare minimum to set:

- `~/.memexa/aliases.yaml` → list of strings the system should match
  to "you" (your name, nicknames, email prefixes, etc.).
- `.env` → `MEMEXA_REMOTE_LLM_BASE_URL`, `MEMEXA_REMOTE_LLM_API_KEY`,
  `MEMEXA_REMOTE_LLM_GATE_MODEL`, `MEMEXA_REMOTE_LLM_EXTRACT_MODEL`.

## 3. Bring up the memory backend

```bash
docker compose -f docker-compose.example.yml up -d
# Wait ~30 s for pgvector + Hindsight to come online
curl -sf http://127.0.0.1:8888/healthz | jq .
# {"status":"ok"}
```

## 4. First query (demo dataset)

```bash
# Sanity-check the bundle parses without any backend running
python -m examples.demo_dataset.ingest --dry-run
# → prints "total = 26 cards across 6 sources"

# Ingest into a real backend (requires step 3 to have succeeded)
make demo-ingest

# Confirm the install end-to-end
memexa doctor
# → [ok] primary /healthz returned 200
# → [ok] bank 'memory_full_v5' has N nodes
# → [ok] LLM/gate ... responded 200

# Run a few subcommands
python -m src.core.memory_query topic    "<your-keyword>"
python -m src.core.memory_query arc      "<entity>"
python -m src.core.memory_query timeline --start 2024-01-01 --end 2024-02-01
```

## 5. Wire up your own data

Each source has a builder that converts raw exports → batch JSON, plus a
driver that runs the extraction pipeline on a 6-hour schedule.

Detailed per-source onboarding:

- **WeChat** — export with [`WeChatMsg`](https://github.com/LC044/WeChatMsg)
  or [`wechatDataBackup`](https://github.com/git-jiadong/wechatDataBackup);
  point `v5_wechat_batch_builder.py` at the JSON.
- **QQ** — set `MEMEXA_QQ_ID`; the builder reads
  `~/Documents/Tencent Files/<qq-id>/nt_qq/nt_db/nt_msg.db` directly.
- **Email** — IMAP credentials in `~/.memexa/identity.yaml`.
- **Browser** — point at your browser profile's `History` SQLite file.
- **Claude Code** — point at `~/.claude/projects/`.
- **Audio** — drop `.wav`/`.m4a` files into `data/audio/inbox/`; the
  audio driver picks them up.

## 6. Schedule the cron

Pick a deployment guide and follow it end-to-end:

- [deployment/macos.md](deployment/macos.md) — launchd plist for each driver
- [deployment/windows.md](deployment/windows.md) — Scheduled Tasks (`schtasks`)
- [deployment/docker-compose.md](deployment/docker-compose.md) — Linux + Docker

## 7. Open the dashboard

```bash
python -m src.dashboard.sys_monitor.server
# Open http://127.0.0.1:8765
```

You should see seven live panels: Win/Mac/GPU CPU+memory, API usage,
memory system, cron health, graph queries, six-source pending, audio
pipeline.

## 8. Troubleshooting

- *Query returns 0 cards even though data is ingested* — see
  [usage_guide.md#troubleshooting](usage_guide.md#troubleshooting).
- *Extractor LLM returns malformed JSON* — see
  [lessons_learned/05_qwen3_no_think.md](lessons_learned/05_qwen3_no_think.md).
- *PG marker drift* — see
  [lessons_learned/03_pg_aware_pending.md](lessons_learned/03_pg_aware_pending.md).

Open an issue at `memexa/issues/new` if none of the above fits.
