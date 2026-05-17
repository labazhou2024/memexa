# 更新日志

本文件记录本项目所有值得注意的变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/spec/v2.0.0.html)。

**英文版本**: [CHANGELOG.md](CHANGELOG.md)（权威源）。本文件是中文镜像。

## [Unreleased]

合并 rc4 发布后审计发现的 rc5 say-do gap 修复。最终版本号待定，
正式发布时本节会改为对应版本号。

### Added (新增)

- **`--json` 在子命令级被接受**: agent 现在可以按 README / quickstart
  文档写法 `memexa quick "X" --json`，不必被迫用
  `memexa --json query quick "X"`。`--json` flag 挂在 `_common` parent
  parser 上，14 个子命令全部继承，所以三种位置都工作:
  `memexa --json query quick "X"` / `memexa query quick "X" --json` /
  `memexa quick "X" --json`。修补了 rc4 "14 个子命令 --json 模式"
  的承诺缺口 — rc4 审计发现子命令级用 --json 会报
  `unrecognized arguments: --json`。

### Changed (变更)

- **`docs/quickstart.md` (+ zh)** Tier 0 期望 demo 输出改写为 Python
  3.11 venv 真实跑出来的结果: `(audio=1, browser_session=10,
  claude_code=3, email=4, qq=3, wechat=5)`, 总数 26 cards。之前
  写的 `(wechat=8, qq=4, email=4, browser=4, claude=3, audio=3)`
  既每个 source 计数错, 又用非规范命名 (`browser`/`claude` vs
  实际 `browser_session`/`claude_code`)。
- **`docs/quickstart.md` (+ zh)** macOS Python 3.9 缺口写在 install
  命令上方作为显式 warning。macOS 系统自带 Python 是 3.9, 低于项目
  最低要求 3.10, 之前 `pip install --pre memexa` 在每台未动过的
  macOS 上都会静默失败 ("Could not find a version that satisfies
  the requirement memexa")。用户现在在 docs 页就能撞上这条要求,
  有 `brew install python@3.11` + `venv` 操作指引。
- **`ROADMAP.md` (+ zh)** 当前状态 header 从 `(v0.1.0-rc2)` 推进到
  `(v0.1.0-rc4)`。Shipped 清单重写: CLI 列表加 `demo`,
  "Eight tests, nineteen CI workflow checks" 改成
  "Ten tests, 六个 CI workflow (lint / test / codeql / security /
  release-drafter / dependabot)" 让计数走 workflow 命名 (rarely
  edited) 而不是 job 数 (易漂), Linux 部署措辞改对 (没有 Linux
  原生 guide; 用户走 docker-compose 路径), 14 子命令拆分从
  "9 基础 + 5 高级" 改成 "7 基础 + 7 高级" 与代码一致。

### Fixed (修复)

- **`memexa quick "X"` 等命令在 backend 不可达时退出码改为 1 + 英文
  stderr 提示**, 不再静默返回 `N=0` + exit 0。Agent subprocess 调
  memexa 依赖 exit code 区分 "无结果" vs "你的 invocation 完全没用
  因为 backend 挂了"。`--json` 模式 stdout 仍然 print `[]` (JSON
  parser 不破), 但同时 exit 1 + stderr 单行提示。旧的 `logger.warning`
  泄漏 GBK 本地化 Windows 系统错误 (`[WinError 10061] 由于目标计算机
  积极拒绝, 无法连接`, 非中文 Windows shell 看不懂) 降级为
  `logger.debug`; 控制台现在只看到纯 ASCII 多行提示, 含 3 个下一步
  操作命令 (`make backend-up`, `MEMEXA_HINDSIGHT_URL=...`,
  `memexa doctor`)。

### Removed (移除)

- 删除重复的 `[0.1.0-rc1]` entry — rc1→rc2 文件重写时不小心写了两遍
  (旧文件 line 127-154)。

## [0.1.0-rc4] — 2026-05-16

