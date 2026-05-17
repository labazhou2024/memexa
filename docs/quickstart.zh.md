# Quickstart

[English](quickstart.md) · **中文**

3 个 Tier, 按你第一天想钻多深选。Tier 0 只要 Python; Tier 1 加 LLM API
key; Tier 2 是完整自托管生产部署。

| Tier | 时间 | 你做什么 | 你需要什么 |
|---|---|---|---|
| **Tier 0** | 30 秒 | 在合成数据上看项目能做什么 | Python 3.10+ |
| **Tier 1** | 5 分钟 | 接 1 个你自己的 source, 跑真查询 | Python 3.10+, LLM API key |
| **Tier 2** | 30 分钟 | 生产部署: cron + dashboard + 6 个 source | Python 3.10+, Docker Desktop, LLM API key, ~8 GB 空闲内存 |

---

## Tier 0 — 30 秒 walkthrough

memexa 服务两类用户: **人类** 在终端跑查询, 和 **AI agent**
(Claude Code / Cursor / Cline) 把 memexa 当 subprocess 调。Tier 0
两类各有一条路径。

### 人类用户路径

```bash
pip install --pre memexa
memexa demo
```

> **macOS 用户**: 系统自带 Python 是 3.9, 低于 3.10 最低要求。先装
> Python 3.11: `brew install python@3.11` (Homebrew) 或从 python.org
> 下载安装包。然后在 `python3.11 -m venv` 新建的 venv 里跑上面两条
> 命令, `pip install --pre memexa` 才能找到兼容 wheel。
>
> **Windows 用户**: `py` launcher 自带 3.10+ 即可。如果
> `python --version` 报 3.9, 从 Microsoft Store 或 python.org 装
> Python 3.11 即可。

你应该看到:

```
memexa demo  —  thirty-second onboarding
────────────────────────────────────────────
[1/3] Ingesting the bundled synthetic dataset (stub extractor) ...
      ✓ Ingested 26 cards across 6 sources (audio=1, browser_session=10,
        claude_code=3, email=4, qq=3, wechat=5).

[2/3] Running five sample queries against the in-memory set ...
  ▸ memexa quick 'Alice'
     [wechat  2024-01-08] Alice: 组会改到周三下午三点了。 | Bob: @Alice 收到，已记下。 ...
  ▸ memexa arc 'Alice ↔ Bob' ...
  ▸ memexa timeline '2024-01' ...
  ▸ memexa pending '(commitment cards)'
     (0 cards — synthetic dataset; expected for some samples)
  ▸ memexa topic 'DDIA'
     [qq      2024-01-05] Alice: 你上次提的那本书我看完了，还挺好的。 | demo_user: 哪本？《数据密集型应用系统设计》？ | Alice: 对，DDIA 那本。

[3/3] Done.  Next steps:
      • memexa init       — scaffold ~/.memexa/ config
      • memexa doctor     — self-diagnostic against your backend
      • docs/quickstart.zh.md — Tier 1 (5 min) 或 Tier 2 (30 min)
```

不需要 Docker, 不需要 LLM API key, 不需要任何配置。这是项目实际能做什么
的诚实第一眼。

