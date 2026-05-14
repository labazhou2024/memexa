"""Pass-2 main extraction prompt for L0 v5 (schema v2 cards) — SOURCE-AWARE.

Spec: docs/l0_v5/MASTER_PLAN.md §4.3

2026-05-12 v2: source-aware extraction. Branches on `source` field to use
the right prompt + sender formatting for wechat / qq / email / browser /
claude_code. Each source has different real-world data shape and demands
different anaphora rules / type allowlists / junk filters.

Bug fixed: previous version dropped `sender_name` entirely (only emitted
`wxid_hash + alias`). The actual readable name (e.g. "Alice 18级 CS")
is the model's primary signal for entity resolution. Now emitted always.

Backward compat: `PASS2_SYSTEM_PROMPT` is kept as a module-level constant
matching the chat-style (wechat/qq) prompt, so any caller that imported
the symbol still works.
"""
from __future__ import annotations


import base64
import hashlib
import json
import quopri
import re
from typing import Any, Dict, List, Optional, Tuple

CANONICAL_SOURCES = ("wechat", "qq", "email", "browser", "claude_code", "audio")


# ──────────────────────────────────────────────────────────────────────
#  Source normalization (heterogeneous field names across pipeline)
# ──────────────────────────────────────────────────────────────────────
def normalize_source(prompt_data: Dict[str, Any]) -> str:
    """Resolve the canonical source from a prompt.json dict.

    Field precedence: explicit `source` -> `source_kind` -> infer from
    `chat_room` prefix -> default 'wechat'.
    """
    explicit = prompt_data.get("source") or prompt_data.get("source_kind")
    if explicit:
        # Normalize a couple of aliases just in case.
        if explicit == "cc":
            return "claude_code"
        return explicit
    room = (prompt_data.get("chat_room") or "")
    if room.startswith("email:"):
        return "email"
    if room.startswith("browser:"):
        return "browser"
    return "wechat"


# ──────────────────────────────────────────────────────────────────────
#  Email-specific helpers
# ──────────────────────────────────────────────────────────────────────
_RFC2047_RE = re.compile(r"=\?([^?]+)\?([BbQq])\?([^?]*)\?=")


def decode_rfc2047(s: str) -> str:
    """Decode an RFC 2047 encoded-word, e.g. =?UTF-8?B?5pWZ5Yqh57O757uf?=
    Returns plain text. If already plain (no encoded-word), returns as-is.
    Multiple encoded-words in a row are concatenated.
    """
    if not s or "=?" not in s:
        return s

    def _decode_one(m: re.Match) -> str:
        charset, encoding, payload = m.group(1), m.group(2).upper(), m.group(3)
        try:
            if encoding == "B":
                raw = base64.b64decode(payload + "==")  # tolerate padding loss
            else:  # Q
                raw = quopri.decodestring(payload.replace("_", " "))
            return raw.decode(charset, errors="replace")
        except Exception:
            return m.group(0)  # keep original if decode fails

    return _RFC2047_RE.sub(_decode_one, s)


_EMAIL_JUNK_PATTERNS = (
    r"🔥|🎁|🎉|限时|福利|空投|USDT|奖励砸中|点击领取|免费领取",
    r"unsubscribe|取消订阅|退订",
)
_EMAIL_JUNK_RE = re.compile("|".join(_EMAIL_JUNK_PATTERNS), re.IGNORECASE)


def is_email_likely_junk(subject: str, body: str) -> bool:
    """Cheap heuristic: marketing / promo / phishing email."""
    blob = (subject or "") + " " + (body or "")[:500]
    return bool(_EMAIL_JUNK_RE.search(blob))


# ──────────────────────────────────────────────────────────────────────
#  Sender-list rendering (per source)
# ──────────────────────────────────────────────────────────────────────
def _display_name(s: Dict[str, Any], source: str) -> str:
    """Pick the best human-readable name from a sender dict, per source.

    Precedence: sender_name > alias_in_manifest_or_None > "(unnamed)".
    Always shows alias as extra if both name and alias exist and differ.
    """
    name = str(s.get("sender_name") or "").strip()
    if source == "email":
        name = decode_rfc2047(name)
    alias = str(s.get("alias_in_manifest_or_None") or "").strip()
    primary = name or alias or "(unnamed)"
    extras: List[str] = []
    if alias and name and alias != name:
        extras.append(f"alias={alias!r}")
    if s.get("is_self"):
        extras.append("is_self=true")
    if s.get("qq_id"):
        extras.append(f"qq_id={s['qq_id']}")
    if extras:
        return f"{primary} [{', '.join(extras)}]"
    return primary


def _render_sender_list(sender_list: List[Dict[str, Any]], source: str) -> List[str]:
    """Render the sender_list section. Always include sender_name."""
    lines = ["# sender_list"]
    if not sender_list:
        lines.append("  (empty)")
        return lines
    for s in sender_list:
        wh = s.get("wxid_hash", "??")
        disp = _display_name(s, source)
        lines.append(f"  - wxid_hash={wh}  name={disp}")
    return lines


