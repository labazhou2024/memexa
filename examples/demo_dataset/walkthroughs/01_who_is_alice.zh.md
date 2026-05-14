# Walkthrough 01 · Alice 是谁?

[English](01_who_is_alice.md) · **中文**

> **30 秒 TL;DR**: 查人首选 `arc`, 千万别用 `topic`。`topic` 默认 11 变体里
> 有"购物决策"语义，会污染人名查询。`arc` 跑 8 个关系语义并发召回。然后
> 用 `quick` 补 `arc` 不强的最近 1-2 周窗口。

## 场景

明天要见 Alice。一周没说话。问题: *她对我是什么角色, 还有什么没办完的事,
有没有踩雷点?*

```
┌─────────────────────────────────────────────────────────────────────┐
│  你晚上 11 点想起来:                                                 │
│  "等下 — Alice 那边在忙啥来着? 我还欠她啥吗?"                       │
│                                                                     │
│                          memexa arc "Alice" → 关系基线               │
│                          memexa quick "Alice 1月" → 最近一周         │
│                          → 5 分钟把心智模型重建出来                  │
└─────────────────────────────────────────────────────────────────────┘
```

## Step 1 — 关系基线

```bash
memexa arc "Alice" --max-cards 60
```

`arc` 是关系视角的子命令。它扇出**8 个语义变体** (history / relationship /
interactions / arc / chronological / together / shared / first-met) 然后合并。
查人就用这个。

⚠️ 不要用 `topic "Alice"` — `topic` 是给"事/物/项目"调的, 默认变体里有
"X 购买 价格" / "X 商家 渠道" / "X 退货", 完全是污染。

预期输出结构:

```
=== arc("Alice") ──── 18 张卡跨 wechat+qq, 2024-01-05 → 2024-01-22 ───

📅 2024-01-05  qq   demo-1on1-alice
   Alice 推荐 DDIA 第 5 章 (replication / leaderless)

📅 2024-01-08  wechat  demo-study-group
   Alice 把组会改到周三下午三点

📅 2024-01-12  qq   demo-1on1-alice
   Alice ppt 已做完, 等 Bob 数据合稿

📅 2024-01-15  wechat  demo-study-group
   Alice 14:00 准时开始组会

📅 2024-01-22  wechat  demo-study-group
   Alice 通知组会因节假日改到 1-30
```

5 行。你现在知道: Alice 是组会牵头人, 读书人 (DDIA), 调度负责人。

## Step 2 — 最近 1-2 周窗口

`arc` 偏广度; 如果 Alice 昨天才发你消息, 可能被 2024-01-05 那张"第一次接触"
压住。用 `quick` 补:

```bash
memexa quick "Alice 1月" --max-k 20
```

```
=== quick("Alice 1月") ──── 6 张卡 / 7 天窗口 ───

📅 2024-01-22  wechat  Alice → group   "下周组会因节假日改到 2024-01-30"
📅 2024-01-15  wechat  Alice → group   "组会 14:00 准时开始"
📅 2024-01-12  qq      Alice → me      "做好了，等 Bob 数据合稿"
...
```

## 为什么这 2 步组合最优

| Step | 干什么 | 不做会漏什么 |
|---|---|---|
| 1. `arc`  | 跨周-到-今天的全关系弧 | 近因偏差 — 只看到最新几条, 漏掉基线 |
| 2. `quick` (人名 + 月份) | 最近 1-2 周, 无语义重排 | 覆盖偏差 — `arc` 60 卡封顶会把昨天挤掉 |

```
       arc:  ●─────●───────●──────●──────●──────●
             关系广度, 时间平衡

     quick:                          ●●●●●●
                                     最近一周近因
```

两个加起来 = 30 秒 wall time 拿到完整画像。

## 用到你自己的数据

只要把你自己微信/QQ 抽进 bank, *同样* 两条命令就好用。把 `Alice` 换成
任何你经常聊的人:

```bash
memexa arc "<你朋友的名字>" --max-cards 60
memexa quick "<你朋友的名字> $(date +%Y)年$(date +%m)月" --max-k 20
```

很多人会把这两行包成 shell 函数 `who`。

## 相关

- [04_what_did_advisor_say.zh.md](04_what_did_advisor_say.zh.md) — 如果对方
  是"对手方/上对下"(导师/老板/客户)，用 `person` 不用 `arc`。
- [docs/case_studies/02_meeting_brief_pattern.zh.md](../../../docs/case_studies/02_meeting_brief_pattern.zh.md)
  — 同一套 2 步模式, 套进 4 段简报模板。
