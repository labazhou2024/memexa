# 快速开始

[English](quickstart.md) · **中文**

> 在干净的 Win / macOS 上 ≈ 30 分钟跑到第一次查询。Linux 用户走
> [deployment/docker-compose.md](deployment/docker-compose.md)。

## 0. 前置依赖

- Python **3.10+**
- Docker (跑 Hindsight 记忆后端) **或** 本地装好 pgvector 的 PostgreSQL 16+
- 一个 OpenAI-compatible chat-completions endpoint (vLLM / Ollama / LiteLLM
  proxy / DeepSeek API / 通义 / Moonshot / OpenRouter / etc)
- 摄入期间 ~8 GB 空闲内存 (LLM extractor 独立进程; 这是 BGE-M3 + 你的代码)

## 1. 安装

```bash
git clone https://github.com/labazhou2024/memex.git memex
cd memex
python -m venv .venv
. .venv/bin/activate     # PowerShell: .venv\Scripts\Activate.ps1
pip install -e .[dev]
```

## 2. 配置

```bash
# 环境变量 (docker-compose + Makefile 读, Python 代码不读)
cp .env.example .env
$EDITOR .env

# 用户配置 (Python 运行时读)
mkdir -p ~/.memex
cp config/aliases.example.yaml   ~/.memex/aliases.yaml
cp config/identity.example.yaml  ~/.memex/identity.yaml
$EDITOR ~/.memex/aliases.yaml
$EDITOR ~/.memex/identity.yaml
```

最少要设的:

- `~/.memex/aliases.yaml` → 系统应该匹配成 "你" 的字符串列表 (你的名字 /
  昵称 / 邮箱前缀 等)
- `.env` → `MEMEX_REMOTE_LLM_BASE_URL`, `MEMEX_REMOTE_LLM_API_KEY`,
  `MEMEX_REMOTE_LLM_GATE_MODEL`, `MEMEX_REMOTE_LLM_EXTRACT_MODEL`

## 3. 启动记忆后端

```bash
docker compose -f docker-compose.example.yml up -d
# 等 ~30 秒 pgvector + Hindsight 起来
curl -sf http://127.0.0.1:8888/healthz | jq .
# {"status":"ok"}
```

## 4. 第一次查询 (用 demo 数据集)

```bash
# 不连后端先验证打包能解析
python -m examples.demo_dataset.ingest --dry-run
# → 打印 "total = 26 cards across 6 sources"

# 真摄入到后端 (要 step 3 成功)
make demo-ingest

# 端到端确认装好了
memex doctor
# → [ok] primary /healthz returned 200
# → [ok] bank 'memory_full_v5' has N nodes
# → [ok] LLM/gate ... responded 200

# 跑几个子命令
python -m src.core.memory_query topic    "<你的关键词>"
python -m src.core.memory_query arc      "<某个实体>"
python -m src.core.memory_query timeline --start 2024-01-01 --end 2024-02-01
```

## 5. 接入你自己的数据

每个 source 都有一个 builder 把原始 export 转成 batch JSON, 加一个
driver 跑 6 小时一轮抽取 pipeline。

每个 source 的详细接入:

- **微信** — 用 [`WeChatMsg`](https://github.com/LC044/WeChatMsg) 或
  [`wechatDataBackup`](https://github.com/git-jiadong/wechatDataBackup) 导出;
  把 `v5_wechat_batch_builder.py` 指向那份 JSON。
- **QQ** — 设 `MEMEX_QQ_ID`; builder 直接读
  `~/Documents/Tencent Files/<qq-id>/nt_qq/nt_db/nt_msg.db`。
- **邮件** — IMAP 凭据填到 `~/.memex/identity.yaml`。
- **浏览器** — 指向浏览器 profile 里的 `History` SQLite。
- **Claude Code** — 指向 `~/.claude/projects/`。
- **语音** — 把 `.wav` / `.m4a` 文件丢到 `data/audio/inbox/`, audio driver
  自己捡。

## 6. 排 cron

挑一个部署指南端到端跟着做:

- [deployment/macos.md](deployment/macos.md) — 每个 driver 一个 launchd plist
- [deployment/windows.md](deployment/windows.md) — Scheduled Tasks (`schtasks`)
- [deployment/docker-compose.md](deployment/docker-compose.md) — Linux + Docker

## 7. 打开 dashboard

```bash
python -m src.dashboard.sys_monitor.server
# 浏览器打开 http://127.0.0.1:8765
```

应该看到 7 个 live 面板: Win/Mac/GPU CPU+内存, API usage, memory system,
cron health, graph queries, six-source pending, audio pipeline。

## 8. 故障排查

- *已摄入数据但查询返回 0 卡* — 见 [usage_guide.zh.md](usage_guide.zh.md)。
- *Extractor LLM 返回坏 JSON* — 见
  [lessons_learned/05_qwen3_no_think.md](lessons_learned/05_qwen3_no_think.md)。
- *PG marker drift* — 见
  [lessons_learned/03_pg_aware_pending.md](lessons_learned/03_pg_aware_pending.md)。

以上都不对的话, 去 `memex/issues/new` 提 issue。
