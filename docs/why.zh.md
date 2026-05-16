# 为什么用 memexa

[English](why.md) · **中文**

本页收集设计意图、与相邻 OSS memory 项目的逐项能力对比、memexa 服务的
5 类用户场景。装机路径见 [`docs/quickstart.zh.md`](quickstart.zh.md)。
前瞻路线见 [`ROADMAP.zh.md`](../ROADMAP.zh.md)。

## 设计意图

memexa 摄入的中文数据 — 微信 / QQ / 飞书 / 钉钉 多人群聊、中文邮件长链、
中文音频 — 要求一种和西方 dev tool 通常构建的不一样的 memory layer。
三个核心属性:

1. **Verbatim 原始存储与结构化抽取并存**。群聊是异构的: 有些是笑话, 有
   些是关键承诺。前置摘要会丢掉之后才发现重要的信息。memexa 永远保留每
   条原始消息 verbatim, 同时产出一份 LLM 抽取的 envelope, 但不替换原文。

2. **每条断言可回溯到原句**。当 deliverable 模板写"Y 老师把组会改到
   周三下午 3 点"时, 用户能点进去看到原始消息: 谁在哪个群什么时候说的。
   这是每个 envelope 上的 `evidence_quotes` 字段。

3. **跨别名实体收敛**。一个人在中文群里会以 `@张三` / `张老师` /
   `zhangsan@gmail.com` / 实名签名 多种形式出现。memexa 用 4 阶段
   0-LLM 算法自动学习这些别名, 全部绑到一个 canonical id 上。

这三个属性 — verbatim + citation + canonicalization — 加在一起让系统
能在脏乱的多人中文数据上做高准确度检索, 这正是项目几乎所有目标用户的
真实用例。

## 与相邻项目的对比

2026 年 OSS memory 生态有 3 个其他主要项目, 潜在用户可能考虑:

- **OpenHuman** (`tinyhumansai/openhuman`): Rust + Tauri desktop
  assistant, 118+ 西方 SaaS 集成。
- **MemPalace** (`MemPalace/mempalace`): Python dev tool, verbatim
  存储 + benchmark 驱动的检索 (LongMemEval R@5 = 96.6 %)。
- **ReMe** (`agentscope-ai/ReMe`): agent memory 管理 kit, AgentScope
  生态出品。

下面是"为什么选 memexa" 的诚实回答:

| 能力 | OpenHuman | MemPalace | ReMe | **memexa** |
|---|---|---|---|---|
| 存储模式 | 3k-token markdown 层级摘要 | Verbatim 全文 + Zettelkasten 字面索引 | agent-context tool kit | **Verbatim 原始 + LLM 抽取 V2 envelope** |
| 多人群聊角色解析 | summary 折掉说话人角色 | 无 role 概念 | agent 层 | ✅ V2 envelope `roles[]` + `identity_assertions` |
| 每条断言可回溯原句 | summary 已折叠 | ✅ verbatim 直回 | agent 层 | ✅ `evidence_quotes` 把每条 claim 绑回原句 + `chunk_id` |
| 跨别名 canonical id | 只到人名, 不抽别名 | 无 entity 概念 | agent 层 | ✅ `identity_manifest` + 4 阶段 0-LLM 算法 |
| 中文相对时间解析 ("上周三") | 只看文档时间 | 无 | 无 | ✅ `time_resolutions` (ISO 8601 + 相对锚点) |
| 抽取幻觉控制 | 无 — 单次摘要 | 无 — 不抽取 | 无 | ✅ 双 LLM gate + extract + DeepSeek arbiter 仲裁 |
| 中文原生 IM 源 (微信 / QQ / 飞书 / 钉钉) | 无 (仅 Western OAuth) | 无 | 无 | ✅ 微信 + QQ 已 ship; 飞书 + 钉钉 在 v0.3 |

论点很直接: 层级摘要 (OpenHuman) 必然丢信息; 字面 Zettelkasten
(MemPalace) 无法在 10 人微信群聊里区分"谁对谁说"; agent-context kit
(ReMe) 在另一个层级。memexa 同时保留 verbatim 原始 + LLM 抽取的结构化
层 + 每条断言原文 citation, 并 ship 其他项目没理由建的中文 IM 摄入路径。

## 为什么 memexa 是 agent-first 的, 以及这为什么重要

对比表里没明说的第 2 个差异化: memexa 是名单里**唯一带有专门 agent
协议文档**的项目。14 个子命令 + [`for_agents.zh.md`](for_agents.zh.md)
的 7 条 hard rule 读起来像专门给 LLM 写的 API 契约 — 严格、密集、
明确写出 agent 常踩的 6 个坑 (对人名调 topic / 外层并行包 topic·arc /
把 pending 当语义召回 / 等)。

为什么这点重要: **memexa 典型用户不是手敲命令的人**, 是代用户调
memexa 的 AI agent (Claude Code / Cursor / Cline / 自写 agent)。
CLI 表面和 agent 协议是同 14 个子命令; agent 读协议, 人读 usage guide,
两者产出同样的调用。

这也是 v0.2 **ship markdown workflow spec** (`docs/templates/weekly.md`
等) 而**不是新增 CLI 子命令**的原因: agent 在运行时读 spec, 然后用
已有的 14 个子命令 orchestrate。用户加自己的模板 = 复制一份
markdown 文件, 不需要写 Python。

