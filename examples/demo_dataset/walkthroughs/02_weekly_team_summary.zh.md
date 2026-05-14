# Walkthrough 02 · 上周组里干了啥?

[English](02_weekly_team_summary.md) · **中文**

> **30 秒 TL;DR**: 查项目用 `topic` 扇出 11 变体, 广度足够好。`trends` 按
> sender/source 在时间窗口里聚合, 适合"谁干了大头活"。两个叠起来用。

## 场景

周日晚, 你要给导师发一句话状态: *组里周一到周五具体做了啥?*

```
                       过去 5 天
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
    微信群             QQ 1-on-1            AI 对话
   (15 条消息)          (Alice / DDIA)     (系统设计)
        │                   │                   │
        └─────────┐         │         ┌─────────┘
                  ▼         ▼         ▼
              memex topic "中期报告"
              memex trends --by sender --window-days 7
                            │
                            ▼
                ┌────────────────────────┐
                │  一段话状态 + 每人贡献 │
                └────────────────────────┘
```

## Step 1 — 项目本周心跳

```bash
memex topic "midterm report" --window-days 7
```

`topic` 默认扇出 **11 个语义变体** — 查项目名正合适: 它会同时召回
"中期报告" / "报告大纲" / "实验部分" / "演示文稿" / "提交截止" 等不同说法。

预期输出:

```
=== topic("midterm report") ──── 14 张卡 / 4 source / 7 天窗口 ───

[wechat]  2024-01-08 10:14  Alice → group   "组会改到周三下午三点"
[wechat]  2024-01-09 18:02  Bob → group     "演示文稿做到第 5 页, 明天交"
[wechat]  2024-01-09 18:03  Carol → group   "实验数据明早可以交, 已跑完"
[wechat]  2024-01-10 09:30  me → group      "第 3 部分写完了"
[qq]      2024-01-12 15:02  Alice → me      "ppt 做好了, 等 Bob 数据合稿"
[email]   2024-01-08 09:00  advisor → me    "中期报告 1-16 23:59 前提交"
[email]   2024-01-08 14:05  advisor → me    "至少包含实验数据, 不能只是综述"
[email]   2024-01-11 22:00  me → group      "整理了一份大纲, 四部分..."
[browser] 2024-01-09 14:20  fastapi-deps doc      
[browser] 2024-01-15 09:45  midterm-report-template
[claude]  2024-01-09 11:00  user → claude   "实验部分该怎么组织？"
[claude]  2024-01-09 11:00  claude          "三段式：设置/结果/讨论"
[audio]   2024-01-11 08:32  voice memo      "Bob 数据明早齐, Carol OK"
...
```

## Step 2 — 谁干了哪部分?

```bash
memex trends --by sender --window-days 7 --filter "topic:midterm"
```

`trends` 按 sender/source/room/types 聚合。**贡献认定** —即 *谁实际露面* —
就用 sender 维度。

```
=== trends by sender, 最近 7 天, midterm-tagged 卡 ───

       sender       │ 卡数  │ 条形图
       ─────────────┼───────┼─────────────────────────────
       Alice        │   5   │ ████████████  排期 + ppt
       me           │   4   │ ██████████    第 3 部分 + 大纲
       Bob          │   2   │ █████         数据 + ppt 第 5 页
       Carol        │   2   │ █████         实验数据
       advisor      │   2   │ █████         指令 + 提醒
       (claude AI)  │   1   │ ██▌           结构脑暴
```

## 拼出一句话状态

现在你有完整信息写三句诚实的话:

```
本周组会推进中期报告。Alice 把组会改到周三 14:00（1-15）并合稿 ppt；
Bob 完成 ppt 第 5 页 + Carol 跑完实验数据（均 1-09 交付）；
我（demo_user）写完第 3 部分并提交大纲（1-10 / 1-11）。
导师 1-08 邮件强调"必须含实验数据"，符合当前进度。
下一里程碑：1-16 23:59 提交完整报告。
```

## 为什么 `topic + trends` 比单跑 `topic` 强

| 单 `topic` | `topic + trends` |
|---|---|
| 14 张原始卡, 你眼看 | 14 张卡 **加上**每人一行的条形 |
| 自己数每人贡献 | 条形图直接给排名 |
| 容易过分归功于"声音大的那位" (Alice 卡最多) | trends 显示 Carol 2 张全是实打实交付 |

## 用到你自己的数据

```bash
memex topic "<你的项目名>" --window-days 7
memex trends --by sender --window-days 7 --filter "topic:<你的项目关键词>"
```

如果想看"哪个 source 主导" (比如"我们最近在微信还是邮件多?"):

```bash
memex trends --by source --window-days 7
```

## 相关

- [03_project_status_check.zh.md](03_project_status_check.zh.md) — 不限 7
  天窗口, 要看完整项目时间线时, 换成 `project + timeline`。
- [docs/usage_guide.zh.md#trends](../../../docs/usage_guide.zh.md) — `trends`
  完整选项。
