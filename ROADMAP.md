# Roadmap

**English** · [中文](ROADMAP.zh.md)

> Aspirational. Not a commitment. Things move when they move.

## Positioning (revised 2026-05-16)

**Stated goal: become the #1 OSS memory-graph project in China for
Chinese-native multi-party data.**

memexa targets **the broader Chinese-speaking market** — *not* a single
demographic. Two compounding moats:

1. **Chinese-native data sources**: WeChat / QQ / 飞书 / 钉钉 multi-party
   group chats, Chinese audio, Chinese email threads — OpenHuman /
   MemPalace's Western-SaaS-OAuth + summary / verbatim model can't
   handle these well.
2. **V2 envelope extraction with per-claim citation**: verbatim raw +
   LLM-extracted narrative + `evidence_quotes` + `identity_assertions`
   + `time_resolutions` + `relation_assertions`. Hierarchical summaries
   (OpenHuman) lose information; literal Zettelkasten (MemPalace)
   can't resolve "who said what to whom" in group chats. memexa keeps
   both layers and binds every claim back to its source sentence.

Adjacent OSS projects (OpenHuman, MemPalace, ReMe) cover the English /
Western-SaaS / desktop-assistant / dev-tool-MCP lanes. memexa stays in
the Chinese-native + high-accuracy-citation lane on purpose.

**Five user scenarios memexa is designed for:**

| Scenario | Typical user | Primary deliverable |
|---|---|---|
| Knowledge worker / PM / consultant | bilingual office worker, freelancer | `weekly` (cross-source weekly report), `brief <person>` (meeting prep) |
| Researcher / student / academic | university student, research-track grad student | `brief <topic>` (defense / talk prep), `retro <window>` (project recap) |
| Content creator / 自媒体 | 公众号 author, 知乎 answerer, Xiaohongshu creator | `retro <window>` (idea recovery), upcoming `notebook` deliverable |
| Small business / 个体户 | freelance professional, small studio owner | `brief <person>` (client / lead prep), `retro <window>` (deal recap) |
| Self-quantified / GTD / privacy power user | self-hosted enthusiast, GTD practitioner | `pending` (cross-source to-do), `retro <window>` (weekly review) |

All five share the same backbone (ingestion + extract + graph + query +
deliverable templates). Templates differ; we ship the three most
universal (`weekly` / `brief` / `retro`) in v0.2 and let users author
their own in v0.7.

---

## v0.1.x — close out stable (1–2 weeks)

