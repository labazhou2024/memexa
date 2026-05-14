# Support

[English](SUPPORT.md) · **中文**

本项目由一个人在业余时间维护。没有 SLA。Issue 在 maintainer 有时间时
分诊。

## 怎么寻求帮助

- 问 *怎么用* → 在 Q&A 类目开 **Discussion**。先搜现有讨论
- 怀疑 bug → 用 bug-report 模板开 **Issue**。附 `memex version`, OS,
  Python 版本, 完整 traceback
- 功能请求 → 用 feature-request 模板开 **Issue**。说明*什么*你需要 +
  *为什么*现有 CLI 不够

## Maintainer 不会替你做的

- 分诊你的数据质量。Pipeline 期望 `docs/integrations/` 文档里那些格式
  的导出聊天归档。导出格式不对自己修
- 跑 hosted 版本。这是仅自托管
- 迁到另一个 memory 后端。Hindsight 是硬依赖
- 商业支持。需要的话 fork

## 响应预期

- 严重安全问题 (提权 / 数据泄露) — 尽力当天确认
- Bug — 尽力 7 天内
- 功能请求 — 无承诺; 可能 backlog 里待无限期
- 文档缺口 — 欢迎 PR, 合最快

## 开 issue 前先看的

1. [docs/troubleshooting.zh.md](docs/troubleshooting.zh.md) — 六层
   诊断阶梯
2. [docs/faq.zh.md](docs/faq.zh.md) — 已答过的问题
3. `memex doctor` — 自动自检
4. `docs/lessons_learned/` — 常见 pipeline 陷阱 + 修法
