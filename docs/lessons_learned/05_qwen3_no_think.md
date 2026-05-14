# 5. Qwen3 needs `/no_think` directive under vllm concurrency

**English** · [中文](05_qwen3_no_think.zh.md)

> Qwen3 ships with a built-in chain-of-thought ("thinking") mode that
> the model emits before the actual answer. Under vllm with
> `max_concurrent_requests > 1`, the thinking output grows unboundedly
> and the engine deadlocks within ~5 minutes. The fix is in the prompt,
> not in the server flags.

## Symptom

A vllm instance hosting Qwen3-14B served single requests fine. As soon
as three concurrent driver threads hit it (gatekeeper stage on a
batch backfill), the GPU memory creeped up to 90 %, the `/health`
endpoint started timing out, and `nvidia-smi` showed the kernel
launches stop. Restart the engine and it works again — for ~5 minutes.

## What was happening

Qwen3's default chat template wraps every assistant response in:

```
<think>
…the model reasons here, can be hundreds or thousands of tokens…
</think>

actual response
```

The KV cache grows linearly with output tokens. With three concurrent
requests each emitting 2000+ thinking tokens before the "actual
response", and vllm's scheduler allocating proportional cache budget,
you saturate the cache and the scheduler stops admitting new tokens.
The model is technically still alive, but it cannot generate.

## Failed fixes

### `chat_template_kwargs={"enable_thinking": False}`

This is the official "off switch" for Qwen3 thinking. It works when
calling the model via the HuggingFace transformers chat template
directly. **It is not forwarded by vllm** as of v0.6.x: the kwarg
silently goes nowhere and the template renders with `enable_thinking=True`
regardless. We verified by tcpdump'ing the request body — the kwarg is
in the JSON, but the rendered prompt that vllm sees still contains the
`<think>` opener.

### Setting `max_tokens=512`

Caps output but does not stop the thinking — the model still uses the
first 512 tokens to think, then emits a truncated JSON response that
fails schema validation.

### Lowering `max_concurrent_requests` to 1

Works but throughput goes from ~250 batches/hour to ~80 batches/hour.
Unacceptable for an 8-month backfill.

## What worked: in-prompt `/no_think` directive

Qwen3's chat template recognises a literal `/no_think` token appended
to the *user message* and switches off the thinking emission:

```python
def _build_user_prompt(text: str, model: str) -> str:
    if "qwen" in model.lower():
        return text.rstrip() + "\n\n/no_think"
    return text
```

After this change:

- Output tokens per request dropped from ~2400 → ~480 (the actual JSON
  payload).
- Three concurrent threads stable for 6+ hour runs.
- Throughput recovered to ~280 batches/hour.

## Gemma does not need this

Gemma-31B's thinking is more disciplined — it has an internal budget
and stops on its own. We let Gemma keep thinking:

```python
if "gemma" in model.lower():
    return text   # leave thinking enabled
```

The branch is in `memexa/extraction/paired_eval.py`.

## When the lesson generalises

Anything labelled "reasoning model" or "thinking-tier model" without a
hard output budget will deadlock vllm under concurrency. Before
deploying any such model:

1. Read the chat template source. Look for `<think>`, `<thought>`,
   `<reasoning>` openers.
2. Find the official directive to suppress them (`/no_think`,
   `/quick`, etc.). It is usually documented near the chat template,
   not in the model card.
3. Bake the directive into your client library, not the request
   parameters. The model template renders client-side; you cannot
   trust runtime flags to reach it.

## See also

- `memexa/extraction/paired_eval.py:130` — branching on model name.
- Qwen3 chat template reference:
  [`qwenlm.github.io/blog/qwen3`](https://qwenlm.github.io/blog/qwen3/)
- vllm issue tracker for `chat_template_kwargs` forwarding (open as
  of v0.6.x).
