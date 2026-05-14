# 2. 双 LLM gate-extract 比单 LLM extract 强

[English](02_two_llm_gate_extract.md) · **中文**

> 直觉设计是 *"一个 LLM, 一个抽取 prompt, 一份 JSON 输出"*。我们试了。
> 慢, 贵, 而且在容易的 case 上还吐坏 JSON。拆成 gatekeeper + extractor
> 砍 ~40% 成本, 改善 schema 合法性, 把故障局部化。

## 直觉 (错的) 设计

```
batch ──▶ extractor LLM ──▶ V2 envelope JSON ──▶ POST 到 memory bank
```

一个模型, 一次调用, 一份响应。简单。

问题: 本代码库的一个 *batch* 是同一对话窗口里 8-15 条消息。约 40-60%
的 batch 是噪声 — 群聊问候, 单 emoji 回复, 外卖确认等。强迫 extractor
看所有这些花费:

- **GPU 时间**: extractor 是 30B+ 参数, 单 A6000 上 ~4 秒/batch。一半时间
  浪费在产 0 卡的噪声 batch 上
- **API 预算**: 商业费率 ($1.5/M output tokens 给 Qwen3 级模型), 浪费的
  一半是真钱
- **Schema 损坏**: 强迫 extractor 给没可抽取事实的 batch 吐 V2 envelope
  JSON 会产空数组, 偶尔产坏 JSON, 因为模型"试图填点啥"

## 修法: 两阶段级联

```
batch ──▶ gatekeeper LLM ──▶ verdict ∈ {HIGH, MEDIUM, LOW}
                                            │
                          LOW ──▶ 跳过      │
                                            │
                          HIGH/MEDIUM       ▼
                                        extractor LLM ──▶ V2 envelope JSON
```

Gatekeeper 是便宜模型 (本地硬件上的 Qwen-14B 4-bit, 或 OpenAI-compatible
API 上的 `qwen3.6-chat`)。它唯一任务: 读 batch, 输出 3 个标签之一。

verdict:

- **LOW**  — 无可抽取事实; 跳
- **MEDIUM** — 有些事实, 可能信号弱; 反正抽
- **HIGH** — 明显含承诺 / 决策 / 命名实体; 抽

Extractor 是重模型 (Gemma-31B 4-bit 或 `deepseek-v4-flash` 级)。它只看
HIGH + MEDIUM batch。

## 实测影响

8 个月个人聊天 exhaust (~16k batch 跨 6 source):

| 指标                              | 单 LLM   | 双 LLM        |
|-------------------------------------|------------|----------------|
| LLM 总调用数                          | 16,091     | 16,091 (gate) + 9,315 (extract) |
| 平均 GPU 秒/batch                   | 4.2        | 0.4 + 2.1 (含 LOW 跳, 均 1.5) |
| Schema 不合法抽取输出                | 1.8 %      | 0.4 %          |
| 全 corpus 端到端 wall time            | 18 h       | 9 h            |
| 估算商业 API 成本 (USD)              | $24        | $14            |

## 为啥它产更好 JSON

Extractor 单位时间看到更多 *事实密集* 的 batch, context window 永远不
被闲聊填一半。要吐结构化 JSON 的模型, 在输入密集就是要抽取的东西时,
吐得最稳。

## 故障隔离

一个 stage 失败 — gatekeeper 超时, 或 extractor 吐坏 JSON — 只丢该 stage
输出。单 LLM 设计里, 模型在 batch 中途崩, 全丢。

双 LLM 设计里 gatekeeper verdict 在调 extractor 前先落盘。如果抽取失败,
还能知道哪些 batch 被分类为 HIGH/MEDIUM, 不用重跑 gate 直接重抽。

## 这招什么时候不划算

收益来自输入的 LOW 率。语料 >90% HIGH 密度时 gate 大半白干, 单 LLM
设计 + 宽容输出 schema 反而更好。

3 个两阶段划算的 source:

1. 群聊 (LOW 率 ~50%)
2. 浏览器历史 (LOW 率 ~70% — 大多数 session 不是"事实")
3. 录音 (LOW 率 ~60% — 静默 + 被动听)

2 个不划算的:

1. 邮件 (LOW 率 ~15% — 即使垃圾邮件结构也够抽)
2. 自写文档 (LOW 率 ~5%)

## 相关

- `memexa/extraction/pass1_prompt.py` — gatekeeper prompt 模板
- `memexa/extraction/pass2_prompt.py` — extractor prompt + V2 envelope spec
- `memexa/extraction/l0_worker_serial.py` — 单笔记本带 2 次模型 swap 的串行
  worker
- `memexa/extraction/l0_worker_api.py` — 两个 OpenAI-compatible endpoint
  (gate + extract 分开模型) 的并行 worker
