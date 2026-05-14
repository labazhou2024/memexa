# Case studies

[English](README.md) · **中文**

> 可复现的方法论 write-up, 蒸馏自真实 memex 部署经验。每篇 case study
> 回答的是: "我已经有了 memex bank 装着自己的数据 — 怎么把它变成有用的
> 东西?"
>
> 全部示例都用合成 [`demo_dataset/`](../../examples/demo_dataset/) 角色
> (Alice / Bob / Carol / advisor@example.com)。方法论直接可移植到你
> 自己的数据。

## 包含什么

| # | Case study | 解决什么问题 | 输出 |
|---|---|---|---|
| [01](01_lab_report_pipeline.zh.md) | **错过 ddl 补救流水线** | "我错过了 deadline, 要快速产出一份交付物" | LaTeX → PDF + 行动卡 |
| [02](02_meeting_brief_pattern.zh.md) | **5 分钟见面简报** | "我明天见 X。我欠她什么? 开放话题是啥? 雷区在哪?" | 4 段 Markdown brief |

## "case study" 在这里 vs `lessons_learned/` 的区别

- `docs/lessons_learned/` — **工程复盘**。我们踩过的 bug, 修过的实现,
  还的债。受众: contributor。
- `docs/case_studies/` (本目录) — **面向用户的食谱**。多命令工作流, 把
  `memex` 子命令拼成一个交付物。受众: 终端用户。

## 怎么写自己的

如果你有值得分享的工作流:

1. 残酷地删个人数据 — 见 [SECURITY.md](../../SECURITY.md)。
   不能有真实姓名 / 邮箱 / 手机号 / 跟具体人对应得上的具体日期。
2. 能换 demo_dataset 角色就换 (Alice / Bob / Carol / `advisor@example.com`),
   方便读者跟着跑。
3. 命令序列**原文照抄** (复制粘贴, 不是改写)。
4. 预期 output **结构**给出 (不是原文 — 只要列结构: 列头, 行数范围)。
5. 末尾写一段 "用到你自己的数据" — 一行替换指南。

提个 PR。门槛是"另一个人按你的步骤走能拿到同类输出吗?" — 不是
"你的英文写得多漂亮"。
