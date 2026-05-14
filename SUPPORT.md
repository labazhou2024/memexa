# Support

**English** · [中文](SUPPORT.zh.md)

This project is maintained by one person in their spare time. There is
no SLA. Issues will be triaged when the maintainer has time.

## How to ask for help

- Question about *how to use* the tool → open a **Discussion** in the
  Q&A category. Search existing discussions first.
- Suspected bug → open an **Issue** with the bug-report template.
  Include `memex version`, OS, Python version, full traceback.
- Feature request → open an **Issue** with the feature-request template.
  Explain *what* you need and *why* the existing CLI cannot do it.

## Things the maintainer will not do for you

- Triage your data quality. The pipeline expects exported chat archives
  in the formats documented under `docs/integrations/`. If your export
  is malformed, fix the export.
- Run a hosted version of the service. This is self-hosted only.
- Migrate to a different memory backend. Hindsight is a hard dependency.
- Provide commercial support. If you need that, fork the project.

## Response expectations

- Critical security issues (privilege escalation, data exfiltration) —
  best-effort same-day acknowledgement.
- Bugs — best-effort within 7 days.
- Feature requests — no commitment; may sit in the backlog indefinitely.
- Documentation gaps — PRs welcome and merged fastest.

## Useful first stops before opening an issue

1. [docs/troubleshooting.md](docs/troubleshooting.md) — six-layer
   diagnostic ladder.
2. [docs/faq.md](docs/faq.md) — already-answered questions.
3. `memex doctor` — automated self-diagnostic.
4. `docs/lessons_learned/` — common pipeline traps with fixes.
