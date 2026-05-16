# 路线图

[English](ROADMAP.md) · **中文**

> 期望式表达。不是承诺。该动的时候才动。

## 定位

memexa 是面向中文原生多人数据的自托管 memory graph — 微信 / QQ /
飞书 / 钉钉 群聊、中文邮件长链、中文音频录音。每条消息按 verbatim
存储, 再由双 LLM 抽取流程产出结构化 envelope（narrative + entities
+ 每条断言对应的 evidence_quotes + time_resolutions +
relation_assertions）。查询跨 source, 返回带 citation 的 cards, 进入
deliverable 模板生成可直接交付的文档。

memexa 占据相邻 OSS 项目未覆盖的赛道: 中文原生数据源、高准确度
回溯到原句的 citation、面向人和 AI agent 双使用的 CLI 优先设计。

关于与相邻项目（OpenHuman、MemPalace、ReMe）的逐项能力对比, 以及
memexa 服务的 5 类用户场景, 见 [docs/why.zh.md](docs/why.zh.md)。

## 当前状态 (v0.1.0-rc2 已在 PyPI, 2026-05-16)

已 ship:

- CLI: `init` / `version` / `config` / `doctor` / `query`
- 14 个查询子命令 (基础 9 + 高级 5)
- 6 个摄入源: 微信、QQ、邮件、浏览、Claude Code、音频
- 双 LLM gate-extract 流程, DeepSeek arbiter 仲裁
- PostgreSQL + pgvector 后端 (docker compose 内含 Hindsight FastAPI)
- 端口 8765 dashboard, 7 个 panel
- macOS / Windows / Linux + Docker 三平台部署指南
- 8 个测试, 19 个 CI workflow 检查, CodeQL 0 open error
- Dependabot 漏洞 alerts 已启用, 自动安全修复已启用
- Fresh-clone smoke test 在 Win + macOS + Linux × Python 3.10 / 3.11 / 3.12 通过

未 ship:

- 一行命令 onboarding（无需 Docker, 无需 LLM API key, 无需配置）
- 自动生成可直接交付的 Markdown 文档的 deliverable 模板
- 微信 + QQ 以外的中文 IM 源（飞书 / 钉钉）
- 本地文档源 (.md / .pdf / .docx / .txt)
- 让用户不装 Docker 也能试用 1 个 source 的 embedded backend mode
- 直接 agent 接入的 MCP server entry point
- 可插拔后端（当前锁在 Hindsight FastAPI）

## v0.1.x — 收尾 stable

剩下的 v0.1 工作 = 同时打通**人类用户**和 **AI agent** 的第一体验路径。
v0.1.0 仅在以下所有条件全部为真时, 才从绿色 rc 中切版本。

- `memexa demo` 子命令: 30 秒 walkthrough, 用 bundled synthetic 数据
  + stub extractor。无需 Docker, 无需 LLM key, 无需配置。
- 14 个查询子命令全部加 `--json` 输出 mode, 让 agent 通过 shell 调
  memexa 时能直接 `json.loads()`, 不用 parse 文本。subprocess CLI 路径
  是当前 first-class agent integration; native MCP server 留 v0.5。
- `Makefile` 的 lint / format target 指向 `memexa tests`（而不是已废
  弃的 `src` 路径）。
- `CHANGELOG.md` 删除"PyPI 尚未上"那条 known-limitation（rc1 起已 LIVE）。
- 至少 1 个非作者的 issue / discussion / pull request 已 land。
- 自最近一个 critical bug fix 起至少经过 1 周。

## v0.2 — agent workflow 模板 (spec 文档, 非 Python 代码)

memexa 是 **agent backbone**, 不是 end-user product。典型流: 人 →
Claude Code / Cursor / Cline → subprocess CLI 调 memexa → 合成
Markdown 答案。v0.2 ship 3 份 workflow **spec 文档** 告诉 agent 如何
组合 14 个 subcmd 产出常见 deliverable —— **不写任何 Python 代码,
不加任何 CLI 子命令**。

- `docs/templates/weekly.md` — 跨源周报 workflow。
- `docs/templates/brief.md` — 见面 / 演讲前 brief workflow。
- `docs/templates/retro.md` — 时段复盘 workflow。
- `docs/templates/README.md` — spec 系统总览 + 用户自定义指南
  (加自己的模板 = 复制 spec 文件, 零代码; v0.7 正式开通提交通道)。
- 1 篇 case study 走查 Claude Code 在 demo dataset 上跑完整 weekly
  流程, 让新访客**看见**"agent + memexa 实际怎么交互"。

v0.2 在 weekly spec land 到 main + 1 篇 walkthrough 演示真 Claude
Code session 产出带 citation 周报 时 ship。

## v0.3 — 中文 IM 与身份解析深化

在上游开发中 LIVE ≥ 4 周无回退的能力, 在此 milestone 反流到 memexa。
每项需先做 PII / 抽象 audit 后才 merge。

