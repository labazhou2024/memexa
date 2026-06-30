# Demo 数据集

[English](README.md) · **中文**

> 一份小的、完全合成的中文对话语料，供 `memexa demo` 使用。**无真实人物数据。**
>
> 语料是手工合成的，不是从任何聊天导出派生。它模拟个人 6 源记忆（群聊 / 私聊 / 邮件 / 浏览历史 / AI 对话 / 语音备忘）的*形态*，但完全虚构。其中的人物（Alice、Bob、Carol、demo_user）都是编造的。

## 文件

| 文件 | 来源 |
|---|---|
| `wechat_demo.json` | 合成的课程学习组群聊 |
| `qq_demo.json` | 两个 demo 用户之间合成的一对一聊天 |
| `email_demo.json` | 合成的课程通知邮件 |
| `browser_demo.json` | 合成的浏览历史条目（标题 + URL） |
| `claude_demo.jsonl` | 合成的 AI 助手对话记录 |
| `audio_demo_transcript.json` | 合成的虚构语音备忘 ASR 输出 |

## Schema

每个源有自己的 JSON schema：

- `wechat_demo.json` — `{room, sender, send_time, content}` 数组
- `qq_demo.json` — 同形态；`room` 是合成 chat-id
- `email_demo.json` — `{from, to, subject, sent_at, body}` 数组
- `browser_demo.json` — `{visit_time, url, title}` 数组
- `claude_demo.jsonl` — 每行一个 JSON：`{ts, role, content}`
- `audio_demo_transcript.json` — `{session_id, started_at, speakers,
  utterances: [{speaker_id, start_ms, end_ms, text}]}`

## 运行

开源 demo 用 stub 抽取器摄入这份语料，并对结果跑几个示例查询，全程在内存中完成——无需后端、无需 LLM key、无需任何配置：

```bash
memexa demo
# 或直接调摄入脚本：
python -m examples.demo_dataset.ingest --dry-run
```

## 许可

这里的合成数据按 CC0 1.0（公有领域）发布——任何用途随便用，无需署名。摄入脚本本身是 Apache-2.0。