# ──────────────────────────────────────────────────────────────────────
#  Message rendering (per source)
# ──────────────────────────────────────────────────────────────────────
_MAX_CONTENT_CHARS = 800


def _truncate(content: str, limit: int = _MAX_CONTENT_CHARS) -> str:
    content = (content or "").strip()
    if len(content) > limit:
        return content[: limit - 3] + "..."
    return content


def _render_messages_chat(messages: List[Dict[str, Any]]) -> List[str]:
    """wechat / qq style: time-ordered multi-speaker chat."""
    lines = ["# messages (按时间序; 多方对话)"]
    for m in messages:
        ts = m.get("ts", "??")
        wh = m.get("wxid_hash", "??")
        lines.append(f"[{ts}] wxid_hash={wh}: {_truncate(m.get('content'))}")
    return lines


def _render_messages_email(messages: List[Dict[str, Any]]) -> List[str]:
    """email: each message is one email. Content has 邮件主题/收件人/正文 markers."""
    lines = ["# emails (按时间序; 每条 = 一封邮件)"]
    for i, m in enumerate(messages):
        ts = m.get("ts", "??")
        wh = m.get("wxid_hash", "??")
        body = _truncate(m.get("content"))
        lines.append(f"--- email #{i + 1} ts={ts} from_wxid_hash={wh} ---")
        # body already has 邮件主题/收件人/正文 prefix lines; emit verbatim
        lines.append(body)
    return lines


_URL_RE = re.compile(r"https?://\S+|file://\S+")


def _summarize_browser_msg(content: str) -> str:
    """Strip URL-encoded gibberish; keep the user-readable query + bare host."""
    content = (content or "").strip()
    # Pull out the leading 搜索: <q> or 访问 line as-is
    head, _, rest = content.partition("\n")
    head = head.strip()
    # Find first URL and shorten to host+path (drop query string)
    url_match = _URL_RE.search(content)
    url_short = ""
    if url_match:
        u = url_match.group(0)
        # take up to '?' or first 80 chars
        url_short = u.split("?", 1)[0]
        if len(url_short) > 100:
            url_short = url_short[:97] + "..."
    extras = []
    # Pull 停留/来源/上游 if present. Use [ \t]* (not \s*) so newlines don't get crossed.
    for key in ("停留", "搜索词", "来源", "上游"):
        m = re.search(rf"{key}:[ \t]*([^\n]*)", rest)
        if m and m.group(1).strip():
            extras.append(f"{key}={m.group(1).strip()[:60]}")
    parts = [head]
    if url_short:
        parts.append(f"url={url_short}")
    if extras:
        parts.extend(extras)
    return " | ".join(parts)


def _render_messages_browser(messages: List[Dict[str, Any]]) -> List[str]:
    """browser: each msg is one search/visit; URL-encoded queries decoded."""
    lines = ["# browsing events (按时间序; 全是 self)"]
    for i, m in enumerate(messages):
        ts = m.get("ts", "??")
        lines.append(f"[{i + 1}] ts={ts}  {_summarize_browser_msg(m.get('content', ''))}")
    return lines


def _classify_cc_message(content: str) -> Tuple[str, str]:
    """Return (kind, payload) for a Claude Code message.

    Kinds: tool_use_bash / tool_use_read / tool_use_other / tool_result_ok /
           tool_result_err / claude_reasoning / user_input
    """
    c = (content or "").strip()
    if c.startswith("[Claude tool_use:Bash]"):
        return "tool_use_bash", c[len("[Claude tool_use:Bash]"):].strip()
    if c.startswith("[Claude tool_use:Read]"):
        return "tool_use_read", c[len("[Claude tool_use:Read]"):].strip()
    if c.startswith("[Claude tool_use:"):
        m = re.match(r"\[Claude tool_use:(\w+)\]\s*(.*)", c, re.S)
        if m:
            return f"tool_use_{m.group(1).lower()}", m.group(2).strip()
    if c.startswith("[Claude]"):
        return "claude_reasoning", c[len("[Claude]"):].strip()
    if c.startswith("[tool result OK]"):
        return "tool_result_ok", ""
    if c.startswith("[tool result ERR"):
        return "tool_result_err", c
    return "user_input", c


