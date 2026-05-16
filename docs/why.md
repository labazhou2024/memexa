# Why memexa

**English** · [中文](why.zh.md)

This page collects the design intent, the per-capability comparison
against neighbouring OSS memory projects, and the user scenarios memexa
is designed for. If you are looking for the install path, see
[`docs/quickstart.md`](quickstart.md). If you are looking for the
forward plan, see [`ROADMAP.md`](../ROADMAP.md).

## Design intent

The Chinese-language data this project ingests — WeChat / QQ / 飞书 /
钉钉 multi-party group chats, Chinese email threads, Chinese audio —
demands a different memory layer than the one Western dev tools
typically build. Three properties matter:

1. **Verbatim raw storage alongside structured extraction.** Group
   chats are heterogeneous: some messages are jokes, some are critical
   commitments. Summarising them up front loses information that
   matters later. memexa stores every original message verbatim and
   produces an LLM-extracted envelope alongside, never replacing the
   raw text.

2. **Per-claim citation back to the source sentence.** When a
   deliverable template asserts "Y professor moved the group meeting
   to Wednesday 3 pm," the user can click through to the original
   message and see who said it, in which group, at what time. This is
   the `evidence_quotes` field on every extracted envelope.

3. **Cross-alias entity resolution.** A single person in a Chinese
   group chat surfaces as `@张三`, `张老师`, `zhangsan@gmail.com`,
   sometimes as a real-name signature. memexa learns these aliases
   automatically with a four-phase zero-LLM algorithm and binds them
   to a single canonical id.

These three properties — verbatim + citation + canonicalization —
together let the system handle high-accuracy retrieval over messy
multi-party Chinese data, which is the actual use case for almost all
the project's target users.

## Comparison to neighbouring projects

The OSS memory landscape in 2026 has three other major projects that a
prospective user might consider:

- **OpenHuman** (`tinyhumansai/openhuman`): Rust + Tauri desktop
  assistant with 118+ Western SaaS integrations.
- **MemPalace** (`MemPalace/mempalace`): Python dev tool with verbatim
  storage and benchmark-driven retrieval (LongMemEval R@5 = 96.6 %).
- **ReMe** (`agentscope-ai/ReMe`): Memory management kit for agents,
  backed by the AgentScope ecosystem.

The capability matrix below is the honest answer to "why pick memexa
instead":

| Capability | OpenHuman | MemPalace | ReMe | **memexa** |
|---|---|---|---|---|
| Storage model | 3 k-token markdown hierarchical summaries | Verbatim text with Zettelkasten literal index | Agent-context tool kit | **Verbatim raw + LLM-extracted V2 envelope** |
| Multi-party group-chat role resolution | Summary collapses speaker roles | No role concept | Agent-level | ✅ V2 envelope `roles[]` + `identity_assertions` |
| Per-claim citation back to original sentence | Summary already folded | ✅ verbatim returns raw | Agent-level | ✅ `evidence_quotes` binds every claim to its source sentence with `chunk_id` |
| Cross-alias canonical id | Names only, no aliases | No entity concept | Agent-level | ✅ `identity_manifest` + four-phase zero-LLM algorithm |
| Chinese relative-time parsing ("上周三") | Doc-time only | None | None | ✅ `time_resolutions` (ISO 8601 + relative anchor) |
| Hallucination control on extraction | None — single-pass summarize | None — no extraction | None | ✅ Two-LLM gate + extract + DeepSeek arbiter quorum |
| Native Chinese IM sources (WeChat / QQ / 飞书 / 钉钉) | None (Western OAuth only) | None | None | ✅ WeChat + QQ shipped; 飞书 + 钉钉 in v0.3 |

The thesis is straightforward. Hierarchical summaries (OpenHuman) lose
information; literal-text Zettelkasten (MemPalace) cannot disambiguate
"who said what to whom" in a ten-person 微信群 chat history; agent-
context kits (ReMe) operate at a different layer entirely. memexa
keeps both verbatim raw and an LLM-extracted structured layer with
per-claim citation, and ships the Chinese-IM ingestion paths the other
projects have no reason to build.

## Why memexa is agent-first, and why that matters

A second differentiator that the comparison table does not surface
clearly: memexa is the only project in this list with a **dedicated
agent protocol document**. The fourteen subcommands and seven hard
rules in [`for_agents.md`](for_agents.md) read like an API contract
written for an LLM — strict, dense, with explicit failure modes that
agents commonly hit (calling `topic` on a person's name, wrapping
external parallelism around `topic`/`arc`, treating `pending` as a
semantic recall instead of a calendar read).

This matters because **the typical memexa user is not the human
typing commands** — it is an AI agent (Claude Code, Cursor, Cline, or
one the user wrote themselves) invoking memexa as a subprocess on the
user's behalf. The CLI surface and the agent protocol are the same
fourteen subcommands; the agent reads the protocol, the human reads
the usage guide, both produce the same calls.

This is why v0.2 ships **markdown workflow specs** (`docs/templates/
weekly.md` etc.) instead of new CLI subcommands: the agent reads the
spec at runtime and orchestrates the existing fourteen subcommands.
Users add their own templates by copying a markdown file; no Python
required.

