<!--
repository-topics:
  - personal-memory
  - knowledge-graph
  - chinese-nlp
  - self-hosted
  - llm
  - mcp
  - demo
-->

# Memexa · 镜我

**English** · [中文](README.zh.md)

> A self-hosted personal memory graph over Chinese-native data —
> WeChat / QQ / email / documents / audio.
> **This repository is the open demo. The full engine is a separate
> proprietary product.**

[![CI](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml/badge.svg)](https://github.com/labazhou2024/memexa/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/memexa?label=PyPI)](https://pypi.org/project/memexa/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)

## What this is

memexa turns scattered, multi-party Chinese data into a queryable
memory graph: every message is stored **verbatim**, extracted into
structured cards, and every answer is **cited back to the original
sentence**. It is fully self-hosted — your data never leaves your
machine.

This repository ships the **open demo**: a small synthetic dataset and a
stub extractor, so you can see the shape of the project in thirty
seconds — no backend, no API key, no configuration.

## Try the demo

```bash
pip install memexa
memexa demo
```

Six synthetic sources (WeChat / QQ / email / browser / AI chat / audio)
are ingested with the stub extractor, then five sample queries run
against the resulting cards — entirely in memory. This is the honest
first look at what the project does.

## The full engine

The demo runs a stub on synthetic data. The full **memexa** engine is a
proprietary product and is **not** included in this repository. It adds:

- **Live ingestion** of your own data across multiple sources, incremental.
- A **two-LLM extraction pipeline** producing cards with per-claim
  citations and cross-alias canonical identities.
- A **multi-channel recall stack** with cross-encoder re-ranking — built
  for high-accuracy retrieval over messy, multi-party Chinese chat, not a
  single-vector lookup.
- An **MCP server + CLI**, so any coding agent (Claude Code, Cursor,
  Cline, Codex) can use your memory as a first-class tool.
- A **local desktop app** — runs the whole stack on your own machine.

For access to the full engine, please reach out via the repository
owner's profile.

## License

The demo in this repository is licensed under **Apache-2.0** (see
[LICENSE](LICENSE)). The full memexa engine is a separate proprietary
product and is not covered by that license.
