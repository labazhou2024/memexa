# 邮件接入

[English](email.md) · **中文**

`memexa` 通过 IMAP 读邮件。不假设特定 provider; 任何
`imap.example.com:993` 都行 (Gmail, Outlook, 腾讯企业邮, Fastmail,
ProtonMail Bridge, 自建 Dovecot)。

## 接入

```bash
# 1. 把 IMAP 配置加到 ~/.memexa/identity.yaml
$EDITOR ~/.memexa/identity.yaml
# imap:
#   host: imap.example.com
#   port: 993
#   user: alice@example.com
#   password_env: MEMEXA_IMAP_PASSWORD    # env var 名字, 不是字面密码
#   folders:
#     - INBOX
#     - "Sent"
#     - "[Gmail]/All Mail"               # provider 特定例子
#   since_days: 90                       # 只拉这么新的消息

# 2. 设密码 env var
export MEMEXA_IMAP_PASSWORD='<你的-imap-应用专用密码>'

# 3. Smoke 测连接
python -m src.ingestion.v5_email_batch_builder --probe

# 4. 跑 builder
python -m src.ingestion.v5_email_batch_builder

# 5. 跑 driver
python -m src.drivers.backfill_v5_email_driver --once --verbose
```

## 用应用专用密码, 不要用账号密码

主流 provider IMAP 都需要应用专用密码, 因为主密码绑硬件 2FA。在 provider
安全设置里生成; 常见入口:

- Gmail → Account → Security → 2-Step Verification → App passwords
- Outlook → Account → Security → Advanced security options → App passwords
- Fastmail → Settings → Password & Security → App passwords
- 腾讯企业邮 → 设置 → 客户端登录 → 生成
- Proton → Bridge app 生成每客户端密码

**永远不要**把密码字面写到 YAML。`password_env` key 写 env var 名字;
loader 做 `os.environ[password_env]`。

## 摄入什么

| Header / part           | 用来做                                              |
|-------------------------|----------------------------------------------------|
| `From`                  | speaker_role 分类                                  |
| `To` / `Cc`             | audience 集合                                      |
| `Date`                  | `when_start`                                       |
| `Subject`               | 进入 narrative seed                                |
| 文本 body               | extractor 输入 (HTML 剥离 + entity 解码)            |
| 附件名                  | 如果有信号则在 narrative 提                        |
| 附件 body               | 不解析 (v0.x 不在范围)                              |
| Threading header        | `episode_chain_builder` 分组回复                    |

## 批量 vs 增量

Builder 默认增量: 从 `data/cursors/email.json` 读 UID cursor, 拉更新的。

第一次回填 N 年邮箱:

```bash
python -m src.ingestion.v5_email_batch_builder \
    --since-days 3650 --batch-size 100
```

注意 LLM provider 成本 — 繁忙邮箱可能 50000 封。Stage A gatekeeper 拒掉
~60–70% 为 LOW (通知, 收据, 邮件列表噪声), extractor 成本约 envelope
数的 ~20–30%。

## Folder 选择

大多数 provider 的 IMAP folder 名都有 provider 特定 quirks:

- Gmail: 每封邮件也在 `[Gmail]/All Mail`; 想 thread-de-dup 只摄入
  `[Gmail]/All Mail`, 不要 `INBOX` + `Sent`
- Outlook: `Sent Items` (注意空格)
- 腾讯企业邮: 中文 folder 名; IMAP server 返回为 UTF-7-modified 字串。
  Builder 透明解码

不确定时:

```bash
python -m src.ingestion.v5_email_batch_builder --list-folders
```

打印你账号的规范名。

## 隐私须知

- IMAP 凭据在 env / shell history 里。用密码管理器 + 临时 export, 别写
  `.zshrc`
- Extractor LLM 看每一封 Stage A 后存活的邮件 body。如果你用 hosted LLM,
  邮件内容对 provider 可见。这点要紧就本地跑 vLLM / Ollama
- Bcc 收件人在某些 provider 通过 `Resent-Bcc` header 泄露 — builder
  剥掉。用 `--dry-run --verbose` 在已知 Bcc 消息上验

## 路线图

- ✅ IMAP plain (v0.1)
- 🔜 IMAP OAuth2 (Gmail / Outlook 个人) — v0.3 候选
- 🔜 Microsoft Graph API 摄入路径 (Outlook 365 企业)
- ❌ POP3 — 不在范围; 用 IMAP
- ❌ MAPI (Outlook 桌面) — 不在范围; export 成 .eml + 摄入
