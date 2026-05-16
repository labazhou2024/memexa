# 成本估算

[English](cost.md) · **中文**

最后更新: 2026-05-16. 价格会变, 用绝对数字前请核实。provider 之间的相
对比例比绝对值稳定。

memexa 核心抽取流程每个 batch 调 chat-completions 端点 2 次: 1 次
gatekeeper 筛 HIGH / MEDIUM / LOW 优先级, 1 次 extractor 真抽 V2
envelope。Tier 0 (`memexa demo`) 不调任何 LLM。Tier 1 和 Tier 2 调。

## Provider 价格表

| Provider | 模型 | 输入 ¥/M | 输出 ¥/M | 备注 |
|---|---|---|---|---|
| **DeepSeek** | V4 Flash | 1.0 | 2.0 | Cache 命中: 0.2 / 2 |
| **DeepSeek** | V4 Pro | 3.0 | 6.0 | 75% 优惠至 2026-05-31, 之后 12 / 24 |
| **Qwen** | qwen-plus | ~4 | ~12 | 约 DeepSeek Flash 4× |
| **Moonshot** | moonshot-v1-32k | ~12 | ~12 | 中文 premium 选项 |
| **OpenAI** | gpt-4o | ~18 | ~72 | 约 DeepSeek Flash 10-15× |
| **OpenAI** | gpt-4o-mini | ~1 | ~4 | 便宜但中文质量降 |
| **Anthropic** | Claude 4.7 Sonnet | ~22 | ~110 | 约 DeepSeek Flash 12-18× |
| **自托管 (Ollama / vLLM)** | 任意 | ~0 | ~0 | 硬件成本另算 |

来源: [DeepSeek API pricing](https://api-docs.deepseek.com/quick_start/pricing/),
各 provider 2026-05-16 价格页, RMB 汇率取自 `chat-deep.ai/pricing`。

## memexa 各 workload 典型调用量

| Tier | 每跑 batches 数 | Token (输入 / 输出) | LLM 调用次数 |
|---|---|---|---|
| Tier 0 (`memexa demo`) | 0 | 0 / 0 | 0 |
| Tier 1 (≈ 100 条自己消息) | ~20 | 2k / 1k | 40 (20 gate + 20 extract) |
| Tier 2 每日轻量 (≈ 300 条/天) | ~60 | 2k / 1k | 120 / 天 |
| Tier 2 每日中等 (≈ 1000 条/天) | ~200 | 2k / 1k | 400 / 天 |
| Tier 2 每日重度 (≈ 5000 条/天) | ~1000 | 2k / 1k | 2000 / 天 |

一个 batch ≈ 5-10 条聊天消息 + manifest slice。输入侧主要是 manifest 和
system prompt, 不是消息本身。输出侧是单个 V2 envelope JSON。

## 推荐模型组合的 workload 成本

中文 workload 推荐组合: **DeepSeek V4 Flash 当 gatekeeper** +
**DeepSeek V4 Pro 当 extractor**。便宜模型放高频 gate-keeping 路径,
好模型只跑 gate 标过 worth 抽的卡。

| Workload | 月调用 | Gate ¥ | Extract ¥ | 月总 ¥ | 月总 $ |
|---|---|---|---|---|---|
| Tier 1 单次 (≈ 100 msg) | 40 | 0.06 | 0.24 | **0.30** | **≈ $0.04** |
| Tier 2 轻量 (≈ 9000 msg/月) | 3 600 | 5.4 | 21.6 | **27** | **≈ $3.8** |
| Tier 2 中等 (≈ 30000 msg/月) | 12 000 | 18 | 72 | **90** | **≈ $12.7** |
| Tier 2 重度 (≈ 150000 msg/月) | 60 000 | 90 | 360 | **450** | **≈ $63** |

把 Pro extractor 换成 Flash, extract 列除 3。把 gatekeeper 换成 Pro
(质量过付费), gate 列乘 3。把 extractor 换 GPT-4o, extract 列乘 12。

## 其他模型组合

| 组合 | Tier 2 中等 ¥/月 | Tier 2 中等 $/月 | 备注 |
|---|---|---|---|
| **DeepSeek Flash + DeepSeek Flash** | 36 | $5 | 全便宜 baseline |
| **DeepSeek Flash + DeepSeek Pro** | 90 | $13 | 推荐 |
| **DeepSeek Flash + Qwen plus** | ~200 | $28 | 账号有 Qwen 额度时 |
| **DeepSeek Flash + GPT-4o-mini** | 60 | $8 | 英文为主 workload |
| **DeepSeek Flash + GPT-4o** | 870 | $123 | 不差钱 |
| **自托管 Qwen 14B via vLLM** | 0 (+ 硬件) | 0 (+ 硬件) | 有 GPU 的话 |

## 成本监控

摄入后, dashboard 的 **API usage** panel (`http://127.0.0.1:8765`)
每日 print 调用次数 + 累积成本估算, 按 `gate` / `extract` 拆。
第 1 周看一遍, 验证实际月成本符合上表。

硬上限: `.env` 设 `MEMEXA_API_BUDGET_DAILY=10` (RMB)。driver 尊重该
阈值, 当日累积成本超出后停调 LLM; cards 排队等次日 00:00 重置。

## 完全免费路径

不想付 API 费用, 2 个选项:

1. **只跑 Tier 0 demo**。`memexa demo` 永远免费, 跑合成数据集, 诚实
   展示查询流程。
2. **自托管 LLM**。用 vLLM 或 Ollama 在自己硬件上跑 Qwen 14B (或任意
   OpenAI 兼容模型), `MEMEXA_REMOTE_LLM_BASE_URL` 指 `http://127.0.0.1:
   11434/v1`。没有月费 API 账单; 成本转到 GPU 电费。

第 1 个适合新用户先看一眼; 第 2 个适合想每天摄入几万条 + 不想长期付
API 费的用户。
