# Roadmap

**English** · [中文](ROADMAP.zh.md)

> Aspirational. Not a commitment. Things move when they move.

## Positioning

memexa is a self-hosted memory graph for Chinese-native multi-party
data — WeChat / QQ / 飞书 / 钉钉 group chats, Chinese email threads,
Chinese audio recordings. Each message is stored verbatim and
extracted into a structured envelope (narrative + entities +
per-claim evidence quotes + time resolutions + relation assertions)
using a two-LLM pipeline. Queries cross sources, return cards with
citations, and feed into deliverable templates.

The project occupies a lane the adjacent OSS memory projects do not
address: Chinese-native data sources, high-accuracy citation back to
source sentences, and CLI-first design usable by both humans and AI
agents.

For the per-capability comparison against neighbouring projects
(OpenHuman, MemPalace, ReMe) and the five user scenarios memexa is
designed for, see [docs/why.md](docs/why.md).

## Current state (v0.1.0 on PyPI, 2026-05-17)

Shipped:

- CLI: `init` / `version` / `config` / `doctor` / `demo` / `query`.
- Fourteen query subcommands (seven basic + seven advanced) with
  `--json` mode accepted both at the top level (`memexa --json query
  quick "X"`) and at the subcommand level (`memexa quick "X" --json`).
- `memexa demo`: thirty-second onboarding on the bundled synthetic
  dataset (no Docker, no LLM key, no configuration).
- Six ingestion sources: WeChat, QQ, email, browser, Claude Code, audio.
- Two-LLM gate-extract pipeline with DeepSeek arbiter quorum.
- PostgreSQL + pgvector backend (Hindsight FastAPI in docker compose).
- Live dashboard on port 8765 with seven panels.
- Deployment guides for macOS and Windows; Linux via the
  docker-compose path.
- Ten tests across unit + integration, six CI workflows
  (lint / test / codeql / security / release-drafter / dependabot),
  CodeQL clean.
- Dependabot security alerts and automated security fixes enabled.
- Fresh-clone smoke test passes on Win + macOS + Linux ×
  Python 3.10 / 3.11 / 3.12 (CI matrix; macOS-py3.10 cell intentionally
  excluded due to a transitive wheel gap, see ci.yml).

Known limitations in v0.1.0 (documented gaps; honest baseline rather
than silent surprises — see [`docs/quickstart.md#tier-3`](docs/quickstart.md)
for the full per-source status table):

- **QQ db-only adapter not yet in OSS.** The recommended QQ read
  path lives in upstream JARVIS as `jarvis/qq_db.py` (single file,
  762 lines, stdlib only). OSS v0.1.0 users wire it in manually;
  v0.2 migrates it. NapCat / OneBot path is **disabled by default**
  due to Tencent's 2025-09-05 fingerprint-ban wave; only
  research-disposable accounts should override with
  `MEMEXA_QQ_NAPCAT_FORCE=1`.
- **WeChat export is Windows-only**, bounded by upstream tool
  availability (WeChatMsg, wechatDataBackup, PyWxDump are all
  Windows-only). macOS / Linux users can deploy memexa but cannot
  ingest WeChat history today.

Not yet shipped:

- One-line onboarding (no Docker, no LLM API key, no configuration).
- Deliverable templates that produce ready-to-use Markdown documents.
- Chinese IM sources beyond WeChat and QQ (飞书, 钉钉).
- Local document source (.md / .pdf / .docx / .txt).
- Embedded backend mode for users who want to try one source without
  Docker.
- MCP server entry point for direct agent integration.
- Pluggable backend (currently locked to Hindsight FastAPI).

## v0.1.x — close out to stable

The remaining v0.1 work unblocks the first-experience path for both
**human users** and **AI agents**. v0.1.0 is cut from a green rc only
after every item in this list is true.

- `memexa demo` subcommand: a thirty-second walkthrough that uses the
  bundled synthetic dataset and the stub extractor. No Docker, no LLM
  key, no configuration.
- `--json` output mode for all fourteen query subcommands, so agents
  invoking memexa via shell can `json.loads()` the result directly
  instead of parsing text. The subprocess-CLI path is the current
  first-class agent integration; native MCP server arrives in v0.5.
- `Makefile` lint and format targets point at `memexa tests` rather
  than the deprecated `src` path.
- `CHANGELOG.md` known-limitation about PyPI availability removed
  (the package has been on PyPI since rc1).
- At least one non-author issue, discussion, or pull request landed.
- At least one full week elapsed since the most recent critical bug
  fix.

## v0.2 — agent workflow templates

memexa is an **agent backbone**, not an end-user product. The typical
flow is: human → Claude Code / Cursor / Cline → subprocess CLI to
memexa → composed Markdown answer. v0.2 ships three workflow **spec
documents** that tell the agent how to orchestrate the fourteen
subcommands for the most common deliverables — **no new Python code,
no new CLI subcommands**.

- `docs/templates/weekly.md` — cross-source weekly report workflow.
- `docs/templates/brief.md` — pre-meeting / pre-talk brief workflow.
- `docs/templates/retro.md` — time-window recap workflow.
- `docs/templates/README.md` — spec system overview and customisation
  guide (users add their own templates by copying a spec file, no
  code; the v0.7 milestone formalises the submission path).
