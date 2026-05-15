# Contributing

**English** · [中文](CONTRIBUTING.zh.md)

> Thanks for the interest. This project is in a "see if anyone else
> wants this" stage — no roadmap yet, no triage SLA, no good-first-issue
> labels. PRs are welcome but please read this once before opening one.

## Scope

In scope:

- Bug fixes to the six ingestion pipelines, query CLI, dashboard,
  cron orchestrator, or memory-backend wrappers.
- New `OpenAI-compatible` LLM provider adapters (so users can plug in
  endpoints I have not personally tested).
- Linux / systemd / nix packaging.
- New per-source builders (e.g. Discord export, Slack export, Telegram
  export — anything that ends as a chat log).
- Documentation, especially translations.

Out of scope:

- Web UI / mobile UI rewrites. This is a CLI + local dashboard
  project.
- Migration to a different memory backend (Mem0, Letta, Zep, etc).
  Hindsight is a hard dependency.
- Multi-tenancy / SaaS. The product is single-user self-hosted.
- Voice synthesis / agent loops. The graph is a query target, not an
  agent platform.

## Development setup

```bash
git clone https://github.com/labazhou2024/memexa.git
cd memexa
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Before opening a PR

- Run the smoke test: `make smoke`. This boots the backend in
  docker-compose, ingests the bundled demo dataset, and runs all eight
  subcommands. Must finish in <5 min on a laptop.
- Run the test suite: `make test`.
- Run the PII scan: `make pii-scan`. This must report zero hits
  (any reference to your own real-life data is unacceptable in a PR).
- Update `CHANGELOG.md` under `## [Unreleased]`.

## PR workflow & merge policy

`main` is protected. Direct pushes are blocked — every change goes
through a PR, even a one-line typo fix. The protection rules are
intentionally minimal for a one-person OSS project:

- 0 required approving reviews (the maintainer can self-merge)
- 0 required status checks (CI runs on every PR but does not block the
  merge button — self-discipline: do not merge red CI)
- Force pushes and branch deletions on `main` are disabled

After opening a PR:

1. Wait for CI (lint + 9-cell test matrix + bandit + pip-audit + demo
   ingest + PII scan + CodeQL). Typical wall time ~3 min.
2. If green and the PR is yours:
   `gh pr merge <num> --squash --delete-branch`
3. If red: investigate, push a fix to the same branch, CI re-runs, repeat.
4. External contributor PR: maintainer reviews, voluntarily approves,
   merges (review is not gate-required but expected as a courtesy).

**Dependabot PRs** auto-merge via
[`.github/workflows/dependabot-auto-merge.yml`](.github/workflows/dependabot-auto-merge.yml)
once CI passes (covers patch / minor / major). If a Dependabot PR ever
breaks `main` after auto-merge, open a revert PR and pin the offending
package in `pyproject.toml`.

Branch naming: `<type>/<short-slug>` (kebab-case)

| Prefix | When |
|---|---|
| `feat/` | new user-facing feature |
| `fix/` | bug fix |
| `docs/` | documentation only |
| `chore/` | dependency bumps, version bumps, CI tweaks |
| `refactor/` | internal restructure, no behavior change |
| `ci/` | CI workflow / pre-commit / dependabot config |
| `test/` | tests-only |
| `release/` | release preparation (CHANGELOG / version bump bundle) |

## Coding style

- Python: PEP 8, `black` formatter (line length 100), `ruff` for
  linting. Run `make fmt` before pushing.
- Type hints encouraged but not required. Public functions in
  `memexa/core/` should be typed.
- Tests live in `tests/unit/`, `tests/integration/`, `tests/e2e/`. Use
  `pytest`.
- Commit messages: imperative mood, ≤72 chars in the subject line.

## Privacy hard rules

This is a personal memory tool. Contributors must not include real
people's identifiers in PRs or issues:

- No real names, real chat group names, real QQ / WeChat IDs, real
  email addresses, real phone numbers.
- Demo data must come from public corpora (LCCC, Common Crawl Chinese
  subset, etc.) — never from your own conversations.
- The pre-commit hook `scripts/pre-commit-pii-scan.sh` runs a regex
  sweep over the staged diff. Do not bypass it with `--no-verify`.

## Reporting bugs

Open an issue with:

- What you tried (one command).
- What you expected.
- What happened (paste the full error including the Python traceback).
- `python -V`, OS, `pip freeze | grep -iE "memexa|hindsight|bge"`.

Security-sensitive issues: see [SECURITY.md](SECURITY.md).

## License

By contributing you agree your code is released under Apache 2.0.
