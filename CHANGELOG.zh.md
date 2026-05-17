# 更新日志

本文件记录本项目所有值得注意的变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/spec/v2.0.0.html)。

**英文版本**: [CHANGELOG.md](CHANGELOG.md)（权威源）。本文件是中文镜像。

## [Unreleased]

(暂无 — `0.1.1` 是最新一切。)

## [0.1.1] — 2026-05-17

Onboarding 重写 + 修复 v0.1.0 意外 ship 的 email broken path。

### 关键修复

- **邮箱 IMAP 路径 v0.1.0 是 broken**。
  `memexa/extraction/email_history_fetcher.py` hard-coded 到 2 个
  maintainer-specific account 名 (`qq_email`, `ustc_email`),
  调用时 import `memexa.qq_email` / `memexa.ustc_email` -- 这 2
  个 module **OSS 包里不存在**。用户跟着
  `docs/integrations/email.md` 走到 ingest email 这一步必撞
  `ModuleNotFoundError`。v0.1.0 切 stable 前的 rc5 audit 漏了这条
  (只跑 `memexa demo` + 查询层, 没跑 ingestion)。被发现时 v0.1.0
  PyPI 下载量基本为 0, 无用户受影响。v0.1.1 真修。不 yank v0.1.0,
  这里 + ROADMAP 透明 disclose。
- `email_history_fetcher` 重写为 generic IMAP client: 读
  `~/.memexa/identity.yaml` 的 `email.accounts.<name>` 块,
  支持多账号, env var 未设 / identity.yaml 缺失时给友好错误。

### Added (新增)

- **`memexa init email`**: 交互式 IMAP 引导 wizard。从邮箱后缀
  自动识别 12+ provider (gmail / googlemail / outlook / hotmail /
  live / icloud / qq / foxmail / 163 / 126 / yeah / sina /
  mail.ustc.edu.cn)。依次问账号 label, 密码 env-var 名, folders,
  抓多少天, 写到 identity.yaml。打印 bash + PowerShell 双 shell
  下一步命令。
- **`memexa init wechat`**: 微信导出引导 wizard (仅 Windows)。
  检测 WeChatMsg 在 4 个常见安装路径; 没装就打印 release URL +
  预期 export 目录。写 `wechat.export_dir` 到 identity.yaml。
  **不** auto-download EXE (安全考虑: 用户自己拿第三方二进制)。
- **`memexa ingest email`**: 顶级 wrapper 封装
  `email_history_fetcher`, 用户不用打
  `python -m memexa.extraction.email_history_fetcher`。
  forward `--account`, `--since`, `--max-per-folder`。`--account`
  省略时跑所有配置账号。
- **`memexa ingest wechat`**: 顶级 wrapper 读 WeChatMsg export 目录
  (`--from` flag 或 identity.yaml 的 `wechat.export_dir`), 驱动
  `v5_wechat_batch_builder`。
- `memexa/cli/wizards.py` -- 新 module 装 wizard + ingest dispatch
  逻辑。未来 source (飞书, 钉钉, 本地文档) 会进这里。

### Changed (变更)

- `docs/quickstart.zh.md` Tier 1 围绕新
  `memexa init <source>` + `memexa ingest <source>` 流程重写。4
  source 内联文档化: Claude Code (5 min, 无第三方), 邮箱 (10 min,
  IMAP), 微信 (仅 Windows, ~30-60 min), QQ (调试中, v0.2)。每节
  给出用户该打哪些命令 + 凭据 / export 工具去哪找。
- `docs/quickstart.zh.md` Tier 3 状态表更新: 邮件行 ✅ 引用
  `memexa init email`; 微信行 ⚠→✅ 反映新 wizard wrap WeChatMsg。
- `ROADMAP.zh.md` Known limitations 段更新: v0.1.0 邮箱 broken 项
  移到新 "v0.1.1 闭合" 子节, 微信 limitation 改写为 wizard 存在
  但上游 exporter 约束仍在。

### 新公开 CLI

```
memexa init email          # 交互式 IMAP wizard
memexa init wechat         # WeChatMsg 导出 wizard
memexa ingest email        # 抓 IMAP for all configured accounts
memexa ingest wechat       # 读 WeChatMsg export -> builder
```

### 修复（re-verify 复盘中浮出的 13 个 fresh-user blocker）

初版 onboarding 重写落地后，按"全新用户"视角在 Win 11 + Mac Studio +
USTC Linux 三平台复测，并接真实 IMAP（QQ + USTC Exmail-reverse-proxy）
和真 WeChatMsg-schema JSON，又揪出 13 个会让新用户走不通的 bug，全部
修掉，列在下面。

