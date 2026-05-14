# Email integration

**English** · [中文](email.zh.md)

`memex` reads email via IMAP. It does not assume a particular provider;
anything with `imap.example.com:993` works (Gmail, Outlook, Tencent
Exmail, Fastmail, ProtonMail Bridge, self-hosted Dovecot).

## Wire-up

```bash
# 1. Add IMAP config to ~/.memex/identity.yaml
$EDITOR ~/.memex/identity.yaml
# imap:
#   host: imap.example.com
#   port: 993
#   user: alice@example.com
#   password_env: MEMEX_IMAP_PASSWORD    # name of an env var, NOT the literal password
#   folders:
#     - INBOX
#     - "Sent"
#     - "[Gmail]/All Mail"               # provider-specific examples
#   since_days: 90                       # only pull messages newer than this

# 2. Set the password env var
export MEMEX_IMAP_PASSWORD='<your-imap-app-specific-password>'

# 3. Smoke test the connection
python -m src.ingestion.v5_email_batch_builder --probe

# 4. Run the builder
python -m src.ingestion.v5_email_batch_builder

# 5. Run the driver
python -m src.drivers.backfill_v5_email_driver --once --verbose
```

## Use app-specific passwords, not your account password

Every major provider requires an app-specific password for IMAP because
their primary password is hardware-2FA-locked. Generate one in your
provider's security settings; common entry points:

- Gmail → Account → Security → 2-Step Verification → App passwords
- Outlook → Account → Security → Advanced security options → App passwords
- Fastmail → Settings → Password & Security → App passwords
- Tencent Exmail → Settings → Client login → Generate
- Proton → Bridge app generates per-client passwords

**Never** put the password literally in YAML. The `password_env` key
names an env var; the loader does `os.environ[password_env]`.

## What gets ingested

| Header / part           | Used for                                           |
|-------------------------|----------------------------------------------------|
| `From`                  | speaker_role classification                        |
| `To` / `Cc`             | audience set                                       |
| `Date`                  | `when_start`                                       |
| `Subject`               | included in narrative seed                         |
| Text body               | extractor input (HTML stripped + entity-encoded)   |
| Attachment names        | mentioned in narrative if signal                   |
| Attachment bodies       | not parsed (out of scope for v0.x)                 |
| Threading headers       | `episode_chain_builder` groups replies             |

## Bulk vs incremental

The builder defaults to incremental: it reads the mailbox UID cursor
from `data/cursors/email.json` and pulls everything newer.

For first-time backfill of an N-year mailbox:

```bash
python -m src.ingestion.v5_email_batch_builder \
    --since-days 3650 --batch-size 100
```

Watch the LLM provider cost — a busy mailbox can be 50 000 emails.
Stage A gatekeeper rejects ~60–70 % of those as LOW (notifications,
receipts, mailing-list noise), so extractor cost is ~20–30 % of envelope
count.

## Folder selection

Most providers use IMAP folder names with provider-specific quirks:

- Gmail: every mail also appears in `[Gmail]/All Mail`; if you want
  thread-de-dup, only ingest `[Gmail]/All Mail`, not `INBOX` + `Sent`.
- Outlook: `Sent Items` (note the space).
- Tencent Exmail: Chinese folder names; the IMAP server returns them as
  UTF-7-modified strings. The builder transparently decodes.

When in doubt:

```bash
python -m src.ingestion.v5_email_batch_builder --list-folders
```

prints the canonical names for your account.

## Privacy notes

- IMAP credentials sit in env / shell history. Use a passphrase manager
  + temporary export, not `.zshrc`.
- The extractor LLM sees every email body that survives Stage A. If you
  are on a hosted LLM, your email contents are visible to the provider.
  Run vLLM / Ollama locally if this matters.
- Bcc recipients leak via the `Resent-Bcc` header on some providers —
  the builder strips it. Verify with `--dry-run --verbose` on a known
  Bcc'd message.

## Roadmap

- ✅ IMAP plain (v0.1)
- 🔜 IMAP OAuth2 (Gmail / Outlook personal) — v0.3 candidate
- 🔜 Microsoft Graph API ingest path (Outlook 365 corporate)
- ❌ POP3 — out of scope; use IMAP
- ❌ MAPI (Outlook desktop) — out of scope; export to .eml + ingest
