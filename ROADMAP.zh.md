# 路线图

[English](ROADMAP.md) · **中文**

> 期望式表达。不是承诺。该动的时候才动。

## 定位 (2026-05-16 修订)

**项目目标**: 成为**国内中文原生多人数据 memory-graph OSS 第一**。

memexa 面向**整个中文市场**，不限定单一群体。**两条复合护城河**：

1. **中文原生数据源**: 微信 / QQ / 飞书 / 钉钉 多人群聊、中文音频、
   中文邮件长链 — OpenHuman / MemPalace 的西方 SaaS OAuth + 摘要 /
   verbatim 模型处理不好这些。
2. **V2 envelope 抽取 + 每条断言原文引用**: verbatim 原始 + LLM
   抽取 narrative + `evidence_quotes` + `identity_assertions` +
   `time_resolutions` + `relation_assertions`。层级摘要（OpenHuman）
   必然丢信息；字面 Zettelkasten（MemPalace）无法在群聊里区分"谁
   对谁说"。memexa 两层兼得 + 每条 claim 绑回原句。

相邻 OSS（OpenHuman、MemPalace、ReMe）覆盖英文 / Western-SaaS /
desktop-assistant / dev-tool-MCP 赛道。memexa 主动留在中文原生 +
高准确度 citation 赛道。

**memexa 服务的 5 类用户场景：**

| 场景 | 典型用户 | 主用 deliverable |
|---|---|---|
| 知识工作者 / PM / 咨询 | 双语办公人士、自由职业 | `weekly` (跨源周报), `brief <人>` (会前 brief) |
| 研究者 / 学生 / 学者 | 在校生、研究生 | `brief <主题>` (答辩 / talk 准备), `retro <时段>` (项目复盘) |
| 内容创作者 / 自媒体 | 公众号作者、知乎答主、小红书博主 | `retro <时段>` (灵感回收), v0.7 自定义 `notebook` 类模板 |
| 中小企业主 / 个体户 | 自由专业人士、小工作室主 | `brief <人>` (客户 / 潜客准备), `retro <时段>` (交易复盘) |
| 自我量化 / GTD / 隐私用户 | self-hosted 极客、GTD 实践者 | `pending` (跨源待办), `retro <时段>` (周回顾) |

5 个场景共用同一套底层（ingestion + 抽取 + 图谱 + 查询 + 交付模板）。
模板有差异；v0.2 ship 3 个最广义的（`weekly` / `brief` / `retro`），
v0.7 让用户自定义自己的模板。

---

## v0.1.x — 收尾 stable (1-2 周)

