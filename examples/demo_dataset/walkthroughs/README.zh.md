# 实例 walkthrough 集

[English](README.md) · **中文**

> 用合成数据集回答 5 个真实日常问题。每条命令、每行输出都可复现 ——
> `make demo-ingest` 之后跟着跑即可。

这些 walkthrough 写出来不是教单个子命令，而是展示**命令组合的套路**。
每篇含:

1. 一个真实场景的问题
2. 该选哪个 subcmd（以及避免哪个坑）
3. CLI 完整调用
4. 合成 output 长啥样
5. 为什么这个组合比单跑某条命令更好

```
┌───────────────────────────────────────────────────────────────────┐
│                       你想回答的问题是…                            │
└───────────────────────────────────────────────────────────────────┘
                              │
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
   X 是谁?                X 项目到哪了?         我这周要做啥?
   → 01_who_is_alice      → 03_project_status   → 05_my_pending_actions
       │                      │                      │
       ▼                      ▼                      ▼
   Y 老师/老板要啥?       上周组里干了啥?       跨源回顾本周?
   → 04_advisor_said      → 02_weekly_summary   → 02_weekly_summary
```

## 数据集说明

```
demo-study-group  (WeChat)   ─── Alice, Bob, Carol, demo_user
demo-1on1-alice   (QQ)       ─── demo_user ↔ Alice (DDIA 读书会)
advisor@example.com (Email)  ─── 中期报告导师指令
浏览历史                       ─── 分布式系统 + RAG 调研
Claude 对话                    ─── 系统设计 + 报告结构脑暴
voice memo                   ─── demo_user 自言自语想中期报告
```

同一周（2024-01-04 到 2024-01-22）从 6 个角度看同一件事。每篇 walkthrough
至少跨两个 source。

## 索引

| # | Walkthrough | 模式 | 子命令 |
|---|---|---|---|
| [01](01_who_is_alice.zh.md)   | Alice 是谁?                | 从名字到人物画像                  | `arc` + `quick` |
| [02](02_weekly_team_summary.zh.md) | 上周组里干了啥?         | 时段跨源汇总                      | `topic` + `trends` |
| [03](03_project_status_check.zh.md) | 中期报告到哪了?         | 项目跨源 rollup                   | `project` + `timeline` |
| [04](04_what_did_advisor_say.zh.md) | 导师要啥?               | 单向对手方深挖                    | `person` |
| [05](05_my_pending_actions.zh.md) | 我这周要做啥?           | 待办面板                          | `pending` + `quick` |

## 在本地跑数据集

```bash
# 在 repo 根目录
docker compose -f docker-compose.example.yml up -d   # 起后端
make demo-ingest                                      # POST 26 张卡
memexa doctor                                          # 确认 bank 有数据

# 之后任何 walkthrough 里的命令都能返回真实输出
```

## 阅读顺序建议

- 新用户: 01 → 04 → 05 ("人 + 待办"循环, 大多数人都从这开始)
- 工程师: 02 → 03 ("rollup" 模式, 写状态报告最有用)
- 老用户: 5 篇通读, 注意里面反复出现的"先广召回 → 再精炼" 2 步模式 ——
  这个模式在每个工作流里都会冒出来
