# WeChat integration

**English** · [中文](wechat.zh.md)

`memexa` ingests WeChat chat history via per-batch JSON envelopes. It
does **not** export the data itself — you bring the export, the builder
normalizes it, the extractor pulls cards.

## Recommended exporters

| Tool                                                                            | Platform        | Output                          | Tested? |
|---------------------------------------------------------------------------------|-----------------|---------------------------------|---------|
| [`WeChatMsg`](https://github.com/LC044/WeChatMsg) by LC044                      | Windows         | JSON / HTML / CSV per chat      | yes — primary path |
| [`wechatDataBackup`](https://github.com/git-jiadong/wechatDataBackup)           | Windows         | JSON per chat                   | yes — fallback     |
| [`PyWxDump`](https://github.com/xaoyaoo/PyWxDump)                               | Windows         | SQLite + JSON                   | community-reported  |

Pick one, follow its setup guide (you need WeChat decryption keys, the
exporter docs each cover this), and end up with a folder of JSON files
per chat / group.

## Builder input contract

The builder reads from a directory tree:

```
<wechat-export-root>/
├── <friend-or-group-name>/
│   ├── messages.json        # array of message objects
│   └── meta.json            # optional; chat metadata
└── ...
```

Each message object must contain at minimum:

```json
{
  "ts": "2026-05-04T14:30:00+08:00",
  "wxid_hash": "<stable-anon-id>",
  "sender_display_name": "<surface form>",
  "content": "<utterance text>",
  "msg_type": "text"
}
```

`msg_type` ∈ `{text, voice, image, video, file, system}`. Non-text types
are kept as references; only `text` and ASR-transcribed `voice` reach
the extractor.

## Wire-up

```bash
# 1. Point the builder at your export
export MEMEXA_WECHAT_EXPORT_DIR=/path/to/wechat/export

# 2. Run the builder once (writes batch files to data/l0_v5/input_batches/)
python -m memexa.ingestion.v5_wechat_batch_builder

# 3. Confirm pending batches appear
ls data/l0_v5/input_batches/$(date +%Y-%m-%d)/ | head

# 4. Run the driver once to extract + POST
python -m memexa.drivers.backfill_v5_wechat_driver --once --verbose

# 5. Query
memexa quick "<an entity name from the export>"
```

## Schema you actually need to fill in

The builder reads `~/.memexa/aliases.yaml` to decide which `wxid_hash`
values are "you". Make sure your own hash appears in `self_aliases`,
otherwise every card will be tagged speaker_role=`third_party`.

A working `aliases.yaml`:

```yaml
self_aliases:
  - "<your-display-name>"
  - "<your-other-display-name>"
  - "<wxid_hash if you know it>"
self_roles:
  - student
timezone: "Asia/Shanghai"
```

## Common problems

### "Builder finds 0 messages"

- Path mismatch — confirm `ls $MEMEXA_WECHAT_EXPORT_DIR` lists chat
  directories, not raw `.db` files.
- Tool used "HTML" or "CSV" output, not "JSON". Re-export with JSON.
- Tool emitted UTF-16 BOM. The builder reads UTF-8; re-encode with
  `iconv -f UTF-16 -t UTF-8`.

### "Group chats explode my batch count"

By default the builder cuts batches at ~30 messages OR ~5 minutes of
clock time, whichever comes first. Group chats with rapid traffic
produce many small batches. Adjust via `--batch-window-min` on the
builder if your provider charges per request.

### "Voice messages don't show up in cards"

Voice messages need ASR. The audio source pipeline is separate from the
WeChat builder; export the `.amr` / `.silk` audio blobs and drop them
into `data/audio/inbox/` for the audio driver to pick up.

### "Stickers + emoji appear as `[微笑]` placeholders"

By design. The extractor LLM is good at inferring intent from the
surrounding text, and explicit emoji-noise filtering loses real
signal (`[捂脸]` after a confession is actually content). Leave them
in.

## Privacy notes

- The exporter tools require WeChat decryption keys. Treat the keys
  like passwords; the export is plaintext.
- Group chat exports include other people's messages. If you publish or
  share the resulting graph, redact non-self entities first. The
  pre-commit PII scanner catches the obvious cases (real names,
  phone-shape strings) but is not a substitute for thinking.
- The `wxid_hash` field is already hash-of-id by the time WeChatMsg
  emits it; the underlying account ID is not recoverable. Builder code
  never re-hashes or de-anonymizes.

## Roadmap

- ✅ Text messages (v0.1)
- ✅ Voice messages via separate audio pipeline (v0.1)
- 🔜 Inline image OCR (v0.4 candidate)
- 🔜 Quoted-message threading (v0.4 candidate)
- ❌ Stickers / mini-apps / red packets — out of scope
