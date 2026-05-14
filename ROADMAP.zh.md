# 路线图

[English](ROADMAP.md) · **中文**

> 期望式表达。不是承诺。该动的时候才动。

## v0.1.x — 让 demo 在干净盒子上端到端跑

- [x] CLI dispatcher (`memexa init / version / config / doctor / query`)
- [x] PII 脱敏 pre-commit hook
- [x] Demo 数据集 (6 source, 公有领域)
- [x] 直接 psycopg2 PG 访问 (默认不走 ssh shell-out)
- [x] Hindsight failover URL 自动重试
- [x] 14 个查询子命令文档化
- [x] `memexa doctor` 端到端检 LLM provider
- [x] PII 残留扫描器 + 自引用 SKIP-list
- [ ] CI 上 Win + macOS + Linux 的 fresh-clone smoke test 通过

## v0.2 — 可交付物模板层 (头号用户面 push)

核心查询系统给原始信号。v0.2 把它们缝成可复制粘贴或打印的文档。每个
模板 = 一个子命令 + 一份 Markdown/LaTeX 布局 + 底下两三个 `memory_query`
调用。

- [ ] `memexa lab-report <实验名>` — 课前报告 (LaTeX → PDF, 含官方讲义
      WebSearch fallback)
- [ ] `memexa weekly-report` — git log + session-end narrative + project
      跨源 summary → 一页 Markdown
- [ ] `memexa action-card <ddl>` — 出门 checklist + Q&A 速答表
- [ ] `memexa brief <人>` — 见面前 brief (基线 / 上次联系 / 开放话题 /
      雷区), 建在 `arc` + `quick` 上
- [ ] `memexa dashboard` — 截止面板, `pending` 的 4 栏 triage

## v0.2 — QQ db-only 适配器移植 (用户影响最大的待办)

- [ ] 把 `jarvis/qq_db.py` (762 行, 仅标准库) 从上游 JARVIS 移植到 `src/extraction/qq/qq_db.py`
- [ ] 同样移植 `jarvis/qq_reader.py` (剪贴板兜底)
- [ ] 把两个 reader 接进 `backfill_v5_qq_driver.py`，让 `--mode dump` 和 `--mode clipboard` 开箱可用
- [ ] 加 SQLCipher synthetic fixture smoke 测试 (不需要真 QQ)
- [ ] 把 OSS 端 NapCat / OneBot 适配器从代码树删掉 (当前还藏在 `MEMEXA_QQ_NAPCAT_FORCE=1` 后面)

## v0.2 — Linux 一级公民 (平行 track)

- [ ] systemd unit 模板给 6 小时 cron 和 dashboard
- [ ] Nix flake (欢迎社区贡献)
- [ ] Docker image 发布到 ghcr.io
- [ ] Headless 摄入模式 (不需要 dashboard server)

## v0.3 — 可插拔 LLM provider

- [ ] 内置 Ollama / vLLM / LiteLLM proxy / OpenRouter 适配器
- [ ] 适配器测试套件用合成 batch 打每个 provider
- [ ] Gmail / Outlook IMAP 的 OAuth2 device-code 认证

## v0.4 — 新 source

- [ ] Discord export
- [ ] Slack export
- [ ] Telegram export
- [ ] iMessage SQLite

## v0.5 — 可观测性 + 可靠性

- [ ] Dashboard server 的 Prometheus `/metrics` endpoint
- [ ] Per-driver SLO dashboard
- [ ] 自动化夜间 recall regression 套件
- [ ] 被遗忘权 CLI (`memexa forget <canonical-id>`)

## v1.0 — schema 稳定承诺

- [ ] V2 envelope 冻结; 迁移只能加 field
- [ ] CLI 参数冻结; 弃用只能提前一版警告
- [ ] On-disk 布局冻结; 改动 = bump major version
- [ ] 所有部署指南被 CI smoke test 覆盖

## 永久不做

- Web / mobile UI 重写
- 多租户 hosted service
- 语音合成 / agent loop
- 任何阻碍你拥有自己数据的东西

## 怎么提路线图变更

在 **Ideas** 类目开一个 Discussion, 写:

- 你会加什么 / 删什么
- 适合哪个 milestone
- 你愿意实现吗

BDFL 会答 yes / no / later, 附一段话理由。
