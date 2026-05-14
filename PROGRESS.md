# OSS Prep Progress

> Phase 1–11 of M-tier code preparation complete (Phase 11 = naming +
> module migration, finalised 2026-05-14 in the `memex / 镜我` rename pass).
> Storytelling (README narrative) and traffic-promotion (knowledge-base /
> awesome-list PR) are intentionally NOT part of this pass.

## Phase status (final)

| Phase | 名称                                          | 状态       | 输出                                                  |
|-------|-----------------------------------------------|------------|-------------------------------------------------------|
| 1     | 建立 oss-prep 独立工作区 + 子目录骨架          | ✅ 完成     | 26 dirs, .gitignore, git init (anon author)            |
| 2     | 主 repo PII 全量精确审计                       | ✅ 完成     | 162 unique 文件 + 8 token 按类拆分                     |
| 3     | 选择性 mirror M 档代码                         | ✅ 完成     | 304 .py mirrored (core 206 / extraction 56 / ...)      |
| 4     | sanitize 批量替换                              | ✅ 完成     | 86 files / 395 replacements / verify 0 residual         |
| 5     | 三层通用化抽象 helper                          | ✅ 完成     | _path_resolver + _user_aliases + _user_identity        |
| 6     | 文档 17 件 (除 README 故事)                    | ✅ 完成     | architecture/quickstart/usage_guide/5_phase + 3 deploy + CONTRIBUTING/SECURITY/CHANGELOG/LICENSE/ISSUE templates × 3 + PR template + migration guide |
| 7     | lesson-learned 6 篇 narrative                  | ✅ 完成     | tags-OR / 2-LLM / PG-aware / win-job-subprocess / qwen3-no-think / dual-GPU-swap |
| 8     | 工程脚手架 E1–E8                               | ✅ 完成     | pyproject.toml + CI + security + dependabot + pre-commit + docker-compose + Makefile + pii-scan |
| 9     | Demo dataset 准备                              | ✅ 完成     | 7 源合成数据集 + ingest.py / dry-run pass 26 cards     |
| 10    | e2e + sanity 完备验证                          | ✅ 完成     | 7/7 verification gates PASS                            |

## Final verification gates (Phase 11, 2026-05-14)

| # | Gate                                          | Result   |
|---|-----------------------------------------------|----------|
| 1 | PII sanity scan (excl. by-design detectors)   | 0 hits   |
| 2 | Python syntax (306 .py files)                 | 306/306 PASS |
| 3 | Three-layer helper smoke test (no env)        | PASS     |
| 4 | pyproject.toml + YAML syntax                  | PASS     |
| 5 | Demo dataset dry-run ingest                   | 26 cards / 6 sources |
| 6 | Main repo not modified by this session        | confirmed |
| 7 | oss-prep tree integrity                       | 306 py / 25 md / 7 yaml / 6 json |
| 8 | `from src.core.X import Y` covers all 8 packages | 0 import failures |
| 9 | pytest tests/                                 | 17 passed / 2 skipped (drift; doc'd) |
| 10 | 25 `TODO(memgraph-oss)` markers              | 0 remaining |
| 11 | `python -m src.core.memory_query --help`     | 14 subcommands listed |

## What's INTENTIONALLY left undone

1. **README storytelling** — engineering scaffold only; narrative TBD per CEO.
2. **PyPI registration** — pending CEO release approval.
3. **GitHub repo creation** — pending CEO `gh repo create labazhou2024/memex`.
4. **Traffic / launch content** — only awesome-ai-memory PR per CEO directive (post-release).
5. **Optional plugin stubs** — `src.wechat_db` ships as stub; users plug in WeChatMsg-style exporter via `docs/integrations/wechat.md`.

## What still must be done LATER (post-naming)

| Owner | Task                                                       | Time est. |
|-------|------------------------------------------------------------|-----------|
| CEO   | Pick project name + English slug                            | 5–30 min  |
| Claude| Find-and-replace `memex` and `memex` across 12 files | 2 min |
| Claude| Migrate the 25 modules still tagged `TODO(memgraph-oss)` to use `_path_resolver` | 3–4 h |
| Claude| Add `__init__.py` stubs and ensure absolute imports work | 2 h |
| CEO   | Create the GitHub repo (public)                             | 2 min     |
| Claude| Push initial squashed commit                                | 1 min     |
| Claude| Cut the v0.1.0 tag                                          | 30 sec    |

## Main repo (memex) — touched zero files in this session

Verified at the end of each phase with `git diff --name-only HEAD | wc -l`.
The count fluctuated 38–40 because cron daemons (calendar / sys_monitor /
hindsight_outbox) keep appending to JSONL log files in `memex/data/`.
None of those edits originated from this Claude session.

## Disk layout

```
~/OneDrive/桌面/claude workspace/oss-prep/
├── README.md                       (placeholder; project name TBD)
├── LICENSE                          (Apache 2.0)
├── CHANGELOG.md
├── CONTRIBUTING.md
├── SECURITY.md
├── Makefile
├── pyproject.toml                  (placeholder name)
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml
├── docker-compose.example.yml
├── .audit/                          (private prep artefacts; not for repo)
│   ├── sanitize_dict.json
│   ├── sanitize_run.py
│   ├── sanity_scan.py
│   ├── add_legacy_path_todo.py
│   ├── PLACEHOLDER_INVENTORY.md
│   └── pii_*.txt                    (per-token grep results)
├── .github/
│   ├── workflows/{ci,security}.yml
│   ├── dependabot.yml
│   ├── ISSUE_TEMPLATE/{bug,feature,question}.md
│   └── PULL_REQUEST_TEMPLATE.md
├── config/
│   ├── aliases.example.yaml
│   └── identity.example.yaml
├── docs/
│   ├── architecture.md
│   ├── quickstart.md
│   ├── usage_guide.md
│   ├── 5_phase_query.md
│   ├── migration/path_resolver.md
│   ├── deployment/{macos,windows,docker-compose}.md
│   └── lessons_learned/{README,01–06}.md
├── examples/demo_dataset/
│   ├── README.md
│   ├── ingest.py
│   ├── wechat_demo.json (19 msgs)
│   ├── qq_demo.json (9 msgs)
│   ├── email_demo.json (4 mails)
│   ├── browser_demo.json (10 entries)
│   ├── claude_demo.jsonl (6 turns)
│   └── audio_demo_transcript.json (1 session)
├── scripts/
│   ├── pre-commit-pii-scan.sh
│   ├── windows/   (Phase-11; depends on naming)
│   ├── macos/     (Phase-11)
│   └── linux/     (Phase-11)
├── src/                             (304 .py files)
│   ├── core/      (206 .py — query, hindsight client, helpers, gates)
│   ├── extraction/ (56 .py — gate + extract pipeline)
│   ├── ingestion/ (4 .py — 4 builders + 2 in extraction subdirs = 6 source)
│   ├── drivers/   (6 .py — per-source cron drivers)
│   ├── cron/      (orchestrator + manifest)
│   ├── audio/     (3 .py — pipeline + voice resolver + classifier)
│   ├── dashboard/sys_monitor/ (FastAPI server + index.html)
│   ├── agents/    (4 .py — Claude Code agent definitions)
│   └── integrations/ (currently empty; reserved)
└── tests/unit/
    ├── test_hindsight_client_encoding.py
    └── test_pass2_prompt.py
```