def _fmt_audio_ts(seconds: float) -> str:
    """Format absolute seconds offset into HH:MM:SS for audio anchors."""
    try:
        s = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "??"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _render_messages_audio(messages: List[Dict[str, Any]],
                            sender_list: List[Dict[str, Any]]) -> List[str]:
    """audio: spoken-language utterances from ASR + speaker diarization.

    Each message: {ts, voice_canonical_id, sender_name, content,
                   audio_ts_start, audio_ts_end, clip_uri,
                   diariz_conf, asr_avg_logprob}.

    Render with absolute clock-time (ts), session offset (HH:MM:SS),
    voice id, and ASR/diariz confidence — so the LLM knows which
    utterance is dialogue vs ASR-noise and can ground quotes.
    """
    lines = ["# spoken utterances (按时间序; 多 speaker 对话, ASR 转写)"]
    lines.append("# 注: [offset] = session 内偏移; ts = 绝对时间; diariz/asr = 置信度")
    for m in messages:
        ts = m.get("ts", "??")
        vcid = m.get("voice_canonical_id", "??")
        sname = m.get("sender_name") or "(unknown)"
        off_s = m.get("audio_ts_start", 0.0)
        off_e = m.get("audio_ts_end", 0.0)
        dc = m.get("diariz_conf", 0.0)
        ac = m.get("asr_avg_logprob", 0.0)
        try:
            dc_str = f"{float(dc):.2f}"
        except (TypeError, ValueError):
            dc_str = "?"
        try:
            ac_str = f"{float(ac):.2f}"
        except (TypeError, ValueError):
            ac_str = "?"
        content = _truncate(m.get("content"))
        lines.append(
            f"[{_fmt_audio_ts(off_s)}-{_fmt_audio_ts(off_e)}] ts={ts} "
            f"voice={vcid} name={sname} diariz={dc_str} asr={ac_str}: {content}"
        )
    return lines


def _render_messages_cc(messages: List[Dict[str, Any]], self_hash: Optional[str]) -> List[str]:
    """claude_code: User-vs-Claude dev session. Most user msgs are tool results."""
    lines = ["# dev session messages (按时间序; 用户 vs Claude AI 工具)"]
    for i, m in enumerate(messages):
        ts = m.get("ts", "??")
        wh = m.get("wxid_hash", "??")
        kind, payload = _classify_cc_message(m.get("content", ""))
        speaker = "用户" if wh == self_hash else "Claude"
        if kind == "tool_result_ok":
            lines.append(f"[{i + 1}] ts={ts}  {speaker}: <tool_result OK>")
        elif kind == "tool_result_err":
            lines.append(f"[{i + 1}] ts={ts}  {speaker}: {_truncate(payload, 200)}")
        else:
            tag = kind.upper()
            lines.append(f"[{i + 1}] ts={ts}  {speaker} {tag}: {_truncate(payload, 600)}")
    return lines


# ──────────────────────────────────────────────────────────────────────
#  System prompts (per source)
# ──────────────────────────────────────────────────────────────────────
_BASE_RULES = """你是 MemoryCard 抽取员。

任务: 从一段 batch 中提炼 0–N 张 schema v2 MemoryCard. 卡片进记忆图谱承载召回。

【全 source 共用 绝对约束 — 违反即无效卡, 整张丢弃】

1. **narrative 30–1200 字, 包含 5W1H** (WHO/WHEN/WHERE/WHAT/WHY[可不知道]/HOW[可省])。
2. **所有时间表达式必须绝对化** — "上周/这周三/过年那阵子/上学期" 全部用 batch_window/calendar 锚定到 ISO; 完全锚不定 → time_resolutions[i].confidence="unresolved"。
3. **manifest 切片是 ground truth** — 切片有的人/物/组织, surface_form 命中即用切片的 canonical_id; 拼音首字母 (manifest.persons[*].pinyin_initials) 严格命中 → 立即合并。
4. **types 从 CANONICAL 选** — announcement / commitment / question / decision / correction / opinion / report / share / interaction / state. 一张卡 ≤4 个 type. 不在 → types=[state] + open_type_hint=自然描述。
5. **salience [0,1]** — 0.3 一般, 0.5 中等, 0.7+ 重要 (含承诺/购买/结果/约定/重大决定). reason ≤60 字。
6. **evidence_quotes ≤5 条 ≤200 字** 直接抄原文; 每张卡至少 1 条。
7. **不知道 → 留空, 绝不编造** — canonical_id=null, resolved_start=null, confidence="unresolved"。
8. **JSON 输出, 最后一行必须 END_OF_OUTPUT** (没有 worker 会 retry)。

【消解优先级 (从高到低)】
manifest_slice > sender_list (本 batch 发送者+sender_name+alias) > 拼音首字母+共现 > anaphora window (5 句) > unresolved

【输出 schema】

```json
{
  "cards": [
    {
      "narrative": "...",
      "evidence_quotes": ["..."],
      "when_start": "ISO",
      "when_end": "ISO",
      "where_chat_room": "原 room",
      "where_chat_room_hash": "32hex",
      "room_tier": 1,
      "entities": [
        {
          "canonical_name": "...",
          "canonical_id": "person_xxx | null",
          "role_in_card": "subject|object|mentioned|audience",
          "surface_form": "原文",
          "sender_wxid_hash": "16hex | null",
          "resolution_confidence": "certain|inferred|ambiguous|unresolved",
          "resolution_evidence": "manifest_slice/pinyin_match/context_5_lookback/..."
        }
      ],
      "speaker_role": "self|relay|third_party|mixed|document",
      "types": ["..."],
      "salience": 0.55,
      "salience_reason": "≤60 chars",
      "attestation_tier": "paired_v2",
      "batch_id": "<from input>",
      "extraction_prompt_sha": "<auto>",
      "source": "<source>",
      "schema_v": 2,
      "open_type_hint": null,
      "supersedes": [],
      "answers": null,
      "related_episode": null,
      "identity_assertions": [...],
      "time_resolutions": [...],
      "relation_assertions": [...],
      "unresolved_references": ["他 (msg #3)"]
    }
  ]
}
END_OF_OUTPUT
```
"""


