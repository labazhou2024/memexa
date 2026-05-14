# 常见问题 FAQ

[English](faq.md) · **中文**

从 maintainer 答得最多的 issue 里挑出来的。如果你的问题不在这里, 先搜
Discussions 再提 issue。

## 装机

### Q: 没有 GPU 能跑吗?

**查询路径**能 (BGE-M3 在 CPU 上跑, 慢但对)。**摄入路径**对中文质量
不行 — extractor LLM (Gemma-31B / Qwen-32B 级) 需要 GPU 或 hosted endpoint。
小模型 (CPU 上的 Qwen-7B 级) 做 PoC 可以但每 batch 抽出来的卡数少
~30%。

### Q: 能用 OpenAI / Claude / Gemini 做 extractor 吗?

能。设 `MEMEX_REMOTE_LLM_BASE_URL` 到任何 OpenAI-compatible endpoint。
确认能跑: vLLM, Ollama, LiteLLM proxy, DeepSeek API, OpenRouter, OneAPI。
Extractor prompt 在
[`src/extraction/pass2_prompt.py`](../src/extraction/pass2_prompt.py),
provider 无关。

### Q: 我的 Hindsight 容器起不来

3 个常见疑犯:

1. 8888 端口被占。`lsof -i :8888` 确认。
2. Postgres init 脚本写不了 `data/pg/`。看 volume mount。
3. `pgvector` extension 缺。自带 image 已包含; 你自带 Postgres 就在目标
   DB 跑 `CREATE EXTENSION IF NOT EXISTS vector`。

### Q: 占多大磁盘?

- ~30 GB 1 年密集摄入 (6 source, ~50 batches/天)
- ~5 GB BGE-M3 模型 + sidecar 权重
- ~2 GB Postgres index
- ~10 GB 原始聊天归档 (压缩 JSON)

## 摄入

### Q: 微信 export 失败, builder 说 "no messages found"

Builder 期望 WeChatMsg 风格 JSON。其他 exporter 用不同 schema。如果你用了
别的工具, 写一个 50 行的转换器把它输出转成规范 envelope schema
(`src/ingestion/v5_wechat_batch_builder.py` 展示目标格式)。

### Q: 为什么 Stage B 在 HIGH-verdict batch 上返 0 卡?

最常见: extractor 模型太小 / 量化太狠。Gemma-31B 4-bit 和 Qwen-32B 4-bit
~99% 时间返合格 JSON; Qwen-7B 4-bit ~30% 时间返坏 JSON。换 extractor
模型, 或接受这个 false-negative 率。

第二常见: gatekeeper 判 HIGH 但 batch 确实没可抽取事实 (闲聊 + 表情包)。
HIGH 是 recall 阈值, 不是 precision 阈值。

### Q: Driver 每次 cron 都重抽同样的 batch

撞上 PG-no-marker 漂移。跑:

```bash
python -m src.core.pg_bid_cache <source> --force-refresh
```

这从 Postgres 真值重建本地 "已在 PG" 缓存, 下次 driver run 会跳过这些
batch。

### Q: 怎么加新 source?

1. 写 builder 把原始 export 转成规范 envelope schema。用
   `src/ingestion/v5_email_batch_builder.py` 当模板。
2. 写 driver 把 builder 接入 6 小时循环。用
   `src/drivers/backfill_v5_email_driver.py` 当模板。
3. 在 `data/cron_manifest.yaml` 注册 driver。
4. 跑 `python -m src.core.cron_orchestrator validate-manifest`。
5. 提 PR。

## 查询

### Q: 查人时 `topic` 为啥返购物相关卡?

By design — `topic` 扇出 11 变体含 "X 价格", "X 商家", "X 退货", 因为
最常见的 topic 题是关于购物的。查人用 `arc`。见
[hard rule](usage_guide.zh.md#硬规则-查人永远不用-topic)。

### Q: 我的查询返 0 卡, 数据其实在里面

按诊断阶梯走:

```bash
# 1. 放宽 recall
python -m src.core.memory_query quick "X" --salience 0.0 --max-k 50
# 2. 试 topic 扇出
python -m src.core.memory_query topic "X" --salience 0.0 --max-cards 100
# 3. 检查 backend health
memex doctor
# 4. 检查 bank 有卡
curl -s http://127.0.0.1:8888/v1/default/banks/memory_full_v5/stats | jq .
```

step 4 显示 `nodes: 0` 说明摄入路径坏了, 不是查询路径。看 cron 日志,
跑 `make demo-ingest` 确认干净基线能工作。

### Q: `reflect` 返空 / 胡说

`reflect` 需要 daemon 侧 LLM provider 配好。如果你没配 (env 是空),
`reflect` 退化成 stub 返垃圾。配 daemon LLM 或换用 `summary` (它走
`MEMEX_REMOTE_LLM_BASE_URL` 在 client 侧综合)。

### Q: 状态题 ("我退掉 Y 课程了吗") 怎么问?

用 5 阶段工作流, 见 [5_phase_query.zh.md](5_phase_query.zh.md)。
单次语义召回三角不出状态 — 需要综合 5 个正交信号。

## 隐私

### Q: 这玩意会把我的聊天记录发到哪儿吗?

只发到你配置的 LLM provider。Hindsight daemon 和 Postgres 都本地。
Dashboard server 本地。Cron orchestrator 只 shell out 到本地 Python。

LLM provider 一次看一个 batch。如果你设
`MEMEX_REMOTE_LLM_BASE_URL=http://127.0.0.1:8000` 并本地跑 vLLM / Ollama,
任何数据都不出机器。

### Q: 怎么从图里删某个特定人?

目前没有 built-in "right to be forgotten" CLI。手动 recipe:

```sql
DELETE FROM memory_units
WHERE bank_id = 'memory_full_v5'
  AND metadata->>'persons' LIKE '%<canonical-id>%';
DELETE FROM memory_units
WHERE bank_id = 'memory_full_v5'
  AND text LIKE '%<surface-form>%';
```

然后跑 `python -m src.core.pg_bid_cache all --force-refresh` 让 driver
重建 pending 集合。想要自动化提 issue。

### Q: 能在 Docker 容器里跑吗?

能 — 见 [deployment/docker-compose.zh.md](deployment/docker-compose.zh.md)。
CLI / ingester / dashboard 在容器里都能跑; 唯一 host 耦合的是 audio
recorder watch 脚本 (它轮询 USB mount), 你需要在 host 上跑。

## 项目

### Q: 为啥中文优先?

现有的中文 exhaust (微信 / QQ) 没有好的开源 memory 工具。Mem.ai / Reflect
这种存 markdown 假设英文 prose。Mem0 / Letta / Zep 是 SDK 没中文原生
extractor。Maintainer 因为没有合用的工具才开干。

Pipeline 抽英文也行 — Stage B 处理混合语言 batch 不需特殊对待。只是
没有英文专属路径的文档因为没人要。

### Q: 会有 hosted 版本吗?

不会。个人记忆这种事 "self-hosted" 就是产品本身, 不是某个 tier。

### Q: v0.1.0 release 在哪?

Win + macOS + Linux 上 fresh-clone smoke test 通过后发布。见
[ROADMAP.md](../ROADMAP.md)。
