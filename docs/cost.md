# Cost estimation

**English** · [中文](cost.zh.md)

Last updated: 2026-05-16. Prices change; verify before relying on
absolute numbers. The ratios between providers are more stable than
the absolute values.

memexa's core extraction pipeline calls a chat-completions endpoint
twice per batch of conversation messages: once as a gatekeeper that
filters HIGH / MEDIUM / LOW priority, once as the actual extractor
that produces a V2 envelope. Tier 0 (`memexa demo`) does not call any
LLM. Tier 1 and Tier 2 do.

## Provider price table

| Provider | Model | Input ¥/M | Output ¥/M | Notes |
|---|---|---|---|---|
| **DeepSeek** | V4 Flash | 1.0 | 2.0 | Cache hit: 0.2 / 2 |
| **DeepSeek** | V4 Pro | 3.0 | 6.0 | 75 % off through 2026-05-31, then 12 / 24 list |
| **Qwen** | qwen-plus | ~4 | ~12 | Roughly 4× DeepSeek Flash |
| **Moonshot** | moonshot-v1-32k | ~12 | ~12 | Premium Chinese option |
| **OpenAI** | gpt-4o | ~18 | ~72 | Roughly 10–15× DeepSeek Flash |
| **OpenAI** | gpt-4o-mini | ~1 | ~4 | Cheap but quality drops on Chinese |
| **Anthropic** | Claude 4.7 Sonnet | ~22 | ~110 | Roughly 12–18× DeepSeek Flash |
| **Self-hosted (Ollama / vLLM)** | any | ~0 | ~0 | Hardware amortised separately |

Sources: [DeepSeek API pricing](https://api-docs.deepseek.com/quick_start/pricing/),
provider price pages as of 2026-05-16, RMB rates from `chat-deep.ai/pricing`.

## Typical call volume per memexa workload

| Tier | Batches per run | Tokens per batch (input / output) | LLM calls |
|---|---|---|---|
| Tier 0 (`memexa demo`) | 0 | 0 / 0 | 0 |
| Tier 1 (≈ 100 messages of your own) | ~20 | 2k / 1k | 40 (20 gate + 20 extract) |
| Tier 2 daily light (≈ 300 messages / day) | ~60 | 2k / 1k | 120 / day |
| Tier 2 daily medium (≈ 1 000 messages / day) | ~200 | 2k / 1k | 400 / day |
| Tier 2 daily heavy (≈ 5 000 messages / day) | ~1 000 | 2k / 1k | 2 000 / day |

A batch ≈ five to ten chat messages plus a manifest slice. Input
side is dominated by the manifest and system prompt, not the messages
themselves. Output side is a single V2 envelope JSON object.

## Cost per workload (recommended model combination)

The recommended combination for Chinese workloads is **DeepSeek V4
Flash as the gatekeeper** + **DeepSeek V4 Pro as the extractor**. This
puts the cheaper model on the high-volume gate-keeping path and the
better model only on the cards the gate marked as worth extracting.

| Workload | Calls/month | Gate cost ¥ | Extract cost ¥ | Total ¥/month | Total $/month |
|---|---|---|---|---|---|
| Tier 1 one-shot (≈ 100 msg) | 40 | 0.06 | 0.24 | **0.30** | **≈ $0.04** |
| Tier 2 light (≈ 9 000 msg/month) | 3 600 | 5.4 | 21.6 | **27** | **≈ $3.8** |
| Tier 2 medium (≈ 30 000 msg/month) | 12 000 | 18 | 72 | **90** | **≈ $12.7** |
| Tier 2 heavy (≈ 150 000 msg/month) | 60 000 | 90 | 360 | **450** | **≈ $63** |

If you swap the Pro extractor for V4 Flash, divide the extract column
by three. If you swap the gatekeeper for V4 Pro (over-pay-for-quality),
multiply the gate column by three. If you swap to GPT-4o on extract,
multiply the extract column by twelve.

## Other model combinations

| Combination | Tier 2 medium ¥/month | Tier 2 medium $/month | Notes |
|---|---|---|---|
| **DeepSeek Flash + DeepSeek Flash** | 36 | $5 | All-cheap baseline |
| **DeepSeek Flash + DeepSeek Pro** | 90 | $13 | Recommended |
| **DeepSeek Flash + Qwen plus** | ~200 | $28 | If your account has Qwen credits |
| **DeepSeek Flash + GPT-4o-mini** | 60 | $8 | English-mostly workloads |
| **DeepSeek Flash + GPT-4o** | 870 | $123 | If money is no object |
| **Self-hosted Qwen 14B via vLLM** | 0 (+ hardware) | 0 (+ hardware) | If you have a GPU |

## Cost monitoring

After ingestion the dashboard's **API usage** panel
(`http://127.0.0.1:8765`) reports per-day call counts and cumulative
cost estimates broken down by `gate` / `extract`. Track this for the
first week to verify the actual monthly cost matches the table above.

For a hard ceiling, set `MEMEXA_API_BUDGET_DAILY=10` (RMB) in
`.env`. The driver respects this and stops calling the LLM once
the day's cumulative cost crosses the threshold; cards remain
queued and resume on the next reset at 00:00 local time.

## Free-tier path

If you do not want to pay for an API at all, two options exist:

1. **Tier 0 demo only.** `memexa demo` is free forever, runs against
   the bundled synthetic dataset, and shows the query flow honestly.
2. **Self-hosted LLM.** Run Qwen 14B (or any OpenAI-compatible model)
   on your own hardware via vLLM or Ollama, and point
   `MEMEXA_REMOTE_LLM_BASE_URL` at `http://127.0.0.1:11434/v1`. There
   is no monthly API bill; the cost moves to GPU electricity.

The first option is the suggested first stop; the second is suggested
for users who want to ingest tens of thousands of messages per day and
do not want a recurring API bill.
