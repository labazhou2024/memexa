<!-- 感谢提 PR。请填以下 sections。 -->
<!-- English: PULL_REQUEST_TEMPLATE.md (authoritative source). -->

## 改了什么

一句话总结。

## 为什么

链接相关 issue, 或描述动机。

## 怎么改的

非显然的实现细节。

## 测试计划

- [ ] `make test` 本地通过
- [ ] `make smoke` 本地通过 (起 backend + ingest demo + 跑 CLI)
- [ ] `make pii-scan` 报 0 hits
- [ ] `CHANGELOG.md` 在 `## [Unreleased]` 下加了 entry
- [ ] 更新了相关文档

## 隐私

- [ ] diff、commit message、PR 描述里都没有真名、真 ID、真群名、
  真邮箱、真手机号。
- [ ] Demo data (如有) 来自公开语料, 不是来自我自己的对话。