_SECTION_CHAT = """
【本 batch 是 多人聊天 (wechat/qq)】

特别规则:
- **sender_name 是主要识别信号** — 形如 "Alice 18级 CS" 或 "Bob （TA". 优先用 sender_name 锚定 sender (不要只看 wxid_hash 那串 hex).
- **"我"** → 当前消息的 sender (查 sender_list 找 is_self=true 那位; 若群里 is_self 不在 sender_list, "我" 指消息发送者本人).
- **"你"** → 上下文最近被 @ 或被回复的人.
- **"他/她/它/那个/那位"** → 上下文 5 句内最近被命名的实体.
- **拼音首字母** (如 wjc, xsh, lxr) → 严格匹配 manifest_slice.persons[*].pinyin_initials; 唯一命中即合并; 多人共享同首字母 → ambiguous + identity_assertions 记录.
- **群名 / sender 后缀** 是身份提示 — 形如 "<姓名> <届号 + 学校 / 机构>" 模式时，把后缀写入 entities[*].attributes.role.

【qq 特殊】
- sender_name 通常为 null, 用 alias_in_manifest_or_None 替代.
- qq_id (数字) 可作为 canonical_id 锚 (e.g. qq_xxxxxxxxxx → person_*).
- "临时会话" alias 表示与陌生人 / 非好友的 1v1 — 不算 friend.
"""

_SECTION_EMAIL = """
【本 batch 是 邮件 (email)】

特别规则:
- **邮件不是对话** — 没有"我/你/他"指代消解; sender 是发件人 (From), recipient 是收件人 (To).
- **sender_name 已经 RFC2047 解码** — 若仍像 "=?UTF-8?B?...?=" 形式视作原文记入 surface_form, confidence=ambiguous.
- **自我识别按邮箱地址** — 用户主邮箱（见 identity.yaml `primary_email`）出现在 From → speaker_role="self"; 仅出现在 To → speaker_role="document" (用户是被动接收者).
- **message content 结构是**: `邮件主题: <subj>\\n收件人: <to>\\n正文: <body>` — 抽 subj 进 narrative, body 进 evidence_quotes.
- **垃圾邮件 / 营销邮件 直接跳过** (输出空 cards). 已通过 `is_email_likely_junk()` 做初筛; 此处只需对漏网之鱼做最后兜底.
- **真值邮件抽取**: 学校 / 项目 / 工作 通知类 → announcement; 自己发出的重要邮件 (申请 / 确认 / 请教导师) → commitment / question. salience 按内容显著度赋值 (具体加权表见 [api_roadmap.md](../../../docs/api_roadmap.md)).
- **types 优先**: announcement / share / decision / state. 严禁 "commitment" 表示"邮件承诺" (单向投递不是用户行为). 严禁 "interaction" (这不是双向对话).
- 单条空正文 + 无关键决定 → 输出空 cards.
"""

_SECTION_BROWSER = """
【本 batch 是 浏览记录 (browser)】

特别规则:
- **浏览不是对话** — sender 永远是 self (alias=用户); NO "你/他" 指代消解.
- **content 两种格式**:
  1. 搜索: `搜索: <query>\\nURL: <搜索引擎-URL>`. 抽 `query` 进 narrative, 不抽 URL.
  2. 访问: `访问 \\nURL: <full-url>\\n搜索词: ...\\n停留: <s>秒\\n来源: ...\\n上游: ...`. 抽 URL 的 host+path (去 query string), 停留秒数 = 关注度.
- **URL-encoded gibberish (%E6%9F...) 不进 narrative** — 渲染层已解码, 直接用 query 文本.
- **多条同主题串成 1 张 card** — 连续搜同一研究方向 / 同一人 / 同一商品 → 1 张主题卡, 不要每条 URL 一张.
- **batch_window 跨度大** (可达数周) — narrative 必须明示"X 到 Y 时段持续关注 Z 主题".
- **types 限定**: state / share / interaction. **严禁** commitment / decision / question / announcement / opinion.
- **人名搜索 → identity_assertions 候选**: 用户搜某个具体人名 → 加 identity_assertions[type=new_entity_candidate, surface_form=该名, evidence_quote="搜索: ..."]; manifest_slice 不命中 → confidence=ambiguous, 留给 Stage 3 仲裁.
- **salience**: 大致按"用户对该主题的关注密度"赋值——单条短停留 → 低, 多条同主题串 / 长停留 / 涉及用户实际项目 → 高. 具体加权表见 [api_roadmap.md](../../../docs/api_roadmap.md).
- 单条无关查询 → 输出空 cards.
"""