- [x] CLI dispatcher (`memexa init / version / config / doctor / query`)
- [x] PII scrubbing pre-commit hook
- [x] Demo dataset (6 sources, public-domain)
- [x] Direct psycopg2 PG access (no ssh shell-out by default)
- [x] Hindsight failover URL with automatic retry
- [x] 14 query subcommands documented
- [x] `memexa doctor` round-trips LLM provider
- [x] PII residual scanner with self-referential SKIP-list
- [x] **CodeQL: 7 open errors → 0** (PR #12, 2026-05-15)
- [x] **Dependabot vulnerability alerts ENABLED + automated security fixes ENABLED**
- [x] **Fresh-clone smoke test passes on Win + macOS + Linux × Python 3.10/3.11/3.12 in CI** (verified by PR #12, 18/18 green)
- [ ] CHANGELOG `known-limitation` line about PyPI availability — update to reflect LIVE on PyPI
- [ ] Cut v0.1.0 stable after ≥ 1 week without a critical bug and ≥ 1 non-author issue / discussion

## v0.2 — three universal deliverable templates (4–8 weeks)

The core query system gives you raw signals. v0.2 stitches them into
copy-paste-ready documents. We ship **three** templates that cover all
five user scenarios — not five separate templates. Each template = one
subcommand + a Markdown layout + a couple of `memory_query` calls.

- [ ] `memexa weekly` — cross-source weekly report
  (git log + email + IM digest + project pulse → one-page Markdown)
- [ ] `memexa brief <person|topic>` — pre-meeting / pre-talk / pre-call brief
  (baseline / last interaction / open threads / landmines)
- [ ] `memexa retro <window>` — time-window recap
  (key events / commitments closed / commitments outstanding / surprises)
- [ ] Template engine: shared Markdown layout + LLM provider abstraction
  so v0.7 user-authored templates plug into the same pipeline
- [ ] Three reproducible walkthroughs under `examples/deliverables/` —
  one per user scenario (knowledge worker / researcher / freelancer)

**Cut from earlier draft** (deferred or absorbed into the three above):
`lab-report` / `action-card` / `dashboard` — too narrow for general
Chinese-market reach; `retro` covers the underlying pattern.

## v0.3 — Chinese IM + identity deepening (reflow JARVIS, 4–6 weeks)

JARVIS upstream has these LIVE for months; the OSS migration is the next
push. Each item links to the JARVIS HANDOFF entry that proves the
capability is production-tested.

- [ ] **QQ db-only adapter** — port `jarvis/qq_db.py` (762 lines, stdlib
      only) → `memexa/extraction/qq/qq_db.py`; rip the OSS-side NapCat
      adapter
      _([JARVIS §C.-24 LIVE 2026-05-15](https://github.com/labazhou2024/memexa))_
- [ ] **doc source = 7th source** — local document graph integration
      (.md / .pdf / .docx / .txt), file_sha1-bound not path-bound, so
      moves and renames don't trigger re-extraction
      _([JARVIS §C.-22 → §C.-26 buildup, v0.5 LIVE 2026-05-15](https://github.com/labazhou2024/memexa))_
- [ ] **Identity manifest auto-learning** — cross-alias entity
      resolution (`@张三` / `张老师` / `zhangsan@example.com` →
      one canonical id), zero-LLM 4-phase algorithm
      _(USAGE_MANUAL §19 in JARVIS, LIVE 2026-05-10)_
- [ ] **WeChat PC backup ingestion** — beyond live MicroMsg.db,
      support PC WeChat backup directory (so users on locked-down
      devices can still ingest)
- [ ] **飞书 (Lark) export** — adapter for the JSON export Lark
      provides for personal accounts
- [ ] **钉钉 (DingTalk) export** — adapter for the chat export
- [ ] **Cut from earlier draft**: Discord / Slack / Telegram / iMessage
      — OpenHuman owns the Western-SaaS lane via Composio OAuth; not
      our market

## v0.4 — Audio + voice-id (reflow JARVIS, 3–5 weeks)

- [ ] **SenseVoice ASR reflow** — JARVIS audio v2 ships SenseVoice
      replacing Whisper: 6.8% CER (vs Whisper ~10%), 5× realtime,
      eliminated English-hallucination on Chinese audio
      _([JARVIS §C.-23/-28 LIVE 2026-05-15/16](https://github.com/labazhou2024/memexa))_
- [ ] **Cross-session voice manifest** — ECAPA embedding cross-session
      voting + enroll-user-voice workflow; identify "self" vs "speaker
      N" across recordings
- [ ] **Multi-device audio merge** — recording pen (USB-MSC) + iPhone
      voice memos + classroom dictation, dedup by content fingerprint
- [ ] `memexa 会议纪要 <session>` — auto extract action items + key
      decisions from a meeting recording (combines audio source +
      `brief` template from v0.2)

## v0.5 — AI agent integration as first-class (3–4 weeks)

- [ ] **`memexa-mcp` MCP server entry-point** — official Model Context
      Protocol server so Claude Code / Cursor / Cline / any MCP-compatible
      agent reads memexa as a memory backend
- [ ] **Official `.mcp.json` template** in `examples/agent_integrations/`
- [ ] **Cursor / Cline integration docs** — step-by-step
- [ ] **`docs/for_agents.md` v2** — covering MCP spec, function-call
      protocols, agent skill spec
- [ ] **Cron + dashboard reflow** — Win schtask + Mac LaunchAgent +
      Linux systemd templates for the 6-hour incremental cron, plus
      the sys_monitor dashboard (port 8765, 7 panels)
      _([JARVIS HANDOFF §E LIVE; Mac failover wrappers §C.-29 LIVE 2026-05-15](https://github.com/labazhou2024/memexa))_

## v0.6 — Pluggable LLM + pluggable backend (4–6 weeks)

The key strategic move: **memexa stops competing with mem0 / MemPalace
on backend; we federate to them as user choice.**

- [ ] **LLM provider abstraction** — adapters for OpenAI / DeepSeek /
      Qwen3 / vLLM / Ollama / LiteLLM proxy / OpenRouter / 自部署
      OpenAI-compatible endpoint
- [ ] **Backend adapter** — `memexa --backend=chroma|mem0|mempalace|hindsight`
      switches the storage layer without changing the query / deliverable layer
- [ ] **Schema drift sanitize reflow** — JARVIS §C.-29 §10 ships
      `_normalize_llm_card` covering 5 new drift classes (date-only ISO,
      time-only ISO, role=relay, related_episode dict, when_end None);
      port to OSS so extractor model swaps don't break PG inserts
- [ ] **5-driver rc=2 graceful-skip pattern** — reflow JARVIS
      [§C.-29 §2 LIVE 2026-05-15](https://github.com/labazhou2024/memexa);
      transient backend outage no longer poisons cron

## v0.7 — User-authored deliverable templates (4 weeks)

The deliverable layer becomes an ecosystem, not a fixed list of three.

- [ ] **Template authoring spec** — `~/.memexa/templates/<name>.yaml`
      with declared inputs (which subcmds to call), Markdown / LaTeX
      layout, and optional LLM-rendering step
- [ ] **Six example user-authored templates** (one per scenario type):
      - 客户跟进 (small-business)
      - 学习笔记 (researcher / student)
      - 阅读简报 (knowledge worker)
      - 创作素材库 (content creator)
      - 每日复盘 (GTD / self-quantified)
      - 答辩 brief (researcher / student)
- [ ] **Template submission contrib path** — community templates land
      in `examples/community_templates/` via PR, not built-in

## v1.0 — stable schema commitment + ecosystem (≥ 6 months out)

- [ ] V2 envelope frozen; migrations only via additive fields
- [ ] CLI args frozen; deprecations only with one-release warning
- [ ] On-disk layout frozen; bumping bumps a major version
- [ ] Backend adapter interface frozen
- [ ] ≥ 4 deliverable templates LIVE (3 builtin + ≥ 1 community)
- [ ] ≥ 4 Chinese-IM sources LIVE (WeChat / QQ / 飞书 / 钉钉)
- [ ] ≥ 3 external contributors with merged PRs
- [ ] ≥ 5 real production users (non-author) documented in
      `docs/case_studies/` or `examples/community_templates/`

## Permanently out of scope (expanded 2026-05-16)

- ❌ **Desktop GUI** — OpenHuman owns this lane via Tauri + 118 OAuth
- ❌ **Western-SaaS OAuth bulk** — Gmail / Slack / Notion / Linear /
      Jira via Composio is OpenHuman's moat; we do not chase
- ❌ **Mobile / web UI rewrites**
- ❌ **Multi-tenant hosted service**
- ❌ **Voice synthesis or agent loops** — distinct project category
- ❌ **English single-thread benchmark race** (LongMemEval / LoCoMo /
      MemBench on English ChatGPT-style threads) — MemPalace owns these.
      memexa **will** publish its own benchmark, but on Chinese-native
      multi-party data: group-chat speaker disambiguation, cross-alias
      entity resolution accuracy, relative-time anchor correctness,
      and `evidence_quotes` citation-back-to-source-sentence precision.
      Target: ship `benchmarks/cn_multiparty/` in v0.3 with 5 reproducible
      Chinese WeChat / QQ scenarios.
- ❌ **Anything that prevents you from owning your own data**

## How to propose a roadmap change

Open a Discussion in the **Ideas** category with:

- What you would add / drop.
- Which milestone it fits.
- Whether you are volunteering to implement it.
- Which user scenario it serves (knowledge worker / researcher /
  creator / small-business / self-quantified — or a new one).

The BDFL will respond yes / no / later, with a one-paragraph reason.

---

## Parallel-execution task plan (for fast iteration with multiple contributors)

Multiple contributors can work in parallel on independent task units.
Each unit = one feature branch + one PR + isolated test scope. Below
is the v0.2 / v0.3 / v0.4 task split designed so 3–5 contributors can
work concurrently without merge conflicts.

### v0.2 — three deliverable templates (3 parallel tracks)

| Track | Owner | Branch | Files touched (isolated) |
|---|---|---|---|
| **A. `memexa weekly`** | contributor #1 | `feat/v0.2-weekly-template` | `memexa/deliverables/weekly.py` (new) + `tests/integration/test_weekly_deliverable.py` (new) + `examples/deliverables/01_weekly_knowledge_worker.md` (new) |
| **B. `memexa brief <person\|topic>`** | contributor #2 | `feat/v0.2-brief-template` | `memexa/deliverables/brief.py` (new) + `tests/...test_brief_deliverable.py` (new) + `examples/deliverables/02_brief_researcher.md` (new) |
| **C. `memexa retro <window>`** | contributor #3 | `feat/v0.2-retro-template` | `memexa/deliverables/retro.py` (new) + `tests/...test_retro_deliverable.py` (new) + `examples/deliverables/03_retro_freelancer.md` (new) |
| **D. Shared template engine** | maintainer | `feat/v0.2-template-engine` | `memexa/deliverables/__init__.py` (new) + `memexa/deliverables/_base.py` (new) + `memexa/deliverables/_provider.py` (LLM provider abstraction) — **lands first, A/B/C depend on this** |
| **E. CLI dispatch + docs** | maintainer | `feat/v0.2-cli-routes` | `memexa/cli/main.py` (add `weekly`/`brief`/`retro` subcmds) + `docs/usage_guide.md` + `docs/usage_guide.zh.md` |

### v0.3 — Chinese IM + identity reflow (5 parallel tracks)

| Track | Owner | Branch | Status |
|---|---|---|---|
| **F. QQ db-only adapter** | contributor / maintainer | `feat/v0.3-qq-db-only` | reflow `jarvis/qq_db.py` 762 lines from upstream JARVIS |
| **G. doc source = 7th** | contributor / maintainer | `feat/v0.3-doc-source` | reflow JARVIS doc-source v0.5 LIVE 2026-05-15 |
| **H. identity manifest** | contributor / maintainer | `feat/v0.3-identity-manifest` | reflow JARVIS USAGE_MANUAL §19 |
| **I. 飞书 (Lark) export adapter** | contributor | `feat/v0.3-lark-adapter` | new from scratch (no upstream parallel) |
| **J. 钉钉 (DingTalk) export adapter** | contributor | `feat/v0.3-dingtalk-adapter` | new from scratch |
| **K. benchmarks/cn_multiparty/** | maintainer | `feat/v0.3-cn-benchmark-suite` | new — 5 reproducible WeChat / QQ scenarios |

### v0.4 — audio + voice (3 parallel tracks)

| Track | Owner | Branch |
|---|---|---|
| **L. SenseVoice ASR reflow** | contributor | `feat/v0.4-sensevoice-asr` |
| **M. Cross-session voice manifest** | contributor | `feat/v0.4-voice-manifest` |
| **N. `memexa 会议纪要 <session>`** | contributor | `feat/v0.4-meeting-summary` |

### Contributor onboarding

When ≥1 contributor is on-board, the maintainer opens a "v0.X
coordination" Discussion thread per release; tracks A–N above are
filed as separate issues with `good-first-issue` or `help-wanted`
labels. Each track has its own `tests/...` scope so parallel PRs do
not collide.

---

## Reflow status from upstream JARVIS (LIVE capability inventory)

| JARVIS LIVE capability | OSS reflow target | Status |
|---|---|---|
| 6 sources (WeChat / QQ / Email / Browser / Claude Code / Audio) | v0.1 | ✅ shipped |
| 14 query subcommands (basic 9 + advanced 5) | v0.1 | ✅ shipped |
| Streaming POST + verify + dead-letter retry | v0.1 | ✅ shipped |
| Doc source (file_sha1-bound, .md/.pdf/.docx/.txt) | **v0.3** | reflow pending |
| QQ db-only (NapCat → SQLCipher direct) | **v0.3** | reflow pending |
| Identity manifest cross-alias resolution | **v0.3** | reflow pending |
| SenseVoice ASR + voice enroll | **v0.4** | reflow pending |
| MCP server entry-point | **v0.5** | new in OSS |
| Schema drift sanitize (5 classes) | **v0.6** | reflow pending |
| 5-driver rc=2 graceful-skip pattern | **v0.6** | reflow pending |
| Mac failover wrappers (PG stale-lock + hindsight pg_isready) | docs in v0.5 | reflow pending |
| Win cron + Mac LaunchAgent + sys_monitor dashboard | **v0.5** | reflow pending |

JARVIS upstream remains the experimental edge; OSS reflow happens after
a capability runs ≥ 4 weeks LIVE in JARVIS without rollback. This keeps
OSS users on capabilities that survived real production load.
