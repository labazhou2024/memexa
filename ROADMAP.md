# Roadmap

**English** · [中文](ROADMAP.zh.md)

> Aspirational. Not a commitment. Things move when they move.

## v0.1.x — get the demo to ingest end-to-end on a clean box

- [x] CLI dispatcher (`memexa init / version / config / doctor / query`)
- [x] PII scrubbing pre-commit hook
- [x] Demo dataset (6 sources, public-domain)
- [x] Direct psycopg2 PG access (no ssh shell-out by default)
- [x] Hindsight failover URL with automatic retry
- [x] 14 query subcommands documented
- [x] `memexa doctor` round-trips LLM provider
- [x] PII residual scanner with self-referential SKIP-list
- [ ] Fresh-clone smoke test passes on Win + macOS + Linux in CI

## v0.2 — deliverable templates layer (top user-facing push)

The core query system gives you the raw signals. v0.2 stitches them into
documents you can copy-paste or print. Each template = one subcommand + a
Markdown/LaTeX layout + a couple of `memory_query` calls under the hood.

- [ ] `memexa lab-report <实验名>` — pre-class report (LaTeX → PDF, with
      WebSearch fallback for official handouts)
- [ ] `memexa weekly-report` — git log + session-end narrative + project
      cross-source summary → one-page Markdown
- [ ] `memexa action-card <ddl>` — outing checklist + Q&A cheatsheet
- [ ] `memexa brief <person>` — pre-meeting brief (baseline / last-meet /
      open threads / landmines), built on `arc` + `quick` queries
- [ ] `memexa dashboard` — deadline panel, 4-column triage of `pending`

## v0.2 — QQ db-only adapter migration (highest user-impact backlog)

- [ ] Migrate `jarvis/qq_db.py` (762 lines, stdlib only) from upstream JARVIS to `memexa/extraction/qq/qq_db.py`
- [ ] Migrate `jarvis/qq_reader.py` (clipboard fallback) similarly
- [ ] Wire both into `backfill_v5_qq_driver.py` so `--mode dump` and `--mode clipboard` work out of the box
- [ ] Add a smoke test against a synthetic SQLCipher fixture (no real QQ required)
- [ ] Drop the OSS-side NapCat / OneBot adapter from the tree (currently kept behind `MEMEXA_QQ_NAPCAT_FORCE=1`)

## v0.2 — Linux first-class (parallel track)

- [ ] systemd unit templates for the 6-hour cron and the dashboard
- [ ] Nix flake (community contribution welcome)
- [ ] Docker image published to ghcr.io
- [ ] Headless ingestion mode (no dashboard server required)

## v0.3 — pluggable LLM providers

- [ ] Built-in adapters for Ollama, vLLM, LiteLLM proxy, OpenRouter
- [ ] Adapter test suite that hits each provider with a synthetic batch
- [ ] Auth via OAuth2 device-code for Gmail / Outlook IMAP

## v0.4 — new sources

- [ ] Discord export
- [ ] Slack export
- [ ] Telegram export
- [ ] iMessage SQLite

## v0.5 — observability + reliability

- [ ] Prometheus `/metrics` endpoint on the dashboard server
- [ ] Per-driver SLO dashboards
- [ ] Automated nightly recall regression suite
- [ ] Right-to-be-forgotten CLI (`memexa forget <canonical-id>`)

## v1.0 — stable schema commitment

- [ ] V2 envelope frozen; migrations only via additive fields
- [ ] CLI args frozen; deprecations only with one-release warning
- [ ] On-disk layout frozen; bumping bumps a major version
- [ ] All deployment guides covered by smoke tests in CI

## Permanently out of scope

- Web / mobile UI rewrites.
- Multi-tenant hosted service.
- Voice synthesis or agent loops.
- Anything that prevents you from owning your own data.

## How to propose a roadmap change

Open a Discussion in the **Ideas** category with:

- What you would add / drop.
- Which milestone it fits.
- Whether you are volunteering to implement it.

The BDFL will respond yes / no / later, with a one-paragraph reason.
