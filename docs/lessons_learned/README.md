# Lessons learned

**English** · [中文](README.zh.md)

> Six post-mortems from running this stack in production for ~8 months.
> Each one is a real bug we hit, a wrong intuition, and the fix that
> stuck.
>
> These are not philosophy. They are pieces of practical wisdom you
> would otherwise have to re-discover the hard way.

| # | Title                                                                          | Bug pattern                                                            |
|---|--------------------------------------------------------------------------------|------------------------------------------------------------------------|
| 1 | [Hindsight tags are OR, not AND](01_tags_are_or.md)                            | API contract mismatch with intuition; recall returns wrong superset    |
| 2 | [Two-LLM gate-extract beats single-LLM extract](02_two_llm_gate_extract.md)    | Cost / quality tradeoff; how to size each role                          |
| 3 | [PG-aware pending vs ghost markers](03_pg_aware_pending.md)                    | Local marker file drift from authoritative DB state                    |
| 4 | [Win subprocess timeout requires Job Object](04_win_job_subprocess.md)         | Python subprocess.run(timeout=...) leaks grandchildren on Windows      |
| 5 | [Qwen3 needs `/no_think` directive](05_qwen3_no_think.md)                      | Thinking-tier models deadlock under vllm concurrency without override   |
| 6 | [Dual-GPU swap beats split for sequential pipelines](06_dual_gpu_swap.md)      | Stage A and Stage B are not equally GPU-hungry; topology matters       |
