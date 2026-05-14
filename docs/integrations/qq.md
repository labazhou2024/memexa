# QQ integration

**English** · [中文](qq.zh.md)

> ## Adapter status (updated 2026-05-15)
>
> **NapCat / OneBot path: deprecated.** Disabled by default after a
> [2025-09-05 NapCat public-OneBot incident](https://www.xcnahida.cn/?p=b8AROpEJ)
> in which Tencent began signature-matching every QQ that ever ran NapCat /
> LiteLoaderQQNT and rolling out batched bans. The maintainer's own QQ was
> caught in this wave on 2026-05-14. Override with `MEMEXA_QQ_NAPCAT_FORCE=1`
> only on a research-disposable account.
>
> **db-only path: the recommended one. Hardened in upstream JARVIS;
> migration to OSS scheduled for v0.2.** OSS v0.1.x users must currently
> wire in the upstream reader (`jarvis/qq_db.py` — single file, stdlib only)
> by hand, or use the clipboard fallback below until v0.2 lands.
>
> **Clipboard fallback: zero protocol traffic.** Reader exists in upstream
> JARVIS (`jarvis/qq_reader.py`); migration to OSS scheduled for v0.2.

---

## 1. Recommended: db-only read path

Reads QQ's local SQLite database directly. Sends no protocol packets,
launches no third-party client. Indistinguishable from any chat-history
backup tool.

> **OSS v0.1.x status**: the SQLCipher decrypt + key-extraction implementation
> lives in upstream JARVIS (`jarvis/qq_db.py`, 762 lines, standard library
> only). It is scheduled for migration to `memexa/extraction/qq/qq_db.py` in
> Memexa v0.2. Until then, OSS users must either copy that file in by hand
> or rely on the clipboard fallback below.

### Trade-offs

- ✅ No known ban cases (`QQBackup/qq-win-db-key` 1k stars, 1-year clean issue tracker)
- ✅ Full history coverage
- ❌ Requires QQ to be logged in *once* so the SQLCipher key can be hooked from memory
- ❌ Real-time: no. You get whatever was synced last time you opened QQ.
- ❌ NT QQ ≥ 9.9.x changed the cipher to SHA-512 in 2024-12 — older guides may
  reference SHA-1 / SHA-256 and will fail.

### Locating the database

Windows canonical path:

```
%USERPROFILE%\Documents\Tencent Files\<qq-id>\nt_qq\nt_db\nt_msg.db
```

`<qq-id>` is the numeric QQ account ID. Each multi-account profile gets its
own subdirectory; the builder reads one at a time.

macOS (path varies by client version — search inside the container):

```
~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/<qq-id>/...
```

### Key extraction (one-time per QQ client install)

The database is encrypted with SQLCipher. Memexa does **not** bundle a key
extractor — that is provided by sister tools:

- [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key) — Windows NTQQ key dump
- [Mythologyli/qq-nt-db](https://github.com/Mythologyli/qq-nt-db) — alternative path

Run their extraction once while QQ is signed in, then write the key into
`~/.memexa/secrets/qq_db.key`. Memexa's reader uses the URI form
`mode=ro&nolock=1` so it can open the database while QQ is still open.

### Wire-up

```bash
# 1. Tell memexa which QQ account ID to ingest
$EDITOR ~/.memexa/identity.yaml
# Add or set:
#   qq_id: "<your-qq-id>"

# 2. Drop the key from qq-win-db-key into the secrets directory
mkdir -p ~/.memexa/secrets
$EDITOR ~/.memexa/secrets/qq_db.key   # raw hex, single line

# 3. Test the reader can open the DB
python -c "
from memexa.extraction.qq.qq_history_to_batches import probe_db
probe_db()
"

# 4. Run the builder once in --mode dump (NapCat HTTP path is disabled)
python -m memexa.extraction.qq.qq_history_to_batches --mode dump \
    --start-date 2026-05-01 --end-date 2026-05-15

# 5. Run the driver once
python -m memexa.drivers.backfill_v5_qq_driver --once --verbose
```

### Lock contention

QQ holds a write lock on `nt_msg.db` while the desktop client is open. The
builder opens read-only — coexistence is fine, but messages mid-write are
invisible until QQ commits the transaction. If you see "database is locked",
your SQLite build does not honor the URI options; upgrade Python or close
QQ during ingestion.

### Schema notes

| QQ client | Support |
|---|---|
| NT QQ 9.9.x (2026-05 current) | ✅ full |
| NT QQ 9.7–9.8 | ✅ full |
| Legacy QQ (mht export) | ❌ not supported — convert externally |

If the builder errors `unknown schema version <N>`, file an issue with the
client version number.

---

## 2. Alternative: clipboard adapter (zero-risk)

If you don't want to extract keys at all, Memexa ships a clipboard reader
that takes manually forwarded messages:

```bash
# Inside QQ: select messages → right-click → 转发 → 复制
# Then run:
python -m memexa.extraction.qq.qq_clipboard_reader
```

The reader parses QQ's "转发" clipboard format and produces the same v5
envelope batches as the db path. Coverage depends entirely on you copying
messages — there is no continuous capture. Useful as a tier-1 path for
high-value threads (e.g. a course-group announcement) when you want
zero footprint.

---

## 3. Discouraged: NapCat / Lagrange / Shamrock / go-cqhttp adapters

The `memexa/extraction/qq_realtime_watcher.py` and `qq_batch_ingest.py` modules
are kept in the tree for historical reference and will refuse to start unless
you set `MEMEXA_QQ_NAPCAT_FORCE=1`. **Setting that variable on an account you
care about is strongly discouraged.**

Reasons:

- 2025-09-05 NapCat public-OneBot incident: thousands of accounts banned in
  a single weekend ([linux.do summary](https://linux.do/t/topic/934328)).
- Tencent now retroactively flags accounts with a history of NapCat / LLOneBot
  client signatures even after you stop using them ([blog summary](https://blog.ziyibbs.com/archives/103.html)).
- `Lagrange.Core` was archived on 2025-10-12.
- `OpenShamrock` last release was v1.1.1 in 2024-07.
- `go-cqhttp` issue tracker is dominated by "bot frozen after using non-official
  client" reports ([Mrs4s/go-cqhttp#2471](https://github.com/Mrs4s/go-cqhttp/issues/2471)).

If you must use one of these for a research project where the QQ account is
disposable, host the OneBot HTTP socket on `127.0.0.1` only, set a strong
token, and accept the account is a research throw-away.

---

## 4. Cannot use: official QQ Bot Open Platform

As of 2026-01-31, the official `bot.q.qq.com` platform [no longer permits
individual developers to bind bots to QQ groups](https://bot.q.qq.com/wiki/).
You can still do channel messages and bot DMs, which Memexa does not currently
integrate (community PR welcome).

---

## Privacy notes

- `nt_msg.db` contains plaintext message content for every chat. It is not
  at-rest encrypted (only SQLCipher field-level on Windows). Treat it as
  highly sensitive; do not commit, do not back up to public storage.
- The PostgreSQL bank stores extracted *narrative* (an LLM-generated
  third-person summary), not raw message text. Raw text stays in `nt_msg.db`.
- Set strong OS-level disk encryption.

## Roadmap

- ✅ Voice messages via separate audio pipeline (v0.1)
- 🔜 db-only adapter migrated from upstream JARVIS (v0.2) — already battle-hardened on the maintainer's own data
- 🔜 Clipboard adapter migrated from upstream JARVIS (v0.2)
- 🔜 Quoted-message threading (v0.4)
- 🔜 Documented sub-account ingestion workflow with cool-down guidelines (v0.4)
- ❌ TIM client variant — community PR welcome
- ❌ Mobile-only QQ — Android export tooling outside maintainer's scope
- ❌ Official QQ Bot adapter — blocked by Tencent policy (2026-01-31)