**Onboarding (`memexa init` / `init llm` / `init email`)**

- `memexa init` 没把 `aliases.example.yaml`/`identity.example.yaml`/
  `.env.example` 三个模板打进 wheel。新用户跑完看见 3 个
  `[warn] template missing` + 空的 `~/.memexa/`。修：模板搬到
  `memexa/templates/`，editable 安装时回退到 repo 根的
  `config/`。`Next steps` 提示从 `make backend-up` 改为
  `memexa backend up`（pip 用户没 Makefile）。
- `memexa init llm` 在中文-locale Windows 控制台直接崩
  `UnicodeEncodeError: 'gbk' codec can't encode character '\xa5'`
  （DeepSeek provider note 里的 `¥` 符号）。修：CLI 入口加
  `_force_utf8_stdio()`，把 `sys.stdout`/`sys.stderr` 切到
  UTF-8 + `errors="replace"`。
- `memexa init email` 给 USTC 邮箱的提示写反了 — 显示
  `imap.exmail.qq.com`，但 LIVE 验证那个 endpoint 会 rate-limit
  / 锁号。真实可用的是 `mail.ustc.edu.cn:993`，用同一个 16 位
  授权码即可。提示反过来。

**Backend (`memexa backend up` / `memexa doctor`)**

- `memexa doctor` 探的是 `/healthz`，但 Hindsight 实际服务于
  `/health`（vectorize-io spec 改了）。修：先试 `/health`，404
  再 fallback `/healthz`，兼容老 fork。
- `memexa doctor` LLM probe 双前缀 `/v1` — 用户的 base URL
  已含 `/v1`，结果 hit `/v1/v1/chat/completions` → 404，永远
  报红。改成 `base.endswith("/v1")` 时不再加前缀。
- `memexa doctor` 读了不存在的 `nodes` 字段，结果"bank has 0
  nodes" 即使 bank 里真有几十个 nodes。真字段是 `total_nodes`，
  顺手把 `total_documents` 和 `total_links` 一起打印。
- `memexa backend up` 健康 poll 60s 太短，覆盖不了 BGE-M3 冷
  load。改成 helper 默认的 180s。
- `docker-compose.yml` 把 `HINDSIGHT_API_LLM_MODEL` 默认指向
  EXTRACT 模型。Reasoning 模型（deepseek-v4-flash-ascend /
  qwen-reasoner）在 Hindsight 的严格 JSON prompt 下只在
  `reasoning_content` 出内容、`content` 字段空着，Hindsight 内
  部 fact extraction 全部失败，`total_nodes` 永远是 0。改成
  默认走 GATE 模型（chat-class 非 reasoning）。
- `docker-compose.yml` 用 `${HF_ENDPOINT:-}` / `${HTTP_PROXY:-}`
  做替换，**未设变量替换出空字符串**喂进容器，huggingface_hub
  把空字符串当成 URL，直接抛
  `httpx.UnsupportedProtocol: Request URL is missing a protocol`，
  Hindsight 容器第一次起就 crash-loop。改成 service-level
  `env_file: .env` — 不设的变量在容器里就是不设，
  huggingface_hub 走自己内置默认 `https://huggingface.co`。
  China 用户在 `~/.memexa/.env` 加 `HF_ENDPOINT=https://hf-mirror.com`
  即可走国内镜像。
- `memexa backend up` 调 docker compose 时没清理 shell 里残留的
  `HINDSIGHT_API_LLM_*`（老 JARVIS 用户 `~/.zshrc` 里通常有），
  导致 `~/.memexa/.env` 被默默覆盖。现在调 compose 前先 strip
  这几个变量，让 `.env` 成为单一真值源。

**Ingestion (`memexa ingest email` / `memexa ingest wechat`)**

- `_normalize_llm_card` 对 LLM 自由发挥的 `confidence` 值不做
  enum 强制 — 数字（`0.85`）、英文（`"high"`）、中文（`"确定"`）
  统统不在 enum 内，`TimeResolution` /
  `Entity.resolution_confidence` / `IdentityAssertion` /
  `RelationAssertion` 验证器全部 reject，相当部分卡片被
  dead-letter。新加 `_normalize_confidence(val, allow_unresolved)`
  helper 把任何输入 map 到合法 enum，并按字段类型分别走 4 值
  （`TimeResolution`/`Entity`）和 3 值（`IdentityAssertion`/
  `RelationAssertion` 不允许 `"unresolved"`）语义。
