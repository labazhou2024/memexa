# Quickstart

**English** · [中文](quickstart.zh.md)

Three tiers, pick the one that matches how deep you want to go on the
first day. The thirty-second tier needs nothing but Python; the
five-minute tier adds an LLM API key; the thirty-minute tier is the
full self-hosted production deployment.

| Tier | Time | What you do | What you need |
|---|---|---|---|
| **Tier 0** | 30 seconds | See what the project does on synthetic data | Python 3.10+ |
| **Tier 1** | 5 minutes | Ingest one of your own sources and run real queries | Python 3.10+, LLM API key |
| **Tier 2** | 30 minutes | Full production deployment with cron + dashboard + all six sources | Python 3.10+, Docker Desktop, LLM API key, ~8 GB free RAM |

---

## Tier 0 — thirty-second walkthrough

memexa is designed for two audiences: **humans** running queries in
a terminal, and **AI agents** (Claude Code, Cursor, Cline) invoking
memexa as a subprocess. Tier 0 has a path for each.

### For humans

```bash
pip install --pre memexa
memexa demo
```

> **macOS users**: the system Python is 3.9, which is below the 3.10
> minimum. Install Python 3.11 first:
> `brew install python@3.11` (Homebrew) or download from python.org.
> Then run the two commands above in a fresh `python3.11 -m venv` so
> `pip install --pre memexa` actually finds a compatible wheel.
>
> **Windows users**: a `py` launcher version ≥ 3.10 works out of the
> box. If `python --version` reports 3.9, install Python 3.11 from
> the Microsoft Store or python.org.

What you should see:

```
memexa demo  —  thirty-second onboarding
────────────────────────────────────────────
[1/3] Ingesting the bundled synthetic dataset (stub extractor) ...
      ✓ Ingested 26 cards across 6 sources (audio=1, browser_session=10,
        claude_code=3, email=4, qq=3, wechat=5).

[2/3] Running five sample queries against the in-memory set ...
  ▸ memexa quick 'Alice'
     [wechat  2024-01-08] Alice: 组会改到周三下午三点了。 | Bob: @Alice 收到，已记下。 ...
  ▸ memexa arc 'Alice ↔ Bob'
     ...
  ▸ memexa timeline '2024-01'
     ...
  ▸ memexa pending '(commitment cards)'
     (0 cards — synthetic dataset; expected for some samples)
  ▸ memexa topic 'DDIA'
     [qq      2024-01-05] Alice: 你上次提的那本书我看完了，还挺好的。 | demo_user: 哪本？《数据密集型应用系统设计》？ | Alice: 对，DDIA 那本。

[3/3] Done.  Next steps:
      • memexa init       — scaffold ~/.memexa/ config
      • memexa doctor     — self-diagnostic against your backend
      • docs/quickstart.md — Tier 1 (5 min) or Tier 2 (30 min)
```

No Docker. No LLM API key. No configuration. This is the honest first
look at what the project does.

