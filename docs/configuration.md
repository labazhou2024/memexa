# Configuration reference

**English** · [中文](configuration.zh.md)

Every knob `memexa` reads at runtime. Anything not listed here is
either an implementation detail or a deprecated env var slated for
removal.

## Where settings live

| Layer            | Path                                      | Format | Reload    |
|------------------|-------------------------------------------|--------|-----------|
| Environment      | shell / `.env`                            | env    | per-process |
| User config      | `~/.memexa/identity.yaml`                  | YAML   | per-process (LRU-cached) |
| User config      | `~/.memexa/aliases.yaml`                   | YAML   | per-process (LRU-cached) |
| User config      | `~/.memexa/config.yaml`                    | YAML   | per-process |
| Project config   | `config/aliases.example.yaml`             | YAML   | template only |
| Project config   | `config/identity.example.yaml`            | YAML   | template only |

Resolution order is **env wins** over **`~/.memexa/*.yaml`** over
**workspace defaults**. Set a key in one layer; do not duplicate it.

## Core environment variables

### Workspace + paths

| Variable                    | Default                     | Used by                          |
|-----------------------------|-----------------------------|----------------------------------|
| `MEMEXA_WORKSPACE_ROOT`      | `~/.claude/projects/`       | `_path_resolver` — every module that reads workspace state |
| `MEMEXA_CONFIG_DIR`          | `~/.memexa/`                 | `memexa init` target              |
| `MEMEXA_QUERY_LOG_PATH`      | `<workspace>/data/memory_query_log.jsonl` | `memory_query` invocation log |

### Memory backend (Hindsight)

| Variable                          | Default                  | Notes                                       |
|-----------------------------------|--------------------------|---------------------------------------------|
| `MEMEXA_HINDSIGHT_URL`             | `http://127.0.0.1:8888`  | Primary daemon URL                          |
| `MEMEXA_HINDSIGHT_FALLBACK_URL`    | unset                    | If set, retried on primary connect/timeout  |
| `MEMEXA_HINDSIGHT_BANK`            | `memory_full_v5`         | Target bank for retain / recall / reflect   |
| `MEMEXA_HINDSIGHT_TIMEOUT`         | `180`                    | Seconds per HTTP call (recall may be slow under load) |
| `MEMEXA_HINDSIGHT_TRACE_LOG`       | unset                    | Path to a JSONL trace file; unset = no trace |

### PostgreSQL (bid cache)

The `pg_bid_cache` module needs read access to the Hindsight Postgres
to authoritatively decide which batches are already in the graph.

| Variable               | Default     | Notes                                              |
|------------------------|-------------|----------------------------------------------------|
| `MEMEXA_PG_DSN`         | unset       | Full DSN (`postgres://user@host:5433/hindsight`). When set, overrides host/port/user/db. |
| `MEMEXA_PG_HOST`        | `127.0.0.1` |                                                    |
| `MEMEXA_PG_PORT`        | `5433`      |                                                    |
| `MEMEXA_PG_USER`        | `$USER`     | Falls back to `getpass.getuser()`                  |
| `MEMEXA_PG_DB`          | `hindsight` |                                                    |
| `MEMEXA_PG_BID_TTL_SEC` | `3600`      | On-disk cache TTL                                  |
| `MEMEXA_PG_SSH_TARGET`  | unset       | If set, uses `ssh <target> psql ...` instead of direct psycopg2. Use only when PG port is firewalled. |
| `MEMEXA_PG_PSQL_BIN`    | `psql`      | Only consulted in ssh mode                         |

### LLM provider (OpenAI-compatible)

Used by Stage A (gatekeeper) and Stage B (extractor).

