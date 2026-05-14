# 配置参考

[English](configuration.md) · **中文**

`memexa` 运行时读的每个旋钮。这里没列的要么是实现细节, 要么是即将废弃
的 env var。

## 设置层次

| 层               | 路径                                       | 格式  | 重载                |
|------------------|--------------------------------------------|--------|---------------------|
| 环境变量          | shell / `.env`                             | env    | per-process         |
| 用户配置          | `~/.memexa/identity.yaml`                   | YAML   | per-process (LRU-cached) |
| 用户配置          | `~/.memexa/aliases.yaml`                    | YAML   | per-process (LRU-cached) |
| 用户配置          | `~/.memexa/config.yaml`                     | YAML   | per-process         |
| 项目配置          | `config/aliases.example.yaml`              | YAML   | 模板, 仅复制用       |
| 项目配置          | `config/identity.example.yaml`             | YAML   | 模板, 仅复制用       |

解析优先级 **env 优先**, 然后 **`~/.memexa/*.yaml`**, 然后 **workspace 默认**。
一个 key 在一层里设, 不要在多层重复。

## 核心环境变量

### Workspace + 路径

| 变量                          | 默认                          | 用在                              |
|-----------------------------|-----------------------------|----------------------------------|
| `MEMEXA_WORKSPACE_ROOT`      | `~/.claude/projects/`       | `_path_resolver` — 任何读 workspace 状态的模块 |
| `MEMEXA_CONFIG_DIR`          | `~/.memexa/`                 | `memexa init` 目标                |
| `MEMEXA_QUERY_LOG_PATH`      | `<workspace>/data/memory_query_log.jsonl` | `memory_query` 调用日志 |

### 记忆后端 (Hindsight)

| 变量                                | 默认                       | 备注                                          |
|-----------------------------------|--------------------------|---------------------------------------------|
| `MEMEXA_HINDSIGHT_URL`             | `http://127.0.0.1:8888`  | 主 daemon URL                              |
| `MEMEXA_HINDSIGHT_FALLBACK_URL`    | unset                    | 设了的话, 主连接 timeout 时重试            |
| `MEMEXA_HINDSIGHT_BANK`            | `memory_full_v5`         | retain / recall / reflect 的目标 bank      |
| `MEMEXA_HINDSIGHT_TIMEOUT`         | `180`                    | 每个 HTTP 调用秒数 (高负载下 recall 慢)    |
| `MEMEXA_HINDSIGHT_TRACE_LOG`       | unset                    | trace JSONL 文件路径; unset = 不 trace      |

### PostgreSQL (bid cache)

`pg_bid_cache` 模块需要读 Hindsight Postgres 来权威判断哪些 batch 已经在图里。

| 变量                     | 默认         | 备注                                                  |
|------------------------|-------------|----------------------------------------------------|
| `MEMEXA_PG_DSN`         | unset       | 完整 DSN (`postgres://user@host:5433/hindsight`)。设了就覆盖 host/port/user/db。|
| `MEMEXA_PG_HOST`        | `127.0.0.1` |                                                    |
| `MEMEXA_PG_PORT`        | `5433`      |                                                    |
| `MEMEXA_PG_USER`        | `$USER`     | 回退到 `getpass.getuser()`                          |
| `MEMEXA_PG_DB`          | `hindsight` |                                                    |
| `MEMEXA_PG_BID_TTL_SEC` | `3600`      | 磁盘缓存 TTL                                        |
| `MEMEXA_PG_SSH_TARGET`  | unset       | 设了就走 `ssh <target> psql ...` 而不是直接 psycopg2。仅当 PG 端口被防火墙挡住时用。 |
| `MEMEXA_PG_PSQL_BIN`    | `psql`      | 仅在 ssh 模式下用                                    |

### LLM provider (OpenAI-compatible)

Stage A (gatekeeper) 和 Stage B (extractor) 用。

