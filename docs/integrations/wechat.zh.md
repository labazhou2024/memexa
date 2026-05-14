# 微信接入

[English](wechat.md) · **中文**

`memex` 通过 per-batch JSON envelope 摄入微信聊天记录。它**不**导出数据
本身 — 你拿 export, builder 规范化, extractor 抽卡。

## 推荐 exporter

| 工具                                                                            | 平台            | 输出                              | 测过? |
|---------------------------------------------------------------------------------|-----------------|---------------------------------|---------|
| [`WeChatMsg`](https://github.com/LC044/WeChatMsg) by LC044                      | Windows         | JSON / HTML / CSV per chat      | 是 — 主路径 |
| [`wechatDataBackup`](https://github.com/git-jiadong/wechatDataBackup)           | Windows         | JSON per chat                   | 是 — fallback |
| [`PyWxDump`](https://github.com/xaoyaoo/PyWxDump)                               | Windows         | SQLite + JSON                   | 社区反馈    |

挑一个, 跟着它的 setup guide (需要微信解密 key, exporter 文档都讲), 最后
得到一堆 per-chat JSON 文件。

## Builder 输入合同

Builder 读这种目录树:

```
<wechat-export-root>/
├── <friend-or-group-name>/
│   ├── messages.json        # 消息 object 数组
│   └── meta.json            # 可选; 聊天 metadata
└── ...
```

每个 message object 至少包含:

```json
{
  "ts": "2026-05-04T14:30:00+08:00",
  "wxid_hash": "<stable-anon-id>",
  "sender_display_name": "<surface form>",
  "content": "<utterance text>",
  "msg_type": "text"
}
```

`msg_type` ∈ `{text, voice, image, video, file, system}`。非 text 类型
作为引用保留; 只有 `text` 和 ASR 转写过的 `voice` 进入 extractor。

## 接入

```bash
# 1. 指向你的 export
export MEMEX_WECHAT_EXPORT_DIR=/path/to/wechat/export

# 2. 跑一次 builder (写 batch 文件到 data/l0_v5/input_batches/)
python -m src.ingestion.v5_wechat_batch_builder

# 3. 确认 pending batch 出现
ls data/l0_v5/input_batches/$(date +%Y-%m-%d)/ | head

# 4. 跑一次 driver 抽取 + POST
python -m src.drivers.backfill_v5_wechat_driver --once --verbose

# 5. 查询
memex quick "<export 里某个实体名>"
```

## 你需要填的 schema

Builder 读 `~/.memex/aliases.yaml` 决定哪些 `wxid_hash` 是 "你"。确保你
自己的 hash 在 `self_aliases` 里, 否则每张卡都会标 speaker_role=`third_party`。

工作中的 `aliases.yaml`:

```yaml
self_aliases:
  - "<你的显示名>"
  - "<你的其他显示名>"
  - "<wxid_hash 如果你知道>"
self_roles:
  - student
timezone: "Asia/Shanghai"
```

## 常见问题

### "Builder 找到 0 条消息"

- 路径不匹配 — 确认 `ls $MEMEX_WECHAT_EXPORT_DIR` 列出聊天目录, 不是
  原始 `.db` 文件
- 工具用了 "HTML" 或 "CSV" 输出, 不是 "JSON"。用 JSON 重导
- 工具产了 UTF-16 BOM。Builder 读 UTF-8; 用 `iconv -f UTF-16 -t UTF-8`
  重编码

### "群聊把我 batch 数炸了"

默认 builder 在 ~30 条消息 OR ~5 分钟时钟时间切 batch, 哪个先到算哪个。
高速群聊产很多小 batch。如果你的 provider 按请求收费, 用
`--batch-window-min` 调。

### "语音消息没出现在卡里"

语音消息需要 ASR。Audio source pipeline 和 WeChat builder 分开; 导出
`.amr` / `.silk` 音频 blob 丢到 `data/audio/inbox/` 让 audio driver 捡。

### "表情包 + emoji 显示成 `[微笑]` 占位"

By design。Extractor LLM 善于从上下文推断意图, 显式过滤 emoji 噪声会
丢真信号 (`[捂脸]` 接在告白后是有意义的内容)。留着。

## 隐私须知

- Exporter 工具需要微信解密 key。把 key 当密码; export 是明文
- 群聊 export 含别人的消息。如果你发布或分享生成的图谱, 先脱敏非自己
  实体。Pre-commit PII scanner 抓明显的 (真名 / 电话形态字串) 但不替代
  思考
- `wxid_hash` 字段在 WeChatMsg 输出时已经是 hash-of-id; 底下账号 ID 不
  可逆。Builder 代码从不 re-hash 或 de-anonymize

## 路线图

- ✅ 文本消息 (v0.1)
- ✅ 语音消息走单独 audio pipeline (v0.1)
- 🔜 内联图片 OCR (v0.4 候选)
- 🔜 引用消息 threading (v0.4 候选)
- ❌ 贴纸 / 小程序 / 红包 — 不在范围内
