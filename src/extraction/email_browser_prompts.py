"""email_browser_prompts.py — extraction prompts for email + browser sources.

These are *time-series event* sources, not conversational ones, so the
chat-style `batch_prompts.batch_v1` doesn't apply. Each source gets a
prompt that maps its raw structure to MEMORYCARD_V2-compatible facts.

CEO directive 2026-05-08:
  - Same Stage A (Qwen gate) → Stage B (Gemma 31B extract) → Stage C
    (BGE-M3 + DeepSeek arbitrate) → Stage D (POST) pipeline
  - prompts will differ per source; details defer to this module
  - NO privacy filter — full URLs / full email bodies

Output schema (Stage B parses this):
  {
    "facts": [
      {
        "subject_canon": "...",   # entity (person / project / domain)
        "predicate": "...",       # action verb in natural Chinese
        "object_canon": "...",    # target entity (event / topic / asset)
        "evidence_quote": "...",  # at most 80 chars from source
        "salience": 0.0-1.0,
        "type": "commitment|event|decision|browse|email_thread|search|...",
        "when_start": "ISO 8601",
        "confidence": 0.0-1.0
      },
      ...
    ]
  }

Public API:
    build_email_prompt(emails: List[dict], context: dict) -> str
    build_browser_session_prompt(visits: List[dict], context: dict) -> str
    build_browser_search_prompt(searches: List[dict], context: dict) -> str
"""
from __future__ import annotations

import json
from textwrap import dedent
from typing import Any, Dict, List


# ────────────────────────────────────────────────────────────────────────
# Email prompt
# ────────────────────────────────────────────────────────────────────────

EMAIL_SYSTEM_RULES = dedent("""
**邮件抽取规则 (硬约束)**:
  1. subject 必须是邮件双方真名/单位名 (从 from / to 字段抽出), 不可用
     "邮件" / "对方" / "对话方" 等泛称.
  2. object 必须是 *实体* — 课程名 / 会议名 / 截止时间 / 文档标题 / 截止日期.
  3. evidence_quote 最长 80 字, 必须是邮件正文连续片段 (不裁剪 / 改写).
  4. predicate 用动词中文短语 (e.g. "通知", "请求确认", "发送附件",
     "提醒截止", "邀请会议").
  5. 同一邮件的 facts ≤ 5 条; 内容空 / 退订邮件 / 营销 → 0 facts.
  6. when_start 使用邮件发送时间 (date_iso), ISO 8601 with timezone.
  7. 抽取类型 type ∈ {announcement, request, deadline, attachment_share,
     meeting_invite, document_revision, system_notification}.
""").strip()


def build_email_prompt(emails: List[Dict[str, Any]], context: Dict[str, Any]) -> str:
    """Build prompt for one batch of emails (typically 1 thread or ≤5 emails).

    Args:
        emails: list of dict {subject, from_raw, to_raw, body_text,
                              date_iso, attachments, ...}.
        context: optional {account, folder, thread_id, ...}.

    Returns:
        Prompt string ready for Stage A/B LLM.
    """
    # Truncate body_text per-email to 800 chars to keep token budget bounded.
    items = []
    for em in emails:
        body = em.get("body_text", "") or ""
        body_trunc = body[:800] + ("…(更多)" if len(body) > 800 else "")
        items.append({
            "subject": em.get("subject", ""),
            "from": em.get("from_raw", ""),
            "to": em.get("to_raw", ""),
            "cc": em.get("cc_raw", ""),
            "date": em.get("date_iso", ""),
            "body": body_trunc,
            "attachments": em.get("attachments", []),
        })

    payload = {
        "context": {
            "account": context.get("account", ""),
            "folder": context.get("folder", ""),
            "n_emails": len(emails),
        },
        "emails": items,
    }

    return dedent(f"""
        /no_think
        你是一个邮件事件抽取器。下面是用户的 {len(emails)} 封邮件 (按时间排序)。
        请抽取出对用户行为/项目/承诺有意义的 facts (一封邮件可有 0-5 条):

        {EMAIL_SYSTEM_RULES}

        **任务**: 输出 JSON, 形如:
        {{"facts": [{{"subject_canon": "...", "predicate": "...",
                      "object_canon": "...", "evidence_quote": "...",
                      "type": "...", "when_start": "...",
                      "salience": 0.6, "confidence": 0.8}}, ...]}}

        **邮件数据**:
        ```json
        {json.dumps(payload, ensure_ascii=False, indent=2)}
        ```

        **JSON**:""").strip()