如果 `memexa demo` 失败, 见
[`docs/troubleshooting.zh.md#tier-0`](troubleshooting.zh.md#tier-0)。

### AI agent 路径

```bash
pip install --pre memexa
# Agent 通过 shell 工具调, 加 --json 拿结构化输出:
memexa quick "<问题>" --json
memexa arc "<人名>" --json
memexa timeline --start 2024-01-01 --end 2024-02-01 --json
```

14 个查询子命令全部从 v0.1.x 起支持 `--json`。Agent 契约 — 7 条
hard rule / 决策表 / 组合模式 — 写在
[`docs/for_agents.zh.md`](for_agents.zh.md)。原生 MCP integration
(`memexa-mcp` server + `.mcp.json`) 在 v0.5 ship; 在此之前 shell
subprocess 是 first-class agent integration, 任何带 shell 工具的
agent runtime 都能用。

---

## Tier 1 — 5 分钟 walkthrough, 用你自己的数据

Tier 0 看了满意后, 想把 pipeline 指向你自己的 source 试一下 — 用 Tier 1。
最简单的入口是 Claude Code session 历史, 不需要导出工具, 不需要应用配置。

### 1. 初始化配置

```bash
memexa init
# → 创建 ~/.memexa/{aliases.yaml, identity.yaml, .env}
```

打开 `~/.memexa/.env` 填 LLM provider。中文场景推荐 DeepSeek
(完整对比见 [`docs/cost.zh.md`](cost.zh.md)):

```
MEMEXA_REMOTE_LLM_BASE_URL=https://api.deepseek.com
MEMEXA_REMOTE_LLM_API_KEY=sk-...
MEMEXA_REMOTE_LLM_GATE_MODEL=deepseek-v4-flash
MEMEXA_REMOTE_LLM_EXTRACT_MODEL=deepseek-v4-pro
```

典型首跑成本: 上面组合每 1000 条消息约 **¥0.30**。Tier 1 跑你自己的
Claude Code projects 目录, 按消息量约 ¥0.10-¥1。

### 2. 接 1 个 source

```bash
memexa ingest claude-code --from ~/.claude/projects/
```

`ingest` 命令跑的是 Tier 2 同一套双 LLM 抽取流程, 只跳过 cron 调度和 6
source orchestrator。预期耗时: 1-5 分钟 (几百个 session)。

### 3. 查询

```bash
memexa quick "我上周做了什么"
memexa topic "<你真有的项目名>"
memexa pending
```

你应该看到真 cards + 真中文 narrative (如果你的 session 是中文) + 每条
断言绑回原 session transcript 的 citation。

### 4. (可选) Doctor

```bash
memexa doctor
```

后端 self-diagnostic。查询返 0 cards 时跑一遍能定位是哪一层出了问题。

> **Tier 1 注意**: Tier 1 目前写入 Tier 2 同一套 Hindsight 兼容后端。
> v0.3 会 ship `memexa backend --embedded`, 让这一步完全不需要 Docker。
> 在 v0.3 之前, Tier 1 要么复用一个跑着的 Tier 2 backend, 要么走
> process-local SQLite mode (见 `MEMEXA_HINDSIGHT_URL=memory://`,
> [`docs/configuration.zh.md`](configuration.zh.md))。

---

## Tier 2 — 完整生产部署 (30 分钟)

Tier 2 = 项目按 schedule 跨 6 source 跑, 含 dashboard, 含 reboot
后能恢复的 memory backend。

### 1. 工具链

按你的平台选一个部署指南:

- macOS — [`docs/deployment/macos.zh.md`](deployment/macos.zh.md)
- Windows — [`docs/deployment/windows.zh.md`](deployment/windows.zh.md)
- Linux + Docker — [`docs/deployment/docker-compose.zh.md`](deployment/docker-compose.zh.md)

3 个指南装的是同 5 个组件: Python 3.10+, Git, Docker Desktop 或
`docker-compose-plugin`, 仓库 clone, `pip install -e ".[dev]"`。

### 2. Clone + 装包

```bash
git clone https://github.com/labazhou2024/memexa.git memexa
cd memexa
python -m venv .venv
. .venv/bin/activate    # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 3. 配置

```bash
cp .env.example .env
$EDITOR .env

mkdir -p ~/.memexa
cp config/aliases.example.yaml  ~/.memexa/aliases.yaml
cp config/identity.example.yaml ~/.memexa/identity.yaml
$EDITOR ~/.memexa/{aliases,identity}.yaml
```

`.env` 最少必填:

- `MEMEXA_REMOTE_LLM_BASE_URL`
- `MEMEXA_REMOTE_LLM_API_KEY`
- `MEMEXA_REMOTE_LLM_GATE_MODEL`
- `MEMEXA_REMOTE_LLM_EXTRACT_MODEL`

### 4. 起 memory backend

```bash
make backend-up
# 或: docker compose -f docker-compose.example.yml up -d
```

等 ~30 秒 pgvector + Hindsight 起来。Makefile target 自动 poll
`:8888/healthz` 健康后才退出。

### 5. 摄入 demo dataset, 再接你自己的

```bash
make demo-ingest        # 把合成数据集摄入真 backend
memexa doctor           # 确认全链路通
make demo-query         # 跑 4 个示例查询
```

接你自己的数据见 [`docs/integrations/`](integrations/), 每个 source
有独立 onboarding 指南。

### 6. 装 cron

每个部署指南包含 `register-cron.sh` (macOS / Linux) 或
`register-tasks.ps1` (Windows), 装好每 6 小时跑一次的增量 driver +
dashboard service。

### 7. 打开 dashboard

```bash
python -m memexa.dashboard.sys_monitor.server
# → http://127.0.0.1:8765
```

7 个 LIVE panel: Win/Mac/GPU CPU+memory / API usage / memory system /
cron 健康 / 近期 graph queries / 6 source pending / audio pipeline。

---

## 故障排查

任一步失败, 第一站是 [`docs/troubleshooting.zh.md`](troubleshooting.zh.md)。
Tier 0/1/2 常见失败模式都在那里, 含确切补救命令。

后端或 LLM provider 问题, 先跑 `memexa doctor` — 它 4 步 probe (后端
health + bank stats + LLM round-trip + identity manifest) 给逐步
pass/fail。

查询返 0 cards / extractor 输出 malformed JSON, 见 lessons-learned 系列:

- [`lessons_learned/03_pg_aware_pending.md`](lessons_learned/03_pg_aware_pending.md) — PG marker drift
- [`lessons_learned/05_qwen3_no_think.md`](lessons_learned/05_qwen3_no_think.md) — Qwen3 `/no_think`

没找到的 issue: <https://github.com/labazhou2024/memexa/issues/new>。
