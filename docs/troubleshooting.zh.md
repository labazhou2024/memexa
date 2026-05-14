# 故障排查

[English](troubleshooting.md) · **中文**

出问题时, 按层排查, 别瞎猜。下面每节是一种常见故障模式 + 验证修好的实际
退出标准。

## Layer 0 — 装机是否正常?

```bash
memex version            # 打印 memex + python + 依赖版本
memex config             # 打印所有解析后的 env + 文件路径
memex doctor             # 端到端检 backend + LLM + identity
```

`memex doctor` 全 `[ok]` 说明装机没问题, 故障在你的数据路径上。直接跳
"Layer 2"。

## Layer 1 — 后端起不来

### 症状: `curl http://127.0.0.1:8888/healthz` 拒绝连接

```bash
docker compose -f docker-compose.example.yml logs hindsight | tail -30
```

找这些行:

| 日志行                                                | 原因                | 修法                                              |
|----------------------------------------------------|----------------------|--------------------------------------------------|
| `OperationalError: could not connect to server`    | Postgres 没就绪      | `docker compose ps`; 等 `pg` healthcheck 通过   |
| `psycopg2.errors.UndefinedFile: vector.so`         | 缺 pgvector          | 用自带 image; 或手动 `CREATE EXTENSION vector;` |
| `ImportError: No module named hindsight`           | image tag 过期       | `docker compose pull`                            |
| `RuntimeError: tiktoken failed to download`        | 无网 + 无 cache      | 预下: `python -c "import tiktoken;tiktoken.encoding_for_model('gpt-4')"` |
| `[Errno 98] Address already in use`                | 8888 端口被占用       | `lsof -i :8888` 然后 kill 或改端口映射            |

退出标准: `curl -sf http://127.0.0.1:8888/healthz` 返回 200。

### 症状: 后端起来但 `bank does not exist`

Bank 是首次 retain 时建的, 不是容器启动时。跑 demo ingest 物化它:

```bash
make demo-ingest
curl -s http://127.0.0.1:8888/v1/default/banks/memory_full_v5/stats
```

## Layer 2 — 摄入静默地不产卡

### 诊断顺序

1. Builder 写出 batch 文件了吗?
   ```bash
   ls data/l0_v5/input_batches/<YYYY-MM-DD>/ | wc -l
   ```
   0 → builder 拒绝了原始 export。加 `--verbose` 看解析决定。

2. Stage A 出 verdict 了吗?
   ```bash
   ls data/l0_v5/work/cards_v2_*/qwen.jsonl 2>/dev/null | head
   ```
   空 → Stage A LLM 在挂。检查 `memex doctor` 的 LLM gate 探测。

3. Stage B 出卡了吗?
   ```bash
   ls data/l0_v5/work/cards_v2_*/27b.jsonl 2>/dev/null | head
   ```
   空 → Stage B LLM 在产坏 JSON。换大模型或加 `/no_think` 指令 (Qwen3
   系列, 见
   [lessons_learned/05_qwen3_no_think.md](lessons_learned/05_qwen3_no_think.md))。

4. Stage D POST 成功了吗?
   ```bash
   ls data/l0_v5/work/posted_v5_*/*.posted 2>/dev/null | wc -l
   ls data/l0_v5/work/posted_v5_*/*.dead   2>/dev/null | wc -l
   ```
   大量 `.dead` 少 `.posted` → 跑恢复工具:
   `python -m tools.recover_phaseB_dead_letter`。

### 症状: cron orchestrator 退了但 driver `n_runs=0`

最可能 driver 在子进程里崩了。日志:

```bash
ls -la data/maintenance_logs/ | tail
tail -100 data/maintenance_logs/memex_core_cron_orchestrator_*.log
```

找 per-driver 的 `dispatch '<id>' ...` 行。下一行告诉你 exit code +
duration。duration > 240 秒通常意味着 LLM provider 挂了; 见下面 "LLM
provider timeout"。

### 症状: driver 每次 cron 都重抽同样的 batch

Marker / PG 真值漂移。从 PG 强制重建 cache:

```bash
python -m src.core.pg_bid_cache all --force-refresh
```

退出标准: 下次 cron run 该 source `pending=0`。

## Layer 3 — LLM provider 问题

### 症状: `memex doctor` LLM 探测返 401 / 403

