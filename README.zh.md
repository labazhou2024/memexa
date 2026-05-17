<!--
repository-topics:
  - personal-memory
  - knowledge-graph
  - chinese-nlp
  - retrieval-augmented-generation
  - self-hosted
  - postgresql
  - pgvector
  - bge-m3
  - llm-pipeline
  - cli
  - deliverable-factory
  - action-card
  - report-generation
-->

# Memexa · 镜我

[English](README.md) · **中文**

> **面向 AI agent 与人类共用、中文原生数据的 memory layer。**
> 自托管 memory graph, 覆盖微信 / QQ / 飞书 / 钉钉 群聊、中文邮件、
> 中文音频。Verbatim 存储 + 结构化抽取; 查询返回带原句 citation 的
> cards。
>
> 🤖 **设计上就是 AI agent 兼容的**。绝大多数真实用法 = AI agent
> (Claude Code / Cursor / Cline / 自写 agent) 把 memexa 当 subprocess
> 调, 代用户回答问题。14 个查询子命令是个小协议; agent 需要遵守的
> 契约写在 [`docs/for_agents.zh.md`](docs/for_agents.zh.md)。原生 MCP
> integration 在 v0.5; 当前 first-class 路径 = shell subprocess +
> `--json` 输出。

[![CI](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml)
[![CodeQL](https://github.com/labazhou2024/memexa/actions/workflows/codeql.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/codeql.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)
[![PyPI](https://img.shields.io/pypi/v/memexa?label=PyPI)](https://pypi.org/project/memexa/)
[![PII scan](https://img.shields.io/badge/PII%20residual-0%20matches-success.svg)](scripts/full_pii_scan.sh)

## Quickstart

2 个起点, 按你身份选。

### 人类用户 — 30 秒看见

```bash
pip install memexa
memexa demo
```

你会看到 6 个 source 的合成对话被 stub extractor 摄入, 接着 5 个示例
查询在终端打印 — `quick` / `arc` / `timeline` / `pending` / `topic`。
不需要后端, 不需要 LLM, 不需要配置。这是项目实际能做什么的第一眼诚实
展示。

### AI agent — 今天 subprocess CLI, v0.5 再上 MCP

```bash
# Agent 今天通过 subprocess 已经能用:
pip install memexa
memexa quick "<你的问题>" --json   # 结构化输出方便 agent parse
memexa arc "<人名>" --json
# ... 共 14 个子命令, 全部 v0.1.x 起支持 --json mode
```

14 个子命令 + [`docs/for_agents.zh.md`](docs/for_agents.zh.md) 的 7
条 hard rule 是 agent 契约。原生 MCP integration (`memexa-mcp` server +
`.mcp.json` snippet) 在 v0.5; 在此之前 shell subprocess 是 first-class
路径, 任何能跑 shell 工具的 agent 都能用。

### 接下来 (对人和 agent 都适用)

要接自己的数据, 配 LLM provider 选 1 个 source 开始。
[`docs/quickstart.zh.md`](docs/quickstart.zh.md) 走 Tier 1 (5 分钟,
1 个 source) 和 Tier 2 (30 分钟, 完整生产部署 + cron + dashboard)。

## 你可以问什么

| 问题 pattern | 子命令 | 返回 |
|---|---|---|
| X 是谁 | `arc "X"` | 关系演化, 8 个 fan-out 变体 |
| X 的全过程 | `topic "Mac 购置"` | 80-200 张带 citation 的 cards |
| Y 老师最近要什么 | `person "Y 老师"` | 人物 article + 近期 events |
| X 项目跨源动态 | `project "X"` | 4 个 source 各 N 张 |
| 我有哪些待办 | `pending` | 日历活跃 commitments |
| 这段时间发生了什么 | `timeline --start ... --end ...` | 时序卡片列表 |
| 综合回答 | `reflect "问题"` | LLM 综合 Markdown |

共 14 个子命令。决策表和组合模式在
[`docs/usage_guide.zh.md`](docs/usage_guide.zh.md)。yes/no 状态查询的
5-phase 工作流见 [`docs/5_phase_query.zh.md`](docs/5_phase_query.zh.md)。

## 为什么用 memexa, 不用 OpenHuman / MemPalace / ReMe?

简短: verbatim 原始存储 + LLM 抽取 V2 envelope + 每条断言
`evidence_quotes` 原文 citation + 跨别名 canonical id, 全部在相邻项目
不打算碰的中文 IM 数据源上。

完整逐项能力对比和 5 类用户场景见 [`docs/why.zh.md`](docs/why.zh.md)。

## 架构一图

```
   微信   ─┐                                              ┌─► "X 是谁?"           (arc + quick)
   QQ     ─┤                                              ├─► "上周组里干啥?"     (topic + trends)
   邮件   ─┼──► 双 LLM 抽取 ──► PG + pgvector ──┤
   浏览   ─┤    (gate+extract)   记忆图谱        ├─► "X 项目到哪了?"     (project + timeline)
   AI 对话─┤                                              ├─► "Y 老师要啥?"       (person)
   语音   ─┘                                              └─► "我有哪些待办?"     (pending)
        ↑                                                       ↑
   原始数据                                              14 个查询子命令
   (本地, 完全自托管)                                    (跨源组合)
```

完整架构见 [`docs/architecture.zh.md`](docs/architecture.zh.md)。

## 文档导航

| 主题 | 链接 |
|---|---|
| Quickstart (3-tier 路径: 30 秒 → 5 分钟 → 30 分钟) | [`docs/quickstart.zh.md`](docs/quickstart.zh.md) |
| 架构 | [`docs/architecture.zh.md`](docs/architecture.zh.md) |
| 为什么用 memexa (对比 / 5 用户场景) | [`docs/why.zh.md`](docs/why.zh.md) |
| 成本估算 (DeepSeek / GPT-4o / Claude 月度) | [`docs/cost.zh.md`](docs/cost.zh.md) |
| 14 个查询子命令详解 | [`docs/usage_guide.zh.md`](docs/usage_guide.zh.md) |
| 5-phase 状态推断 | [`docs/5_phase_query.zh.md`](docs/5_phase_query.zh.md) |
| 完整环境变量 | [`docs/configuration.zh.md`](docs/configuration.zh.md) |
| FAQ / 故障排查 | [`docs/faq.zh.md`](docs/faq.zh.md) · [`docs/troubleshooting.zh.md`](docs/troubleshooting.zh.md) |
| 各 source 接入指南 | [`docs/integrations/`](docs/integrations/) |
| 跨平台部署 | [`docs/deployment/`](docs/deployment/) |
| 示例 walkthrough (合成数据) | [`examples/demo_dataset/walkthroughs/`](examples/demo_dataset/walkthroughs/) |
| Case studies | [`docs/case_studies/`](docs/case_studies/) |
| **AI agent 接入 (MCP / integration spec)** | [`docs/for_agents.zh.md`](docs/for_agents.zh.md) |
| 路线图 | [`ROADMAP.zh.md`](ROADMAP.zh.md) |
| 贡献指南 | [`CONTRIBUTING.zh.md`](CONTRIBUTING.zh.md) |
| 安全策略 | [`SECURITY.zh.md`](SECURITY.zh.md) |
| 治理 | [`GOVERNANCE.md`](GOVERNANCE.md) |

## 两种 LLM 运行方式

memexa 的核心是双 LLM gate-extract 流程。OSS ship 了你本地用任何 OpenAI
兼容 endpoint 跑它需要的一切。

```bash
# 默认: 内置 prompt + 你自己的 LLM provider
export MEMEXA_EXTRACTOR_TIER=bundled

# BYO: 自带 prompt 做高级调优
export MEMEXA_EXTRACTOR_TIER=byo
export MEMEXA_PROMPT_PATH=/path/to/your_prompts.py
```

中文场景推荐 DeepSeek V4 Flash (gate) + V4 Pro (extractor) — 典型成本
**每 1000 条消息 ¥0.30**。GPT-4o 和 Claude 4.x 也支持, 但成本贵 5-10
倍。完整对比见 [`docs/cost.zh.md`](docs/cost.zh.md)。

## License

Apache 2.0. See [`LICENSE`](LICENSE). OSS core 永远 Apache 2.0。
