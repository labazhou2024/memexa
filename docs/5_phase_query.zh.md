# 5 阶段状态推理 worked example

[English](5_phase_query.md) · **中文**

> 单次语义召回能找出*关于某主题的帖子*, 但**没法推断**用户当前关于该主题
> 的*状态*。5 阶段工作流综合 5 个正交信号回答状态题, 比如*"X 项目还活着
> 吗?"* / *"我退掉了 Y 课程吗?"*。
>
> 下面是一个完整 worked example。名字已替换为通用占位。

## 问题

> *"Y 课程 — 我还在上吗?"*

背景: 用户学期开始时报了 Y 课。一学期后想确认有没有退掉。一次
`quick("Y")` 返回 ~30 卡 (通知、同学闲聊) 但没法回答**状态**题。

## Phase A — seed

```bash
python -m src.core.memory_query quick "Course Y" --max-k 30
python -m src.core.memory_query arc  "Course Y"  --max-cards 80
```

结果:

- 群 `<COURSE-Y-GROUP>` 里 11 张卡
- 日期范围: `2026-03-10` (入群) 到 `2026-04-23` (最后观察到活动)
- 用户是 ~30 个群成员之一

## Phase B — 实体扩展

弄清群里还有谁, 他们的角色是什么。

```bash
python -m src.core.memory_query arc "<TA-handle>"      --max-cards 20
python -m src.core.memory_query arc "<peer-handle>"    --max-cards 20
```

- `<TA-handle>` 在同一群发了 7 次作业通知 → 角色 = TA
- `<peer-handle>` 出现在另一个共享群 (`<dept-alumni-group>`) → 角色 = 系内同学

## Phase C — 5 个正交信号

| # | 信号               | 查询                                                | Y 课程的结果                                       |
|---|--------------------|------------------------------------------------------|------------------------------------------------------|
| 1 | user-speaks        | `quick("Course Y")` 过滤 `speaker_role=self`         | 课程群里 0 条用户发言                                  |
| 2 | user-silence       | `timeline(room=<COURSE-Y-GROUP>) ∩ user`             | 用户从未在该群发言                                      |
| 3 | boundary           | `quick("drop deadline cutoff")`                      | 命中: 退课截止 2026-03-13 18:00, 需教务签字            |
| 4 | peer-triangulation | `arc("<peer-handle>") ∩ "Course Y"`                  | Peer 和 user 2026-04-28 私聊 "申请通过"                  |
| 5 | private            | `quick("Course Y review")` 过滤 `speaker_role=self`   | 2026-04-03 自记: "复习 Course Y 期末用"                |

## Phase D — 推理链

```
2026-03-10  用户入课程群 (报名成功)
2026-03-13  退课截止 18:00, 需教务签字
2026-03-26  TA 警告: "考勤太低, 要点名"
2026-04-03  用户自记: "复习 Course Y"          ← 还打算继续上
2026-04-22  其他学生请假 (课在继续)
2026-04-28  用户 + peer 私下讨论 "申请通过"
2026-04-29  用户确认: "我那个通过了"

对齐:
  Peer 是同届系内同学 (Phase B 从系内群推断)。"申请通过" + 同届 + 
  模糊机构流程 + 用户 4 月后从课程群消失 = 退课申请。
```

## Phase E — 反证

```
反证 1: 如果没退, 4 月末后应该有
  - 课程群里用户回复          → 0 卡
  - 私下吐槽难                 → 0 卡
  - 期末复习笔记               → 0 卡
  全 0 → 退课结论站得住

反证 2: "申请通过"会不会是别的?
  - 用户和 peer 2026-04-29: "我那个没找老师就过了"。这是机构流程,
    有普通和教务签字两条路 — 匹配退课, 不匹配其他候选
    (暑研 / 直博申请从不提教务签字)。
  → 退课是唯一拟合
```

## 结论

> **用户退掉了 Y 课程。** 用了首次退课特权; 还剩 1 次退课额度。

## 为什么单次召回不行

如果你只跑信号 1 ("用户不在群里发言"), 会得出 "用户在悄悄旁听"。
跑 1 + 2 会得出 "用户悄悄退课但还在名单上"。至少需要 1 + 2 + 4 + 反证
扫描才能锁定唯一状态。

这个模式可以泛化到任何用户已离开社交语境的 *yes / no* 问题。模板:

```
A. Seed 卡      → 识别社交语境
B. 扩展         → 标注语境内其他角色
C. 5 个信号     → user-speaks / user-silence / boundary / peer / private
D. 链           → 最可能状态
E. 反证         → 证伪查询
```

## 相关

- [usage_guide.zh.md](usage_guide.zh.md) — 上面用到的 8 个子命令
- [architecture.zh.md#6-five-phase-semantic-state-inference](architecture.zh.md) — 设计理由
