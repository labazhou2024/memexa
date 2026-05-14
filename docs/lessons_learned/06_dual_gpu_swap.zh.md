# 6. Dual-GPU swap 比 split 强 (顺序 pipeline)

[English](06_dual_gpu_swap.md) · **中文**

> 两个 GPU 和两个 pipeline stage。直觉布局是 *GPU 0 跑 Stage A, GPU 1
> 跑 Stage B, 并行*。我们的 gate-extract 级联里这比两 GPU 都跑两 stage
> 中间 swap 模型慢 1.7×。

## Setup

Stage A (gatekeeper) 模型是 Qwen-14B 4-bit, ~12 GB VRAM, 推理快
(~0.4 秒/batch)。

Stage B (extractor) 模型是 Gemma-31B 4-bit, ~22 GB VRAM, 推理慢
(~2.1 秒/batch)。每个模型在单 A6000 (48 GB) 装得下还剩空间。

要处理: 1000 batch。

## 直觉布局 — "split"

```
              ┌─────────┐
   Batch  ──▶│  GPU 0  │ Stage A (Qwen-14B)
              │         │ 输出: per batch 的 HIGH/MEDIUM/LOW verdict
              └────┬────┘
                   │
            HIGH/MEDIUM batch 往下
                   │
              ┌────▼────┐
              │  GPU 1  │ Stage B (Gemma-31B)
              │         │ 输出: per batch 的 V2 envelope JSON
              └─────────┘
```

两 stage 并行跑。吞吐应是 `min(A_rate, B_rate)` = B 的速率, 瓶颈。
Stage B 在 2.1 秒/batch × 1000 batch = **2100 秒 = 35 分钟**。
Stage A 7 分钟做完 (0.4 × 1000) 然后空闲。

总 wall time: ~35 分钟。GPU 0 利用率: 20%。

## "swap" 布局

```
   Phase 1 (Gate)              Phase 2 (Extract)

   Batch  ──▶ GPU 0 (Qwen)    GPU 0 (Gemma) ──┐
              GPU 1 (Qwen) ──▶ GPU 1 (Gemma) ──┴─▶ V2 envelope JSON

   |---- ~3 分 ----|   ~2 分模型 swap        |---- ~15 分 ----|
```

Phase 1: 两 GPU 都跑 gatekeeper, 跑不同 batch 切片。吞吐是单 GPU 的 2×。
Phase 1 总时间 = (1000 / 2) × 0.4 秒 = 200 秒 = 3.3 分钟。

模型 swap: 2 分钟 (卸 Qwen, 在两 GPU 装 Gemma)。

Phase 2: 两 GPU 都跑 extractor 在 HIGH/MEDIUM batch (假设 1000 里 600)。
2× 吞吐。Phase 2 总时间 = (600 / 2) × 2.1 秒 = 630 秒 = 10.5 分钟。

总 wall time: 3.3 + 2 + 10.5 = **15.8 分钟**。整个过程 GPU 利用率 90%+。

## 实测加速

| 布局   | Wall time | GPU 0 利用 | GPU 1 利用 |
|--------|-----------|------------|------------|
| Split  | 5.6 h*    | 22 %       | 89 %       |
| Swap   | 2.5 h*    | 91 %       | 90 %       |

*在完整 8 个月 backfill (≈1.2 M batch 跨 6 source) 上。

**1.7× 快**, 同硬件。

## 为啥 "split" 输

split 布局里, 瓶颈是更慢的那个 stage — Stage B, 差 5×。GPU 0 大多数时间
空闲, 因为 Stage A 在 Stage B 需要时间的零头就清空了队列。

swap 布局里 *两 stage 都跨两 GPU 并行*。Stage A 快, Phase 1 短。Stage B
慢但拿到 2× 硬件。模型 swap 是一次性税, 摊在整个 batch 队列上。

## swap 什么时候输

stage 时间相当 (Stage A ≈ Stage B) 时, split 布局赢, 因为不付 swap 税。
切换点大概在 Stage A 的 wall time > Stage B 的 40% 时。低于 40%, swap 赢。

LLM gate-extract 级联里, Stage A 几乎总是比 Stage B 快 >10× (gating 是
1-token 分类; extraction 是完整 JSON 合成)。swap 是合理默认。

## 操作机制

```bash
# Phase 1 — gate
ssh gpu-host '
  pkill -f vllm_server_gemma
  screen -dmS qwen_gpu0 vllm_server.sh --model qwen-14b --gpu 0 --port 8312
  screen -dmS qwen_gpu1 vllm_server.sh --model qwen-14b --gpu 1 --port 8313
'

# 等两个 vllm 实例 healthy
python scripts/wait_for_vllm.py http://gpu-host:8312 http://gpu-host:8313

# 跑 gate worker (一 port 一个)
python -m src.extraction.l0_worker_api --stage A --gate-port 8312 &
python -m src.extraction.l0_worker_api --stage A --gate-port 8313 &
wait

# Phase 2 — swap 到 extractor
ssh gpu-host '
  screen -S qwen_gpu0 -X quit
  screen -S qwen_gpu1 -X quit
  sleep 5   # 给内核时间释 VRAM
  screen -dmS gemma_gpu0 vllm_server.sh --model gemma-31b --gpu 0 --port 8312
  screen -dmS gemma_gpu1 vllm_server.sh --model gemma-31b --gpu 1 --port 8313
  sleep 180 # gemma 约 3 分钟装好
'

# 跑 extract worker
python -m src.extraction.l0_worker_api --stage B --extract-port 8312 &
python -m src.extraction.l0_worker_api --stage B --extract-port 8313 &
wait
```

## 这个教训泛化时

任何顺序级联里, 一个 stage 比下一个明显便宜, 且你有多个同质加速器,
都会从 swap 风格调度受益相对 split 风格。一般原则: *并行应跨数据项,
不应跨 pipeline stage, 当 stage 异构开销时*。

## 相关

- `src/extraction/l0_worker_api.py` — 同一代码库支持 `--stage A` 或
  `--stage B`
- `scripts/swap_to_gemma.sh` — Phase 2 swap 操作 helper