_SECTION_AUDIO = """
【本 batch 是 现场录音 (audio)】

特别规则:
- **来源**: 录音笔/手机连续录音, 经 Silero-VAD 砍静音 + Whisper-large-v3 转写 + 声纹聚类 (ECAPA-TDNN). 每条 utterance 带:
  - `voice_canonical_id`: 形如 `voice_Alice` (已 enroll) / `voice_unknown_a3f2` (未 enroll 暂用 hash).
  - `audio_ts_start/end`: session 内偏移秒 (整段录音从 0 算).
  - `diariz_conf`: 声纹聚类置信度 [0,1]; <0.6 表示 speaker 归属不可信.
  - `asr_avg_logprob`: Whisper 转写平均 logprob; 越接近 0 越可信, < -1.0 表示 ASR 大概率有错字.

- **已知 2-party 会话模式** (当 prompt 含 `known_speakers` 字段时):
  - 本会话**确定**只有列出的 N 个参与者 (通常 N=2: 1 个 is_self + 1 个对方). 其他 voice_unknown_* 均为同 N 人之一在不同 ASR/diariz 噪声段下的别名, **必须**收敛到 known_speakers.
  - 不要再用 voice_unknown_* 作 canonical_name; 用 known_speakers 中的 display_name 与 canonical_id.
  - **归属规则**: 优先用第一人称 / 第二人称 / 话题所有权 / 明确呼名等显式语言信号。多个信号冲突时, **采保守归属**: 标 speaker_role=`mixed` + surface_form="未明朗 (双方之一)", canonical_id 留空。完整 5 优先级表 + tie-breaker 规则 见 [api_roadmap.md](../../../docs/api_roadmap.md).
  - speaker_role:
    - `self`: 整张 card 主轴是 is_self 的发言/承诺/观点
    - `third_party`: 整张 card 主轴是对方的发言/转述
    - `mixed`: 双方往复对话, 无法把核心点归属单一方
  - **每条 evidence_quote** 在时间锚后可补 speaker 标签 (例: `[01:23:45-01:23:58] <Alice> 我下周三去见 Bob`). 不可判定时不加标签.
  - **entities** 数组中 known_speakers 必须各自占一条记录, role_in_card 视情况选 `subject` / `audience` / `mentioned`, canonical_id 用 known_speakers.canonical_id.
  - 即使 diariz_conf 高的 voice_unknown_X 也要被收敛 (高 conf 不代表 voice id 正确, 仅代表聚类内部一致).

- **ASR 错字是头号风险 — 抽取必须保守**:
  - narrative 中 **关键事实** (人名 / 地点 / 数字 / 决定) 必须能在 evidence_quotes 字面找到; 找不到 → 不抽这条事实.
  - 同音字明显错 (manifest 中已有近音 canonical) → 用 manifest canonical_name, 但在 surface_form 记原音 + identity_assertions.resolution_evidence="asr_homophone".
  - asr_avg_logprob < -1.0 的 utterance → 不用作 evidence_quote 唯一来源, 必须有第二条佐证.

- **speaker_canonical_id 双向映射**:
  - 已 enroll 的 voice_*: 直接当 person canonical_id (manifest 把 voice 当作 wxid 之外的另一类 surface form).
  - voice_unknown_*: role_in_card="mentioned", canonical_id=null, surface_form=voice_canonical_id; 不要瞎认.
  - diariz_conf<0.6: surface_form 标 "speaker_uncertain"; resolution_confidence="ambiguous".

- **evidence_quotes 必带时间锚**: 每条 quote 前缀加 `[HH:MM:SS-HH:MM:SS]` (session 偏移), 用于反查听原音. 例:
  `"[01:23:45-01:23:58] 我下周三去见 Alice 确认这个事"`

- **where_chat_room 是场景推断**, 不是真实"群". 形如 `audio:<场景>·<细分>` (e.g. `audio:私聊·Alice` / `audio:自言自语`). room_tier 取 1 (私聊/自语) / 2 (家庭/工作场所) / 3 (公共场所多人). 完整场景类别表见 [api_roadmap.md](../../../docs/api_roadmap.md).

- **types 优先**: opinion / decision / commitment / question / report / state. **严禁** announcement (录音不是群通知). interaction 仅在双向多轮对话主轴时用.

- **salience**: 自言自语低密度 → 低; 双人计划约定 → 中; 多人技术决策 → 高; 全段 ASR 低置信 → 上限 0.4. 具体数值表见 [api_roadmap.md](../../../docs/api_roadmap.md). salience_reason ≤ 60 字, 说明依据.

- **抽取要点**:
  - 用户/对方做出的 **承诺** ("我下周交", "我帮你看看") → commitment, salience 0.6+.
  - 时间地点的 **约定** ("明天 3 点量信楼") → commitment, time_resolutions 严格锚定.
  - **决定** (买什么、选什么、做什么方向) → decision.
  - 用户表达的 **观点/感受** (含价值判断词) → opinion.
  - 学到的事实 / 别人告知的信息 → report / state.
  - 单条无关闲聊 ("嗯", "哦", "对") → 跳过, 不抽 card.

- **batch_window 跨度**: 通常 5-15 min (语义切块产物); narrative 必须明示具体子时段, 不要笼统"这段录音里".

- **identity_assertions 候选**:
  - 新 voice (manifest_slice 未命中) → identity_assertions[type=new_voice_candidate, surface_form=voice_canonical_id, evidence_quote="...对话内容..."], 留 Stage 3 仲裁是否合并到已知 person.
  - 自报家门 ("我是 X 实验室的 Y") → identity_assertions[type=self_introduction, surface_form="X实验室Y", canonical_name="Y"].

- 整 batch 无有效信息 (全静音背景音 / 全 ASR 乱码 / 全 'OK 嗯 啊') → 输出空 cards.
"""