## Glossary

A few project-specific terms used throughout the documentation:

- **Verbatim raw + V2 envelope**: every ingested message is stored
  unchanged; alongside it, a separate "V2 envelope" record carries
  the LLM-extracted structured layer (narrative, entities,
  evidence_quotes, time_resolutions, identity_assertions,
  relation_assertions). Raw and extracted always coexist; the
  extraction never replaces the original.

- **Reflow**: porting a capability that has been LIVE for ≥ 4 weeks
  in upstream development (without rollback) into the public memexa
  repository. Each reflow requires a PII / abstraction audit before
  merge so that no private data, real names, or system-specific
  paths leak from upstream into the OSS codebase. The Chinese-IM
  ingestion reflow (v0.3) and the audio + voice reflow (v0.4) are
  the two main reflow milestones currently planned.

- **Chinese-IM reflow** (v0.3 milestone): porting the QQ db-only
  adapter (SQLCipher direct read), the local document source
  (.md/.pdf/.docx/.txt with file-sha1 binding), the identity
  manifest auto-learning algorithm, the WeChat PC backup ingestion
  path, and new 飞书 / 钉钉 export adapters. The goal is to expand
  the Chinese-IM coverage from WeChat + QQ today to four major
  Chinese IM platforms plus local documents.

- **Audio + voice reflow** (v0.4 milestone): porting the SenseVoice
  ASR engine (Chinese CER ~6.8 %, 5× realtime, replacing Whisper),
  the cross-session voice manifest (ECAPA-based "self vs speaker N"
  resolution), and the multi-device audio merge (recording-pen +
  phone + classroom voice memos deduplicated by content fingerprint).
  Goal: Chinese audio recordings become a first-class source with
  speaker disambiguation, not just transcripts.

- **Workflow spec** (v0.2 milestone): a Markdown document under
  `docs/templates/` that describes how an AI agent should orchestrate
  the fourteen query subcommands to produce a specific deliverable
  (weekly report, meeting brief, time-window recap). The spec is
  read by the agent at runtime — no Python code is added to memexa
  to ship a new spec.

## User scenarios

memexa is designed for five scenarios, all of which share the same
backbone (ingestion + extract + graph + query) and differ only in the
deliverable template on top.

| Scenario | Typical user | Primary workflow (v0.2 +) |
|---|---|---|
| **Knowledge worker / PM / consultant** | Bilingual office worker, freelancer | `weekly` spec (cross-source weekly recap), `brief` spec (meeting prep on a person) |
| **Researcher / student / academic** | University student, graduate research | `brief` spec (defense or talk prep on a topic), `retro` spec (project recap over a window) |
| **Content creator / 自媒体** | 公众号 author, 知乎 answerer, 小红书 creator | `retro` spec (idea recovery), v0.7 community spec templates for material library |
| **Small business / 个体户** | Freelance professional, small studio owner | `brief` spec (client / lead prep), `retro` spec (deal recap) |
| **Self-quantified / GTD / privacy power user** | Self-hosted enthusiast, GTD practitioner | `memexa pending` query (cross-source to-do), `retro` spec (weekly review) |

All five scenarios are first-class. v0.2 ships three workflow specs
(`weekly` / `brief` / `retro` as markdown documents under
`docs/templates/`) that cover the most common needs across all
scenarios. The agent reads the spec at runtime, runs the relevant
subset of the fourteen query subcommands, and composes a Markdown
report with per-claim citations. v0.7 opens the same authoring path
to users — a new template is a new markdown file, no Python required.

## V2 envelope field reference

For developers who want to understand what the extractor actually
produces, every extracted card carries the following fields:

- `narrative`: Chinese-language summary of the original event,
  ≤ 200 characters.
- `entities`: list of `{surface, kind, canonical_id?}` extracted from
  the source. `kind` is one of `person` / `org` / `place` / `event` /
  `thing`.
- `evidence_quotes`: list of original sentences from the source that
  justify the claims in `narrative`. **Every claim must be backed by
  at least one quote**; this is enforced at extraction time.
- `time_resolutions`: list of `{surface, iso_start, iso_end?,
  anchor?}` — turns relative times ("上周三下午", "前天") into
  absolute ISO 8601 timestamps.
- `identity_assertions`: list of `{surface_a, surface_b, confidence}`
  — claims that two surface forms refer to the same canonical entity.
- `relation_assertions`: list of `{subject, predicate, object,
  evidence}` — extracted relationships ("Y professor recommended
  DDIA chapter 5 to Alice").
- `roles[]`: per-message speaker role tags (sender / mentioned /
  audience), allowing group-chat speaker disambiguation.
- `salience`: 0.0 - 1.0 importance score; cards below the default
  `0.4` floor are filtered from `session-context` injection.
- `chunk_id` + `source_origin`: pointer back to the raw batch JSON
  file the card was extracted from, so the user can always retrieve
  the original message context.

The thesis above is real because these fields are populated by every
card; the citation flow is not an aspiration but a tested invariant.
