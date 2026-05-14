# QQ integration

**English** · [中文](qq.zh.md)

QQ uses a SQLite database on disk (`nt_msg.db`) that the builder reads
directly. Unlike WeChat there is no recommended export tool — you point
at the live file and the builder does incremental reads.

## Locating the database

On Windows the canonical path is:

```
%USERPROFILE%\Documents\Tencent Files\<qq-id>\nt_qq\nt_db\nt_msg.db
```

`<qq-id>` is the numeric QQ account ID. If you have multiple accounts,
each gets its own subdirectory; the builder reads one at a time.

On macOS:

```
~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/<qq-id>/...
```

The exact path differs by QQ client version — search inside
`~/Library/Containers/com.tencent.qq/` for `nt_msg.db`.

## Wire-up

```bash
# 1. Tell memex which QQ account ID to ingest
$EDITOR ~/.memex/identity.yaml
# Add or set:
#   qq_id: "<your-qq-id>"

# 2. Test the reader can open the DB
python -c "
from src.extraction.qq.qq_history_to_batches import probe_db
probe_db()
"

# 3. Run the builder once
python -m src.extraction.qq.qq_history_to_batches

# 4. Run the driver once
python -m src.drivers.backfill_v5_qq_driver --once --verbose
```

## Lock contention

QQ holds a write lock on `nt_msg.db` while the desktop client is open.
The builder opens the database in read-only mode (`mode=ro` + `nolock=1`
URI), so simultaneous read is safe — but bear in mind:

- A message that the desktop client is mid-write is invisible to the
  reader until QQ finishes the transaction.
- If you see "database is locked" errors anyway, your SQLite build
  doesn't honor the URI options. Upgrade Python or close QQ while
  ingesting.

## Schema notes

QQ's nt_msg schema has changed multiple times across client versions.
The builder supports:

- NT QQ 9.9.x (current as of 2026-05) — full support
- NT QQ 9.7–9.8 — full support
- Older "legacy QQ" (mht export) — not supported; use a one-time
  conversion script

If the builder errors `unknown schema version <N>`, file an issue with
the version number; adding a new schema variant is a half-day patch.

## Group chats

QQ group chat messages include the sender's `wxid_hash`-equivalent
(numeric QQ ID hashed). The builder canonicalizes this through
`~/.memex/aliases.yaml` the same way WeChat does. Set your own QQ ID
under `self_aliases` to get speaker_role=`self` cards.

```yaml
self_aliases:
  - "<your-display-name>"
  - "<your-qq-id>"      # numeric, treated as string
self_roles:
  - student
timezone: "Asia/Shanghai"
```

## Privacy notes

- The `nt_msg.db` file contains plaintext message content for all
  chats. It is **not** encrypted at rest. The builder treats it as
  highly sensitive; do not commit, do not back up to public storage.
- The PostgreSQL bank stores the extracted *narrative* (an LLM-generated
  third-person summary), not the raw message text. The raw text stays
  in `nt_msg.db`. Set a strong OS-level disk encryption key.

## Roadmap

- ✅ Text messages (v0.1)
- ✅ Voice messages via separate audio pipeline (v0.1)
- 🔜 Quoted-message threading (v0.4)
- ❌ TIM client variant — community PR welcome; maintainer has no setup
- ❌ Mobile-only QQ — Android export tooling outside maintainer's scope