_SECTION_CLAUDE_CODE = """
【本 batch 是 Claude Code 开发会话 (claude_code)】

特别规则:
- **这是 user-vs-AI 开发交互, 不是人际对话**. "Claude" 是 AI 工具, **永远不进 person entity**.
  - Claude 若需作为 entity 出现 → role_in_card="mentioned", canonical_id="tool_claude_code", 视作 inanimate.
  - **绝不抽 RelationAssertion** 涉及 Claude.
- **"Alice" (is_self=true) 大部分消息是 `[tool result OK]` 或 `[tool result ERR: ...]`** — 这是工具执行回显, 不算用户说话. 真正"用户输入"是非 [tool result ...] 前缀的消息.
- **消息标签语义**:
  - `[Claude tool_use:Bash] <cmd>` → Claude 调 Bash 命令, payload 是 shell 命令.
  - `[Claude tool_use:Read] <path>` → Claude 读文件, payload 是路径.
  - `[Claude] <text>` → Claude 的中间推理/解释.
  - `[tool result OK]` → 工具成功 (信息密度低).
  - `[tool result ERR: ...]` → 工具失败 (高密度 — 错误现场).
- **抽取要点**:
  - 这个 session 在做什么任务 (项目/调试/重构).
  - 关键决定 (架构选型 / 文件结构 / 算法选择).
  - 完成的工作 (Claude 报"已成功 X").
  - 失败/错误 (Claude 多次重试 / [tool result ERR]).
- **types 优先**: decision / share / correction / question / state. **严禁** commitment / interaction.
- **chat_room 是 cwd 路径**: 解码示例 `C--Users-username-workspace-project-research-X` → `C:/Users/username/workspace/project/research/X` (双 `--` 是 `:/`, 单 `-` 是 `/`; Windows 上也可写作 `\\`). 进 narrative 作 WHERE; 用 `cwd` 字段(若有)更可靠.
- **session_id** (若有) 作为 batch 关联 key 进 related_episode.
- **room_tier=2** (开发会话) 写入 card.room_tier.
- 单一 tool_use + 单 tool_result + 无 Claude 推理 → salience<0.3, 跳过.
"""

PASS2_SOURCE_SECTIONS: Dict[str, str] = {
    "wechat": _SECTION_CHAT,
    "qq": _SECTION_CHAT,
    "email": _SECTION_EMAIL,
    "browser": _SECTION_BROWSER,
    "claude_code": _SECTION_CLAUDE_CODE,
    "audio": _SECTION_AUDIO,
}


def get_pass2_system_prompt(source: str = "wechat") -> str:
    """Compose the pass-2 system prompt for ``source``.

    Routes through :mod:`src.core.prompt_router` to honor the
    ``MEMEX_EXTRACTOR_TIER`` env (bundled / byo). When the router
    returns ``None`` (the default bundled mode) we fall back to the
    in-tree OSS prompt below.
    """
    try:
        from src.core.prompt_router import get_extraction_prompt
        external = get_extraction_prompt("pass2", source=source)
        if external is not None:
            return external
    except Exception:
        pass
    section = PASS2_SOURCE_SECTIONS.get(source, _SECTION_CHAT)
    return _BASE_RULES + "\n" + section


# Backward-compat constant (wechat default, matches legacy behavior).
PASS2_SYSTEM_PROMPT = get_pass2_system_prompt("wechat")


