# 架构

[English](architecture.md) · **中文**

> 设计理由 + 模块边界 + 数据流。要快速装机指南去
> [quickstart.zh.md](quickstart.zh.md)。每个查询子命令的细节看
> [usage_guide.zh.md](usage_guide.zh.md)。

## 1. 目标

把 6 类个人中文日常 exhaust — 聊天记录、邮件、浏览活动、AI 对话、语音
备忘 — 变成一张可查询的记忆图谱, 能回答:

- 实体题 (*"X 是谁"*) — 点查
- 主题题 (*"X 的全过程"*) — 扇出语义召回
- 关系题 (*"我是怎么认识 X 的"*) — 按时间戳排序的关系弧
- 状态题 (*"我有没有退掉 Y 课程"*) — 5 阶段推理, 综合 5 个正交信号

## 2. 分层

```
                ┌────────────────────────────────────────┐
   Layer 0      │  用户私有原始 export                    │
   (数据)        │   wechat dump / qq nt_msg.db / IMAP    │
                │   / browser sqlite / claude transcripts │
                │   / wav 录音                            │
                └────────────────────────────────────────┘
                                  │
                ┌────────────────────────────────────────┐
   Layer 1      │  Per-source batch builders             │
   (摄入)        │   memexa/ingestion/v5_*_batch_builder.py  │
                │   + memexa/extraction/qq/*                 │
                │   + memexa/extraction/claude_code_to_v5*  │
                │   规整成单一                            │
                │   ``messages_envelope.json`` schema     │
                └────────────────────────────────────────┘
                                  │
                ┌────────────────────────────────────────┐
   Layer 2      │  抽取 pipeline (每 batch)               │
   (抽取)        │                                         │
                │   Stage A: gatekeeper LLM               │
                │     verdict ∈ {HIGH, MEDIUM, LOW}       │
                │   Stage B: extractor LLM                │
                │     输出 V2 envelope JSON, 含           │
                │     entities / predicates / evidence /   │
                │     time resolutions                     │
                │   Stage C: BGE-M3 cosine quorum         │
                │     + DeepSeek arbiter (有分歧时)        │
                │   Stage D: POST → memory_full_v5        │
                │     via streaming_post_v5.py             │
                └────────────────────────────────────────┘
                                  │
                ┌────────────────────────────────────────┐
   Layer 3      │  Hindsight FastAPI daemon               │
   (存储)        │   PostgreSQL + pgvector + BGE-M3        │
                │   embeddings + temporal links           │
                │   3-API 合同: retain / recall / reflect │
                └────────────────────────────────────────┘
                                  │
                ┌────────────────────────────────────────┐
   Layer 4      │  查询 CLI + 5 阶段状态推理              │
   (查询)        │   memexa/core/memory_query.py              │
                │   14 子命令 +                            │
                │   dashboard 实时进度面板                 │
                └────────────────────────────────────────┘
```

## 3. 双 LLM gate-extract

拆 *gatekeeper* 和 *extractor* 角色的原因:

- **成本** — 便宜的 chat-tier 模型 (Qwen-14B 4-bit, Qwen3.6-chat API 之类)
  过滤掉 ~40-60% 无可抽取事实的 batch。这部分扔给慢 extractor 是烧 GPU / 烧钱。
- **质量** — extractor 模型需要更高 reasoning budget。Mac 上跑 Gemma-31B
  + thinking 开; 远程 GPU 上跑 30B+ reasoning-tuned 模型。让它只关注
  HIGH/MEDIUM batch, 保 schema 合法。
- **故障隔离** — 一个 stage hang 或返坏 JSON, 另一个 stage 输出还在。

两 stage 都用 OpenAI-compatible base URL。vLLM / Ollama / OneAPI / LiteLLM
proxy / 任何商业 endpoint 提供 `/v1/chat/completions` 都行。

## 4. V2 envelope schema (规范卡片)

记忆图谱里每张卡是一行 PostgreSQL 记录, `text` 列携带一个用 sentinel token
包起来的 JSON envelope:

