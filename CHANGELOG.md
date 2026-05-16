# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`memexa demo` subcommand**: thirty-second onboarding that ingests
  the bundled synthetic dataset with the stub extractor and runs five
  sample queries (`quick` / `arc` / `timeline` / `pending` / `topic`).
  No Docker, no LLM API key, no configuration required. Designed as
  the first-time visitor path; advertised in README quickstart.
- **`--json` output mode for all fourteen query subcommands**.
  Top-level flag short-circuits text rendering and emits the raw
  return value (list or dict) as a single JSON document on stdout.
  This is the structured-output path for AI agents (Claude Code,
  Cursor, Cline) invoking memexa via shell subprocess — the
  first-class agent integration until v0.5 ships the native MCP
  server.
- **`docs/why.md` + `docs/why.zh.md`**: per-capability comparison
  against OpenHuman / MemPalace / ReMe, agent-first design rationale,
  glossary covering project-specific terms (verbatim raw + V2
  envelope; reflow; Chinese-IM reflow; audio + voice reflow; workflow
  spec).
- **`docs/cost.md` + `docs/cost.zh.md`**: API call volume and cost
  estimation for DeepSeek (V4 Flash / Pro), GPT-4o, and Claude 4.x,
  with three-tier user profiles and recommended model combinations
  for Chinese workloads.

### Changed

- **Project positioning**: clarified as **agent backbone** —
  the primary user is an AI agent (Claude Code / Cursor / Cline)
  invoking memexa as a subprocess on a human user's behalf. The
  fourteen subcommands plus seven hard rules in `docs/for_agents.md`
  are the agent contract. Direct CLI use by a human is also
  supported but is the secondary path.
- **README first screen**: positioning line and the "AI-agent
  compatible by design" paragraph restored; Quickstart now has two
  sections (humans 30-second visual, agents subprocess + `--json`);
  documentation index updated.
- **`docs/quickstart.md`**: Tier 0 now has two paths — humans run
  `memexa demo`, agents call subcommands with `--json`. Tier 1 and
  Tier 2 remain unchanged.
- **`ROADMAP.md`**: v0.2 redefined from "Python deliverable code +
  CLI subcommands" to **Markdown workflow specs** under
  `docs/templates/`. Agents read the spec at runtime; users add
  their own by copying a markdown file. v0.5 promotes the subprocess
  path to a native MCP server. v0.7 formalises user-authored
  workflow specs. New v0.8+ section for optional desktop GUI
  exploration, gated on v0.5 / v0.7 success conditions.
- **`Makefile`**: `fmt` and `lint` targets corrected to use
  `memexa tests` instead of the deprecated `src tests` path (residue
  from PR #9's `src/`→`memexa/` rename).

### Fixed

- CodeQL: seven error-level alerts resolved (uninitialised local
  variables in `mlx_lm_wrapper.py` and `mini_loop_pretool_hook.py`,
  unused loop variables in two `l0_worker_v2_*.py` modules). 866
  note- and warning-level alerts dismissed as known alpha-stage
  technical debt (intentional graceful-degrade patterns; queued for
  full ruff sweep in v0.2). Open code-scanning alerts now zero.

### Security

- Dependabot vulnerability alerts and automated security fixes
  enabled at the repository level.

## [0.1.0-rc2] — 2026-05-14

Install bug fix on top of rc1.

### Fixed

- Package layout: `src/` renamed to `memexa/` to match the installed
  import path; PR #9 + CI followup in PR #10.

## [0.1.0-rc1] — 2026-05-14

First public release candidate. Single orphan commit. Open for feedback
before cutting v0.1.0 stable.

### Added

- Six ingestion sources: WeChat, QQ, email, browser, Claude Code, audio.
- Two-LLM gate-extract pipeline (gatekeeper + extractor + BGE-M3 quorum
  + DeepSeek arbiter).
- PostgreSQL + pgvector backend via Hindsight FastAPI.
- 14 query subcommands plus the five-phase state inference workflow.
- Live dashboard on `:8765`.
- Cron orchestrator with dead-letter retry and PG-aware pending.
- Windows / macOS / Linux deployment guides.
- Five reproducible walkthroughs against the bundled demo dataset
  (`examples/demo_dataset/walkthroughs/`).
- Two end-to-end case studies (`docs/case_studies/`).
- AI-agent protocol document (`docs/for_agents.md`) — hard rules,
  decision table, composition patterns, common pitfalls.
- Full Chinese mirror of every user-facing doc (`*.zh.md`).

### Security

- PII scrubbing pre-commit hook (`scripts/pre-commit-pii-scan.sh`).
- Full-tree PII residual scan with self-referential SKIP list
  (`scripts/full_pii_scan.sh`).
- Threat-model documentation (`SECURITY.md`).

## [0.1.0] — TBD (release criteria, kept in sync with ROADMAP)

Cut from a green release candidate when **all** of the following hold:

- The `memexa demo` subcommand has been LIVE on PyPI for at least one
  week and `pip install --pre memexa && memexa demo` returns rc=0 on
  fresh Windows, macOS, and Linux installs (already verified in CI
  by PR #12 / PR #14 matrices, but the LIVE PyPI check is the gate).
- At least one issue, discussion, or pull request from a non-author
  contributor has been opened against the repository.
- No critical bug fix has shipped in the past seven days.

The actual version-bump pull request closes this section and links to
the rc that became `0.1.0`.

## [0.1.0-rc1] — 2026-05-14

First public release candidate. Single orphan commit. Open for feedback
before cutting v0.1.0 stable.

### Added

- Six ingestion sources: WeChat, QQ, email, browser, Claude Code, audio.
- Two-LLM gate-extract pipeline (gatekeeper + extractor + BGE-M3 quorum
  + DeepSeek arbiter).
- PostgreSQL + pgvector backend via Hindsight FastAPI.
- 14 query subcommands plus the five-phase state inference workflow.
- Live dashboard on `:8765`.
- Cron orchestrator with dead-letter retry and PG-aware pending.
- Windows / macOS / Linux deployment guides.
- Five reproducible walkthroughs against the bundled demo dataset
  (`examples/demo_dataset/walkthroughs/`).
- Two end-to-end case studies (`docs/case_studies/`).
- AI-agent protocol document (`docs/for_agents.md`) — hard rules,
  decision table, composition patterns, common pitfalls.
- Full Chinese mirror of every user-facing doc (`*.zh.md`).

### Security

- PII scrubbing pre-commit hook (`scripts/pre-commit-pii-scan.sh`).
- Full-tree PII residual scan with self-referential SKIP list
  (`scripts/full_pii_scan.sh`).
- Threat-model documentation (`SECURITY.md`).

[Unreleased]: https://github.com/labazhou2024/memexa/compare/v0.1.0-rc2...HEAD
[0.1.0-rc2]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc2
[0.1.0-rc1]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc1
[0.1.0]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0
