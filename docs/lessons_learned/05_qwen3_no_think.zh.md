# 5. Qwen3 在 vllm 并发下需要 `/no_think` 指令

[English](05_qwen3_no_think.md) · **中文**

> Qwen3 自带一个 chain-of-thought ("thinking") 模式, 模型在真答案前
> emit 它。vllm 上 `max_concurrent_requests > 1` 时, thinking 输出无界
> 增长, ~5 分钟内 engine 死锁。修法在 prompt 里, 不在 server flag 里。

## 症状

托 Qwen3-14B 的 vllm 实例单请求跑得好。3 个并发 driver 线程一打 (backfill
的 gatekeeper stage), GPU 内存爬到 90%, `/health` endpoint 开始超时,
`nvidia-smi` 显示 kernel launch 停了。重启 engine 又好 — 持续 ~5 分钟。

## 背后发生啥

Qwen3 默认 chat 模板把每个 assistant 响应包成:

```
<think>
…模型在这推理, 可能几百到几千 token…
</think>

实际响应
```

KV cache 线性增长跟输出 token 数。3 个并发请求每个 emit 2000+ thinking
token 才到"实际响应", vllm scheduler 按比例分配 cache 预算, 你饱和 cache
scheduler 停止接受新 token。模型技术上还活着, 但生成不出来。

## 失败的修法

### `chat_template_kwargs={"enable_thinking": False}`

这是 Qwen3 thinking 的官方 "关" 开关。直接调 HuggingFace transformers
chat 模板时有效。**vllm v0.6.x 不转发这个 kwarg**: kwarg 静默扔掉,
渲染模板 `enable_thinking=True` 不变。我们用 tcpdump 抓请求 body 验证 —
kwarg 在 JSON 里, 但 vllm 看到的渲染 prompt 还含 `<think>` opener。

### 设 `max_tokens=512`

截 output 但不停 thinking — 模型还用前 512 token 思考, 然后 emit 截断
JSON, schema 验证失败。

### 降 `max_concurrent_requests` 到 1

能用但吞吐从 ~250 batch/小时 掉到 ~80 batch/小时。8 个月 backfill 受
不了。

## 成功的修法: prompt 内 `/no_think` 指令

Qwen3 的 chat 模板识别附在 *user message* 后的字面 `/no_think` token,
关掉 thinking emission:

```python
def _build_user_prompt(text: str, model: str) -> str:
    if "qwen" in model.lower():
        return text.rstrip() + "\n\n/no_think"
    return text
```

改完后:

- 每请求输出 token 从 ~2400 → ~480 (实际 JSON payload)
- 3 并发线程 6+ 小时跑稳
- 吞吐恢复到 ~280 batch/小时

## Gemma 不需要这个

Gemma-31B 的 thinking 更克制 — 有内部 budget 会自己停。我们让 Gemma 接着
思考:

```python
if "gemma" in model.lower():
    return text   # 让 thinking 开着
```

分支在 `memexa/extraction/paired_eval.py`。

## 这个教训泛化时

任何标 "reasoning model" 或 "thinking-tier model" 又没硬 output 预算的,
在并发下都会让 vllm 死锁。部署任何这种模型前:

1. 读 chat 模板源码。找 `<think>`, `<thought>`, `<reasoning>` opener
2. 找官方指令压制它们 (`/no_think`, `/quick` 之类)。通常记在 chat 模板
   附近, 不在 model card 里
3. 把指令烤进 client lib, 不要靠 request 参数。模型模板 client 端渲染;
   不能信运行时 flag 能到那

## 相关

- `memexa/extraction/paired_eval.py:130` — 按 model name 分支
- Qwen3 chat 模板参考:
  [`qwenlm.github.io/blog/qwen3`](https://qwenlm.github.io/blog/qwen3/)
- vllm issue tracker 关于 `chat_template_kwargs` 转发 (v0.6.x 仍 open)
