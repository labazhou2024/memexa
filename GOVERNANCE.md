# Project governance

`memex` follows a Benevolent Dictator For Life (BDFL) model. The
project lead has final say on:

- Scope (what the project does and does not do).
- Architecture (which dependencies to take on, which to drop).
- Release cadence.
- Maintainer onboarding.

## Who decides

- Day-to-day code merges — the BDFL or any commit-bit maintainer.
- New maintainer invitations — the BDFL.
- Breaking changes (CLI args, config schema, on-disk layout) — the BDFL
  must sign off; otherwise the change ships only behind an opt-in flag.
- Security-sensitive merges — the BDFL or a designated security reviewer.

## How decisions get made

Discussion happens in the relevant GitHub issue or pull request. When
opinions diverge, the BDFL writes a one-paragraph decision in the PR
description and the merge proceeds. Decisions are documented in commit
messages — there is no separate ADR repo.

## Becoming a maintainer

Open PRs that get merged. After three substantive merged PRs you can
ask for commit access; the BDFL will say yes or no, with a reason.
There is no formal vote.

## Stepping down

A maintainer who has not merged a PR in 6 months loses commit access
automatically. They can ask for it back at any time.

## Disagreement

If a governance decision feels wrong to you, the project is Apache 2.0
licensed. Forking is the supported escape hatch and a normal outcome of
open source.

## Public communication

- Repo issues + Discussions — primary channel.
- Release notes — published with each tag.
- Roadmap — [ROADMAP.md](ROADMAP.md), updated when priorities change.

## Confidential communication

- Security reports — GitHub Security Advisory channel for this repo.
- Anything sensitive about another contributor — see
  [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) reporting section.
