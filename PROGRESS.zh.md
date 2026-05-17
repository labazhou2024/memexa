# OSS 准备进度

**English**: [PROGRESS.md](PROGRESS.md)（权威源）。本文件是中文镜像。

> M 档代码准备 Phase 1-11 完成 (Phase 11 = 命名 + 模块迁移, 2026-05-14
> 在 `memexa / 镜我` 改名 pass 中收尾)。
> README 故事化叙述、流量推广 (知识库 / awesome-list PR) **故意** 不在
> 本 pass 范围内。

## Phase 状态 (最终)

| Phase | 名称                                          | 状态       | 输出                                                  |
|-------|-----------------------------------------------|------------|-------------------------------------------------------|
| 1     | 建立 oss-prep 独立工作区 + 子目录骨架          | ✅ 完成     | 26 dirs, .gitignore, git init (anon author)            |
| 2     | 主 repo PII 全量精确审计                       | ✅ 完成     | 162 unique 文件 + 8 token 按类拆分                     |
| 3     | 选择性 mirror M 档代码                         | ✅ 完成     | 304 .py mirrored (core 206 / extraction 56 / ...)      |
| 4     | sanitize 批量替换                              | ✅ 完成     | 86 files / 395 replacements / verify 0 residual         |
| 5     | 三层通用化抽象 helper                          | ✅ 完成     | _path_resolver + _user_aliases + _user_identity        |
| 6     | 17 件文档 (除 README 故事)                     | ✅ 完成     | architecture / quickstart / usage_guide / 5_phase + 3 deploy + CONTRIBUTING / SECURITY / CHANGELOG / LICENSE / 3 issue + PR template + migration guide |
| 7     | 6 篇 lesson-learned narrative                  | ✅ 完成     | tags-OR / 2-LLM / PG-aware / win-job-subprocess / qwen3-no-think / dual-GPU-swap |
| 8     | 工程脚手架 E1-E8                               | ✅ 完成     | pyproject.toml + CI + security + dependabot + pre-commit + docker-compose + Makefile + pii-scan |
| 9     | Demo dataset 准备                              | ✅ 完成     | 7 源合成数据集 + ingest.py / dry-run pass 26 cards     |
| 10    | e2e + sanity 完备验证                          | ✅ 完成     | 7/7 verification gates PASS                            |
| 11    | 命名 + 模块迁移 (memexa / 镜我)                | ✅ 完成     | 12 文件 find-and-replace + 25 模块 _path_resolver 接入 |

## 最终验证 gate (Phase 11, 2026-05-14)

| # | Gate                                          | Result   |
|---|-----------------------------------------------|----------|
| 1 | PII sanity scan (排除 by-design 探测器)        | 0 hits   |
| 2 | Python syntax (306 .py)                       | 306/306 PASS |
| 3 | 三层 helper smoke test (无 env)                | PASS     |
| 4 | pyproject.toml + YAML syntax                  | PASS     |
| 5 | Demo dataset dry-run ingest                   | 26 cards / 6 sources |
| 6 | 主 repo 本会话未被改                          | confirmed |
| 7 | oss-prep tree integrity                       | 306 py / 25 md / 7 yaml / 6 json |
| 8 | `from memexa.core.X import Y` 覆盖 8 包       | 0 import 失败 |
| 9 | pytest tests/                                 | 17 passed / 2 skipped (drift; doc'd) |
| 10 | 25 `TODO(memgraph-oss)` markers              | 0 remaining |
| 11 | `python -m memexa.core.memory_query --help`   | 14 子命令列出 |

## 故意不做

1. **README 故事叙述** — 仅工程脚手架; 叙述待 CEO 定。
2. **PyPI 注册** — 等 CEO 发布批准。
3. **GitHub repo 创建** — 等 CEO `gh repo create labazhou2024/memexa`。
4. **流量 / 发布内容** — 仅 awesome-ai-memory PR (CEO 指令; 发布后做)。

## 备注

完整目录布局与 Owner-Task-时间预估表见英文 [PROGRESS.md](PROGRESS.md)。
本文为中英镜像约束履约，不重新维护两份独立 sprint log。