If `memexa demo` fails, see
[`docs/troubleshooting.md#tier-0`](troubleshooting.md#tier-0).

### For AI agents

```bash
pip install --pre memexa
# Agent invokes via its shell tool with --json for structured output:
memexa quick "<question>" --json
memexa arc "<person>" --json
memexa timeline --start 2024-01-01 --end 2024-02-01 --json
```

All fourteen query subcommands support `--json` starting in v0.1.x.
The agent contract — seven hard rules, decision table, composition
patterns — lives in [`docs/for_agents.md`](for_agents.md). Native
MCP integration (`memexa-mcp` server + `.mcp.json`) ships in v0.5;
until then the shell-subprocess path above is the first-class agent
integration and it works in any agent runtime with a shell tool.

---

## Tier 1 — five-minute walkthrough with your own data

Use Tier 1 when Tier 0 satisfied you and you want to point the
pipeline at one of your own sources. Claude Code session history is
the easiest source to start with because no export tool or app
configuration is required.

### 1. Scaffold config

```bash
memexa init
# → creates ~/.memexa/{aliases.yaml, identity.yaml, .env}
```

Open `~/.memexa/.env` and fill in your LLM provider. The recommended
default for Chinese workloads is DeepSeek (see
[`docs/cost.md`](cost.md) for full price comparison):

```
MEMEXA_REMOTE_LLM_BASE_URL=https://api.deepseek.com
MEMEXA_REMOTE_LLM_API_KEY=sk-...
MEMEXA_REMOTE_LLM_GATE_MODEL=deepseek-v4-flash
MEMEXA_REMOTE_LLM_EXTRACT_MODEL=deepseek-v4-pro
```

Typical first-run cost: about ¥0.30 per 1 000 messages with the above
combination. Tier 1 against your own Claude Code projects directory
will consume on the order of ¥0.10–¥1 depending on volume.

### 2. Ingest one source

```bash
memexa ingest claude-code --from ~/.claude/projects/
```

The ingest command runs the same two-LLM extraction pipeline that
Tier 2 uses but skips the cron-driven scheduling and the full six-
source orchestrator. Expected runtime: 1–5 minutes for a few hundred
sessions.

### 3. Query

```bash
memexa quick "what did I work on last week"
memexa topic "<a project name you actually have>"
memexa pending
```

You should see real cards with real Chinese narrative (assuming your
sessions are in Chinese) and per-claim citations back to the original
session transcripts.

### 4. (Optional) Doctor

```bash
memexa doctor
```

Self-diagnostic against the backend. Useful if a query returned
unexpected zero cards.

> **Tier 1 caveat**: Tier 1 currently writes into the same Hindsight-
> compatible backend that Tier 2 uses. v0.3 will ship `memexa backend
> --embedded` so this step does not need Docker at all. Until then,
> Tier 1 either reuses a running Tier 2 backend or runs in process-
> local SQLite mode (see `MEMEXA_HINDSIGHT_URL=memory://` in
> `docs/configuration.md`).

---

## Tier 2 — full production deployment (thirty minutes)

Use Tier 2 when you want the project running on a schedule across all
six sources, with a dashboard and a memory backend that survives
reboots.

### 1. Tooling

Pick the deployment guide that matches your platform:

- macOS — [`docs/deployment/macos.md`](deployment/macos.md)
- Windows — [`docs/deployment/windows.md`](deployment/windows.md)
- Linux + Docker — [`docs/deployment/docker-compose.md`](deployment/docker-compose.md)

All three guides install the same five components: Python 3.10+, Git,
Docker Desktop or `docker-compose-plugin`, a clone of the repository,
and the `pip install -e ".[dev]"` editable install.

### 2. Clone + install

```bash
git clone https://github.com/labazhou2024/memexa.git memexa
cd memexa
python -m venv .venv
. .venv/bin/activate    # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
$EDITOR .env

mkdir -p ~/.memexa
cp config/aliases.example.yaml  ~/.memexa/aliases.yaml
cp config/identity.example.yaml ~/.memexa/identity.yaml
$EDITOR ~/.memexa/{aliases,identity}.yaml
```

Minimum required fields in `.env`:

- `MEMEXA_REMOTE_LLM_BASE_URL`
- `MEMEXA_REMOTE_LLM_API_KEY`
- `MEMEXA_REMOTE_LLM_GATE_MODEL`
- `MEMEXA_REMOTE_LLM_EXTRACT_MODEL`

### 4. Bring up the memory backend

```bash
make backend-up
# or: docker compose -f docker-compose.example.yml up -d
```

Wait ~30 seconds for pgvector + Hindsight to come online. The
Makefile target polls `:8888/healthz` and exits when ready.

### 5. Ingest the demo dataset, then your own

```bash
make demo-ingest        # ingest synthetic dataset against the real backend
memexa doctor           # confirm everything is wired up
make demo-query         # run four sample queries
```

Then point each source builder at your own data. Detailed per-source
onboarding lives in [`docs/integrations/`](integrations/).

### 6. Schedule the cron

Each deployment guide includes a `register-cron.sh` (macOS / Linux)
or `register-tasks.ps1` (Windows) that installs a six-hour incremental
job per driver plus the dashboard service.

### 7. Open the dashboard

```bash
python -m memexa.dashboard.sys_monitor.server
# → http://127.0.0.1:8765
```

Seven live panels: Win/Mac/GPU CPU+memory, API usage, memory system
health, cron status, recent graph queries, six-source pending, audio
pipeline.

---

## Troubleshooting

If a step fails, the first stop is
[`docs/troubleshooting.md`](troubleshooting.md). Common Tier 0/1/2
failure modes are covered there with the exact remediation command.

For backend or LLM-provider failures, run `memexa doctor` first — it
runs a four-step probe (backend health, bank stats, LLM round-trip,
identity manifest) and prints a per-step pass/fail.

For query-returns-zero-cards or extractor-malformed-JSON, see the
lessons-learned series:

- [`lessons_learned/03_pg_aware_pending.md`](lessons_learned/03_pg_aware_pending.md) — PG marker drift
- [`lessons_learned/05_qwen3_no_think.md`](lessons_learned/05_qwen3_no_think.md) — Qwen3 `/no_think` directive

Open an issue at <https://github.com/labazhou2024/memexa/issues/new>
if none of the above fits.
