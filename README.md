<!--
repository-topics:
  - personal-memory
  - knowledge-graph
  - chinese-nlp
  - retrieval-augmented-generation
  - self-hosted
  - postgresql
  - pgvector
  - bge-m3
  - llm-pipeline
  - cli
  - deliverable-factory
  - action-card
  - report-generation
-->

# Memexa · 镜我

**English** · [中文](README.zh.md)

> **Memory layer for AI agents and humans, on Chinese-native data.**
> Self-hosted memory graph over WeChat / QQ / 飞书 / 钉钉 group chats,
> Chinese email, and Chinese audio. Verbatim storage plus structured
> extraction; queries return cards with per-claim citations back to
> the original sentence.
>
> 🤖 **AI-agent compatible by design.** Most real usage is an AI agent
> (Claude Code, Cursor, Cline, or one you wrote yourself) invoking
> memexa as a subprocess to answer questions on the user's behalf.
> The fourteen query subcommands are a small protocol; the contract
> agents follow is in [`docs/for_agents.md`](docs/for_agents.md).
> Native MCP integration arrives in v0.5; the current first-class
> path is shell subprocess with `--json` output.

[![CI](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml)
[![CodeQL](https://github.com/labazhou2024/memexa/actions/workflows/codeql.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/codeql.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)
[![PyPI](https://img.shields.io/pypi/v/memexa?label=PyPI)](https://pypi.org/project/memexa/)
[![PII scan](https://img.shields.io/badge/PII%20residual-0%20matches-success.svg)](scripts/full_pii_scan.sh)

## Quickstart

Two starting points; pick whichever describes you.

### Humans — 30-second visual

```bash
pip install --pre memexa
memexa demo
```

You will see a synthetic conversation set ingested from six sources
with the stub extractor, followed by five example queries printed to
your terminal — `quick`, `arc`, `timeline`, `pending`, `topic`. No
backend, no LLM, no configuration. This is the honest first look at
what the project does.

### AI agents — subprocess CLI today, MCP in v0.5

```bash
# Agents already work today via subprocess:
pip install --pre memexa
memexa quick "<your question>" --json   # structured output for agent parsing
memexa arc "<person>" --json
# ... fourteen subcommands total, all with --json mode (v0.1.x)
```

The fourteen subcommands plus seven hard rules in
[`docs/for_agents.md`](docs/for_agents.md) are the agent contract.
Native MCP integration (`memexa-mcp` server + `.mcp.json` snippet)
arrives in v0.5; until then shell subprocess is the first-class path
and it works in any agent that has a shell tool.

### What's next for both

To ingest your own data, configure an LLM provider and pick one
source. [`docs/quickstart.md`](docs/quickstart.md) walks through Tier
1 (5 minutes, one source) and Tier 2 (30 minutes, full production
deployment with cron + dashboard).

## What you can ask

| Question pattern | Subcommand | Returns |
|---|---|---|
| Who is Alice? | `arc "Alice"` | Relationship arc, 8 fan-out variants across sources |
| What was the whole story behind X? | `topic "Mac purchase"` | 80–200 cards with citations |
| What did Y professor want? | `person "Y professor"` | Profile article + recent events |
| What is project X across all sources? | `project "X"` | Cross-source pulse, 4 source groups |
| What is on my plate? | `pending` | Active commitments from calendar |
| What did this period look like? | `timeline --start ... --end ...` | Chronological card list |
| Synthesise an answer | `reflect "question"` | LLM-synthesised Markdown |

Fourteen subcommands total. Decision table and composition patterns
are in [`docs/usage_guide.md`](docs/usage_guide.md). See also
[`docs/5_phase_query.md`](docs/5_phase_query.md) for the state-
inference workflow used on yes/no questions.

## Why memexa instead of OpenHuman / MemPalace / ReMe?

In short: verbatim raw storage + LLM-extracted V2 envelope + per-claim
`evidence_quotes` citation + cross-alias canonical id, all on
Chinese-IM-native data sources the adjacent projects do not target.

The full per-capability comparison and the five user scenarios memexa
serves live in [`docs/why.md`](docs/why.md).

## Architecture, one screen

```
   WeChat ─┐                                              ┌─► "Who is X?"           (arc + quick)
   QQ     ─┤                                              ├─► "Group activity last week?" (topic + trends)
   Email  ─┼──► two-LLM extract ──► PG + pgvector ──┤
   Browser─┤    (gate+extract)      memory graph        ├─► "Project X status?"    (project + timeline)
   AI chat─┤                                              ├─► "What does Y want?"   (person)
   Audio  ─┘                                              └─► "My pending actions?" (pending)
        ↑                                                       ↑
   your raw data                                          14 query subcommands
   (local, fully self-hosted)                             (cross-source composable)
```

Full architecture in [`docs/architecture.md`](docs/architecture.md).

## Documentation

| Topic | Link |
|---|---|
| Quickstart (3-tier path: 30 s → 5 min → 30 min) | [`docs/quickstart.md`](docs/quickstart.md) |
| Architecture | [`docs/architecture.md`](docs/architecture.md) |
| Why memexa (vs OpenHuman / MemPalace; 5 user scenarios) | [`docs/why.md`](docs/why.md) |
| Cost estimation (DeepSeek / GPT-4o / Claude monthly) | [`docs/cost.md`](docs/cost.md) |
| 14 query subcommands in depth | [`docs/usage_guide.md`](docs/usage_guide.md) |
| 5-phase state inference | [`docs/5_phase_query.md`](docs/5_phase_query.md) |
| Full environment variables | [`docs/configuration.md`](docs/configuration.md) |
| FAQ / troubleshooting | [`docs/faq.md`](docs/faq.md) · [`docs/troubleshooting.md`](docs/troubleshooting.md) |
| Per-source onboarding | [`docs/integrations/`](docs/integrations/) |
| Cross-platform deployment | [`docs/deployment/`](docs/deployment/) |
| Example walkthroughs (synthetic data) | [`examples/demo_dataset/walkthroughs/`](examples/demo_dataset/walkthroughs/) |
| Case studies | [`docs/case_studies/`](docs/case_studies/) |
| **For AI agents (MCP / integration spec)** | [`docs/for_agents.md`](docs/for_agents.md) |
| Roadmap | [`ROADMAP.md`](ROADMAP.md) |
| Contributing | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Security policy | [`SECURITY.md`](SECURITY.md) |
| Governance | [`GOVERNANCE.md`](GOVERNANCE.md) |

## Two ways to run the LLM

memexa's core is a two-LLM gate-extract pipeline. The OSS ships
everything you need to run it locally with any OpenAI-compatible
endpoint.

```bash
# Default: bundled prompts + your own LLM provider
export MEMEXA_EXTRACTOR_TIER=bundled

# BYO: bring your own prompt for advanced tuning
export MEMEXA_EXTRACTOR_TIER=byo
export MEMEXA_PROMPT_PATH=/path/to/your_prompts.py
```

Recommended provider for Chinese workloads is DeepSeek V4 Flash (gate)
+ V4 Pro (extractor) — typical cost is **¥0.30 per 1 000 messages**.
GPT-4o and Claude 4.x are supported but cost 5–10× more.
See [`docs/cost.md`](docs/cost.md) for the full breakdown.

## License

Apache 2.0. See [`LICENSE`](LICENSE). OSS core stays Apache 2.0
forever.
