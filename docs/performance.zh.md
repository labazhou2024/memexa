# 性能

[English](performance.md) · **中文**

期待什么延迟 / 吞吐, 瓶颈在哪。数据来自 maintainer 的部署 (M4 Max Mac
Studio + 远程 GPU box 做抽取), 同硬件可复现。

## 端到端摄入

一次 6 小时 cron 循环, 跨 6 source ~50 个新 batch:

| Stage          | Wall time       | 瓶颈                                    |
|----------------|------------------|-------------------------------------------|
| Builders       | 10–60 s          | I/O (读原始 export)                      |
| Stage A gate   | 90–180 s         | LLM provider 延迟 × 并发                  |
| Stage B extract| 180–600 s        | LLM provider 延迟 (大模型 + thinking)     |
| Stage C arbiter| 10–30 s          | DeepSeek API 或本地 LLM                  |
| Stage D POST   | 20–60 s          | BGE-M3 embedding + PG insert             |
| **总计**       | **5–15 min**     |                                           |

6 小时调度在这速率下有 ~5.7 h slack。如果你日均 >1000 batch 会用光,
要么 (a) 调 LLM provider 提吞吐, 要么 (b) 接受 backlog 增长。

## 查询延迟

在 16k 卡 bank + 冷 BGE-M3 sidecar 上测:

| 子命令               | Wall time      | API 调用       | 备注                            |
|----------------------|----------------|-----------------|--------------------------------|
| `quick`              | 3–10 s         | 2               | Bank A + B 并集                |
| `topic`              | 100–200 s      | 22              | 11 变体 × 2 bank 扇出           |
| `arc`                | 30–90 s        | 16              | 8 意图变体 × 2 bank             |
| `timeline`           | 5–30 s         | 6               | 多变体扇出                       |
| `person`             | 5–15 s         | 2               |                                |
| `project`            | 15–40 s        | 6               | 6 source × `quick`             |
| `pending`            | <1 s           | 0               | 读 `calendar_index.json`        |
| `reflect`            | 10–60 s        | 1 + LLM         | 服务端综合                       |
| `summary`            | 10–60 s        | 1 + LLM         | 客户端综合                       |
| `cross-source`       | 15–40 s        | 6               | 6 source `quick` 并发            |

`topic` 和 `arc` 故意慢 — 用 wall-time 换召回广度。**不要**在外层加并发:
内部扇出已经饱和 daemon 的 BGE worker pool。

## 吞吐旋钮

### LLM 并发

`MEMEXA_EXTRACT_CONCURRENT` 控制单个 driver 并发发多少抽取请求。大多数
provider 甜点是 5:

| 设置 | 每分钟 batch (extractor) | 备注                                 |
|------|---------------------------|------------------------------------|
| 1    | 0.9                       | 串行, 吞吐没用满                     |
| 3    | 2.1                       | 线性 scaling                        |
| **5**| **3.4**                   | **默认 — 甜点**                     |
| 7    | 3.0                       | queue 饱和, 比 5 慢                  |
| 9    | (变化)                     | 很多 provider 在这开始返 429        |

升过 5 之前先验证你的 provider 曲线。

### BGE-M3 sidecar

Sidecar 处理 retain 和 recall 所有 embedding。Apple Silicon 上一次处理
1 个请求 (Metal MPS context 不可重入), CUDA 上并行 ~8 个。超过的在
sidecar 内 queue — daemon 不在 sidecar 间负载均衡。

如果 trace log 里 recall 延迟主要是 "BGE-M3 sidecar queued", 修法是给
daemon 更多 sidecar 副本, 不是在 client 端加并发。

### PostgreSQL

`memory_units` 表线性增长。重要 index:

- `(bank_id, metadata->>'source')` — `pg_bid_cache` 用
- `(bank_id, metadata->>'when_start')` — `timeline` 用
- `embedding ivfflat` — recall 用

每 ~50k 新卡 reindex 一次保持 ivfflat 高效:

```sql
REINDEX INDEX CONCURRENTLY memory_units_embedding_idx;
```

## 长大了用不下这套栈了

架构是为 "一个用户, 6 年个人数据" 调的, 大约 100k–500k 卡。超过这个会撞:

- Postgres ivfflat ANN 质量下降; 换 HNSW 或 pgvector 0.7+ 的新 `hnsw`
  index type
- BGE-M3 sidecar 在单机部署上成 recall 瓶颈; 分片 bank 或在负载均衡器
  后跑多 sidecar
- Cron orchestrator 的 per-driver 子进程 overhead 变明显; 考虑把
  orchestrator 重写成长跑 daemon + in-process worker pool

这些都不在 v0.x 路线图上。真撞到就开 issue — 非常欢迎补丁。

## Profiling

```bash
MEMEXA_HINDSIGHT_TRACE_LOG=/tmp/memexa_trace.jsonl \
  python -m memexa.core.memory_query topic "<query>" --max-cards 100

jq '. | select(.event=="recall_request") | {ts, duration_ms, query, n_results}' \
  /tmp/memexa_trace.jsonl
```

各 stage 拆解在 [`docs/lessons_learned/`](lessons_learned/) — 加新性能
优化前先读那些; 项目已经从 "看起来明显并行" 的几个陷阱里学到教训了。
