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
pip install memexa
memexa demo
```

> **macOS users**: the system Python is 3.9, which is below the 3.10
> minimum. Install Python 3.11 first:
> `brew install python@3.11` (Homebrew) or download from python.org.
> Then run the two commands above in a fresh `python3.11 -m venv` so
> `pip install memexa` actually finds a compatible wheel.
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
pip install memexa
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

### 1. Scaffold base config (LLM provider)

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
combination.

### 2. Onboard a source (v0.1.1: interactive wizards)

Pick whichever source you have data in. The wizard writes the
appropriate block into `~/.memexa/identity.yaml`; you do not have to
hand-edit YAML.

#### Email (10 min, any IMAP provider)

```bash
memexa init email
```

Six providers are auto-detected from the email domain you enter
(gmail / outlook / icloud / qq / 163 / foxmail / ustc + more). The
wizard then prompts for: account label, password env-var name,
folders, since-days. Get an app-specific password from your provider
(the wizard prints the URL), export it, and ingest:

```bash
export MEMEXA_IMAP_ALICE_PASSWORD='<paste-app-password>'
memexa ingest email
```

#### Claude Code (5 min, no third-party tool)

```bash
memexa ingest claude-code --from ~/.claude/projects/
```

The simplest source — reads JSONL session files directly. No wizard
needed because there are no credentials.

#### WeChat (Windows only, ~30-60 min first run)

```bash
memexa init wechat
```

This wizard wraps [WeChatMsg](https://github.com/LC044/WeChatMsg)
(third-party, GPL-licensed). The wizard detects an existing install,
or points you at the release page. You install WeChatMsg, sign in to
your WeChat client, export chats as JSON, then run:

```bash
memexa ingest wechat
```

memexa reads the exported JSON directory, normalises into batches,
and runs the same two-LLM extraction pipeline.

**macOS / Linux users**: WeChat history extraction is currently
Windows-only because every recommended exporter (WeChatMsg,
wechatDataBackup, PyWxDump) is Windows-only. Not a memexa limitation;
upstream ecosystem.

#### QQ (in flight, v0.2)

QQ db-only adapter migration from upstream JARVIS is tracked in v0.2.
v0.1.x has the NapCat path behind `MEMEXA_QQ_NAPCAT_FORCE=1` for
disposable research accounts, but it is not recommended due to
Tencent's 2025-09-05 fingerprint-ban wave.

### 3. Query

```bash
memexa quick "what did I work on last week"
memexa topic "<a project name you actually have>"
memexa pending
```

You should see real cards with real Chinese narrative (assuming your
sources are in Chinese) and per-claim citations back to the original
sentences.

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

## Tier 3 — connecting your real data sources

Tier 0 / 1 / 2 prove the **plumbing** works: the package installs, the
backend boots, a synthetic dataset ingests, queries return cards. To
get value out of memexa on **your own messages**, you also need to
get those messages into the JSON envelope the builders read. memexa
does not export data from any closed platform itself — it consumes
exports produced by upstream tools, then normalises / extracts /
indexes them.

The table below is the honest per-source status as of `0.1.0`. ✅
means an OSS-only path works end-to-end; ⚠ means it works but
requires a third-party export tool or manual file move; ❌ means
there is no recommended OSS path today and you must wait for the
listed milestone.

| Source         | OS path that works     | Today (v0.1.0)                                                                                       | When better                              |
|----------------|------------------------|------------------------------------------------------------------------------------------------------|------------------------------------------|
| **Email**      | Win / macOS / Linux    | ✅ `memexa init email` wizard (v0.1.1) — 6 providers auto-detected (gmail/outlook/icloud/qq/163/foxmail/ustc), 10 min total | —                                        |
| **Audio**      | Win / macOS / Linux    | ✅ recorder export → Whisper / SenseVoice → JSON → builder                                            | v0.4 (cross-device merge, ECAPA enroll)  |
| **Browser**    | Win / macOS / Linux    | ✅ read Chrome / Firefox SQLite history → builder                                                     | —                                        |
| **Claude Code**| Win / macOS / Linux    | ✅ read `~/.claude/projects/*/conversations.jsonl` → builder                                          | —                                        |
| **WeChat**     | **Windows only**       | ✅ `memexa init wechat` wizard (v0.1.1) wraps [WeChatMsg](https://github.com/LC044/WeChatMsg) — detects existing install or points at release page; you export in WeChatMsg, then `memexa ingest wechat`. macOS / Linux users still have no path (upstream tooling Windows-only). | v0.2+ (auto-download + GUI hand-off) |
| **QQ**         | Win / macOS / Linux    | ⚠ **db-only adapter not yet in OSS**. NapCat / OneBot path is **disabled by default** (Tencent fingerprint-bans accounts that ever ran NapCat — see [`integrations/qq.md`](integrations/qq.md)). To use the db-only path today, manually copy `jarvis/qq_db.py` from the upstream JARVIS repo (762 lines, stdlib only) into `memexa/extraction/qq/`. Clipboard fallback also lives upstream and has not been migrated. | v0.2 (db-only adapter + clipboard fallback migrated; NapCat path removed) |

### Recommended first-day order

1. **Email** (smallest configuration surface, 10 min) — proves the
   backend, the LLM key, and the query layer end-to-end on real data.
2. **Browser** + **Claude Code** (both read local files, 5 min each)
   — adds two more sources without any third-party tool.
3. **Audio** if you have a recorder workflow already, or skip until
   v0.4 lands the cross-device merge.
4. **WeChat** — only if you are on Windows; budget 30-60 min for the
   first export run, less for incremental.
5. **QQ** — only if you are willing to wire in the upstream
   `qq_db.py` manually. Otherwise wait for v0.2.

### Known limitations of v0.1.0

- **QQ db-only adapter** is not yet in the OSS package; the reference
  implementation lives in upstream JARVIS as a single 762-line
  stdlib-only file. v0.2 migrates it.
- **WeChat export is Windows-only** because all three recommended
  exporters (WeChatMsg, wechatDataBackup, PyWxDump) are
  Windows-only. macOS / Linux users have a deployment guide but no
  WeChat-history path. Tracked in v0.3 (WeChat PC backup ingestion)
  but ultimately bounded by upstream tool availability.
- **No 飞书 / 钉钉 adapter** — v0.3.
- **No local-document source** (`.md` / `.pdf` / `.docx` / `.txt`) —
  v0.3.
- **No `--embedded` backend mode** — Tier 1 / Tier 2 still need
  Docker; sqlite-vss alternative ships in v0.3.

These are real, documented gaps — not "stable means perfect", but
"stable means honest about exactly what works today."

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
