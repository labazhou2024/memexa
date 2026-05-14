# Configuration reference

**English** · [中文](configuration.zh.md)

Every knob `memex` reads at runtime. Anything not listed here is
either an implementation detail or a deprecated env var slated for
removal.

## Where settings live

| Layer            | Path                                      | Format | Reload    |
|------------------|-------------------------------------------|--------|-----------|
| Environment      | shell / `.env`                            | env    | per-process |
| User config      | `~/.memex/identity.yaml`                  | YAML   | per-process (LRU-cached) |
| User config      | `~/.memex/aliases.yaml`                   | YAML   | per-process (LRU-cached) |
| User config      | `~/.memex/config.yaml`                    | YAML   | per-process |
| Project config   | `config/aliases.example.yaml`             | YAML   | template only |
| Project config   | `config/identity.example.yaml`            | YAML   | template only |

Resolution order is **env wins** over **`~/.memex/*.yaml`** over
**workspace defaults**. Set a key in one layer; do not duplicate it.

## Core environment variables

### Workspace + paths

| Variable                    | Default                     | Used by                          |
|-----------------------------|-----------------------------|----------------------------------|
| `MEMEX_WORKSPACE_ROOT`      | `~/.claude/projects/`       | `_path_resolver` — every module that reads workspace state |
| `MEMEX_CONFIG_DIR`          | `~/.memex/`                 | `memex init` target              |
| `MEMEX_QUERY_LOG_PATH`      | `<workspace>/data/memory_query_log.jsonl` | `memory_query` invocation log |

### Memory backend (Hindsight)

| Variable                          | Default                  | Notes                                       |
|-----------------------------------|--------------------------|---------------------------------------------|
| `MEMEX_HINDSIGHT_URL`             | `http://127.0.0.1:8888`  | Primary daemon URL                          |
| `MEMEX_HINDSIGHT_FALLBACK_URL`    | unset                    | If set, retried on primary connect/timeout  |
| `MEMEX_HINDSIGHT_BANK`            | `memory_full_v5`         | Target bank for retain / recall / reflect   |
| `MEMEX_HINDSIGHT_TIMEOUT`         | `180`                    | Seconds per HTTP call (recall may be slow under load) |
| `MEMEX_HINDSIGHT_TRACE_LOG`       | unset                    | Path to a JSONL trace file; unset = no trace |

### PostgreSQL (bid cache)

The `pg_bid_cache` module needs read access to the Hindsight Postgres
to authoritatively decide which batches are already in the graph.

| Variable               | Default     | Notes                                              |
|------------------------|-------------|----------------------------------------------------|
| `MEMEX_PG_DSN`         | unset       | Full DSN (`postgres://user@host:5433/hindsight`). When set, overrides host/port/user/db. |
| `MEMEX_PG_HOST`        | `127.0.0.1` |                                                    |
| `MEMEX_PG_PORT`        | `5433`      |                                                    |
| `MEMEX_PG_USER`        | `$USER`     | Falls back to `getpass.getuser()`                  |
| `MEMEX_PG_DB`          | `hindsight` |                                                    |
| `MEMEX_PG_BID_TTL_SEC` | `3600`      | On-disk cache TTL                                  |
| `MEMEX_PG_SSH_TARGET`  | unset       | If set, uses `ssh <target> psql ...` instead of direct psycopg2. Use only when PG port is firewalled. |
| `MEMEX_PG_PSQL_BIN`    | `psql`      | Only consulted in ssh mode                         |

### LLM provider (OpenAI-compatible)

Used by Stage A (gatekeeper) and Stage B (extractor).

| Variable                          | Required? | Example                                |
|-----------------------------------|-----------|----------------------------------------|
| `MEMEX_REMOTE_LLM_BASE_URL`       | yes       | `http://127.0.0.1:8000`                |
| `MEMEX_REMOTE_LLM_API_KEY`        | provider-dependent | `sk-…`                       |
| `MEMEX_REMOTE_LLM_GATE_MODEL`     | yes       | `qwen2.5-14b-instruct` / `gpt-4o-mini` |
| `MEMEX_REMOTE_LLM_EXTRACT_MODEL`  | yes       | `gemma-2-27b-it` / `deepseek-chat`     |
| `MEMEX_USTC_CONCURRENT`           | `5`       | Per-driver concurrency for the extract endpoint. Sweet spot is 5 for most vLLM deploys. |

### Audio (optional)

Only relevant if you ingest voice memos. The audio pipeline can run
locally (MLX whisper on Apple Silicon) or remotely (faster-whisper on
Linux/CUDA).

| Variable               | Default                          | Notes                            |
|------------------------|----------------------------------|----------------------------------|
| `HF_HUB_OFFLINE`       | unset                            | Set to `1` after first model download to skip Hub probes |
| `MEMEX_AUDIO_INBOX`    | `<workspace>/data/audio/inbox`   | Where the LaunchAgent / cron picks up new files |
| `MEMEX_AUDIO_DEVICE`   | `cpu`                            | One of `cpu` / `cuda` / `mps`    |

### Dashboard

| Variable                   | Default          | Notes                                         |
|----------------------------|------------------|-----------------------------------------------|
| `MEMEX_DASHBOARD_PORT`     | `8765`           |                                               |
| `MEMEX_DASHBOARD_HOSTS`    | unset            | JSON array of remote hosts to probe (see schema in `src/dashboard/sys_monitor/server.py` docstring). Unset → localhost-only dashboard. |
| `MEMEX_PG_SNAPSHOT_DIR`    | unset            | Path on `MEMEX_PG_SNAPSHOT_HOST` where `v5_*.sql.gz` snapshots live |
| `MEMEX_PG_SNAPSHOT_HOST`   | unset            | ssh target for the snapshot poll              |

### Cron / orchestrator

| Variable                  | Default | Notes                                                       |
|---------------------------|---------|-------------------------------------------------------------|
| `MEMEX_CRON_TIMEOUT_SEC`  | `14400` | Per-driver subprocess wall-clock; older default was 1 800.  |
| `MEMEX_DEBUG`             | unset   | Set to any truthy value to print full tracebacks on CLI fail |

## YAML files

### `~/.memex/identity.yaml`

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
  password_env: MEMEX_IMAP_PASSWORD  # never inline secrets in YAML
```

### `~/.memex/aliases.yaml`

```yaml
self_aliases:
  - Alice
  - alice
  - 小爱
self_roles:
  - student
timezone: "Asia/Shanghai"
```

### `~/.memex/config.yaml` (optional)

```yaml
workspace_root: /home/alice/memex-data
```

## Inspection

```bash
memex config         # prints every resolved variable + file presence
memex doctor         # round-trips backend + LLM provider + identity
```

If `memex doctor` reports `[fail] LLM/gate ... probe error`, fix the
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