- `_normalize_llm_card` 对 `anchor_message_ts` 不做 ISO 强制，
  LLM 偶尔吐 bare date `"2026-05-13"`（真实 QQ 邮件
  thread），`TimeResolution` 直接 reject
  `anchor_message_ts must be ISO 8601`。现在和
  `resolved_start`/`resolved_end` 共用一个 `_normalize_iso`
  路径；`when_start_default` 也在 ISO 标准化之后才捕获，避免
  把 bare date 继承给 anchor。
- `_build_wechat_prompt_from_messages` 把 `StrContent` 为空的
  消息直接 silently drop — 而**真实 WeChatMsg 导出里这是所有
  非文本消息类型**（`Type=3` 图片、`Type=43` 视频、`Type=47`
  贴纸、`Type=34` 语音、`Type=48` 位置、`Type=49` appmsg
  卡片、`Type=50` 语音/视频通话、`Type=10000` 系统消息）。
  真实聊天 30-60% 的消息被吃掉。新加 `_wechatmsg_content`
  helper：图片 → `"[图片]"`、贴纸 → `"[表情]"`、视频 →
  `"[视频]"`、appmsg → 从 XML 抽 `<title>` 出
  `"[分享: 标题]"`、系统消息原样穿透。
- `_build_wechat_prompt_from_messages` 不传 `IsSender=1` 给
  下游，每条消息也不带 `is_self_message` 标记，batch 级也没
  `n_self_msgs`/`n_other_msgs`/`is_solo_self`/`is_self_chat`
  四个字段，导致 `pass2_prompt.py` 里的 §SELF_NOTE_MODE 完全
  不触发，CEO 自己说的话 LLM 当成"他人语录"。现在 4 字段全
  emit + per-msg `is_self_message` hint。

**验证**

- 新加 25 个 unit test (`tests/unit/test_confidence_sanitizer.py`
  57 参数化 case 覆盖数字/英文/中文/bool/None/4-value/3-value
  enum；`tests/unit/test_wechat_msg_adapter.py` 25 case 覆盖
  10 个 msg type、IsSender 语义、字段别名)。pytest 全过
  (140 passed, 2 skipped pre-existing prompt-drift)。
- Win 11 + Mac Studio LIVE 复测：pip install → demo →
  init wizards → backend up → `memexa ingest wechat`
  (demo + 真实 schema WeChatMsg-style JSON) → `memexa quick`
  返回 N>0 真卡片。
- Win 11 真 IMAP LIVE：QQ (`imap.qq.com:993` 16 位授权码) +
  USTC Exmail-reverse-proxy (`mail.ustc.edu.cn:993` 同型授权
  码)。两个 provider 真邮件 fetch → extract → POST → 索引 →
  可查。
- USTC Ubuntu 22.04 (docker-compose standalone): pip install
  + demo + init wizards + doctor 全过。`backend up` 被校园网
  防火墙挡 docker.io（环境问题非 code）。

## [0.1.0] — 2026-05-17

首个 stable 发布。合并 rc4 发布后审计的 rc5 say-do gap 修复, 加上
切 stable 前一刻补的 Tier 3 "真实数据源" 诚实声明 patch。

**切 stable 的理由**: ROADMAP §[0.1.0] 原列的 3 个 gate 里有 2 个
生态信号 ("≥1 非作者 issue/PR" + "≥7 天距上次 critical fix") 在
切的时点没满。仍切, 因为:

  - rc4 后审计的 6 个 say-do gap 全部闭环 (真 --json 支持 / backend
    down exit code / 3 个文档正确性修复 / 双语 100% / Tier 3 逐源
    真实状态诚实声明)。
  - 跨平台新装 smoke 在 Win (Python 3.11.9) / macOS (Python 3.13.12
    miniforge) / Linux (Python 3.10.12 Ubuntu 22.04) LIVE 验证通
    过。同时 rc4 的 2 个 bug 在 3 平台上**也 LIVE 复现**, 证明
    rc5 修复不是水文档。
  - 那 2 个未满 gate 是**de-facto** 信号 (社区速率 / soak 时间),
    不是 de-jure 正确性信号。死等会无限冻结 rc 链, 而 say-do gap
    已经可执行修复。
  - v0.1.0 ship 时带文档化 "Known limitations" 块, 同时写在
    `docs/quickstart.zh.md` Tier 3 和 `ROADMAP.zh.md` Shipped, 用户
    一打开文档就撞见逐源 ✅ / ⚠ / ❌ 表 — 不存在沉默 surprise。

