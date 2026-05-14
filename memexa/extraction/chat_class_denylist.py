"""U7 TU-3: chat_class_denylist — drop sensitive raw text from chat ingestion.

5 categories (per chat_to_graph plan_v3_FINAL §3 U7):
  - 医疗 (medical):     诊断/处方/疾病/医院 keywords
  - 金融 (financial):   金额/账户/转账/余额 keywords
  - 性 (sexual):        explicit adult content keywords
  - 政治 (political):   politically sensitive keywords (PRC context)
  - 3rd-party PII:      他人 phone/email/ID/bank card detected via regex

Behavior (CEO directive #2 全部入图):
  - 命中 → drop msg (filter_message returns None)
  - NOT 命中 → pass through (return msg unchanged)
  - **NOT block 群聊 itself** — only filter content with sensitive keywords

axis_anchor: [C:cli:chat_denylist]
trace event: chat_denylist_dropped
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Sensitive keyword categories
# ---------------------------------------------------------------------------

MEDICAL_KEYWORDS = (
    # diagnosis / disease names
    "诊断", "处方", "病历", "癌症", "肿瘤", "艾滋", "HIV", "肝炎", "糖尿病",
    "高血压", "心脏病", "抑郁症", "精神病", "住院", "手术",
    # medical institutions / actions
    "医生开", "医院说", "化验", "检查报告", "复诊",
)

FINANCIAL_KEYWORDS = (
    # transfer / amount / account
    "转账", "汇款", "支付", "付款给", "余额", "账户余额",
    "银行密码", "支付密码", "信用卡密码", "PIN",
    # specific financial actions
    "贷款", "按揭", "抵押", "理财产品", "炒股",
)

SEXUAL_KEYWORDS = (
    # explicit (kept as terse list; not exhaustive)
    "约炮", "色情", "成人视频", "AV", "嫖", "卖淫", "援交",
    # encoded sexual content
    "fwb", "FWB", "性服务",
)

POLITICAL_KEYWORDS = (
    # PRC sensitive (keep terse; reviewer can expand)
    "六四", "天安门事件", "藏独", "疆独", "港独", "台独",
    "法轮功", "党中央", "翻墙", "VPN绕过审查",
)

# 3rd-party PII regex patterns (similar to keystone_outbox but applied to chat content)
PII_REGEX = (
    # phone (Chinese mobile)
    re.compile(r"(?<!\d)(?:\+?86)?1[3-9]\d{9}(?!\d)"),
    # 18-digit ID
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    # bank card 16-19 digit
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    # email
    re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"),
)


@dataclass
class FilterResult:
    """Outcome of filter_message."""
    passed: bool
    msg: Optional[dict]
    reason: str  # "ok" | "medical" | "financial" | "sexual" | "political" | "pii"
    matched_pattern: str  # for trace/debug


def _check_keywords(text: str, keywords: Sequence[str]) -> Optional[str]:
    """Return matched keyword or None."""
    for kw in keywords:
        if kw in text:
            return kw
    return None


def _check_pii_regex(text: str) -> Optional[str]:
    """Return matched regex pattern (compiled) or None."""
    for pattern in PII_REGEX:
        if pattern.search(text):
            return pattern.pattern
    return None


def filter_message(msg: dict) -> FilterResult:
    """Apply 5-category denylist to msg content.

    msg is expected to have at minimum a 'content' key (str). Other keys
    (timestamp, sender, chat_name, msg_type) preserved when passing through.

    Group chats are NOT blocked by this function — only content with sensitive
    keywords. If chat_name ends with '@chatroom' (group), the same content
    rules apply.
    """
    content = str(msg.get("content", "") or "")
    if not content:
        return FilterResult(passed=True, msg=msg, reason="empty", matched_pattern="")

    # 5-category sequential check
    for category, kw_set in (
        ("medical", MEDICAL_KEYWORDS),
        ("financial", FINANCIAL_KEYWORDS),
        ("sexual", SEXUAL_KEYWORDS),
        ("political", POLITICAL_KEYWORDS),
    ):
        hit = _check_keywords(content, kw_set)
        if hit:
            _emit_drop_trace(category, hit, msg)
            return FilterResult(passed=False, msg=None, reason=category, matched_pattern=hit)

    # 3rd-party PII regex check
    pii_hit = _check_pii_regex(content)
    if pii_hit:
        _emit_drop_trace("pii", pii_hit, msg)
        return FilterResult(passed=False, msg=None, reason="pii", matched_pattern=pii_hit)

    return FilterResult(passed=True, msg=msg, reason="ok", matched_pattern="")


def _emit_drop_trace(category: str, matched: str, msg: dict) -> None:
    """Best-effort trace_sink emit; msg content hashed (privacy)."""
    try:
        from memexa.core.trace_sink import emit  # type: ignore
        msg_hash = hashlib.sha256(
            str(msg.get("content", "")).encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        emit("chat_denylist_dropped", {
            "category": category,
            "matched_excerpt": matched[:30],
            "msg_hash": msg_hash,
            "chat_room": str(msg.get("chat_name", ""))[:40],
        })
    except Exception:
        pass