- One case study walking Claude Code through the weekly workflow on
  the bundled demo dataset, so newcomers can see the agent + memexa
  interaction concretely.

v0.2 ships when the weekly spec lands on main and one walkthrough
demonstrates a real Claude Code session producing a citation-bearing
report.

## v0.3 — Chinese IM and identity deepening

Capabilities proven LIVE in upstream development for ≥ 4 weeks
without rollback reflow to memexa here. Each item requires a
PII / abstraction audit before merge.

- QQ db-only adapter; replaces the OSS-side NapCat path, which is
  removed.
- Local document source (.md / .pdf / .docx / .txt with file-sha1
  binding so moves and renames do not trigger re-extraction).
- Identity manifest auto-learning for cross-alias entity resolution.
- WeChat PC backup ingestion.
- 飞书 (Lark) export adapter.
- 钉钉 (DingTalk) export adapter.
- Embedded backend mode (`memexa backend --embedded`): sqlite-vss
  alternative to docker-compose for users who only need one source.

## v0.4 — audio and voice

- SenseVoice ASR (Chinese CER ~6.8 %, replaces Whisper).
- Cross-session voice manifest (ECAPA-based speaker enrollment).
- Multi-device audio merge (recording-pen, iPhone Voice Memos,
  classroom dictation).
- `memexa 会议纪要 <session>`: meeting summary deliverable.

## v0.5 — AI agent integration completion

The `subprocess` CLI path is the v0.1.x first-class agent integration.
v0.5 promotes that to a native MCP integration so agents can invoke
memexa as a structured tool without spawning a shell.

- `memexa-mcp`: Model Context Protocol server entry point exposing all
  fourteen query subcommands and the v0.2 workflow specs as MCP tools.
- Official Claude Code, Cursor, and Cline integration examples under
  `examples/agent_integrations/` with a one-line `.mcp.json` snippet.
- `docs/for_agents.md` updated to v2 covering MCP spec, function-call
  protocol, and agent skill specification.

## v0.6 — pluggable LLM and pluggable backend

memexa stops competing with adjacent backends and federates to them
as a user choice.

- LLM provider abstraction: adapters for OpenAI, DeepSeek, Qwen,
  vLLM, Ollama, LiteLLM, OpenRouter, and self-hosted OpenAI-compatible
  endpoints.
- Backend adapter: `memexa --backend=chroma|mem0|mempalace|hindsight`
  switches the storage layer without changing the query or deliverable
  layer.
- Schema-drift sanitizer so extractor model swaps do not break
  database inserts.

## v0.7 — user-authored workflow spec templates

The workflow-template layer becomes an ecosystem rather than a fixed
list of three. Users author their own spec documents the same way the
v0.2 specs are written — Markdown that the agent reads at runtime.

- Spec loading mechanism: memexa discovers user spec files under
  `~/.memexa/templates/` in addition to the bundled
  `docs/templates/`. Agents see the combined list as available tools.
- Six example community spec templates under
  `examples/community_templates/` covering distinct user scenarios
  (client follow-up, study notes, reading brief, content backlog,
  daily retro, defense brief).
- Spec submission path via pull request into
  `examples/community_templates/`.

No new Python code is required to add a template; v0.7 is documented
schema + a submission path.

## v0.8+ — desktop GUI (optional, condition-gated)

Desktop GUI is acknowledged as a real driver of broader audience reach
but it is not on the critical path. memexa is agent-backbone first;
a desktop shell is layered on top only when the foundation is solid.

Conditions for opening a v0.8 GUI exploration:

- v0.5 MCP integration shipped and at least one well-known AI agent
  (Claude Code / Cursor / Cline) has the memexa server snippet pinned
  in their community docs.
- v0.7 community templates ≥ 5 merged.
- Roughly 3 000 GitHub stars and 5 000 PyPI monthly downloads.

If those conditions hold, an evaluation PR opens to pick one of:

- (a) Tauri shell wrapping the CLI as backend.
- (b) Streamlit local web UI.
- (c) Stay terminal + agent only.

This milestone is explicitly **not** committed work; the project may
remain terminal-only forever and still meet its goals.

## v1.0 — stable schema commitment

- V2 envelope frozen; migrations only via additive fields.
- CLI arguments frozen; deprecations only with one-release warning.
- On-disk layout frozen; bumping bumps a major version.
- Backend adapter interface frozen.
- At least three external contributors with merged pull requests.
- At least five non-author production users documented in case
  studies or community templates.

## Permanently out of scope

- Desktop GUI applications.
- Bulk Western-SaaS OAuth integrations (Gmail, Slack, Notion, Linear,
  Jira via Composio-style gateways).
- Mobile or web UI rewrites.
- Multi-tenant hosted service.
- Voice synthesis or autonomous agent loops.
- Anything that prevents users from owning their own data.

## How to propose a change

Open a Discussion in the **Ideas** category with what to add or drop,
which milestone it fits, and whether you can implement it. The
maintainer responds yes / no / later with a paragraph of reasoning.