rc3 之上的发布管道正确性修复 (PR #16)。

### Fixed (修复)

- **Demo 数据集打进 wheel**: `[tool.setuptools.package-data]` 现在
  包含 `examples/demo_dataset/*.json` 和 `*.jsonl`, 所以
  `memexa demo` 在新 `pip install --pre memexa` 上能跑, 不需要 clone
  源码树。之前打包的 JSON fixture 只在源码树里有, PyPI 装的话 demo
  报缺数据文件错。
- **动态 `__version__`**: `memexa/__init__.py` 现在通过
  `importlib.metadata.version()` 从 package metadata 读版本, 所以
  `memexa version` 输出始终和 pip 解析的 wheel 一致。之前 hard-coded
  `__version__` 滞后 `pyproject.toml`, 多次 rc bump 之间会漂。

## [0.1.0-rc3] — 2026-05-16

Agent-first 品牌整合 + 首次访问者引导路径 (PR #14 / PR #15)。

### Added (新增)

- **`memexa demo` 子命令**: 30 秒新手引导, 跑内置合成数据集 (stub
  extractor) + 5 个示例查询 (`quick` / `arc` / `timeline` /
  `pending` / `topic`)。无需 Docker, 无需 LLM API key, 无需任何配置。
  设计为首次访问者路径; README quickstart 中宣传。
- **14 个查询子命令的 `--json` 输出模式**。顶层 flag
  (`memexa --json query quick "X"`) 短路文本渲染, 把原始返回值
  (list 或 dict) 当作单个 JSON document 打到 stdout。这是 agent
  (Claude Code, Cursor, Cline) 通过 shell subprocess 调 memexa 的
  结构化输出路径 — 直到 v0.5 上原生 MCP server 之前的一线 agent
  集成方式。(rc5 跟进: 子命令级也接受 --json。)
- **`docs/why.md` + `docs/why.zh.md`**: OpenHuman / MemPalace / ReMe
  逐项能力对比, agent-first 设计动机, 项目专有术语 glossary
  (verbatim raw + V2 envelope; reflow; Chinese-IM reflow; audio +
  voice reflow; workflow spec)。
- **`docs/cost.md` + `docs/cost.zh.md`**: DeepSeek (V4 Flash / Pro)、
  GPT-4o、Claude 4.x 的 API 调用量与成本估算, 三层用户画像 + 中文
  workload 推荐模型组合。

### Changed (变更)

- **项目定位**: 明确为 **agent backbone** — 主要用户是 AI agent
  (Claude Code / Cursor / Cline) 代表 human 用户把 memexa 当
  subprocess 调。14 子命令 + `docs/for_agents.md` 7 条 hard rule
  构成 agent 契约。human 直接 CLI 用也支持, 但是次要路径。
- **README 第一屏**: 定位句和 "AI-agent compatible by design" 段落
  恢复; Quickstart 现在有两节 (human 30 秒视觉演示, agent subprocess
  + `--json`); 文档索引更新。
- **`docs/quickstart.md`**: Tier 0 现在有两条路径 — human 跑
  `memexa demo`, agent 用 `--json` 调子命令。Tier 1 / Tier 2 不变。
- **`ROADMAP.md`**: v0.2 重定义, 从 "Python deliverable 代码 + CLI
  子命令" 改成 **`docs/templates/` 下的 Markdown workflow spec**。
  Agent runtime 读 spec; user 复制 markdown 文件添加自己的 spec。
  v0.5 把 subprocess 路径升级为原生 MCP server。v0.7 把
  user-authored workflow spec 正式化。新增 v0.8+ 段落预留可选桌面
  GUI 探索, 门槛是 v0.5 / v0.7 成功条件。
- **`Makefile`**: `fmt` 和 `lint` 目标改为指向 `memexa tests` 而
  不是已弃用的 `src tests` 路径 (PR #9 `src/`→`memexa/` 重命名
  残留)。

### Fixed (修复)

- CodeQL: 7 个 error 级 alert 修掉 (uninit local var in
  `mlx_lm_wrapper.py` and `mini_loop_pretool_hook.py`, unused loop
  var in 2 个 `l0_worker_v2_*.py`)。866 个 note 和 warning 级 alert
  dismissed 为已知 alpha 阶段技术债 (intentional 优雅降级模式;
  排队 v0.2 全 ruff 扫)。Open code-scanning alert 现在为零。

### Security (安全)

- 仓库级启用 Dependabot 漏洞 alert 和自动安全修复。

## [0.1.0-rc2] — 2026-05-14

rc1 之上的安装 bug 修复。

### Fixed (修复)

- 包布局: `src/` 改名 `memexa/` 与安装 import 路径一致;
  PR #9 + CI 跟进 PR #10。

## [0.1.0-rc1] — 2026-05-14

首个公开 release candidate。单 orphan commit。开放 feedback,
等切 v0.1.0 stable 之前的窗口期。

### Added (新增)

- 6 个摄入源: WeChat, QQ, email, browser, Claude Code, audio。
- 双 LLM gate-extract 管线 (gatekeeper + extractor + BGE-M3
  quorum + DeepSeek arbiter)。
- PostgreSQL + pgvector 后端经 Hindsight FastAPI。
- 14 查询子命令 + 五阶段状态推断 workflow。
- 端口 `:8765` 实时 dashboard。
- Cron 编排, dead-letter 重试, PG-aware pending。
- Windows / macOS / Linux 部署指南。
- 5 个 demo dataset walk-through
  (`examples/demo_dataset/walkthroughs/`)。
- 2 个端到端 case study (`docs/case_studies/`)。
- AI-agent 协议文档 (`docs/for_agents.md`) — hard rule、决策表、
  组合模式、常见陷阱。
- 每个 user-facing doc 的完整中文镜像 (`*.zh.md`)。

### Security (安全)

- PII 清扫 pre-commit hook
  (`scripts/pre-commit-pii-scan.sh`)。
- 全树 PII 残留扫描带自指 SKIP 列表
  (`scripts/full_pii_scan.sh`)。
- 威胁模型文档 (`SECURITY.md`)。

## [0.1.0] — 待定 (release 准则, 与 ROADMAP 保持同步)

满足 **所有** 以下条件后, 从 green release candidate 切出:

- `memexa demo` 在 PyPI LIVE 至少 1 周, 且
  `pip install --pre memexa && memexa demo` 在新 Windows、macOS、
  Linux 上返回 rc=0 (CI 矩阵已验, 但 LIVE PyPI 检查是 gate)。
- 至少 1 个 issue / discussion / PR 来自非作者贡献者。
- 过去 7 天没切过 critical bug fix。

实际 version-bump PR 会 close 本节, link 那个变成 `0.1.0` 的 rc。

[Unreleased]: https://github.com/labazhou2024/memexa/compare/v0.1.0-rc4...HEAD
[0.1.0-rc4]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc4
[0.1.0-rc3]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc3
[0.1.0-rc2]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc2
[0.1.0-rc1]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc1
[0.1.0]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0
