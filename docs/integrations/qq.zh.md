# QQ 接入

[English](qq.md) · **中文**

> ## ⚠️ 状态: 实验性 / 高风险 (2026-05-15 更新)
>
> **本项目 maintainer 的 QQ 账号 2026-05-14 被腾讯封号**, 在开发本项目的过程中。
> 事后分析指向 [2025-09-05 NapCat 公网 OneBot 攻击事件](https://www.xcnahida.cn/?p=b8AROpEJ),
> 该事件之后腾讯开始对**所有曾使用过 NapCat / LiteLoaderQQNT 的 QQ**
> 进行指纹匹配并批量封号。
>
> **Memex 默认不再附带 NapCat / OneBot 适配器。**
> 如果你强行启用, 请接受 QQ 随时可能被封, 通常无预警, 有时是停用工具几周/几个月后。
> 这一类封号腾讯客服**不会**因申诉解封。
>
> 完整事件时间线见 [`docs/lessons_learned/`](../lessons_learned/) 和
> [JARVIS 上游调研笔记](https://github.com/labazhou2024/memex)。

本页只覆盖 **db-only** 路径 (截至 2026-05 唯一零封号案例) + 剪贴板兜底。

---

## 1. 推荐: db-only 只读路径

直接读 QQ 本地的 SQLite 数据库。**不**发任何协议包, **不**启动任何第三方客户端。
对腾讯端的可见性等同于"普通聊天记录备份工具"。

### 权衡

- ✅ 无公开封号案例 (`QQBackup/qq-win-db-key` 1k stars, 一年 issue 跟踪零封号 report)
- ✅ 历史全量覆盖
- ❌ 需要 QQ 登录一次以 hook SQLCipher key (一次性, 之后可关 QQ)
- ❌ 不支持实时: 只能拿到 QQ 上次同步到本地的内容
- ❌ NT QQ ≥ 9.9.x 在 2024-12 改了 cipher 到 SHA-512, 老教程引用 SHA-1 / SHA-256 不再可用

### 定位数据库

Windows 规范路径:

```
%USERPROFILE%\Documents\Tencent Files\<qq-id>\nt_qq\nt_db\nt_msg.db
```

`<qq-id>` 是数字 QQ 账号 ID。多账号 profile 每个有独立子目录, builder 一次读一个。

macOS (路径因客户端版本而异 — 在 container 内搜):

```
~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/<qq-id>/...
```

### Key 提取 (每个 QQ 客户端装机一次)

数据库用 SQLCipher 加密。Memex **不**附带 key 提取工具 — 由姊妹工具提供:

- [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key) — Windows NTQQ key dump
- [Mythologyli/qq-nt-db](https://github.com/Mythologyli/qq-nt-db) — 替代实现

QQ 登录中跑一次提取, 把 key 写到 `~/.memex/secrets/qq_db.key`。Memex reader
用 URI 形式 `mode=ro&nolock=1` 打开数据库, QQ 仍开着也不冲突。

### 接入

```bash
# 1. 告诉 memex 摄入哪个 QQ 账号
$EDITOR ~/.memex/identity.yaml
# 加或设:
#   qq_id: "<你的 QQ ID>"

# 2. 把 qq-win-db-key 提取的 key 写进 secrets
mkdir -p ~/.memex/secrets
$EDITOR ~/.memex/secrets/qq_db.key   # 原始 hex, 单行

# 3. 测 reader 能开 DB
python -c "
from src.extraction.qq.qq_history_to_batches import probe_db
probe_db()
"

# 4. 跑一次 builder, 用 --mode dump (NapCat HTTP 路径已禁)
python -m src.extraction.qq.qq_history_to_batches --mode dump \
    --start-date 2026-05-01 --end-date 2026-05-15

# 5. 跑一次 driver
python -m src.drivers.backfill_v5_qq_driver --once --verbose
```

### 锁竞争

QQ 桌面客户端开着时持 `nt_msg.db` 写锁。Builder 以只读模式打开 — 共存 OK,
但 QQ 写到一半的消息直到 QQ 提交事务后才可见。若仍报 "database is locked",
你的 SQLite build 不尊重 URI 选项; 升级 Python 或摄入时关 QQ。

### Schema 备注

| QQ 客户端 | 支持 |
|---|---|
| NT QQ 9.9.x (2026-05 当前) | ✅ 全支持 |
| NT QQ 9.7–9.8 | ✅ 全支持 |
| Legacy QQ (mht export) | ❌ 不支持, 自行转换 |

Builder 报 `unknown schema version <N>` 时带版本号提 issue。

---

## 2. 备选: 剪贴板适配器 (零风险)

如果连 key 提取都不想做, Memex 自带剪贴板 reader 接受用户主动转发的消息:

```bash
# QQ 里: 选消息 → 右键 → 转发 → 复制
# 然后跑:
python -m src.extraction.qq.qq_clipboard_reader
```

Reader 解析 QQ "转发" 剪贴板格式, 生成跟 db 路径相同的 v5 envelope batches。
覆盖率完全靠你手动复制 — 没有连续抓取。适合**高价值线程** (课程群通知)
做零痕迹接入。

---

## 3. 不推荐: NapCat / Lagrange / Shamrock / go-cqhttp 适配器

`src/extraction/qq_realtime_watcher.py` 和 `qq_batch_ingest.py` 保留在树中作为
历史参考, 默认拒绝启动除非你设 `MEMEX_QQ_NAPCAT_FORCE=1`。**在你在乎的账号上
开这个 flag 是强烈不推荐的。**

原因:

- 2025-09-05 NapCat 公网 OneBot 事件: 一个周末数千账号被批量封 ([linux.do 综述](https://linux.do/t/topic/934328))
- 腾讯现在会**追溯**标记任何曾用过 NapCat / LLOneBot 客户端指纹的账号, 即使你已停用 ([紫血小站综述](https://blog.ziyibbs.com/archives/103.html))
- `Lagrange.Core` 2025-10-12 archived
- `OpenShamrock` 最后 release 是 2024-07 v1.1.1
- `go-cqhttp` issue tracker 充斥 "因使用非官方客户端被冻结" 报告 ([Mrs4s/go-cqhttp#2471](https://github.com/Mrs4s/go-cqhttp/issues/2471))

如果你必须用这条路 (一次性研究项目, QQ 账号可弃), OneBot HTTP socket 只绑
`127.0.0.1`, 设强 token, 把账号当 throw-away。

---

## 4. 不可用: 官方 QQ Bot 开放平台

[官方 `bot.q.qq.com` 自 2026-01-31 起个人开发者不再支持群接入](https://bot.q.qq.com/wiki/)。
频道消息和 bot 私聊仍可做, Memex 暂未集成 (欢迎社区 PR)。

---

## 隐私须知

- `nt_msg.db` 含所有聊天的明文消息内容。它**不**是 at-rest 加密 (仅 SQLCipher
  字段级)。视为极度敏感; 不要 commit, 不要备份到公共存储
- PostgreSQL bank 存的是抽取出来的 *narrative* (LLM 生成的第三人称摘要),
  不是原始消息文本。原始留 `nt_msg.db`
- 设强 OS 级磁盘加密 key

## 路线图

- ✅ 文本消息走 db-only 路径 (v0.1)
- ✅ 语音消息走单独 audio pipeline (v0.1)
- ✅ 剪贴板适配器 (v0.1)
- 🔜 引用消息 threading (v0.4)
- 🔜 完整 sub-account 摄入工作流 + 养号冷却指南 (v0.4)
- ❌ TIM 客户端变体 — 欢迎社区 PR
- ❌ 仅移动端 QQ — Android export 工具超 maintainer 范围
- ❌ 官方 QQ Bot 适配器 — 被腾讯政策卡住 (2026-01-31)