API key 错或过期。`memex config` 看 masked 值; 需要的话重发。

### 症状: 探测返 200 但 cron 静默产 0 卡

provider 在响应但返坏 JSON。抓一次真调用:

```bash
MEMEX_HINDSIGHT_TRACE_LOG=/tmp/memex_trace.jsonl \
  python -m src.drivers.backfill_v5_wechat_driver --once --verbose
jq . /tmp/memex_trace.jsonl
```

常见病:

- `choices[].message.content` 空 → 模型 context window 溢出; 缩小 batch
- 仅 thinking 输出 (Qwen3 系列) → 追加 `/no_think` 指令
- JSON 被 markdown fence 包起来 → parser 已处理, 但 trace 看到这个说明
  parser threshold 可能漂移了
- 幻觉式 tool-call 帧 → extractor 模型被微调过支持函数调用, 无视 JSON-only
  system prompt。换模型。

### 症状: LLM provider 60 秒超时

默认 `httpx` timeout 对 30B+ 模型的 chat-completions 偏短。Driver 已经传
4 小时 timeout; 问题通常是 provider 自己断了连接。看 provider 自己的日志。

vLLM 具体:

- GPU OOM → 缩 `--max-model-len` 或 batch size
- 高并发 engine 死锁 → 设 `MEMEX_EXTRACT_CONCURRENT=5` (大多数 vLLM 部署
  甜点)

## Layer 4 — 查询返回无用结果

见 [usage_guide.zh.md#troubleshooting](usage_guide.zh.md) 的 Q1/Q2/Q3
诊断阶梯。

常见坑:

- 查人用了 `topic` 而不是 `arc` (见 hard rule)
- 默认 `--salience 0.3` 过滤掉所有低优先级。传 `--salience 0.0` 看完整分布
- 不小心查到了 legacy `memory_full` bank。默认是 `_v5`; 如果你 shadow 了
  `MEMEX_HINDSIGHT_BANK` 先 unset
- Windows GBK 终端把中文输出搞乱码。设 `PYTHONIOENCODING=utf-8` 或用
  Windows Terminal

## Layer 5 — Cron orchestrator 系统级问题

### 症状: GraphMaintenance schtask 失败 rc=0x00041306

Per-cycle 超时。Windows Job Object 在 orchestrator 超过 schtask
`ExecutionTimeLimit` 时 kill 它和所有 child。常见原因: audio driver 在
orchestrator 里跑且 ASR 步骤要 50 分钟。

修法: audio 从 orchestrator 移出 (manifest 里已经这么干; 如果你 re-add
要设 `skip_in_orchestrator: true`)。Audio 单独 6 小时 schtask。

### 症状: dashboard "Cron health" count 和 `Get-ScheduledTask` 对不上

`schtask_health.py` 除了看 last-result RC 还看任务年龄; dashboard 只看
RC。一个 8 小时前 RC=0 的任务在 dashboard 看是 "健康" 在 `schtask_health`
看是 "stale"。从各自视角都对 — 看你问题选用哪个。

## Layer 6 — Dashboard

### 症状: dashboard 渲染但每个面板都显示 "—"

仅 localhost 装机。设 `MEMEX_DASHBOARD_HOSTS` env 成 JSON 数组点亮
远程 host 面板。Schema 见
[`src/dashboard/sys_monitor/server.py`](../src/dashboard/sys_monitor/server.py)
docstring。

### 症状: "Graph queries" 面板跑了查询还是空

检查日志路径:

```bash
python -c "from src.core.memory_query import _QUERY_LOG_PATH; print(_QUERY_LOG_PATH)"
ls -la $(python -c "from src.core.memory_query import _QUERY_LOG_PATH; print(_QUERY_LOG_PATH)")
```

如果路径在 `_MEI*\Temp\` 下你撞上 PyInstaller frozen-path 问题 — 从源码
跑而不是 bundled exe。`src/core/_path_resolver.py` 里的 path resolver
就是为了打这个补丁, 所以提 issue 时附上实际打印的路径。

## 最后兜底

提 issue 时附上:

1. `memex version` + `memex config` + `memex doctor` 输出
2. 失败的精确命令 + 完整 traceback
3. 相关 cron 日志切片 (`data/maintenance_logs/*.log`)

Maintainer 是 best-effort triage; 清晰复现能省一轮往返。
