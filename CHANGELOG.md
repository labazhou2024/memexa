# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Aggregates the rc5 say-do gap closure surfaced by the post-rc4 audit.
Final version number is being decided; ship-time entry will rename
this section to whichever number is cut.

### Added

- **`--json` accepted at the subcommand level**: agents can now write
  `memexa quick "X" --json` exactly as documented in the README /
  quickstart, instead of being forced to use `memexa --json query
  quick "X"`. The flag lives on a `_common` parent parser inherited
  by all fourteen subcommands, so all three positions work:
  `memexa --json query quick "X"`, `memexa query quick "X" --json`,
  `memexa quick "X" --json`. Closes the rc4 "fourteen subcommands
  with `--json` mode" claim, which the rc4 audit found to fail with
  `unrecognized arguments: --json` at the subcommand level.

### Changed

- **`docs/quickstart.md` (+ zh)** Tier 0 expected demo output rewritten
  to match what `memexa demo` actually prints in a fresh Python 3.11
  venv: `(audio=1, browser_session=10, claude_code=3, email=4, qq=3,
  wechat=5)` totalling 26 cards. The previous line printed
  `(wechat=8, qq=4, email=4, browser=4, claude=3, audio=3)`, which
  was both wrong per-source and used non-canonical source names
  (`browser`/`claude` vs the real `browser_session`/`claude_code`).
- **`docs/quickstart.md` (+ zh)** macOS Python 3.9 gap surfaced as
  an explicit warning above the install command. Stock macOS Python
  is 3.9, below the project's 3.10 minimum, so `pip install --pre
  memexa` had been failing silently on every untouched macOS install
  with "Could not find a version that satisfies the requirement
  memexa." Users now hit the requirement check on the docs page,
  with `brew install python@3.11` + `venv` instructions.
- **`ROADMAP.md` (+ zh)** Current state header advanced from
  `(v0.1.0-rc2)` to `(v0.1.0-rc4)`. Shipped list rewritten: `demo`
  added to CLI list, "Eight tests, nineteen CI workflow checks"
  replaced with "Ten tests, six CI workflows (lint / test / codeql /
  security / release-drafter / dependabot)" so the count names
  workflows (rarely edited) instead of jobs (churn), Linux
  deployment phrasing corrected (no Linux-native guide; users follow
  the docker-compose path), and the 14-subcommand split corrected
  from "nine basic + five advanced" to "seven basic + seven advanced"
  to match the actual code.

### Fixed

- **`memexa quick "X"` and friends now exit 1 with an English stderr
  hint when the Hindsight backend is unreachable**, instead of
  silently returning `N=0` + exit 0. Agents subprocess-invoking
  memexa rely on exit codes to distinguish "no results" from "your
  invocation produced nothing usable because the backend was down."
  `--json` mode keeps printing `[]` on stdout (so JSON parsers do
  not break) but also exits 1 with a one-line stderr hint. The old
  `logger.warning` that leaked GBK-localized Windows OS error
  strings (`[WinError 10061] 由于目标计算机积极拒绝, 无法连接`,
  unreadable on non-Chinese-Windows shells) is demoted to
  `logger.debug`; the console now sees an ASCII-only multi-line
  hint with three concrete next-step commands (`make backend-up`,
  `MEMEXA_HINDSIGHT_URL=...`, `memexa doctor`).

### Removed

- Duplicate `[0.1.0-rc1]` entry that was accidentally written twice
  in the rc1→rc2 reflow (lines 127-154 of the old file).

## [0.1.0-rc4] — 2026-05-16

Release-pipeline correctness on top of rc3 (PR #16).

### Fixed

- **Demo dataset bundled in the wheel**: `[tool.setuptools.package-data]`
  now includes `examples/demo_dataset/*.json` and `*.jsonl`, so
  `memexa demo` works on a fresh `pip install --pre memexa` without
  needing the cloned source tree. Previously the bundled JSON
  fixtures were source-tree-only and the demo failed with a missing
  data-file error on PyPI installs.
- **Dynamic `__version__`**: `memexa/__init__.py` now reads its
  version from package metadata via `importlib.metadata.version()`,
  so `memexa version` always matches the wheel that pip resolved.
  Previously the hard-coded `__version__` lagged behind
  `pyproject.toml` and could drift across rc bumps.

## [0.1.0-rc3] — 2026-05-16

Agent-first brand consolidation + first-time-visitor onboarding path
(PR #14 / PR #15).

### Added

- **`memexa demo` subcommand**: thirty-second onboarding that ingests
  the bundled synthetic dataset with the stub extractor and runs five
  sample queries (`quick` / `arc` / `timeline` / `pending` / `topic`).
  No Docker, no LLM API key, no configuration required. Designed as
  the first-time visitor path; advertised in README quickstart.
- **`--json` output mode for all fourteen query subcommands**.
  Top-level flag (`memexa --json query quick "X"`) short-circuits
  text rendering and emits the raw return value (list or dict) as a
  single JSON document on stdout. This is the structured-output
  path for AI agents (Claude Code, Cursor, Cline) invoking memexa
  via shell subprocess — the first-class agent integration until
  v0.5 ships the native MCP server. (rc5 follow-up: also accepted
  at the subcommand level.)
- **`docs/why.md` + `docs/why.zh.md`**: per-capability comparison
  against OpenHuman / MemPalace / ReMe, agent-first design
  rationale, glossary covering project-specific terms (verbatim
  raw + V2 envelope; reflow; Chinese-IM reflow; audio + voice
  reflow; workflow spec).
- **`docs/cost.md` + `docs/cost.zh.md`**: API call volume and cost
  estimation for DeepSeek (V4 Flash / Pro), GPT-4o, and Claude 4.x,
  with three-tier user profiles and recommended model combinations
  for Chinese workloads.

### Changed

- **Project positioning**: clarified as **agent backbone** —
  the primary user is an AI agent (Claude Code / Cursor / Cline)
  invoking memexa as a subprocess on a human user's behalf. The
  fourteen subcommands plus seven hard rules in
  `docs/for_agents.md` are the agent contract. Direct CLI use by
  a human is also supported but is the secondary path.
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
  their own by copying a markdown file. v0.5 promotes the
  subprocess path to a native MCP server. v0.7 formalises
  user-authored workflow specs. New v0.8+ section for optional
  desktop GUI exploration, gated on v0.5 / v0.7 success conditions.
- **`Makefile`**: `fmt` and `lint` targets corrected to use
  `memexa tests` instead of the deprecated `src tests` path
  (residue from PR #9's `src/`→`memexa/` rename).

### Fixed

- CodeQL: seven error-level alerts resolved (uninitialised local
  variables in `mlx_lm_wrapper.py` and
  `mini_loop_pretool_hook.py`, unused loop variables in two
  `l0_worker_v2_*.py` modules). 866 note- and warning-level alerts
  dismissed as known alpha-stage technical debt (intentional
  graceful-degrade patterns; queued for full ruff sweep in v0.2).
  Open code-scanning alerts now zero.

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

[Unreleased]: https://github.com/labazhou2024/memexa/compare/v0.1.0-rc4...HEAD
[0.1.0-rc4]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc4
[0.1.0-rc3]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc3
[0.1.0-rc2]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc2
[0.1.0-rc1]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0-rc1
[0.1.0]: https://github.com/labazhou2024/memexa/releases/tag/v0.1.0
