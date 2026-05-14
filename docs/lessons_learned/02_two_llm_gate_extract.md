# 2. Two-LLM gate-extract beats single-LLM extract

**English** · [中文](02_two_llm_gate_extract.zh.md)

> The intuitive design is *"one LLM, one extraction prompt, one JSON
> output"*. We tried that. It was slow, expensive, and emitted bad JSON
> on the easy cases. Splitting into gatekeeper + extractor cut cost ~40 %,
> improved schema validity, and made failures localised.

## The intuitive (and wrong) design

```
batch ──▶ extractor LLM ──▶ V2 envelope JSON ──▶ POST to memory bank
```

One model. One call. One response. Easy.

The problem: a *batch* in this codebase is 8-15 messages from a single
conversation window. About 40-60 % of batches are noise — group-chat
greetings, single-emoji replies, food delivery confirmations, etc.
Forcing the extractor to look at all of them costs:

- **GPU time**: the extractor model is 30 B+ parameters, ~4 sec per
  batch on a single A6000. Half of those seconds are wasted on noise
  batches that produce zero cards.
- **API budget**: at commercial rates ($1.5/M output tokens for a
  Qwen3-class model), the wasted half costs real money.
- **Schema corruption**: forcing the extractor to emit V2 envelope JSON
  for a batch with no extractable facts produces empty arrays, but
  occasionally produces malformed JSON because the model "tries to fill
  something in".

## The fix: two-stage cascade

```
batch ──▶ gatekeeper LLM ──▶ verdict ∈ {HIGH, MEDIUM, LOW}
                                            │
                          LOW ──▶ skip      │
                                            │
                          HIGH/MEDIUM       ▼
                                        extractor LLM ──▶ V2 envelope JSON
```

The gatekeeper is a cheaper model (Qwen-14B 4-bit on local hardware,
or `qwen3.6-chat` on an OpenAI-compatible API). Its only job: read
the batch, output one of three labels.

Verdicts:

- **LOW**  — no extractable facts; skip.
- **MEDIUM** — some facts, possibly low-signal; extract anyway.
- **HIGH** — clearly contains commitments, decisions, named entities;
  extract.

The extractor is the heavy model (Gemma-31B 4-bit or `deepseek-v4-flash`-class).
It only sees HIGH + MEDIUM batches.

## Measured impact

On 8 months of personal chat exhaust (~16 K batches across six sources):

| Metric                              | Single-LLM | Two-LLM        |
|-------------------------------------|------------|----------------|
| Total LLM calls                      | 16,091     | 16,091 (gate) + 9,315 (extract) |
| Mean GPU seconds per batch           | 4.2        | 0.4 + 2.1 (mean: 1.5 incl. LOW skips) |
| Schema-invalid extraction outputs    | 1.8 %      | 0.4 %          |
| End-to-end wall time on full corpus  | 18 h       | 9 h            |
| Estimated commercial-API cost (USD)  | $24        | $14            |

## Why does it produce better JSON

The extractor sees more *facts-rich* batches per unit time, so its
context window is never half-full of small talk. Models that emit
structured JSON tend to do so most reliably when the input is dense
with what they are supposed to extract.

## Failure isolation

When one stage fails — gatekeeper times out, or extractor emits
malformed JSON — only that stage's output is lost. With a single-LLM
design, a model crash halfway through a batch loses everything.

In the two-LLM design we materialise the gatekeeper verdict to disk
before invoking the extractor. If extraction fails, we know which
batches were classified HIGH/MEDIUM and can re-extract them without
re-running the gate.

## When the trick does not pay off

The gain comes from the LOW-rate of your input. If your corpus is
>90 % HIGH-density, the gate is mostly wasted work and you would do
better with the single-LLM design plus a permissive output schema.

Three data sources where two-stage pays off:

1. Group chats (the LOW rate is ~50 %).
2. Browser history (LOW rate ~70 % — most sessions are not "facts").
3. Voice recordings (LOW rate ~60 % — silence and passive listening).

Two where it does not:

1. Email (LOW rate ~15 % — even spam is "structured" enough to extract).
2. Self-authored documents (LOW rate ~5 %).

## See also

- `memexa/extraction/pass1_prompt.py` — gatekeeper prompt template.
- `memexa/extraction/pass2_prompt.py` — extractor prompt + V2 envelope spec.
- `memexa/extraction/l0_worker_serial.py` — serial worker that ping-pongs
  between the two stages on a single laptop with two model swaps.
- `memexa/extraction/l0_worker_api.py` — parallel worker for two OpenAI-
  compatible endpoints (separate gate + extract models).
