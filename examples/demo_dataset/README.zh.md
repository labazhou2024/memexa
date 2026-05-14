# Demo 数据集

[English](README.md) · **中文**

> 一份小的, 完全合成的中文对话 corpus, 由 `make demo-ingest` 和
> `make smoke` 用。**无真实人数据。**
>
> Corpus 是手工合成的, 不是从任何聊天 export 派生的。它模拟个人 6 source
> 记忆 (群聊 / 私聊 / 邮件 / 浏览历史 / AI 对话 / 语音备忘) 的*形态*, 但
> 完全虚构。

## 文件

| 文件                                    | 行数  | 来源              |
|-----------------------------------------|-------|---------------------|
| `wechat_demo.json`                      | 120   | 合成的课程学习组群聊 |
| `qq_demo.json`                          | 80    | 两个 demo user 之间合成的 1-on-1 聊天 |
| `email_demo.json`                       | 25    | 合成的课程通知邮件 thread |
| `browser_demo.json`                     | 40    | 合成的浏览历史条目 (title + URL) |
| `claude_demo.jsonl`                     | 60    | 合成的 Claude 对话 transcript |
| `audio_demo_transcript.json`            | 15    | 合成的虚构 3 分钟 memo ASR 输出 |

所有文件名用 `_demo` 后缀方便 grep 识别。

## Schema

每个 source 有自己 JSON schema; schema 镜像真摄入 builder 期望的:

- `wechat_demo.json` — 消息 object 数组 `{room, sender, send_time, content}`
- `qq_demo.json` — 同形态; `room` 是合成 chat-id
- `email_demo.json` — 数组 `{from, to, subject, sent_at, body}`
- `browser_demo.json` — 数组 `{visit_time, url, title}`
- `claude_demo.jsonl` — 每行一个 JSON `{ts, role, content}`
- `audio_demo_transcript.json` — `{session_id, started_at, speakers,
  utterances: [{speaker_id, start_ms, end_ms, text}]}`

## 摄入

```bash
python -m examples.demo_dataset.ingest
```

脚本:

1. 读每个 demo 文件
2. 调 `src/ingestion/` 和 `src/extraction/` 里匹配 builder 在
   `data/demo/<source>/batches/` 下产 per-source batch
3. 跑双 LLM 抽取 (gate + extract)。**默认用 stub LLM** 吐确定性合成 V2
   envelope, 所以 smoke test 不需要真 LLM endpoint。设
   `MEMEX_REMOTE_LLM_BASE_URL` 用真模型
4. POST 结果卡到本地 Hindsight daemon `http://127.0.0.1:8888`

摄入后 `memory_full_v5_demo` bank 应该有 ~80-120 张卡 (与你可能有的真
bank 分开)。

## 查询

```bash
python -m src.core.memory_query topic "studying" --bank memory_full_v5_demo
python -m src.core.memory_query timeline --start 2024-01-01 --end 2024-02-01 --bank memory_full_v5_demo
python -m src.core.memory_query arc "Alice" --bank memory_full_v5_demo
```

## License

这里的合成数据按 CC0 1.0 (公有领域) 发布。任何用途随便用, 不需归属。
摄入脚本本身是 Apache 2.0。

## 为啥合成不用 LCCC

Large-scale Chinese Conversation (LCCC) corpus 和类似公开数据集对训练
chatbot 很好但对我们 smoke test 有两个缺陷:

1. 太大 (>1 GB), 不适合 CI
2. 缺我们需要练每个 builder 的*6 source 多样性*

合成 corpus ~30 KB, 完整 smoke test <30 秒覆盖。