## 术语表

文档中反复出现的项目专属术语:

- **Verbatim 原始 + V2 envelope**: 每条摄入消息原样保存; 旁边再放
  一条独立的"V2 envelope" 记录, 装 LLM 抽取的结构化层 (narrative /
  entities / evidence_quotes / time_resolutions / identity_assertions /
  relation_assertions)。原始与抽取共存, 抽取永不替换原文。

- **反流 (reflow)**: 把上游开发中 LIVE ≥ 4 周无回退的能力, 移植到
  公开 memexa 仓。每次反流必先做 PII / 抽象 audit, 保证上游的真名 /
  学校 / 私有路径不漏到 OSS。当前规划中 2 个主反流 milestone =
  **中文 IM 反流 (v0.3)** 与 **音频 + 声纹反流 (v0.4)**。

- **中文 IM 反流** (v0.3): 反流 QQ db-only 适配器 (SQLCipher 直读) /
  本地文档源 (.md/.pdf/.docx/.txt + file-sha1 绑定) / Identity
  manifest 自学习算法 / WeChat PC 备份摄入路径, 加 2 个全新 适配器
  飞书 + 钉钉。目标: 中文 IM 覆盖从今日 WeChat+QQ 扩到 4 大主流 IM
  平台 + 本地文档。

- **音频 + 声纹反流** (v0.4): 反流 SenseVoice ASR 引擎 (中文 CER
  ~6.8%, 5× realtime, 替换 Whisper) / 跨 session voice manifest
  (基于 ECAPA 嵌入的 "自己 vs speaker N" 识别) / 多设备音频合并
  (录音笔 + iPhone + 课堂口录, 按内容指纹去重)。目标: 中文音频成为
  一等公民 source, 带说话人消歧, 不只是 transcript。

- **Workflow spec** (v0.2): `docs/templates/` 下的 Markdown 文档,
  描述 AI agent 应该如何 orchestrate 14 个查询子命令产出某个具体
  deliverable (周报 / 会前 brief / 时段复盘)。spec 在运行时被 agent
  读取 — ship 新 spec 不需要给 memexa 加 Python 代码。

## 用户场景

memexa 服务 5 类场景, 共用同一套底层 (ingestion + 抽取 + 图谱 + 查询),
只在上层 deliverable 模板不同。

| 场景 | 典型用户 | 主用 workflow (v0.2 +) |
|---|---|---|
| **知识工作者 / PM / 咨询** | 双语办公人士、自由职业 | `weekly` spec (跨源周报), `brief` spec (会前 brief, 针对人) |
| **研究者 / 学生 / 学者** | 在校生、研究生 | `brief` spec (答辩 / talk 准备, 针对主题), `retro` spec (项目复盘, 按时段) |
| **内容创作者 / 自媒体** | 公众号作者、知乎答主、小红书博主 | `retro` spec (灵感回收), v0.7 社区 spec 模板 |
| **中小企业主 / 个体户** | 自由专业人士、小工作室主 | `brief` spec (客户 / 潜客准备), `retro` spec (交易复盘) |
| **自我量化 / GTD / 隐私用户** | self-hosted 极客、GTD 实践者 | `memexa pending` 查询 (跨源待办), `retro` spec (周回顾) |

5 个场景都是一级公民。v0.2 ship 3 个 workflow spec (`weekly` /
`brief` / `retro`, 作为 `docs/templates/` 下的 markdown 文档) 覆盖
5 场景的最常见需求。Agent 在运行时读 spec, 跑 14 个查询子命令中相关的
那批, 合成带逐条 citation 的 Markdown 报告。v0.7 把同一套创作路径开
放给用户 — 新模板 = 新 markdown 文件, 不写 Python。

## V2 envelope 字段参考

开发者如果想理解 extractor 实际产出什么, 每张抽取卡含以下字段:

- `narrative`: 中文摘要, ≤ 200 字
- `entities`: `{surface, kind, canonical_id?}` 列表; `kind` 是
  `person` / `org` / `place` / `event` / `thing` 之一
- `evidence_quotes`: 来自原文的句子列表, 用来证明 `narrative` 中的断言。
  **每个 claim 必须有 ≥ 1 条 quote 支持**, 在抽取时强制
- `time_resolutions`: `{surface, iso_start, iso_end?, anchor?}` 列表;
  把相对时间 ("上周三下午"、"前天") 转成绝对 ISO 8601
- `identity_assertions`: `{surface_a, surface_b, confidence}` 列表;
  断言两个表面形式指向同一 canonical entity
- `relation_assertions`: `{subject, predicate, object, evidence}` 列表;
  抽取出的关系 ("Y 老师把 DDIA 第 5 章推荐给 Alice")
- `roles[]`: 每条消息的说话人角色 (sender / mentioned / audience),
  支持群聊说话人消歧
- `salience`: 0.0 - 1.0 重要度评分; 低于默认 `0.4` 阈值的卡不进
  `session-context` 注入
- `chunk_id` + `source_origin`: 指回原始 batch JSON 文件, 用户随时能查
  原始消息上下文

上面的论点是真实的因为这些字段每张卡都填; citation 不是愿景, 是 tested
invariant。
