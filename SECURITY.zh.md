# 安全政策

[English](SECURITY.md) · **中文**

## 支持版本

只有 `main` 分支和最新 tag release 收安全修复。老 release 视为废弃。

## 报漏洞

发现安全问题 — 特别是可能导致别人本地 memory 图谱泄露的 — 请**不要**
开公开 issue。邮件给 maintainer `<security-email>`, 附:

- 问题描述
- 最小复现步骤
- 受影响版本 (你测的 build 的 `git rev-parse HEAD`)
- 可选: 建议补丁

7 天内会得到确认。披露时间线:

- Day 0  — 收到报告
- Day 7  — 初步响应, 确认复现
- Day 30 — 补丁落 `main`
- Day 45 — 协调披露 (适用时 CVE filed, advisory 发布)

## 不在范围

- 需要用户机器本地 root 的问题
- `hindsight-api` 自身的问题 — 上报上游
  https://github.com/vectorize-io/hindsight
- 外部 LLM provider 的问题 (OpenAI, DeepSeek, Qwen 等)
- 针对用户自己数据源的社工攻击 (e.g. 有人骗用户导出并摄入恶意聊天历史)。
  用户侧策展不在我们威胁模型里

## 威胁模型

我们保护的资产:

- 用户本地 memory 图谱 (PostgreSQL 内容)
- 用户运行时凭据 (`.env`, `~/.memexa/*.yaml`)
- 用户 LLM API key, 包括嵌入 `.env` 的

假设环境:

- 单用户, 本地装的软件
- Memory 后端只在 `127.0.0.1` 或 LAN 可达 (没反代 + auth 就不暴露到公网)
- 用户负责备份和磁盘加密

明确不保护的:

- 攻击者有用户账号 shell 访问
- 攻击者物理提取用户硬盘
- 用户自己的 LLM provider 错处理提交的 prompt (读他们隐私政策)
