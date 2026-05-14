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

## 代码风格

- Python: PEP 8, `black` formatter (行长 100), `ruff` lint。推前
  `make fmt`
- Type hint 鼓励但不必须。`src/core/` 的 public function 应加 type
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