| 变量                                | 必填?     | 例                                       |
|-----------------------------------|-----------|----------------------------------------|
| `MEMEXA_REMOTE_LLM_BASE_URL`       | 是        | `http://127.0.0.1:8000`                |
| `MEMEXA_REMOTE_LLM_API_KEY`        | provider 决定 | `sk-…`                                |
| `MEMEXA_REMOTE_LLM_GATE_MODEL`     | 是        | `qwen2.5-14b-instruct` / `gpt-4o-mini` |
| `MEMEXA_REMOTE_LLM_EXTRACT_MODEL`  | 是        | `gemma-2-27b-it` / `deepseek-chat`     |
| `MEMEXA_EXTRACT_CONCURRENT`        | `5`       | 每 driver extract endpoint 并发。大多数 vLLM 部署甜点是 5。 |

### Audio (可选)

只有摄入语音备忘才相关。Audio pipeline 可本地跑 (Apple Silicon MLX
whisper) 或远程跑 (Linux/CUDA faster-whisper)。

| 变量                     | 默认                              | 备注                                |
|------------------------|----------------------------------|----------------------------------|
| `HF_HUB_OFFLINE`       | unset                            | 模型下完后设 `1`, 跳过 Hub 探测     |
| `MEMEXA_AUDIO_INBOX`    | `<workspace>/data/audio/inbox`   | LaunchAgent / cron 取新文件的位置  |
| `MEMEXA_AUDIO_DEVICE`   | `cpu`                            | `cpu` / `cuda` / `mps` 之一        |

### Dashboard

| 变量                         | 默认             | 备注                                          |
|----------------------------|------------------|-----------------------------------------------|
| `MEMEXA_DASHBOARD_PORT`     | `8765`           |                                               |
| `MEMEXA_DASHBOARD_HOSTS`    | unset            | JSON 数组列出要探测的远程 host (schema 见 `memexa/dashboard/sys_monitor/server.py` docstring)。unset → 只看 localhost。 |
| `MEMEXA_PG_SNAPSHOT_DIR`    | unset            | `MEMEXA_PG_SNAPSHOT_HOST` 上 `v5_*.sql.gz` snapshot 路径 |
| `MEMEXA_PG_SNAPSHOT_HOST`   | unset            | snapshot 轮询的 ssh target                   |

### Cron / orchestrator

| 变量                       | 默认     | 备注                                                          |
|---------------------------|---------|-------------------------------------------------------------|
| `MEMEXA_CRON_TIMEOUT_SEC`  | `14400` | 每 driver 子进程 wall-clock; 老默认是 1 800                  |
| `MEMEXA_DEBUG`             | unset   | 设任意 truthy 值, CLI 失败时打印完整 traceback              |

## YAML 文件

### `~/.memexa/identity.yaml`

```yaml
primary_email: alice@example.com
qq_id: ""                # 不用 QQ 就留空
display_name: "Alice"
timezone: "Asia/Shanghai"
roles:
  - student
  - researcher
imap:
  host: imap.example.com
  port: 993
  user: alice@example.com
  password_env: MEMEXA_IMAP_PASSWORD  # 永远不要在 YAML 内联密钥
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

### `~/.memexa/config.yaml` (可选)

```yaml
workspace_root: /home/alice/memexa-data
```

## 检查

```bash
memexa config         # 打印所有解析后的变量 + 文件是否存在
memexa doctor         # 端到端检 backend + LLM provider + identity
```

`memexa doctor` 报 `[fail] LLM/gate ... probe error` 时, 先修 LLM provider
的 env var 再跑别的 — 否则 cron job 会在 Stage A 静默失败。

## *不*可配置的

这些是故意硬编码的:

- V2 envelope schema (`MEMORYCARD_V2_HEADER_BEGIN`)
- Hindsight bank 命名约定 (`memory_full_v5` vs `memory_full_v3` legacy)
- 6 小时 cron 节奏 (orchestrator + per-driver)。跑更勤 → PG 压力大;
  跑更少 → 抽取 backlog 堆
- 5 阶段查询信号定义

要改任何上面这些, fork。
