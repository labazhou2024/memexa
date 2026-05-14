# 踩坑复盘

[English](README.md) · **中文**

> 在生产里跑这套栈 ~8 个月的 6 篇 post-mortem。每篇都是一个真 bug,
> 一个错的直觉, 一个最终立住的修法。
>
> 不是哲学。是你不读就要自己重新踩一遍的实用智慧。

| # | 标题                                                                          | Bug 模式                                                              |
|---|--------------------------------------------------------------------------------|------------------------------------------------------------------------|
| 1 | [Hindsight tags 是 OR 不是 AND](01_tags_are_or.zh.md)                          | API 合同与直觉不匹配; recall 返回错的 superset                          |
| 2 | [双 LLM gate-extract 比单 LLM extract 强](02_two_llm_gate_extract.zh.md)        | 成本 / 质量取舍; 每个角色怎么定 size                                    |
| 3 | [PG-aware pending vs ghost marker](03_pg_aware_pending.zh.md)                  | 本地 marker 文件相对权威 DB 状态漂移                                    |
| 4 | [Win subprocess timeout 需要 Job Object](04_win_job_subprocess.zh.md)          | Python subprocess.run(timeout=...) 在 Windows 上漏孙进程                |
| 5 | [Qwen3 需要 `/no_think` 指令](05_qwen3_no_think.zh.md)                          | thinking 档模型不覆盖 vllm 并发会死锁                                   |
| 6 | [Dual-GPU swap 比 split 强 (顺序 pipeline)](06_dual_gpu_swap.zh.md)             | Stage A 和 Stage B GPU 渴求度不等; 拓扑重要                            |
