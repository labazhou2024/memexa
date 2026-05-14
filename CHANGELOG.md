# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **docs(qq)**: rewrite the QQ integration page to reflect the dual-track
  reality — NapCat path deprecated and disabled by default, db-only and
  clipboard adapters LIVE in upstream JARVIS (`jarvis/qq_db.py`,
  `jarvis/qq_reader.py`) and scheduled for OSS migration in v0.2. Bilingual.
- **docs(env)**: rename `MEMEX_USTC_CONCURRENT` → `MEMEX_EXTRACT_CONCURRENT`
  in `configuration.md`, `performance.md`, `troubleshooting.md` (en + zh).
  No `src/` callsites — pure docs rename.
- **roadmap**: add explicit v0.2 milestone for the QQ adapter migration
  (5 sub-tasks).

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

### Known limitations (rc1)

- `pip install memex` not yet available on PyPI; install from GitHub.
- Mac/Linux fresh-clone smoke test not yet covered by CI on every push;
  scheduled for v0.1.0 stable.

## [0.1.0] — TBD

Cut from a green rc after ≥ 1 week of feedback, when:

- Fresh-clone smoke test passes on Win + macOS + Linux in CI.
- All eight query subcommands return non-empty results against the
  demo dataset.
- Test suite passes on Python 3.10 / 3.11 / 3.12.
- Dashboard renders on a fresh install without manual intervention.

[Unreleased]: https://github.com/labazhou2024/memex/compare/v0.1.0-rc1...HEAD
[0.1.0-rc1]: https://github.com/labazhou2024/memex/releases/tag/v0.1.0-rc1
[0.1.0]: https://github.com/labazhou2024/memex/releases/tag/v0.1.0