# ──────────────────────────────────────────────────────────────────────
#  User-prompt builder (source-aware dispatch)
# ──────────────────────────────────────────────────────────────────────
def build_pass2_user_prompt(
    batch_id: str,
    chat_room: str,
    room_hash: str,
    batch_window_local: str,
    sender_list: List[Dict[str, Any]],
    manifest_slice: Dict[str, Any],
    messages: List[Dict[str, Any]],
    chinese_calendar_window: Optional[Dict[str, str]] = None,
    user_calendar_window: Optional[Dict[str, str]] = None,
    extraction_prompt_sha_placeholder: str = "<TO_FILL>",
    source: str = "wechat",
    cwd: Optional[str] = None,
    room_tier: Optional[int] = None,
    session_id: Optional[str] = None,
    known_speakers: Optional[List[Dict[str, Any]]] = None,
    passive_listener_session: bool = False,
) -> str:
    """Source-aware user prompt builder."""
    lines: List[str] = []

    lines.append("# 当前 batch")
    lines.append(f"batch_id: {batch_id}")
    lines.append(f"source: {source}")
    if source == "claude_code" and cwd:
        lines.append(f"cwd (decoded): {cwd}")
    if source == "claude_code" and session_id:
        lines.append(f"session_id: {session_id}")
    lines.append(f"chat_room: {chat_room}")
    lines.append(f"room_hash: {room_hash}")
    if room_tier is not None:
        lines.append(f"room_tier: {room_tier}")
    lines.append(f"batch_window: {batch_window_local}")
    lines.append(f"extraction_prompt_sha (你 must echo): {extraction_prompt_sha_placeholder}")
    lines.append("")

    # sender_list
    lines.extend(_render_sender_list(sender_list, source))
    lines.append("")

    # known_speakers (audio sessions with pre-declared participants)
    if known_speakers:
        lines.append("# known_speakers (本会话确定参与者)")
        for s in known_speakers:
            self_str = " is_self=true" if s.get("is_self") else ""
            role_str = f" role={s.get('role_in_session')}" if s.get("role_in_session") else ""
            aliases = s.get("aliases") or []
            akas = (", aliases=" + str(aliases)) if aliases else ""
            lines.append(f"  - canonical_id={s.get('canonical_id')}  "
                         f"display={s.get('display_name')!r}{self_str}{role_str}{akas}")
        if passive_listener_session:
            lines.append(
                "  ⚠️ **passive_listener_session=true** — 本会话用户(is_self)是听众,"
                " 默认归属反转: 讲解/介绍/演示语境下'我'归 voice_unknown_teacher, "
                "仅学生发问/被呼名/自言自语才归 is_self. 详 §AUDIO 被动听众模式规则."
            )
        else:
            lines.append(
                f"  ⚠️ 本 batch 全部 utterances 必须归属上面 {len(known_speakers)} 人之一; "
                "不存在 voice_unknown_* 作 final canonical_id; 按 §AUDIO 中"
                "已知 2-party 模式规则归属。"
            )
        lines.append("")

    # manifest_slice
    lines.append("# manifest_slice (ground truth; 优先使用)")
    persons = manifest_slice.get("persons", {}) if manifest_slice else {}
    if persons:
        lines.append("  persons:")
        for cid, p in persons.items():
            self_str = " is_self=true" if p.get("is_self") else ""
            lines.append(
                f"    {cid}: primary={p.get('primary_name')!r}, "
                f"aka={p.get('aka', [])}, pinyin={p.get('pinyin_initials', [])}"
                f"{self_str}, wxid_hashes={p.get('wxid_hashes', [])}"
            )
    orgs = manifest_slice.get("organizations", {}) if manifest_slice else {}
    if orgs:
        lines.append("  organizations:")
        for cid, o in orgs.items():
            lines.append(
                f"    {cid}: primary={o.get('primary_name')!r}, "
                f"aka={o.get('aka', [])}, pinyin={o.get('pinyin_initials', [])}"
            )
    inanimate = manifest_slice.get("inanimate", {}) if manifest_slice else {}
    if inanimate:
        lines.append("  inanimate:")
        for cid, it in inanimate.items():
            lines.append(
                f"    {cid}: primary={it.get('primary_name')!r}, "
                f"aka={it.get('aka', [])}, owned_by={it.get('owned_by')}"
            )
    pf = manifest_slice.get("public_figures", {}) if manifest_slice else {}
    if pf:
        lines.append("  public_figures:")
        for cid, p in pf.items():
            lines.append(
                f"    {cid}: primary={p.get('primary_name')!r}, "
                f"aka={p.get('aka', [])}, category={p.get('category')}"
            )
    if not (persons or orgs or inanimate or pf):
        lines.append("  (empty — no manifest entries injected for this batch)")
    lines.append("")

    # Calendar refs (only meaningful for chat / email)
    if chinese_calendar_window:
        lines.append("# chinese_calendar (附近节日)")
        for date, name in chinese_calendar_window.items():
            lines.append(f"  {date}: {name}")
        lines.append("")
    if user_calendar_window:
        lines.append("# user_calendar (用户日程附近)")
        for date_range, name in user_calendar_window.items():
            lines.append(f"  {date_range}: {name}")
        lines.append("")

    # Messages — branch on source
    if source == "email":
        lines.extend(_render_messages_email(messages))
    elif source == "browser":
        lines.extend(_render_messages_browser(messages))
    elif source == "claude_code":
        self_hash = next(
            (s.get("wxid_hash") for s in sender_list if s.get("is_self")), None
        )
        lines.extend(_render_messages_cc(messages, self_hash))
    elif source == "audio":
        lines.extend(_render_messages_audio(messages, sender_list))
    else:
        lines.extend(_render_messages_chat(messages))
    lines.append("")

    # Tail reminder (source-aware nudges)
    lines.append("# 任务: 输出 cards JSON + END_OF_OUTPUT")
    if source == "email":
        lines.append("# 提醒: 时间绝对化; 营销/广告/钓鱼邮件 → 输出空 cards.")
    elif source == "browser":
        lines.append("# 提醒: 多搜索串成 1 张 card; URL-encoded 不进 narrative; 人名搜索进 identity_assertions.")
    elif source == "claude_code":
        lines.append("# 提醒: Claude 是工具不是人; [tool result OK] 不算用户说话; types 限 decision/share/correction/question/state.")
    elif source == "audio":
        lines.append("# 提醒: ASR 可能有错字, narrative 关键事实必须在 evidence_quotes 字面命中; "
                     "evidence_quotes 必带 [HH:MM:SS-HH:MM:SS] 时间锚; "
                     "diariz_conf<0.6 的 speaker 不要 confidently 指认; "
                     "声纹未知 → identity_assertions[new_voice_candidate].")

    return "\n".join(lines)