### Added (rc4 之后新)

- **`--json` 在子命令级被接受**: agent 现在可以按 README /
  quickstart 文档写法 `memexa quick "X" --json`, 不必被迫用
  `memexa --json query quick "X"`。`--json` 挂在 `_common` parent
  parser, 14 个子命令全部继承, 3 种位置都工作。修补了 rc4 审计在 3
  平台上发现的 `unrecognized arguments: --json` argparse 拒收。
- **`docs/quickstart.zh.md` Tier 3**: "接你自己的真实数据源". 6
  行逐源状态表 (邮件 / 音频 / 浏览 / Claude Code / 微信 / QQ) 附
  ✅ / ⚠ / ❌ + "何时更好" 列指向未来 ROADMAP 里程碑 + 推荐接入
  顺序 + "v0.1.0 已知 limitation" 子节。诚实回答 "今天能不能在我
  自己数据上真用?"。
- **`ROADMAP.zh.md` Known limitations 段**: 3 个 v0.1.0-specific
  事项 (QQ db-only adapter 等 v0.2 移植 / 微信导出仅 Windows 受
  上游生态约束 / 其他交叉链接到 docs/quickstart Tier 3)。

### Changed (rc4 之后改)

- **`docs/quickstart.zh.md`** Tier 0 期望 demo 输出改写为 Python
  3.11 venv 真实跑出: `(audio=1, browser_session=10,
  claude_code=3, email=4, qq=3, wechat=5)`, 总 26 cards。之前
  `(wechat=8, qq=4, email=4, browser=4, claude=3, audio=3)` 既错
  数字又错命名 (`browser`/`claude` vs `browser_session`/`claude_code`)。
- **`docs/quickstart.zh.md`** macOS Python 3.9 缺口写在 install
  命令上方 warning。macOS 系统 Python 是 3.9, 低于 3.10, 之前
  `pip install --pre memexa` 在每台未动 macOS 上静默失败。现在
  用户在 docs 页就撞上要求, 有 `brew install python@3.11` + venv
  操作指引。
- **全部安装命令从 `pip install --pre memexa` 翻为
  `pip install memexa`** — 这是 stable 发布, `--pre` 不再需要。
- **`ROADMAP.zh.md`** 当前状态从 `(v0.1.0-rc4)` 推到 `(v0.1.0)`。
  Shipped 重写: CLI 加 demo, "Eight tests, 19 CI" 改 "Ten tests,
  6 CI workflows", Linux 改"Linux via docker-compose", 14 subcmd
  拆分从 "9 + 5" 改 "7 + 7"。

### Fixed (rc4 之后修)

- **`memexa quick "X"` 等命令在 backend 不可达时退出 1 + 英文
  stderr 提示**, 不再静默 `N=0` + exit 0。Agent subprocess 调
  memexa 依赖 exit code 区分 "无结果" vs "你的 invocation 完全没用
  因为 backend 挂了"。
  - `--json` 模式 stdout 仍 print `[]` (JSON parser 不破), 但同时
    exit 1 + stderr 单行提示。
  - 旧 `logger.warning` 泄漏 GBK 本地化 Windows 系统错误降级
    `logger.debug`; 控制台只见纯 ASCII 多行提示, 含 3 个下一步
    命令 (`make backend-up`, `MEMEXA_HINDSIGHT_URL=...`,
    `memexa doctor`)。

### Removed (rc4 之后删)

- 重复的 `[0.1.0-rc1]` entry — CHANGELOG rc1→rc2 文件重写时不小心
  写了两遍。

### 双语覆盖 (rc4 之后)

- 加 8 个缺失 `.zh.md` 镜像 (CHANGELOG, CODE_OF_CONDUCT,
  GOVERNANCE, PROGRESS, 4 个 `.github/` issue + PR 模板), 闭环
  rc4 审计找出的双语硬约束 gap。切发时验证: 49 EN / 49 ZH / 0 缺。

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

<!-- 0.1.0 历史准则段于 2026-05-17 切 stable 时下线。见上方 [0.1.0]
     entry 看实际 release notes 和切 stable 的理由 (3 个准则中有 2
     个推迟到 v0.1.x post-release 文档化诚实)。 -->


[Unreleased]: https://github.com/labazhou2024/memexa/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.1
[0.1.0]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0
[0.1.0-rc4]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc4
[0.1.0-rc3]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc3
[0.1.0-rc2]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc2
[0.1.0-rc1]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc1
