# Security policy

**English** · [中文](SECURITY.zh.md)

## Supported versions

Only the `main` branch and the most recent tagged release receive
security fixes. Older releases are considered abandoned.

## Reporting a vulnerability

If you find a security issue — particularly anything that could lead to
exfiltration of someone's local memory graph — please do **not** open a
public issue. Email the maintainer at `<security-email>` with:

- A description of the issue.
- The minimum repro steps.
- Affected version (`git rev-parse HEAD` of the build you tested).
- Optional: a suggested patch.

You will get an acknowledgement within 7 days. Disclosure timeline:

- Day 0  — report received.
- Day 7  — initial response, confirmed reproduction.
- Day 30 — patch landed in `main`.
- Day 45 — coordinated disclosure (CVE filed if applicable, advisory
  published).

## Out of scope

- Issues that require local root on the user's machine.
- Issues in `hindsight-api` itself — please report upstream at
  https://github.com/vectorize-io/hindsight.
- Issues in external LLM providers (OpenAI, DeepSeek, Qwen, etc.).
- Social engineering attacks against the user's own data sources
  (e.g. someone tricked the user into exporting and ingesting a
  malicious chat history). User-side curation is out of our threat
  model.

## Threat model

Assets we protect:

- The user's local memory graph (PostgreSQL contents).
- The user's runtime credentials (`.env`, `~/.memexa/*.yaml`).
- The user's LLM API keys, including any embedded in `.env`.

Assumed environment:

- Single-user, locally-installed software.
- Memory backend reachable on `127.0.0.1` or LAN only (never exposed
  to the public internet without a reverse proxy + auth).
- The user is responsible for backups and disk encryption.

Things we explicitly do not protect against:

- Attacker with shell access to the user's account.
- Attacker who has physically extracted the user's disk.
- The user's own LLM provider mishandling submitted prompts (read
  their privacy policy).
