# 贡献指南

[English](CONTRIBUTING.md) · **中文**

> 谢谢关注。本项目处于"看看有没有别人想要"阶段 — 没有 roadmap, 没有
> 分诊 SLA, 没有 good-first-issue 标签。欢迎 PR 但请先读一遍这个。

## 范围

在范围内:

- 6 个摄入 pipeline / 查询 CLI / dashboard / cron orchestrator /
  memory-backend wrapper 的 bug 修
- 新的 `OpenAI-compatible` LLM provider adapter (让用户能插我没亲测过
  的 endpoint)
- Linux / systemd / nix 打包
- 新的 per-source builder (e.g. Discord / Slack / Telegram export —
  任何最终是聊天记录的东西)
- 文档, 尤其翻译

不在范围:

- Web UI / mobile UI 重写。这是 CLI + 本地 dashboard 项目
- 迁到另一个 memory 后端 (Mem0, Letta, Zep 等)。Hindsight 是硬依赖
- 多租户 / SaaS。产品是单用户自托管
- 语音合成 / agent loop。图谱是查询目标, 不是 agent 平台

## 开发 setup

```bash
git clone https://github.com/labazhou2024/memexa.git
cd memexa
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## 开 PR 前

- 跑 smoke test: `make smoke`。它 docker-compose 起后端, 摄入自带 demo
  数据集, 跑全部 8 个子命令。笔记本上 <5 min 完成
- 跑测试: `make test`
- 跑 PII 扫描: `make pii-scan`。必须 0 命中 (PR 里出现你自己真实数据是
  不可接受的)
- 在 `CHANGELOG.md` 的 `## [Unreleased]` 下加条目

## PR 流程 + merge 策略

`main` 受保护。**禁止直接 push** —— 任何改动 (哪怕一行 typo 修复) 都走 PR。
1-人 OSS 项目, 保护规则刻意保持极简:

- 0 必需 review (maintainer 可自 merge)
- 0 必需 status check (CI 在每 PR 上跑但不阻塞 merge 按钮 —— 自律: 别 merge 红 CI)
- 禁止 force push 和 main 分支删除

开完 PR:

1. 等 CI (lint + 9 格 test matrix + bandit + pip-audit + demo ingest +
   PII scan + CodeQL)。典型 ~3 min
2. 绿且是你自己 PR: `gh pr merge <num> --squash --delete-branch`
3. 红: 排查, push fix 到同 branch, CI 重跑, 重复
4. 外部 contributor PR: maintainer review, voluntary approve, merge
   (review 不 gate 但作为礼貌期待)

**Dependabot PR** 通过
[`.github/workflows/dependabot-auto-merge.yml`](.github/workflows/dependabot-auto-merge.yml)
在 CI 通过后自动 merge (覆盖 patch / minor / major)。如果 Dependabot PR
auto-merge 后炸了 main, 开 revert PR + 在 `pyproject.toml` 锁定该包版本。

Branch 命名: `<type>/<short-slug>` (kebab-case)

| 前缀 | 何时 |
|---|---|
| `feat/` | 新用户面向功能 |
| `fix/` | bug 修 |
| `docs/` | 仅文档 |
| `chore/` | 依赖 bump / 版本 bump / CI 微调 |
| `refactor/` | 内部重组, 行为不变 |
| `ci/` | CI workflow / pre-commit / dependabot 配置 |
| `test/` | 仅 test |
| `release/` | 发版准备 (CHANGELOG / version bump 打包) |

## 代码风格

- Python: PEP 8, `black` formatter (行长 100), `ruff` lint。推前
  `make fmt`
- Type hint 鼓励但不必须。`memexa/core/` 的 public function 应加 type
- 测试在 `tests/unit/`, `tests/integration/`, `tests/e2e/`。用 `pytest`
- Commit message: 祈使语气, subject 行 ≤72 字符

## 隐私硬规则

这是个人 memory 工具。Contributor 不许在 PR 或 issue 里带真人 identifier:

- 不许真名 / 真群名 / 真 QQ / 微信 ID / 真邮箱 / 真手机号
- Demo 数据必须来自公开 corpora (LCCC, Common Crawl 中文子集等) —
  永远不要用自己对话
- pre-commit hook `scripts/pre-commit-pii-scan.sh` 在 staged diff 上跑
  regex 扫。不要用 `--no-verify` 绕过

## 报 bug

开 issue 附:

- 你试了啥 (一条命令)
- 你期望啥
- 实际发生啥 (粘完整错误含 Python traceback)
- `python -V`, OS, `pip freeze | grep -iE "memexa|hindsight|bge"`

安全敏感 issue: 见 [SECURITY.zh.md](SECURITY.zh.md)。

## License

贡献即同意你的代码在 Apache 2.0 下发布。
