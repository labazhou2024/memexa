# 6. Dual-GPU swap beats split for sequential pipelines

**English** · [中文](06_dual_gpu_swap.zh.md)

> Two GPUs and two pipeline stages. The intuitive layout is *GPU 0 runs
> Stage A, GPU 1 runs Stage B, in parallel*. For our gate-extract
> cascade this is 1.7× slower than running both stages on both GPUs in
> series with a model swap in between.

## Setup

The Stage A (gatekeeper) model is Qwen-14B 4-bit, ~12 GB VRAM, fast
inference (~0.4 sec/batch).

The Stage B (extractor) model is Gemma-31B 4-bit, ~22 GB VRAM, slower
inference (~2.1 sec/batch). Each model fits in a single A6000 (48 GB)
with room to spare.

The corpus to process: 1000 batches.

## The intuitive layout — "split"

```
              ┌─────────┐
   Batches ──▶│  GPU 0  │ Stage A (Qwen-14B)
              │         │ output: HIGH/MEDIUM/LOW verdict per batch
              └────┬────┘
                   │
            HIGH/MEDIUM batches forward
                   │
              ┌────▼────┐
              │  GPU 1  │ Stage B (Gemma-31B)
              │         │ output: V2 envelope JSON per batch
              └─────────┘
```

Two stages run in parallel. Throughput should be `min(A_rate, B_rate)` =
B's rate, the bottleneck. Stage B at 2.1 sec/batch × 1000 batches =
**2100 sec = 35 min**. Stage A finishes 7 min in (0.4 × 1000) and then
sits idle.

Total wall time: ~35 min. GPU 0 utilisation: 20 %.

## The "swap" layout

```
   Phase 1 (Gate)              Phase 2 (Extract)

   Batches ──▶ GPU 0 (Qwen)    GPU 0 (Gemma) ──┐
              GPU 1 (Qwen) ──▶ GPU 1 (Gemma) ──┴─▶ V2 envelope JSON

   |---- ~3 min ----|   ~2 min model swap     |---- ~15 min ----|
```

Phase 1: both GPUs run the gatekeeper on different batch slices.
Throughput is 2× a single GPU. Total Phase 1 time = (1000 / 2) × 0.4
sec = 200 sec = 3.3 min.

Model swap: 2 min (unload Qwen, load Gemma on both GPUs).

Phase 2: both GPUs run the extractor on HIGH/MEDIUM batches (say 600
of 1000). 2× throughput. Total Phase 2 time = (600 / 2) × 2.1 sec =
630 sec = 10.5 min.

Total wall time: 3.3 + 2 + 10.5 = **15.8 min**. GPU utilisation 90 %+
throughout.

## Measured speedup

| Layout | Wall time | GPU 0 util | GPU 1 util |
|--------|-----------|------------|------------|
| Split  | 5.6 h*    | 22 %       | 89 %       |
| Swap   | 2.5 h*    | 91 %       | 90 %       |

*on the full 8-month backfill (≈1.2 M batches across six sources).

**1.7× faster** on the same hardware.

## Why "split" loses

In the split layout, the bottleneck is whichever stage is slower —
Stage B, by a 5× margin. GPU 0 sits idle most of the time because
Stage A clears its queue in a fraction of the time Stage B needs.

In the swap layout, *both stages parallelise across both GPUs*.
Stage A is fast, so Phase 1 is short. Stage B is slow, but it gets
2× the hardware. The model swap is a one-time tax that is amortised
over the entire batch queue.

## When swap loses

If your stage times are comparable (Stage A ≈ Stage B), the split
layout wins by avoiding the swap tax. The break-even point is roughly
when Stage A's wall time is >40 % of Stage B's. Below 40 %, swap is a
win.

For LLM gate-extract cascades, Stage A is almost always >10× faster
than Stage B (gating is one-token classification; extraction is full
JSON synthesis). Swap is the right default.

## Operational mechanics

```bash
# Phase 1 — gate
ssh gpu-host '
  pkill -f vllm_server_gemma
  screen -dmS qwen_gpu0 vllm_server.sh --model qwen-14b --gpu 0 --port 8312
  screen -dmS qwen_gpu1 vllm_server.sh --model qwen-14b --gpu 1 --port 8313
'

# wait for both vllm instances to be healthy
python scripts/wait_for_vllm.py http://gpu-host:8312 http://gpu-host:8313

# run gate workers (one per port)
python -m src.extraction.l0_worker_api --stage A --gate-port 8312 &
python -m src.extraction.l0_worker_api --stage A --gate-port 8313 &
wait

# Phase 2 — swap to extractor
ssh gpu-host '
  screen -S qwen_gpu0 -X quit
  screen -S qwen_gpu1 -X quit
  sleep 5   # give kernel time to release VRAM
  screen -dmS gemma_gpu0 vllm_server.sh --model gemma-31b --gpu 0 --port 8312
  screen -dmS gemma_gpu1 vllm_server.sh --model gemma-31b --gpu 1 --port 8313
  sleep 180 # gemma takes ~3 min to load
'

# run extract workers
python -m src.extraction.l0_worker_api --stage B --extract-port 8312 &
python -m src.extraction.l0_worker_api --stage B --extract-port 8313 &
wait
```

## When the lesson generalises

Any sequential cascade where one stage is significantly cheaper than
the next, and you have multiple homogeneous accelerators, will
benefit from swap-style scheduling over split-style. The general
principle: *parallelism should be across data items, not across
pipeline stages, when the stages are heterogeneously expensive*.

## See also

- `src/extraction/l0_worker_api.py` — supports `--stage A` or `--stage B`
  on the same codebase.
- `scripts/swap_to_gemma.sh` — operational helper for Phase 2 swap.