```text
【MEMORYCARD_V2_HEADER_BEGIN】
{
  "entities": [...],
  "predicates": [...],
  "evidence_quotes": [...],
  "time_resolutions": {...},
  "narrative": "...",
  "source": "wechat",
  "when_start": "2026-04-13T10:42:00+08:00",
  "salience": 0.71,
  "types_csv": "decision,announcement"
}
【MEMORYCARD_V2_HEADER_END】
```

Schema 规范在 `memexa/extraction/pass2_prompt.py` 是单一真值源。
`memory_full_v5` 里出现非 V2 行 **是 bug** (摄入守门在
`memexa/extraction/streaming_post_v5.py`)。

## 5. PG-aware pending 追踪

朴素的 backfill driver 用本地 `*.posted` marker 文件追踪已摄入的 batch。
会漂移:

- *Ghost markers* — marker 在但 PostgreSQL 里没对应行 (POST 失败但 marker
  提前写了)
- *PG-no-marker* — PostgreSQL 有行, 本地 marker 没写 (worker 在写 sidecar
  前就被 kill)

`memexa/core/pg_bid_cache.py` 在 PostgreSQL 真值上加 1 小时 LRU。Driver 优先
查它; marker 降级为快速路径提示, 不再是真值。见
[lessons_learned/03_pg_aware_pending.md](lessons_learned/03_pg_aware_pending.md)。

## 6. 5 阶段语义状态推理

单次语义召回回答不了 *"我有没有退掉 Y 课程"*。BGE-M3 的 top-K 会返
课程相关帖子, 但**没法三角校正**你到底还在不在上。5 阶段工作流:

1. **Seed** — 课程名上跑 `quick` + `arc`
2. **实体扩展** — 找同学 / TA / instructor 的 surface form
3. **5 个正交信号** — user-speaks / user-silence / boundary /
   peer-triangulation / private-notes
4. **推理链** — 把信号合成最可能的状态
5. **反证** — 主动搜可证伪推理的帖子

详细协议 + 模板查询在
[usage_guide.zh.md#5-phase-state-inference](usage_guide.zh.md)。

## 7. Cron orchestrator

`memexa/cron/cron_orchestrator.py` 掌管 6 小时增量循环:

1. 每 source driver `list_pending_batches()` (PG-aware)
2. 输入数据增长了就 build 新 batch
3. 每个 pending batch 跑抽取 pipeline
4. POST 结果, 成功写 `.posted` marker, 重试 budget 耗尽写 `.dead`
5. 保 cursor

Driver 在一次 orchestrator invocation 里串行跑。Audio 是例外 — 它有自己
的 scheduler (ASR 是长跑步骤)。见
[deployment/macos.md](deployment/macos.md) 和
[deployment/windows.md](deployment/windows.md)。

## 8. Dashboard

`memexa/dashboard/sys_monitor` 是 FastAPI + vanilla-JS dashboard, 2 秒 tick
轮询 live state:

- Cron health 表 (last result / runtime / scheduled-vs-actual)
- Live activity 面板 (哪个 driver 在跑, queue 深度, 上轮处理项数)
- Memory query 日志 (最近 25 次调用 含 subcmd / query / 结果数 / 延迟)
- Per-source PG 卡数
- Six-source pending backlog 表

## 9. 故障语义

- Stage A/B/C 输出 JSON 解析失败 → 标记 batch 为 `.dead`, 保留原始响应到
  `data/dead_letters/`。用 `tools/recover_phaseB_dead_letter.py` 恢复。
- Stage D 500/422 → 指数退避 (0/30/300/1800/14400 秒) 最多 5 次重试;
  最终失败写 `.dead`。
- Hindsight daemon 不可达 → 若设了 `MEMEXA_HINDSIGHT_FALLBACK_URL` 则回退;
  否则错误传给调用方。
- 子进程超时 (Windows) → Job Object 保证孙进程清理。见
  [lessons_learned/04_win_job_subprocess.md](lessons_learned/04_win_job_subprocess.md)。
