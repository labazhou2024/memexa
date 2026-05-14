# 用法指南

[English](usage_guide.md) · **中文**

> 每个查询子命令, 何时用, 返回什么。想搞清每条命令背后发生了什么先看
> [architecture.zh.md](architecture.zh.md)。

## 决策表

| 用户问题模式                                                           | 子命令                                              | 为什么                                                    |
|----------------------------------------------------------------------|-----------------------------------------------------|-----------------------------------------------------------|
| "X 是谁 / X 干了什么 / 我怎么认识 X / 我和 X 关系"                    | `arc "X"`                                           | 8 个意图变体 + 按时间戳排序                                |
| "X 的全过程" (X = 事 / 项目 / 不是人)                                | `topic "X"`                                         | 11 变体跨两个 bank 扇出                                    |
| "A 到 B 这段时间发生了什么"                                           | `timeline --start A --end B`                        | 多变体扇出 + `when_start` 过滤                            |
| "Y 老师 / 同学 近况"                                                   | `person "Y"`                                        | 人物档 (文章 + 事件)                                       |
| "Z 项目跨源最新动态"                                                  | `project "Z"`                                       | wechat / qq / email / browser 聚合                         |
| "我有哪些 commitment / 未答疑问"                                       | `pending`                                           | 读 `calendar_index.json:status=active`                     |
| "我有没有退掉 Y 课程" / 状态题                                         | **5 阶段工作流** (见下)                            | 单次召回三角不出来                                          |
| "给我一个综合答案的 Q"                                                | `reflect "Q"`                                       | 服务端 LLM 在召回卡上综合                                   |

## 硬规则: 查人永远不用 `topic`

`topic` 默认 11 变体是给 *购买 / 决策选择* 调的 (e.g. "X 价格", "X 商家",
"X 退货")。当 `X` 是个人时, BGE-M3 cosine 会捞起购物噪声, 返回 ~0 张
相关卡。**人名永远用 `arc`。**

实测: `arc("<某人 A>")` 命中 6/6; `topic("<某人 A>")` 命中 0/100。

## 5 阶段状态推理

问题是 *"X 是 yes 还是 no"* 且单次语义召回不够时, 用这个协议。

```
Phase A  Seed
  quick("X")         → 实体表面形式
  arc("X")           → 关系弧

Phase B  实体扩展
  对 Phase A 里每个人:
    arc("<人>")        → 推断角色 (同学 / TA / 导师 / 朋友)

Phase C  5 个正交信号
  1 user-speaks    → quick("X") where speaker_role=self
  2 user-silence   → timeline(room=<X>) ∩ user 没出现
  3 boundary       → quick("X deadline") / quick("X cutoff")
  4 peer           → arc(<Phase B 里的同学>) ∩ X
  5 private        → quick("X notes") where speaker_role=self

Phase D  推理链
  5 个信号合成最可能的当前状态

Phase E  反证
  主动搜会证伪 Phase D 的卡
  若反证为空 → 结论站得住
```

完整 worked example 见 [5_phase_query.zh.md](5_phase_query.zh.md)。

## 各子命令选项

### `quick`

```
python -m src.core.memory_query quick "X" [--max-k 30] [--salience 0.0]
```

- `--max-k` 提高 recall budget (默认 10)。配 `--salience 0.0` 看完整分布

### `topic`

```
python -m src.core.memory_query topic "X" [--max-cards 100] [--by-salience]
```

- 11 变体并发扇出 (`ThreadPoolExecutor(max_workers=4)`)
- Wall time 由 Hindsight server 主导 (典型 15-60s)
- **不要**在外层加并发 — 内部扇出已经饱和 daemon 的 BGE worker

### `arc`

```
python -m src.core.memory_query arc "X" [--max-cards 80]
```

- 8 个意图变体**串行**跑 (每个变体的结果引导下一个)
- 卡按时间排序; 第一行是最早接触

### `timeline`

```
python -m src.core.memory_query timeline --start ISO --end ISO [--source S] [--room R]
```

- 多变体扇出 (event / message / important / email / etc.) → union →
  `when_start` 过滤 → 排序

### `person`

```
python -m src.core.memory_query person "Y"
```

- 返合成的 article-card + 底下的 event card

### `project`

```
python -m src.core.memory_query project "Z"
```

- 对 (wechat, qq, email, browser_session, browser_search, claude_code)
  各跑 `quick`, 按 source 分组展示并集

### `pending`

```
python -m src.core.memory_query pending
```

- 读 calendar index。返 active commitment 按 `due_iso` 升序, `salience` 平局

### `reflect`

```
python -m src.core.memory_query reflect "Q"
```

- 服务端 LLM 综合。慢 (10-60s) 且需 daemon 配好 LLM provider

## 常见坑

- **Tags 是 OR, 不是 AND。** Hindsight recall API 把 `tags=[...]` 当析取。
  在 client 端 post-filter。见
  [lessons_learned/01_tags_are_or.md](lessons_learned/01_tags_are_or.md)。
- **`budget="medium"` 会被拒。** enum 是 `low / mid / high`, 不是 `medium`。
  见 `src/core/hindsight_client.py`。
- **`max_tokens` 太小返 1 张卡。** V2 envelope 卡平均 ~866 tokens。默认
  `max_tokens=1024` 只塞得下 1 张。
- **legacy bank 0 卡。** 老 `memory_full` bank 没有 `schema:v2` tag 政策;
  强行用 `tags=[kind:event,schema:v2]` 查会全空。查 legacy bank 用空 tag。

## 故障排查

`Q1 — 查询返回 0 张卡`

```bash
# 诊断
python -m src.core.memory_query quick "X" --salience 0.0 --max-k 50
# 还 0 → 试 topic (不同扇出)
python -m src.core.memory_query topic "X" --salience 0.0 --max-cards 100
# 还 0 → 看 daemon 是否可达
curl -s http://127.0.0.1:8888/healthz
```

`Q2 — 查询超时 (>60s)`

- BGE-M3 sidecar 冷启 — 等 30s 重试
- daemon 内存压力 — 见 lifecycle docs
- `--max-k 50` 是甜点; 更高有超时风险

`Q3 — Windows 上 UnicodeEncodeError`

- 设 `PYTHONIOENCODING=utf-8` 或在 Windows Terminal 跑
