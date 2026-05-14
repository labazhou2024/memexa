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

> **Your personal Pensieve.**
> 把你散落在 6 个 silo 的数据，按你当下要做的事，编成一份能直接交付/带走的文档。

[![CI](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml)
[![CodeQL](https://github.com/labazhou2024/memexa/actions/workflows/codeql.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/codeql.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![PII scan](https://img.shields.io/badge/PII%20residual-0%20matches-success.svg)](scripts/full_pii_scan.sh)

## 这是什么

`memexa` 摄入你 6 类中文日常数据（微信、QQ、邮件、浏览、AI 对话、语音备忘），
用双 LLM 抽取实体 / 关系 / 时间证据，存进 PostgreSQL + pgvector 记忆图谱，
然后用 **14 个查询子命令** 把当下要用的东西捞出来 — 谁找过我？这件事的全过程？
我有哪些待办？X 项目跨源动态？

灵感来自《哈利波特》的冥想盆（Pensieve）—— 把脑子里散落的记忆倒出来，
重组、观察、抽取出当下要用的东西。

```
   微信 ─┐                                         ┌─► "X 是谁?"           (arc + quick)
   QQ   ─┤                                         ├─► "上周组里干了啥?"   (topic + trends)
   邮件 ─┼──► 双 LLM 抽取 ──► PG + pgvector ──┤
   浏览 ─┤    (gate+extract)   记忆图谱        ├─► "X 项目到哪了?"     (project + timeline)
   AI   ─┤                                         ├─► "Y 老师要啥?"       (person)
   语音 ─┘                                         └─► "我有哪些待办?"     (pending)
        ↑                                                  ↑
   你的原始数据                                       14 个查询子命令
   (本地, 完全自托管)                                  (cross-source 组合)
```

> **v0.1 范围**: 完整 ingestion + 抽取 + 查询 + dashboard + **5 篇 walkthrough + 2 篇 case study**。
> 上层"按用途自动生成可交付物" (实验报告 / 行动卡 / 周报 / 会前简报) 在
> [ROADMAP.md](ROADMAP.md) v0.2 — 现在可以用 14 个查询命令手动组合得到大致效果。
>
> 🚀 **第一次看？** 直接跳 [Example walkthroughs ↓](#-example-walkthroughs--5-个-reproducible-实例) 看 5 个真实场景怎么用。

> 🤖 **设计上就是 AI agent 兼容的**。绝大多数真实用户是让 AI agent
> (Claude Code / Cursor / Cline / 自己写的 agent) 替自己调用 memexa,
> 而不是手敲子命令。14 个查询子命令是一个小协议; 给 agent 的协议文档在
> [docs/for_agents.zh.md](docs/for_agents.zh.md) (硬规则 / 决策表 /
> 组合模式 / 常见坑)。如果你在做需要中文数据记忆层的 agent, 从那开始。

## 服务两类用户 (设计意图)

| 人群 | 触发场景 | 当下可用 (v0.1) | 路线图 (v0.2+) |
|---|---|---|---|
| **科研 / 学生** | 实验做完 → 写报告；导师约谈 → 准备 | 14 个 query 手动 + LaTeX 模板 | `memexa lab-report X` / `memexa brief <人>` |
| **办事人 / 打工人** | 错过 ddl → 补做；出门办事 → 怕漏 | `memexa pending` + `memexa quick` | `memexa action-card X` / `memexa dashboard` |

两类人共用同一套底层（ingestion + 双 LLM 抽取 + 图谱 + 查询），上层模板包不同。

## 6 个数据源

| Source | Builder | Driver |
|---|---|---|
| 微信 WeChat | `memexa/ingestion/v5_wechat_batch_builder.py` | `memexa/drivers/backfill_v5_wechat_driver.py` |
| QQ | `memexa/extraction/qq/qq_history_to_batches.py` | `memexa/drivers/backfill_v5_qq_driver.py` |
| 邮件 Email | `memexa/ingestion/v5_email_batch_builder.py` | `memexa/drivers/backfill_v5_email_driver.py` |
| 浏览历史 Browser | `memexa/ingestion/v5_browser_batch_builder.py` | `memexa/drivers/backfill_v5_browser_driver.py` |
| AI 对话 Claude Code | `memexa/extraction/claude_code_to_v5_converter.py` | `memexa/drivers/backfill_v5_cc_driver.py` |
| 语音 Audio (mic) | `memexa/ingestion/v5_audio_batch_builder.py` | `memexa/drivers/backfill_v5_audio_driver.py` |

## Quickstart

```bash
# 1. 装包
pip install -e .

# 2. 初始化配置 (创建 ~/.memexa/ + 3 个示例文件)
memexa init                          # → ~/.memexa/{aliases,identity}.yaml + .env

# 3. 起后端
docker compose -f docker-compose.example.yml up -d

# 4. 跑 demo (不连后端可用 --dry-run)
python -m examples.demo_dataset.ingest --dry-run

# 5. 自检 + 第一次查询
memexa doctor                        # 验证后端 + LLM provider
memexa quick "<你的关键词>"
```

完整步骤: [docs/quickstart.zh.md](docs/quickstart.zh.md)

## 双 LLM gate-extract 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  6 类中文数据                                                          │
│  微信 │ QQ │ 邮件 │ 浏览 │ AI 对话 │ 语音备忘                          │
└──────────────────────────────────────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Per-source batch builder  →  JSON envelopes                         │
└──────────────────────────────────────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Stage A: gatekeeper LLM  (筛 HIGH/MEDIUM/LOW)                         │
│  Stage B: extractor LLM   (V2 envelope JSON)                          │
│  Stage C: BGE-M3 quorum + arbiter                                     │
│  Stage D: POST → memory_full_v5 bank                                  │
└──────────────────────────────────────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  PostgreSQL + pgvector + BGE-M3 embeddings + temporal links          │
└──────────────────────────────────────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  14 个查询命令 + 5-phase 状态推理 + Deliverable templates              │
└──────────────────────────────────────────────────────────────────────┘
```

完整架构: [docs/architecture.zh.md](docs/architecture.zh.md)

## 查询 CLI

```bash
memexa <subcmd> "<query>" [options]
```

14 个子命令分三档（基础 / 高级 / 复合链）。最常用的 8 个：

| Subcommand | 用例 |
|---|---|
| `quick` | "X 是谁" 点查 |
| `topic` | "X 的全过程" 主题展开 (人名禁用！见 hard rule) |
| `arc` | "我和 X 怎么认识" 关系演化 (人名首选) |
| `timeline` | "X 这段时间发生啥" 时序 |
| `person` | "Y 老师近况" 人物档 |
| `project` | "Z 项目跨源动态" |
| `pending` | "我有哪些待办" 主动 commitment |
| `reflect` | LLM 综合答案 |

完整用法: [docs/usage_guide.zh.md](docs/usage_guide.zh.md)

## 📖 Example walkthroughs — 5 个 reproducible 实例

> 装包 → `make demo-ingest` → 跟着 walkthrough 跑命令 → 看 memexa 实际怎么工作。
> 全程基于合成 demo dataset (Alice / Bob / Carol / advisor@example.com)，
> 任何人 1:1 复现，**没有任何真实个人数据**。

```
┌────────────────────────────────────────────────────────────────────┐
│                      你想回答的问题是…                              │
└────────────────────────────────────────────────────────────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
   "X 是谁?"           "上周组里在干嘛?"      "我这周要做啥?"
   01_who_is_alice     02_weekly_team        05_my_pending
   arc + quick          topic + trends        pending + quick
        │                    │                    │
        ▼                    ▼                    ▼
   "Y 老师要啥?"       "X 项目到哪了?"
   04_advisor_said     03_project_status
   person              project + timeline
```

**5 个 walkthrough** (每个 5-10 min 看完):

| # | Walkthrough | 触发场景 | 命令组合 |
|---|---|---|---|
| [01](examples/demo_dataset/walkthroughs/01_who_is_alice.zh.md) | Alice 是谁? | "我和 X 关系怎样" | `arc` + `quick` |
| [02](examples/demo_dataset/walkthroughs/02_weekly_team_summary.zh.md) | 组里上周干了啥 | "上周组里干了啥" | `topic` + `trends` |
| [03](examples/demo_dataset/walkthroughs/03_project_status_check.zh.md) | 项目到哪了 | "X 项目到哪了" | `project` + `timeline` |
| [04](examples/demo_dataset/walkthroughs/04_what_did_advisor_say.zh.md) | 导师/老板要啥 | "Y 老师/老板要啥" | `person` |
| [05](examples/demo_dataset/walkthroughs/05_my_pending_actions.zh.md) | 我有哪些待办 | "我有哪些待办" | `pending` + `quick` |

**2 篇 case study** (方法论，10-15 min 各看完):

| # | Case study | 适合谁 | 输出 |
|---|---|---|---|
| [01](docs/case_studies/01_lab_report_pipeline.zh.md) | 错过 ddl 补救流水线 | 错过 ddl 要交报告的人 | LaTeX → PDF + 行动卡 (20 min 端到端) |
| [02](docs/case_studies/02_meeting_brief_pattern.zh.md) | 5 分钟见面简报模板 | 见某人前要"脑暖"的人 | 4 段 Markdown brief (5 min 端到端) |

→ 索引页: [`examples/demo_dataset/walkthroughs/`](examples/demo_dataset/walkthroughs/README.zh.md) · [`docs/case_studies/`](docs/case_studies/README.zh.md)

## 两种跑 LLM 的方式

`memexa` 的核心是双 LLM 抽取 pipeline。OSS 完整自带，可以本地跑。

```bash
# 默认：OSS bundled prompt + 你自己的 LLM provider
#   你只要在 .env 设置 OpenAI / DeepSeek / 本地 vLLM 的 base_url + key
export MEMEXA_EXTRACTOR_TIER=bundled

# BYO：你自填 prompt（已有自己 prompt 工程的高级用户）
export MEMEXA_EXTRACTOR_TIER=byo
export MEMEXA_PROMPT_PATH=/path/to/your_prompts.py
```

**路线图**：v0.5 会上线一个可选的付费 API endpoint，
按 token 计费（OpenAI-style，无订阅）。这是个增值入口，
**不影响 OSS 完整可用**。详见 [docs/api_roadmap.zh.md](docs/api_roadmap.zh.md)。

## 文档索引

| 主题 | 链接 |
|---|---|
| 30 分钟首次跑通 | [docs/quickstart.zh.md](docs/quickstart.zh.md) |
| 架构设计 | [docs/architecture.zh.md](docs/architecture.zh.md) |
| 14 个查询命令详解 | [docs/usage_guide.zh.md](docs/usage_guide.zh.md) |
| 5-phase 状态推理 | [docs/5_phase_query.zh.md](docs/5_phase_query.zh.md) |
| 完整环境变量 | [docs/configuration.zh.md](docs/configuration.zh.md) |
| 常见问题 | [docs/faq.zh.md](docs/faq.zh.md) |
| 故障排查 | [docs/troubleshooting.zh.md](docs/troubleshooting.zh.md) |
| 性能数据 | [docs/performance.zh.md](docs/performance.zh.md) |
| 6 个 source 接入指南 | [docs/integrations/](docs/integrations/) |
| macOS / Windows / Linux 部署 | [docs/deployment/](docs/deployment/) |
| **Example walkthroughs (合成数据)** | [examples/demo_dataset/walkthroughs/](examples/demo_dataset/walkthroughs/README.zh.md) |
| **Case studies (方法论)** | [docs/case_studies/](docs/case_studies/README.zh.md) |
| **🤖 给 AI agent (协议文档)** | [docs/for_agents.zh.md](docs/for_agents.zh.md) |
| 付费 API endpoint (roadmap) | [docs/api_roadmap.zh.md](docs/api_roadmap.zh.md) |
| 工程踩坑总结 | [docs/lessons_learned/](docs/lessons_learned/) |
| 贡献指南 | [CONTRIBUTING.zh.md](CONTRIBUTING.zh.md) |
| 行为准则 | [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |
| 安全政策 | [SECURITY.zh.md](SECURITY.zh.md) |
| 治理 | [GOVERNANCE.md](GOVERNANCE.md) |
| Roadmap | [ROADMAP.zh.md](ROADMAP.zh.md) |
| Support | [SUPPORT.zh.md](SUPPORT.zh.md) |
| Citation | [CITATION.cff](CITATION.cff) |

## License

Apache 2.0. See [LICENSE](LICENSE).

OSS 内核 = Apache 2.0，无限制商用。可选的付费 API endpoint 当未来上线时
会有独立服务条款，见 [docs/api_roadmap.zh.md](docs/api_roadmap.zh.md)。