| Variable                          | Required? | Example                                |
|-----------------------------------|-----------|----------------------------------------|
| `MEMEXA_REMOTE_LLM_BASE_URL`       | yes       | `http://127.0.0.1:8000`                |
| `MEMEXA_REMOTE_LLM_API_KEY`        | provider-dependent | `sk-…`                       |
| `MEMEXA_REMOTE_LLM_GATE_MODEL`     | yes       | `qwen2.5-14b-instruct` / `gpt-4o-mini` |
| `MEMEXA_REMOTE_LLM_EXTRACT_MODEL`  | yes       | `gemma-2-27b-it` / `deepseek-chat`     |
| `MEMEXA_EXTRACT_CONCURRENT`        | `5`       | Per-driver concurrency for the extract endpoint. Sweet spot is 5 for most vLLM deploys. |

### Audio (optional)

Only relevant if you ingest voice memos. The audio pipeline can run
locally (MLX whisper on Apple Silicon) or remotely (faster-whisper on
Linux/CUDA).

| Variable               | Default                          | Notes                            |
|------------------------|----------------------------------|----------------------------------|
| `HF_HUB_OFFLINE`       | unset                            | Set to `1` after first model download to skip Hub probes |
| `MEMEXA_AUDIO_INBOX`    | `<workspace>/data/audio/inbox`   | Where the LaunchAgent / cron picks up new files |
| `MEMEXA_AUDIO_DEVICE`   | `cpu`                            | One of `cpu` / `cuda` / `mps`    |

### Dashboard

| Variable                   | Default          | Notes                                         |
|----------------------------|------------------|-----------------------------------------------|
| `MEMEXA_DASHBOARD_PORT`     | `8765`           |                                               |
| `MEMEXA_DASHBOARD_HOSTS`    | unset            | JSON array of remote hosts to probe (see schema in `memexa/dashboard/sys_monitor/server.py` docstring). Unset → localhost-only dashboard. |
| `MEMEXA_PG_SNAPSHOT_DIR`    | unset            | Path on `MEMEXA_PG_SNAPSHOT_HOST` where `v5_*.sql.gz` snapshots live |
| `MEMEXA_PG_SNAPSHOT_HOST`   | unset            | ssh target for the snapshot poll              |

### Cron / orchestrator

| Variable                  | Default | Notes                                                       |
|---------------------------|---------|-------------------------------------------------------------|
| `MEMEXA_CRON_TIMEOUT_SEC`  | `14400` | Per-driver subprocess wall-clock; older default was 1 800.  |
| `MEMEXA_DEBUG`             | unset   | Set to any truthy value to print full tracebacks on CLI fail |

## YAML files

### `~/.memexa/identity.yaml`

```yaml
primary_email: alice@example.com
qq_id: ""                # leave empty if you don't use QQ
display_name: "Alice"
timezone: "Asia/Shanghai"
roles:
  - student
  - researcher
imap:
  host: imap.example.com
  port: 993
  user: alice@example.com
  password_env: MEMEXA_IMAP_PASSWORD  # never inline secrets in YAML
```

### `~/.memexa/aliases.yaml`

```yaml
self_aliases:
  - Alice
  - alice
  - 小爱
self_roles:
  - student
timezone: "Asia/Shanghai"
```

### `~/.memexa/config.yaml` (optional)

```yaml
workspace_root: /home/alice/memexa-data
```

## Inspection

```bash
memexa config         # prints every resolved variable + file presence
memexa doctor         # round-trips backend + LLM provider + identity
```

If `memexa doctor` reports `[fail] LLM/gate ... probe error`, fix the
LLM provider env vars before running anything else — the cron jobs will
silently fail at Stage A otherwise.

## What is *not* configurable

These are intentional hard-codes:

- The V2 envelope schema (`MEMORYCARD_V2_HEADER_BEGIN`).
- The Hindsight bank name conventions (`memory_full_v5` vs
  `memory_full_v3` legacy).
- The 6-hour cron cadence (orchestrator + per-driver). Run more often →
  PG load; run less often → extraction backlog.
- The 5-phase query workflow signal definitions.

If you need to change any of the above, fork.