# ────────────────────────────────────────────────────────────────────────
# Browser session prompt — aggregates one "session" (clusters of visits)
# ────────────────────────────────────────────────────────────────────────

BROWSER_SESSION_RULES = dedent("""
**浏览会话抽取规则 (硬约束)**:
  1. subject 必须是 "用户" (因为浏览主体只有一个).
  2. object 必须是 *实体* — 网站域名 / 论文标题 / 课程名 / 工具名 / 项目名.
  3. evidence_quote 是 1-3 个最具代表性的 page title (拼接 "→" 分隔, 总长 ≤80).
  4. predicate 用动词中文短语 (e.g. "查阅", "搜索", "下载",
     "登录", "完成在线作业", "查文献", "调试代码").
  5. 同一 session 的 facts: 1-3 条 (聚合, 不每个 URL 一条).
  6. when_start 使用 session 起始时间.
  7. type ∈ {research, study, communication, shopping, entertainment,
            development, admin, social_media, news, navigation}.
  8. 跳过 about: / chrome:// / file:// 三类内部 URL.
  9. 跳过纯重定向 (transition=server_redirect / client_redirect).
""").strip()


def build_browser_session_prompt(
    visits: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> str:
    """Build prompt for one browsing session (≤30 visits, ≤30 min span).

    Args:
        visits: list of VisitEvent dicts ordered by time.
        context: {browser_id, profile, session_id, span_minutes}.
    """
    items = []
    for v in visits:
        items.append({
            "t": v.get("visit_time_iso", "")[-8:],  # HH:MM:SS only
            "url": v.get("url", "")[:200],
            "title": (v.get("title", "") or "")[:80],
            "transition": v.get("transition_label", ""),
            "duration_s": round(v.get("visit_duration_s", 0), 1),
        })

    payload = {
        "context": {
            "browser": context.get("browser_id", ""),
            "session_start": context.get("session_start_iso", ""),
            "span_minutes": context.get("span_minutes", 0),
            "n_visits": len(visits),
        },
        "visits": items,
    }

    return dedent(f"""
        /no_think
        你是浏览行为聚合抽取器。下面是用户在 {context.get('span_minutes', '?')} 分钟内
        的 {len(visits)} 次页面访问 (一个 session)。请抽取 1-3 条聚合 fact 描述
        用户在这段时间做了什么:

        {BROWSER_SESSION_RULES}

        **任务**: 输出 JSON:
        {{"facts": [{{"subject_canon": "用户", "predicate": "...",
                      "object_canon": "...", "evidence_quote": "...",
                      "type": "...", "when_start": "...",
                      "salience": 0.5, "confidence": 0.7}}, ...]}}

        **浏览数据**:
        ```json
        {json.dumps(payload, ensure_ascii=False, indent=2)}
        ```

        **JSON**:""").strip()


# ────────────────────────────────────────────────────────────────────────
# Browser search prompt — keyword_search_terms
# ────────────────────────────────────────────────────────────────────────

BROWSER_SEARCH_RULES = dedent("""
**搜索查询抽取规则 (硬约束)**:
  1. subject 必须是 "用户".
  2. predicate 固定为 "搜索".
  3. object_canon 是搜索词本身 (≤40 字符, 截掉就丢一条).
  4. evidence_quote 是搜索结果页面 URL 的 host 部分.
  5. type 固定为 "search".
  6. salience: 看搜索词长度 + 是否专业术语:
     - 短词 (≤4 字) 且为常见名词 → 0.3
     - 专业术语 / 长 query → 0.6
""").strip()


def build_browser_search_prompt(
    searches: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> str:
    """Build prompt for batched search-keyword facts.

    Note: searches are usually high-density and low-context. We send up
    to 50 per batch; LLM mostly transcribes 1:1 facts (subject=用户,
    predicate=搜索, object=keyword). This is more about data shaping than
    interpretation, so cheap Stage A model is sufficient.
    """
    items = [{
        "t": s.get("visit_time_iso", ""),
        "keyword": s.get("keyword", ""),
        "search_url_host": s.get("url", "").split("/")[2] if "://" in s.get("url", "") else "",
    } for s in searches]

    payload = {
        "context": {"browser": context.get("browser_id", ""), "n": len(searches)},
        "searches": items,
    }

    return dedent(f"""
        /no_think
        把下面 {len(searches)} 条用户搜索记录每一条转成一个 fact:

        {BROWSER_SEARCH_RULES}

        **任务**: 输出 JSON:
        {{"facts": [{{"subject_canon": "用户", "predicate": "搜索",
                      "object_canon": "<keyword>",
                      "evidence_quote": "<host>",
                      "type": "search", "when_start": "<t>",
                      "salience": 0.3-0.6, "confidence": 0.95}}, ...]}}

        **搜索数据**:
        ```json
        {json.dumps(payload, ensure_ascii=False, indent=2)}
        ```

        **JSON**:""").strip()


# ────────────────────────────────────────────────────────────────────────
# Prompt registry — used by batch_builder + Stage A/B routers
# ────────────────────────────────────────────────────────────────────────

PROMPT_REGISTRY = {
    "email_v1": build_email_prompt,
    "browser_session_v1": build_browser_session_prompt,
    "browser_search_v1": build_browser_search_prompt,
}


def get_prompt_builder(prompt_id: str):
    """Return prompt builder function by id."""
    if prompt_id not in PROMPT_REGISTRY:
        raise ValueError(f"unknown prompt_id: {prompt_id!r}; "
                          f"available: {list(PROMPT_REGISTRY)}")
    return PROMPT_REGISTRY[prompt_id]


# ────────────────────────────────────────────────────────────────────────
# Smoke test
# ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample_emails = [{
        "subject": "项目经费报销提醒",
        "from_raw": "advisor@example.com",
        "to_raw": "user@example.com",
        "date_iso": "2026-04-26T09:00:00+08:00",
        "body_text": "请于本月底前完成本季度项目经费报销的相关材料提交...",
        "attachments": ["报销说明.pdf"],
    }]
    sample_visits = [
        {"visit_time_iso": "2026-05-08T10:01:23+08:00",
         "url": "https://courses.example.edu/portal/...",
         "title": "课程资源 - 半导体物理",
         "transition_label": "link", "visit_duration_s": 30.0},
        {"visit_time_iso": "2026-05-08T10:05:11+08:00",
         "url": "https://github.com/anthropic/claude",
         "title": "GitHub - anthropic/claude",
         "transition_label": "typed", "visit_duration_s": 90.0},
    ]
    sample_searches = [
        {"visit_time_iso": "2026-05-08T11:00:00+08:00",
         "keyword": "FCI Slater determinant Python",
         "url": "https://www.bing.com/search?q=..."},
    ]
    print("=== email_v1 ===")
    print(build_email_prompt(sample_emails, {"account": "demo_email", "folder": "INBOX"})[:600])
    print("\n=== browser_session_v1 ===")
    print(build_browser_session_prompt(sample_visits, {
        "browser_id": "edge", "session_start_iso": "2026-05-08T10:01:00+08:00",
        "span_minutes": 5,
    })[:600])
    print("\n=== browser_search_v1 ===")
    print(build_browser_search_prompt(sample_searches, {"browser_id": "demo"})[:400])
