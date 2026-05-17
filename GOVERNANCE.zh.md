# 项目治理

**English**: [GOVERNANCE.md](GOVERNANCE.md)（权威源）。本文件是中文镜像。

`memexa` 遵循 **Benevolent Dictator For Life (BDFL)** 模型。项目主理人
对以下事项有最终决定权:

- **范围**: 项目做什么、不做什么。
- **架构**: 接受哪些依赖、放弃哪些。
- **发布节奏**。
- **维护者上岗**。

## 谁决定

- 日常代码 merge — BDFL 或任何带 commit 权的维护者。
- 新维护者邀请 — BDFL。
- 破坏性变更 (CLI 参数 / config schema / on-disk 布局) — BDFL 必须
  签字; 否则只能 ship 在 opt-in flag 后面。
- 涉安全的 merge — BDFL 或指定的安全 reviewer。

## 决策如何做出

讨论发生在相关 GitHub issue 或 pull request。意见分歧时, BDFL 在 PR
描述写一段决策说明, merge 继续。决策落在 commit message — 没有单独
的 ADR 仓。

## 如何成为维护者

开 PR, 让它被 merge。3 个有实质内容的 PR 被合并后, 可申请 commit
权; BDFL 给 yes / no + 理由。无正式投票。

## 如何离任

6 个月没 merge 过 PR 的维护者自动失去 commit 权。可随时申请恢复。

## 分歧

如果某个治理决策让你觉得不对, 项目是 Apache 2.0 协议, **fork 是
官方支持的退路**, 也是开源生态正常结果。

## 公开沟通

- 仓库 issue + Discussions — 一线渠道。
- Release notes — 每个 tag 发布时一并发。
- Roadmap — [ROADMAP.md](ROADMAP.md) (zh: [ROADMAP.zh.md](ROADMAP.zh.md)),
  优先级变更时更新。

## 保密沟通

- 安全 report — 本仓 GitHub Security Advisory 渠道。
- 任何涉另一贡献者的敏感事 — 见 [CODE_OF_CONDUCT.zh.md](CODE_OF_CONDUCT.zh.md)
  举报章节。