- QQ db-only 适配器; 替换并删除 OSS 端 NapCat 路径。
- 本地文档源 (.md / .pdf / .docx / .txt, 用 file-sha1 绑定, 移动 /
  重命名不触发重抽)。
- Identity manifest 自学习, 跨别名实体收敛。
- 微信 PC 备份摄入。
- 飞书 (Lark) export 适配器。
- 钉钉 (DingTalk) export 适配器。
- Embedded backend mode (`memexa backend --embedded`): sqlite-vss 后端
  替代 docker-compose, 给只想接 1 个 source 的用户。

## v0.4 — 音频与声纹

- SenseVoice ASR (中文 CER ~6.8 %, 替换 Whisper)。
- 跨 session voice manifest, 基于 ECAPA 嵌入的说话人 enrollment。
- 多设备音频合并 (录音笔、iPhone 录音备忘、课堂口录)。
- `memexa 会议纪要 <session>`: 会议纪要 deliverable。

## v0.5 — AI agent integration 完整化

subprocess CLI 路径是 v0.1.x 的 first-class agent integration。v0.5
把它升级为 native MCP integration, 让 agent 不用 spawn shell 即可把
memexa 当结构化工具调。

- `memexa-mcp`: Model Context Protocol server entry point, 把 14 个查
  询子命令和 v0.2 的 workflow spec 一并暴露为 MCP tools。
- Claude Code / Cursor / Cline 官方 integration 示例落在
  `examples/agent_integrations/`, 含一行 `.mcp.json` snippet。
- `docs/for_agents.md` 升级到 v2, 覆盖 MCP spec / function-call 协议 /
  agent skill specification。

## v0.6 — 可插拔 LLM 与可插拔后端

memexa 停止在 backend 层与相邻项目竞争, federate 到它们作为用户选项。

- LLM provider 抽象: OpenAI / DeepSeek / Qwen / vLLM / Ollama /
  LiteLLM / OpenRouter / 自部署 OpenAI 兼容 endpoint 适配。
- 后端适配器: `memexa --backend=chroma|mem0|mempalace|hindsight`
  切换底层存储, 不动 query / deliverable 层。
- Schema drift sanitizer, 让 extractor 模型切换不破 DB insert。

## v0.7 — 用户自定义 workflow spec 模板

workflow 模板层变生态, 不再是固定 3 个。用户写自己的 spec 文档跟
v0.2 内置 spec 同一种格式 —— agent 在运行时读 Markdown。

- spec 加载机制: memexa 发现 `~/.memexa/templates/` 下的用户 spec 文件
  + bundled `docs/templates/`, agent 看到合并后的可用工具列表。
- 6 个示例社区 spec 模板落在 `examples/community_templates/`, 每个覆盖
  一类独立用户场景 (客户跟进 / 学习笔记 / 阅读简报 / 创作素材库 /
  每日复盘 / 答辩 brief)。
- spec 通过 pull request 提交进 `examples/community_templates/`。

加新模板**不需要写 Python 代码**; v0.7 = 文档化 schema + 提交通道。

## v0.8+ — desktop GUI (可选, 条件锚)

桌面 GUI 公认能扩大受众, 但**不在关键路径**。memexa 是 agent backbone
优先, 桌面 shell 只在底座稳后再叠。

开 v0.8 GUI 评估的条件:

- v0.5 MCP integration 已 ship, 至少 1 个知名 AI agent (Claude Code /
  Cursor / Cline) 社区文档 pin 了 memexa server 配置 snippet。
- v0.7 社区 spec 模板 ≥ 5 个 merged。
- 约 3 000 GitHub stars + 5 000 PyPI 月下载。

条件满足后, 开评估 PR 选其一:

- (a) Tauri shell 包装 CLI 作 backend。
- (b) Streamlit 本地 web UI。
- (c) 保持终端 + agent only。

本 milestone **不**是承诺工作; 项目可永久 terminal-only 仍达目标。

## v1.0 — schema 稳定承诺

- V2 envelope 冻结; 迁移只能加 field。
- CLI 参数冻结; 弃用前提前一版警告。
- On-disk layout 冻结; 改动 = bump major version。
- 后端适配器接口冻结。
- ≥ 3 个外部 contributor 有 merged pull request。
- ≥ 5 个非作者生产用户在 case studies 或 community templates 中留下案例。

## 永久不做

- Desktop GUI 应用。
- 泛 Western-SaaS OAuth 集成 (Gmail / Slack / Notion / Linear /
  Jira 通过 Composio 式 gateway)。
- Mobile / web UI 重写。
- 多租户 hosted service。
- 语音合成或 autonomous agent loop。
- 任何阻止用户拥有自己数据的设计。

## 提议路线图变更

在 **Ideas** 类目开 Discussion, 写: 加什么 / 砍什么 / 落哪个 milestone /
你是否愿意实现。维护者一段话回 yes / no / later。