def compute_pass2_prompt_sha(system_prompt: str, user_prompt: str) -> str:
    h = hashlib.sha256()
    h.update(system_prompt.encode("utf-8"))
    h.update(b"\x00")
    h.update(user_prompt.encode("utf-8"))
    return h.hexdigest()[:32]


# ──────────────────────────────────────────────────────────────────────
#  Output validation (unchanged from v1)
# ──────────────────────────────────────────────────────────────────────
class Pass2OutputError(ValueError):
    """LLM output didn't conform to schema v2 cards JSON."""


def parse_pass2_output(raw: str) -> List[Dict[str, Any]]:
    if not raw or not isinstance(raw, str):
        raise Pass2OutputError("empty or non-string output")

    text = raw.split("END_OF_OUTPUT")[0].strip()

    if "```" in text:
        idx_start = text.find("```json")
        if idx_start < 0:
            idx_start = text.find("```")
        if idx_start >= 0:
            idx_start = text.find("\n", idx_start) + 1
            idx_end = text.find("```", idx_start)
            if idx_end > 0:
                text = text[idx_start:idx_end].strip()

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace < 0 or last_brace < first_brace:
        raise Pass2OutputError("no JSON object found in output")
    text = text[first_brace:last_brace + 1]

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        # Lenient fallback: qwen3.6-reasoner sometimes emits trailing commas,
        # Chinese curly quotes, or stray control chars. Try light repairs.
        # (Originally added by 2026-05-12 api-cron-e2e-audit §4.2.)
        repaired = text
        repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
        repaired = repaired.replace("\u201c", '"').replace("\u201d", '"')
        repaired = repaired.replace("\u2018", "'").replace("\u2019", "'")
        try:
            obj = json.loads(repaired)
        except json.JSONDecodeError:
            raise Pass2OutputError(f"JSON parse failed: {e}") from e

    if not isinstance(obj, dict):
        raise Pass2OutputError("output root must be object")

    cards = obj.get("cards", [])
    if not isinstance(cards, list):
        raise Pass2OutputError("cards must be list")
    return cards


def validate_card_dict(card: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    for k in ("narrative", "evidence_quotes", "when_start", "when_end",
              "where_chat_room", "where_chat_room_hash", "room_tier",
              "entities", "speaker_role", "types", "salience",
              "salience_reason", "attestation_tier", "batch_id",
              "extraction_prompt_sha"):
        if k not in card:
            issues.append(f"missing field: {k}")
    if "schema_v" in card and card["schema_v"] != 2:
        issues.append(f"schema_v must be 2, got {card['schema_v']}")
    if "narrative" in card:
        n = len(card["narrative"] or "")
        if n < 30:
            issues.append(f"narrative too short ({n} chars)")
        if n > 1200:
            issues.append(f"narrative too long ({n} chars)")
    if "salience" in card:
        try:
            s = float(card["salience"])
            if s < 0 or s > 1:
                issues.append(f"salience out of [0,1]: {s}")
        except (TypeError, ValueError):
            issues.append(f"salience not numeric: {card['salience']!r}")
    return issues


__all__ = [
    "CANONICAL_SOURCES",
    "PASS2_SYSTEM_PROMPT",
    "PASS2_SOURCE_SECTIONS",
    "get_pass2_system_prompt",
    "build_pass2_user_prompt",
    "normalize_source",
    "decode_rfc2047",
    "is_email_likely_junk",
    "compute_pass2_prompt_sha",
    "parse_pass2_output",
    "validate_card_dict",
    "Pass2OutputError",
]
