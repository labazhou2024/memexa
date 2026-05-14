# QQ 接入

[English](qq.md) · **中文**

QQ 用磁盘上的 SQLite 数据库 (`nt_msg.db`), builder 直接读。和微信不同
没有推荐 export 工具 — 你指向 live 文件, builder 做增量读。

## 定位数据库

Windows 规范路径:

```
%USERPROFILE%\Documents\Tencent Files\<qq-id>\nt_qq\nt_db\nt_msg.db
```

`<qq-id>` 是数字 QQ 账号 ID。如果多账号, 每个账号有自己的子目录;
builder 一次读一个。

macOS:

```
~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/<qq-id>/...
```

具体路径因 QQ 客户端版本而异 — 在
`~/Library/Containers/com.tencent.qq/` 里搜 `nt_msg.db`。

## 接入

```bash
# 1. 告诉 memex 摄入哪个 QQ 账号
$EDITOR ~/.memex/identity.yaml
# 加或设:
#   qq_id: "<你的 QQ ID>"

# 2. 测 reader 能开 DB
python -c "
from src.extraction.qq.qq_history_to_batches import probe_db
probe_db()
"

# 3. 跑一次 builder
python -m src.extraction.qq.qq_history_to_batches

# 4. 跑一次 driver
python -m src.drivers.backfill_v5_qq_driver --once --verbose
```

## 锁竞争

QQ 桌面客户端开着时, 它对 `nt_msg.db` 持写锁。Builder 以只读模式打开
数据库 (`mode=ro` + `nolock=1` URI), 所以同时读是安全的 — 但注意:

- 桌面客户端正在写到一半的消息, reader 看不到, 直到 QQ 提交事务
- 如果还是看到 "database is locked" 错误, 你的 SQLite build 不尊重 URI
  选项。升级 Python 或摄入时关 QQ。

## Schema 备注

QQ nt_msg schema 跨客户端版本变过几次。Builder 支持:

- NT QQ 9.9.x (2026-05 当前版本) — 全支持
- NT QQ 9.7–9.8 — 全支持
- 老 "legacy QQ" (mht export) — 不支持; 用一次性转换脚本

如果 builder 报 `unknown schema version <N>`, 带版本号提 issue; 加新
schema variant 是半天的补丁。

## 群聊

QQ 群聊消息含发送者的 `wxid_hash` 等价物 (hash 过的数字 QQ ID)。Builder
跟 WeChat 一样通过 `~/.memex/aliases.yaml` 规范化。把你自己 QQ ID 放
`self_aliases` 拿到 speaker_role=`self` 卡。

```yaml
self_aliases:
  - "<你的显示名>"
  - "<你的 QQ ID>"      # 数字, 当字符串处理
self_roles:
  - student
timezone: "Asia/Shanghai"
```

## 隐私须知

- `nt_msg.db` 文件含所有聊天的明文消息内容。它**不**是 at-rest 加密的。
  Builder 把它当极度敏感; 不要 commit, 不要备份到公共存储
- PostgreSQL bank 存的是抽取出来的 *narrative* (LLM 生成的第三人称摘要),
  不是原始消息文本。原始文本留在 `nt_msg.db`。设强 OS 级磁盘加密 key

## 路线图

- ✅ 文本消息 (v0.1)
- ✅ 语音消息走单独 audio pipeline (v0.1)
- 🔜 引用消息 threading (v0.4)
- ❌ TIM 客户端变体 — 欢迎社区 PR; maintainer 没装
- ❌ 仅移动端 QQ — Android export 工具超出 maintainer 范围
