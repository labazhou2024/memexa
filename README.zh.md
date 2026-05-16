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
> 上层"按用途自动生成可交付物"在 [ROADMAP.md](ROADMAP.md) v0.2，
> 以**三个通用模板** ship — `weekly` / `brief` / `retro` —
> 覆盖 5 类用户场景（知识工作者 / 研究者 / 创作者 / 中小企业主 / GTD）。
> v0.7 开放用户自定义模板, 交付层升级为生态。
> v0.1 阶段先用 14 个查询命令手动组合得到大致效果。
>
> 🚀 **第一次看？** 直接跳 [Example walkthroughs ↓](#-example-walkthroughs--5-个-reproducible-实例) 看 5 个真实场景怎么用。

> 🤖 **设计上就是 AI agent 兼容的**。绝大多数真实用户是让 AI agent
> (Claude Code / Cursor / Cline / 自己写的 agent) 替自己调用 memexa,
> 而不是手敲子命令。14 个查询子命令是一个小协议; 给 agent 的协议文档在
> [docs/for_agents.zh.md](docs/for_agents.zh.md) (硬规则 / 决策表 /
> 组合模式 / 常见坑)。如果你在做需要中文数据记忆层的 agent, 从那开始。

## 5 类用户场景 (面向中文整个市场的设计)

memexa 面向**整个中文市场**，不局限于某个单一群体。5 类场景共用同一套
底层（ingestion + 双 LLM 抽取 + 图谱 + 查询）；上层交付物模板按场景出
不同输出。v0.2 ship 3 个最通用的模板（`weekly` / `brief` / `retro`），
v0.7 开放用户自定义模板。

| 用户场景 | 触发情境 | 当下可用 (v0.1) | v0.2 交付物 |
|---|---|---|---|
| **知识工作者 / PM / 咨询** | 周报到期；明天有会 | 14 个 query 手动组合 | `memexa weekly` / `memexa brief <人>` |
| **研究者 / 学生 / 学者** | 实验做完 → 写报告；答辩准备 | `arc` + `quick` + `topic` | `memexa brief <主题>` / `memexa retro <时段>` |
| **内容创作者 / 自媒体** | 灵感回收；周素材整理 | `topic` + `timeline` | `memexa retro <时段>` (+ v0.7 社区模板) |
| **中小企业主 / 个体户** | 客户见面准备；交易复盘 | `arc` + `project` | `memexa brief <人>` / `memexa retro <时段>` |
| **自我量化 / GTD / 隐私用户** | 每日 / 每周回顾 | `memexa pending` | `memexa retro <时段>` |

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

## 为什么不直接用 OpenHuman 或 MemPalace？

memexa 摄入的中文数据（微信 / QQ / 飞书 / 钉钉 多人群聊、中文音频、
中文邮件长链）要求一种相邻 OSS memory 项目不提供的能力：

| 能力 | OpenHuman (Memory Tree) | MemPalace (Verbatim) | **memexa (V2 envelope)** |
|---|---|---|---|
| 存储模式 | 3k-token markdown 层级摘要 | Verbatim 全文 + Zettelkasten 字面索引 | **Verbatim 原始 + LLM 抽取的 narrative + entities + evidence_quotes + relations + time_resolutions** |
| 多人群聊角色解析（谁对谁说） | summary 折叠掉角色 | 无 role 概念, 只字面索引 | ✅ V2 envelope `roles[]` + `identity_assertions` |
| 每条断言可回溯到原文一句话 | summary 已折叠 | ✅ verbatim 直接回 | ✅ `evidence_quotes` 把每条 claim 绑回原句 + `chunk_id` + 原始 batch 路径 |
| 跨别名实体收敛（`@张三` / `张老师` / `zhangsan@...` → 一个 id） | 只到人名, 不抽别名 | 无 entity 概念 | ✅ `identity_manifest` + `canonical_id` (4 阶段 0-LLM 算法) |
| 中文相对时间解析（"上周三"、"前天下午"） | 只看文档时间, 不解析 | 无 | ✅ `time_resolutions` (ISO 8601 + 相对锚点) |
| 抽取幻觉控制 | 无 — 单次摘要 | 无 — 不抽取 | ✅ **双 LLM gate + extract + DeepSeek arbiter** 仲裁, schema 校验 |

**论点**: 层级摘要（OpenHuman）必然丢信息, 对高准确度任务不够;
字面 Zettelkasten（MemPalace）无法在 10 人微信群聊里区分"谁
在什么时候对谁说了什么"。memexa 的 V2 envelope 两者兼得 — 既存
verbatim 原始, 又保留 LLM 抽取的结构化层 + 每条断言的原文引用。
当 v0.2 交付模板引用一个事实时, 用户能秒级回到原句, 群聊说话者
在所有别名间被正确识别。

这是 memexa 真正占据的赛道。中文 IM 数据源（v0.3）是这条赛道
触达用户的方式; 交付模板（v0.2）是这条赛道兑现价值的方式。

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
