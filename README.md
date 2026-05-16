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

> **Your personal Pensieve.**
> Take the data scattered across your six silos, reorganize it around the task you have right now, and walk away with a usable document.

[![CI](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml)
[![CodeQL](https://github.com/labazhou2024/memexa/actions/workflows/codeql.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/codeql.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![PII scan](https://img.shields.io/badge/PII%20residual-0%20matches-success.svg)](scripts/full_pii_scan.sh)

## What is this

`memexa` ingests six categories of everyday Chinese-language data (WeChat, QQ,
email, browser history, AI conversations, voice memos), extracts entities /
relationships / temporal evidence with a two-LLM pipeline, stores them in a
PostgreSQL + pgvector memory graph, and then uses **14 query subcommands** to
pull out whatever you need right now — *who was looking for me? what's the
whole story behind this? what's on my plate? where is project X across all
sources?*

Inspired by the Pensieve in Harry Potter — pour the memories scattered in
your head into a basin, rearrange them, observe them, extract what matters
for the moment.

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

> **v0.1 scope**: complete ingestion + extraction + query + dashboard +
> **5 walkthroughs + 2 case studies**. The "auto-generate deliverables"
> layer ships in [ROADMAP.md](ROADMAP.md) v0.2 as **three universal
> templates** — `weekly` / `brief` / `retro` — covering all five user
> scenarios (knowledge worker / researcher / creator / small-business /
> self-quantified). v0.7 opens up user-authored templates so the
> deliverable layer becomes an ecosystem. For v0.1, compose the 14
> query commands manually for the same effect.
>
> 🚀 **First time here?** Jump to [Example walkthroughs ↓](#-example-walkthroughs--5-reproducible-scenarios)
> to see 5 real scenarios end-to-end.

> 🤖 **AI-agent compatible by design.** Most real users invoke memexa
> through an AI agent — Claude Code, Cursor, Cline, or one they wrote
> themselves — rather than typing subcommands by hand. The 14 query
> subcommands are a small protocol; the protocol document for agents is
> [docs/for_agents.md](docs/for_agents.md) (hard rules, decision table,
> composition patterns, common pitfalls). If you're shipping an
> agent that needs a Chinese-data memory layer, start there.

## Five user scenarios (broad Chinese-market design intent)

memexa targets the broader Chinese-speaking market — not a single
demographic. All five scenarios share the same backbone (ingestion +
two-LLM extract + graph + query); the deliverable templates on top
differ. We ship three universal templates in v0.2 (`weekly` / `brief` /
`retro`) and user-authored templates in v0.7.

| Scenario | Trigger | Available now (v0.1) | v0.2 deliverable |
|---|---|---|---|
| **Knowledge worker / PM / consultant** | Weekly status due; meeting tomorrow | 14 queries by hand | `memexa weekly` / `memexa brief <person>` |
| **Researcher / student / academic** | Experiment done → write report; defense prep | `arc` + `quick` + `topic` | `memexa brief <topic>` / `memexa retro <window>` |
| **Content creator / 自媒体** | Idea recovery; weekly material round-up | `topic` + `timeline` | `memexa retro <window>` (+ v0.7 community templates) |
| **Small business / 个体户** | Client meeting prep; deal recap | `arc` + `project` | `memexa brief <person>` / `memexa retro <window>` |
| **Self-quantified / GTD / privacy power user** | Daily / weekly review | `memexa pending` | `memexa retro <window>` |

## Six data sources

| Source | Builder | Driver |
|---|---|---|
| WeChat | `memexa/ingestion/v5_wechat_batch_builder.py` | `memexa/drivers/backfill_v5_wechat_driver.py` |
| QQ | `memexa/extraction/qq/qq_history_to_batches.py` | `memexa/drivers/backfill_v5_qq_driver.py` |
| Email | `memexa/ingestion/v5_email_batch_builder.py` | `memexa/drivers/backfill_v5_email_driver.py` |
| Browser | `memexa/ingestion/v5_browser_batch_builder.py` | `memexa/drivers/backfill_v5_browser_driver.py` |
| AI chat (Claude Code) | `memexa/extraction/claude_code_to_v5_converter.py` | `memexa/drivers/backfill_v5_cc_driver.py` |
| Audio (microphone) | `memexa/ingestion/v5_audio_batch_builder.py` | `memexa/drivers/backfill_v5_audio_driver.py` |

## Quickstart

```bash
# 1. Install
pip install -e .

# 2. Initialize config (creates ~/.memexa/ with 3 example files)
memexa init                          # → ~/.memexa/{aliases,identity}.yaml + .env

# 3. Start the backend
docker compose -f docker-compose.example.yml up -d

# 4. Run the demo (use --dry-run if backend isn't up yet)
python -m examples.demo_dataset.ingest --dry-run

# 5. Self-check + first query
memexa doctor                        # verify backend + LLM provider
memexa quick "<your keyword>"
```

Full walkthrough: [docs/quickstart.md](docs/quickstart.md)

## Why memexa instead of OpenHuman or MemPalace?

The Chinese-language data memexa ingests (WeChat / QQ / 飞书 / 钉钉
multi-party group chats, Chinese audio, Chinese email threads) demands
something the adjacent OSS memory projects don't provide:

| Capability | OpenHuman (Memory Tree) | MemPalace (Verbatim) | **memexa (V2 envelope)** |
|---|---|---|---|
| Storage model | 3k-token markdown hierarchical summaries | Verbatim text + Zettelkasten literal index | **Verbatim raw + LLM-extracted narrative + entities + evidence_quotes + relations + time_resolutions** |
| Multi-party group-chat role resolution (谁对谁说) | Summary collapses roles | No role concept, literal index only | ✅ V2 envelope `roles[]` + `identity_assertions` |
| Per-claim source citation back to original text | Summary already folded | ✅ verbatim returns raw | ✅ `evidence_quotes` binds every claim to its source sentence + `chunk_id` + raw batch path |
| Cross-alias entity canonicalization (`@张三` / `张老师` / `zhangsan@...` → one id) | Names only, no aliases | No entity concept | ✅ `identity_manifest` + `canonical_id` (4-phase, zero-LLM) |
| Chinese relative-time resolution ("上周三", "前天下午") | Doc-time only, no parsing | None | ✅ `time_resolutions` (ISO 8601 + relative-anchor) |
| Hallucination control on extraction | None — single-pass summarize | None — no extract | ✅ **Two-LLM gate + extract + DeepSeek arbiter** quorum, schema-validated |

**The thesis**: hierarchical summaries (OpenHuman) lose information for
high-accuracy tasks; literal-text Zettelkasten (MemPalace) cannot
disambiguate "who said what to whom when" in a 10-person WeChat
group-chat history. memexa's V2 envelope keeps both — verbatim raw
plus an LLM-extracted structured layer with per-claim citation. When a
deliverable template ([ROADMAP](ROADMAP.md) v0.2) cites a fact, the
user can drill back to the exact original quote in seconds, with the
group-chat speaker correctly identified across all aliases.

This is the lane memexa actually owns. The Chinese-IM data sources
([ROADMAP](ROADMAP.md) v0.3) are how that lane reaches users; the
deliverable templates ([ROADMAP](ROADMAP.md) v0.2) are how it pays off.

## Two-LLM gate-extract architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  6 categories of Chinese-language data                               │
│  WeChat │ QQ │ Email │ Browser │ AI chat │ Voice memo                │
└──────────────────────────────────────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Per-source batch builder  →  JSON envelopes                         │
└──────────────────────────────────────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Stage A: gatekeeper LLM  (filter HIGH/MEDIUM/LOW)                   │
│  Stage B: extractor LLM   (V2 envelope JSON)                         │
│  Stage C: BGE-M3 quorum + arbiter                                    │
│  Stage D: POST → memory_full_v5 bank                                 │
└──────────────────────────────────────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  PostgreSQL + pgvector + BGE-M3 embeddings + temporal links          │
└──────────────────────────────────────────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  14 query subcommands + 5-phase state inference + deliverable layer  │
└──────────────────────────────────────────────────────────────────────┘
```

Full architecture: [docs/architecture.md](docs/architecture.md)

## Query CLI

```bash
memexa <subcmd> "<query>" [options]
```

14 subcommands in three tiers (basic / advanced / composite). The 8 most common:

| Subcommand | Use case |
|---|---|
| `quick` | "Who is X" — point query |
| `topic` | "The whole story of X" — theme expansion (DO NOT use on names! see hard rules) |
| `arc` | "How did I meet X" — relationship arc (preferred for names) |
| `timeline` | "What happened during this period?" — temporal |
| `person` | "Status of professor Y" — person profile |
| `project` | "Cross-source pulse of project Z" |
| `pending` | "What's on my plate" — active commitments |
| `reflect` | LLM-synthesized answer |

Full usage: [docs/usage_guide.md](docs/usage_guide.md)

## 📖 Example walkthroughs — 5 reproducible scenarios

> Install → `make demo-ingest` → follow a walkthrough → see memexa in action.
> Everything runs on a synthetic dataset (Alice / Bob / Carol /
> advisor@example.com). Anyone can reproduce 1:1, **no real personal data**.

```
┌────────────────────────────────────────────────────────────────────┐
│                  What question are you asking?                     │
└────────────────────────────────────────────────────────────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
   "Who is X?"        "Group activity last week?"  "What's on my plate?"
   01_who_is_alice    02_weekly_team               05_my_pending
   arc + quick         topic + trends               pending + quick
        │                    │                    │
        ▼                    ▼                    ▼
   "What does Y want?" "Project X status?"
   04_advisor_said     03_project_status
   person              project + timeline
```

**5 walkthroughs** (5–10 min each):

| # | Walkthrough | Scenario | Command combo |
|---|---|---|---|
| [01](examples/demo_dataset/walkthroughs/01_who_is_alice.md) | Who is Alice? | "How do I know X?" | `arc` + `quick` |
| [02](examples/demo_dataset/walkthroughs/02_weekly_team_summary.md) | Weekly team summary | "What did the group do last week?" | `topic` + `trends` |
| [03](examples/demo_dataset/walkthroughs/03_project_status_check.md) | Project status check | "Where is project X?" | `project` + `timeline` |
| [04](examples/demo_dataset/walkthroughs/04_what_did_advisor_say.md) | What did advisor say? | "What does Y (advisor/boss) want?" | `person` |
| [05](examples/demo_dataset/walkthroughs/05_my_pending_actions.md) | My pending actions | "What's on my plate?" | `pending` + `quick` |

**2 case studies** (methodology, 10–15 min each):

| # | Case study | Audience | Output |
|---|---|---|---|
| [01](docs/case_studies/01_lab_report_pipeline.md) | Late-bound deliverable pipeline | Anyone recovering from a missed deadline | LaTeX → PDF + action card (20 min end-to-end) |
| [02](docs/case_studies/02_meeting_brief_pattern.md) | 5-minute meeting brief | Anyone prepping for a meeting | 4-section Markdown brief (5 min end-to-end) |

→ Index pages: [`examples/demo_dataset/walkthroughs/`](examples/demo_dataset/walkthroughs/) · [`docs/case_studies/`](docs/case_studies/)

## Two ways to run the LLM

memexa's core is a two-LLM extract pipeline. The OSS ships everything you need
to run it locally.

```bash
# Default: OSS bundled prompt + your own LLM provider
#   Set OpenAI / DeepSeek / local vLLM base_url + key in .env
export MEMEXA_EXTRACTOR_TIER=bundled

# BYO: bring your own prompt (for advanced users with existing prompt tuning)
export MEMEXA_EXTRACTOR_TIER=byo
export MEMEXA_PROMPT_PATH=/path/to/your_prompts.py
```

**Roadmap**: v0.5 will add an optional paid API endpoint, billed per token
(OpenAI-style, no subscription). This is an upgrade path, **not a gate** —
the OSS stays fully usable forever. See [docs/api_roadmap.md](docs/api_roadmap.md).

## Documentation index

| Topic | Link |
|---|---|
| 30-minute first run | [docs/quickstart.md](docs/quickstart.md) |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| 14 query subcommands in depth | [docs/usage_guide.md](docs/usage_guide.md) |
| 5-phase state inference | [docs/5_phase_query.md](docs/5_phase_query.md) |
| Full environment variables | [docs/configuration.md](docs/configuration.md) |
| FAQ | [docs/faq.md](docs/faq.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Performance numbers | [docs/performance.md](docs/performance.md) |
| Per-source onboarding | [docs/integrations/](docs/integrations/) |
| macOS / Windows / Linux deployment | [docs/deployment/](docs/deployment/) |
| **Example walkthroughs (synthetic data)** | [examples/demo_dataset/walkthroughs/](examples/demo_dataset/walkthroughs/) |
| **Case studies (methodology)** | [docs/case_studies/](docs/case_studies/) |
| **🤖 For AI agents (protocol doc)** | [docs/for_agents.md](docs/for_agents.md) |
| Paid API endpoint (roadmap) | [docs/api_roadmap.md](docs/api_roadmap.md) |
| Engineering lessons learned | [docs/lessons_learned/](docs/lessons_learned/) |
| Contribution guide | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Code of conduct | [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |
| Security policy | [SECURITY.md](SECURITY.md) |
| Governance | [GOVERNANCE.md](GOVERNANCE.md) |
| Roadmap | [ROADMAP.md](ROADMAP.md) |
| Support | [SUPPORT.md](SUPPORT.md) |
| Citation | [CITATION.cff](CITATION.cff) |

## License

Apache 2.0. See [LICENSE](LICENSE).

OSS core = Apache 2.0, unrestricted commercial use. The optional paid API
endpoint, when it ships, will have its own service terms — see
[docs/api_roadmap.md](docs/api_roadmap.md).