- [x] CLI dispatcher (`memexa init / version / config / doctor / query`)
- [x] PII 脱敏 pre-commit hook
- [x] Demo 数据集 (6 source, 公有领域)
- [x] 直接 psycopg2 PG 访问
- [x] Hindsight failover URL 自动重试
- [x] 14 个查询子命令文档化
- [x] `memexa doctor` 端到端检 LLM provider
- [x] PII 残留扫描器 + 自引用 SKIP-list
- [x] **CodeQL: 7 个 open error → 0** (PR #12, 2026-05-15)
- [x] **Dependabot 漏洞 alerts 已启用 + 自动安全修复已启用**
- [x] **Fresh-clone smoke test 在 Win + macOS + Linux × Python 3.10/3.11/3.12 全 CI 通过** (PR #12 18/18 绿验证)
- [ ] CHANGELOG 中 "PyPI 还没上" 这条 known-limitation 改成 LIVE on PyPI
- [ ] ≥ 1 周无 critical bug + ≥ 1 个非作者 issue / discussion → 发 v0.1.0 stable

## v0.2 — 3 个通用 deliverable 模板 (4-8 周)

核心查询系统给原始信号。v0.2 把它们缝成可复制粘贴的文档。覆盖 5 个用户
场景的**3 个最广义模板**（不是 5 个独立模板）。每个模板 = 一个子命令
+ Markdown 布局 + 几个 `memory_query` 调用。

- [ ] `memexa weekly` — 跨源周报
  (git log + 邮件 + IM 摘要 + 项目脉搏 → 一页 Markdown)
- [ ] `memexa brief <人 | 主题>` — 见面 / 演讲 / 客户来电前 brief
  (基线 / 上次互动 / 未结清话题 / 雷区)
- [ ] `memexa retro <时段>` — 时段复盘
  (重要事件 / 已闭环承诺 / 未闭环承诺 / 意外)
- [ ] 模板引擎：共享 Markdown 布局 + LLM provider 抽象层
  (这样 v0.7 用户自定义模板能直接插入同一管道)
- [ ] 3 篇可复现 walkthrough 落在 `examples/deliverables/` —
  每个用户场景一篇（知识工作者 / 研究者 / 自由职业）

**从早期草案砍掉**: `lab-report` / `action-card` / `dashboard`
（学生场景太窄，被 `retro` 模板的通用 pattern 吸收）

## v0.3 — 中文 IM + 身份解析深化 (反流 JARVIS, 4-6 周)

JARVIS 上游已 LIVE 数月，OSS 反流是下一波推进重点。每项注明对应的
JARVIS HANDOFF 节点，证明能力已经过生产验证。

- [ ] **QQ db-only 适配器** — 把 `jarvis/qq_db.py` (762 行, 仅标准库)
      移植到 `memexa/extraction/qq/qq_db.py`；删 OSS 端 NapCat 适配器
      _(JARVIS §C.-24 LIVE 2026-05-15)_
- [ ] **doc source = 第 7 个 source** — 本地文档入图
      (.md / .pdf / .docx / .txt), file_sha1 绑定不绑 path,
      移动 / 重命名不触发重抽
      _(JARVIS §C.-22 → §C.-26 buildup, v0.5 LIVE 2026-05-15)_
- [ ] **Identity manifest 自学习** — 跨别名实体收敛
      (`@张三` / `张老师` / `zhangsan@example.com` → 一个 canonical id),
      4 阶段 0-LLM 算法
      _(JARVIS USAGE_MANUAL §19, LIVE 2026-05-10)_
- [ ] **微信 PC 备份摄入** — 不只 live MicroMsg.db，
      支持 PC 微信备份目录 (被锁定设备的用户也能用)
- [ ] **飞书 (Lark) export 适配器** — 个人账号 JSON 导出
- [ ] **钉钉 (DingTalk) export 适配器** — 聊天导出
- [ ] **从早期草案砍掉**: Discord / Slack / Telegram / iMessage —
      OpenHuman 通过 Composio OAuth 占了 Western-SaaS 赛道，不是我们的市场

## v0.4 — 音频 + 声纹中文场景增强 (反流 JARVIS, 3-5 周)

- [ ] **SenseVoice ASR 反流** — JARVIS audio v2 用 SenseVoice 替换
      Whisper：中文 CER 6.8% (vs Whisper ~10%), 5× realtime,
      消除中文音频英文 hallucination
      _(JARVIS §C.-23/-28 LIVE 2026-05-15/16)_
- [ ] **跨 session voice manifest** — ECAPA 嵌入跨 session 投票 +
      enroll-user-voice 工作流; 跨录音识别"自己"vs "speaker N"
- [ ] **多设备音频合并** — 录音笔 (USB-MSC) + iPhone 录音备忘 +
      课堂 / 会议口录, 按内容指纹去重
- [ ] `memexa 会议纪要 <session>` — 从录音自动抽行动项 + 关键决策
      (结合 audio source + v0.2 的 `brief` 模板)

## v0.5 — AI agent integration 升级到一级公民 (3-4 周)

- [ ] **`memexa-mcp` MCP server entry-point** — 官方 Model Context
      Protocol server, 让 Claude Code / Cursor / Cline / 任何 MCP 兼容
      agent 把 memexa 当 memory backend 调
- [ ] **官方 `.mcp.json` 模板** 落在 `examples/agent_integrations/`
- [ ] **Cursor / Cline integration docs** — 一步步教
- [ ] **`docs/for_agents.md` v2** — 覆盖 MCP spec / function-call 协议 /
      agent skill spec
- [ ] **Cron + dashboard 反流** — Win schtask + Mac LaunchAgent +
      Linux systemd 模板, 6 小时增量 cron, sys_monitor dashboard
      (端口 8765, 7 个面板)
      _(JARVIS HANDOFF §E LIVE; Mac failover wrappers §C.-29 LIVE 2026-05-15)_

## v0.6 — Pluggable LLM + Pluggable Backend (4-6 周)

关键战略动作: **memexa 停止在 backend 层与 mem0 / MemPalace 竞争,
我们 federate 到它们作为用户选项。**

- [ ] **LLM provider 抽象** — 适配 OpenAI / DeepSeek / Qwen3 / vLLM /
      Ollama / LiteLLM proxy / OpenRouter / 自部署 OpenAI 兼容 endpoint
- [ ] **Backend adapter** — `memexa --backend=chroma|mem0|mempalace|hindsight`
      切换底层存储, 不动 query / deliverable 层
- [ ] **Schema drift sanitize 反流** — JARVIS §C.-29 §10 的
      `_normalize_llm_card` 覆盖 5 类新 drift (date-only ISO / time-only ISO
      / role=relay / related_episode dict / when_end None);
      反流让 OSS 切换 extractor 模型时不破 PG insert
- [ ] **5 driver rc=2 graceful-skip 模式反流** — JARVIS §C.-29 §2
      LIVE 2026-05-15; backend 临时不可达不再污染 cron

## v0.7 — 用户自定义 deliverable 模板 (4 周)

交付层升级为生态, 不再是固定 3 个。

- [ ] **模板创作 spec** — `~/.memexa/templates/<name>.yaml`
      声明输入 (调哪些 subcmd) / Markdown / LaTeX 布局 / 可选 LLM 渲染步
- [ ] **6 个示例用户自定义模板** (每个场景一个):
      - 客户跟进 (中小企业主)
      - 学习笔记 (研究者 / 学生)
      - 阅读简报 (知识工作者)
      - 创作素材库 (内容创作者)
      - 每日复盘 (GTD / 自我量化)
      - 答辩 brief (研究者 / 学生)
- [ ] **模板提交 contrib 通道** — 社区模板通过 PR 进
      `examples/community_templates/`, 不内置

## v1.0 — schema 稳定承诺 + 生态形成 (≥ 6 个月之后)

- [ ] V2 envelope 冻结; 迁移只能加 field
- [ ] CLI 参数冻结; 弃用前提前一版警告
- [ ] On-disk 布局冻结; 改动 = bump major version
- [ ] Backend adapter 接口冻结
- [ ] ≥ 4 个交付模板 LIVE (3 内建 + ≥ 1 社区)
- [ ] ≥ 4 个中文 IM source LIVE (微信 / QQ / 飞书 / 钉钉)
- [ ] ≥ 3 个外部 contributor merge PR
- [ ] ≥ 5 个真实生产用户 (非作者) 在 `docs/case_studies/` 或
      `examples/community_templates/` 留下案例

## 永久不做 (2026-05-16 扩充)

- ❌ **Desktop GUI** — OpenHuman 通过 Tauri + 118 OAuth 已占
- ❌ **泛 Western-SaaS OAuth** — Gmail / Slack / Notion / Linear /
      Jira 通过 Composio = OpenHuman 护城河, 不追
- ❌ **Mobile / web UI 重写**
- ❌ **多租户 hosted service**
- ❌ **语音合成 / agent loop** — 不同项目类别
- ❌ **英文单线 benchmark 战** (LongMemEval / LoCoMo / MemBench
      在英文 ChatGPT-style 单线对话上) — MemPalace 占。memexa **会**
      发自己的 benchmark, 但在中文原生多人数据上: 群聊说话者
      消歧 / 跨别名实体收敛精度 / 相对时间锚点正确性 /
      `evidence_quotes` 回溯原句精度。目标: v0.3 落
      `benchmarks/cn_multiparty/` 含 5 个可复现的中文微信 / QQ 场景
- ❌ **任何阻止你拥有自己数据的设计**

## 提议路线图改动

在 **Ideas** 类别开个 Discussion, 写:

- 加什么 / 砍什么
- 落哪个 milestone
- 你是否愿意实现
- 服务哪个用户场景 (知识工作者 / 研究者 / 创作者 / 中小企业主 /
  自我量化 — 或新场景)

BDFL 会一段话回 yes / no / later。

---

## 并行执行任务划分 (快速迭代 / 多 contributor 协作)

多个 contributor 可并行做独立 task。每个 unit = 1 个 feature branch +
1 个 PR + 隔离的 test scope。下面是 v0.2 / v0.3 / v0.4 拆分，
3-5 个 contributor 能同时推进不冲突。

### v0.2 — 3 个交付模板 (3 条并行 track)

| Track | Owner | Branch | 涉及文件 (隔离) |
|---|---|---|---|
| **A. `memexa weekly`** | contributor #1 | `feat/v0.2-weekly-template` | `memexa/deliverables/weekly.py` (新) + `tests/integration/test_weekly_deliverable.py` (新) + `examples/deliverables/01_weekly_knowledge_worker.md` (新) |
| **B. `memexa brief <人\|主题>`** | contributor #2 | `feat/v0.2-brief-template` | `memexa/deliverables/brief.py` (新) + `tests/...test_brief_deliverable.py` (新) + `examples/deliverables/02_brief_researcher.md` (新) |
| **C. `memexa retro <时段>`** | contributor #3 | `feat/v0.2-retro-template` | `memexa/deliverables/retro.py` (新) + `tests/...test_retro_deliverable.py` (新) + `examples/deliverables/03_retro_freelancer.md` (新) |
| **D. 共享模板引擎** | 维护者 | `feat/v0.2-template-engine` | `memexa/deliverables/__init__.py` (新) + `memexa/deliverables/_base.py` (新) + `memexa/deliverables/_provider.py` (LLM provider 抽象) — **先 ship, A/B/C 依赖** |
| **E. CLI 路由 + docs** | 维护者 | `feat/v0.2-cli-routes` | `memexa/cli/main.py` (加 `weekly`/`brief`/`retro` subcmd) + `docs/usage_guide.md` + `docs/usage_guide.zh.md` |

### v0.3 — 中文 IM + identity 反流 (5 条并行 track)

| Track | Owner | Branch | 状态 |
|---|---|---|---|
| **F. QQ db-only 适配器** | contributor / 维护者 | `feat/v0.3-qq-db-only` | 反流 JARVIS `jarvis/qq_db.py` 762 行 |
| **G. doc source 第 7 个** | contributor / 维护者 | `feat/v0.3-doc-source` | 反流 JARVIS doc-source v0.5 LIVE 2026-05-15 |
| **H. identity manifest** | contributor / 维护者 | `feat/v0.3-identity-manifest` | 反流 JARVIS USAGE_MANUAL §19 |
| **I. 飞书 (Lark) export 适配器** | contributor | `feat/v0.3-lark-adapter` | 全新 (无上游反流) |
| **J. 钉钉 (DingTalk) export 适配器** | contributor | `feat/v0.3-dingtalk-adapter` | 全新 |
| **K. benchmarks/cn_multiparty/** | 维护者 | `feat/v0.3-cn-benchmark-suite` | 全新 — 5 个可复现微信 / QQ 场景 |

### v0.4 — 音频 + 声纹 (3 条并行 track)

| Track | Owner | Branch |
|---|---|---|
| **L. SenseVoice ASR 反流** | contributor | `feat/v0.4-sensevoice-asr` |
| **M. 跨 session voice manifest** | contributor | `feat/v0.4-voice-manifest` |
| **N. `memexa 会议纪要 <session>`** | contributor | `feat/v0.4-meeting-summary` |

### Contributor onboarding

≥ 1 contributor 上线时, 维护者每个 release 开一个 "v0.X coordination"
Discussion thread; track A–N 各自 file 为独立 issue, 标
`good-first-issue` 或 `help-wanted`。每个 track 自带 `tests/...`
隔离 scope, 并行 PR 不互踩。

---

## 上游 JARVIS 反流状态 (LIVE 能力清单)

| JARVIS LIVE 能力 | OSS 反流目标 | 状态 |
|---|---|---|
| 6 source (WeChat / QQ / Email / Browser / Claude Code / Audio) | v0.1 | ✅ 已 ship |
| 14 查询 subcmd (基础 9 + 高级 5) | v0.1 | ✅ 已 ship |
| Streaming POST + verify + dead-letter retry | v0.1 | ✅ 已 ship |
| Doc source (file_sha1 绑定, .md/.pdf/.docx/.txt) | **v0.3** | 反流待办 |
| QQ db-only (NapCat → SQLCipher 直读) | **v0.3** | 反流待办 |
| Identity manifest 跨别名收敛 | **v0.3** | 反流待办 |
| SenseVoice ASR + voice enroll | **v0.4** | 反流待办 |
| MCP server entry-point | **v0.5** | OSS 新建 |
| Schema drift sanitize (5 类) | **v0.6** | 反流待办 |
| 5 driver rc=2 graceful-skip pattern | **v0.6** | 反流待办 |
| Mac failover wrappers (PG stale-lock + hindsight pg_isready) | v0.5 docs | 反流待办 |
| Win cron + Mac LaunchAgent + sys_monitor dashboard | **v0.5** | 反流待办 |

JARVIS 上游保持实验边界；OSS 反流在能力在 JARVIS LIVE ≥ 4 周无回退后
才做。这样 OSS 用户拿到的是真实生产负载验证过的能力。
