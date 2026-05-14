"""
DDL Scanner — LLM-first deadline extraction over hindsight v5 cards.

Primary detector: DeepSeek (deepseek-chat). Cloud LLM, ~1.5s/call, JSON-stable.
The card's narrative + evidence_quotes are sent with a recall-first prompt.
CEO directive: regex misses too much daily-language DDL.

Fallback: Qwen3-14B at Mac :18080 if DeepSeek key missing or 5xx persists.

Output: data/calendar_planning/ddl_inbox.jsonl
Progress checkpoint: data/calendar_planning/ddl_scan_progress.json
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "calendar_planning"
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_PATH = OUT_DIR / "ddl_scan_progress.json"

DEFAULT_BANK = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")
HINDSIGHT_BASE = os.environ.get(
    "MEMEXA_HINDSIGHT_BASE", "http://127.0.0.1:8888"
).rstrip("/")
LLM_PROVIDER = os.environ.get("MEMEXA_DDL_LLM_PROVIDER", "deepseek").lower()
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("MEMEXA_DDL_DEEPSEEK_MODEL", "deepseek-chat")
QWEN_BASE = os.environ.get("MEMEXA_DDL_QWEN_BASE", "http://127.0.0.1:18080").rstrip("/")
QWEN_MODEL = os.environ.get("MEMEXA_DDL_QWEN_MODEL", "mlx-community/Qwen3-14B-4bit")
PAGE = 200

CARD_BEGIN = "MEMORYCARD_V2_HEADER_BEGIN"
CARD_END = "MEMORYCARD_V2_HEADER_END"


@dataclass
class DdlCandidate:
    card_id: str
    has_ddl: bool
    due_iso: str | None
    what: str
    who_for: str
    confidence: float
    reason: str
    source: str
    salience: float
    types: list[str] = field(default_factory=list)
    narrative_head: str = ""
    mentioned_at: str = ""
    when_end: str | None = None
    detector: str = "deepseek-chat"
    # FIX 2026-05-09: persist evidence_quotes + full narrative + grounding
    # evidence so offline re-verification is possible without hindsight API.
    narrative_full: str = ""
    evidence_quotes: list[str] = field(default_factory=list)
    speaker_role: str = ""
    grounding_evidence: str = ""
    anchor_iso: str = ""


# ---------- Card text parsing ----------


def _strip_card_text(text: str) -> dict[str, Any] | None:
    if CARD_BEGIN not in text or CARD_END not in text:
        return None
    body = text.split(CARD_BEGIN, 1)[1].split(CARD_END, 1)[0]
    body = body.strip().lstrip("】").rstrip("【").strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        try:
            start = body.index("{")
            depth = 0
            for i in range(start, len(body)):
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(body[start : i + 1])
        except (ValueError, json.JSONDecodeError):
            pass
    return None


# ---------- LLM detector ----------

SYSTEM_PROMPT = """你是一个严格的 DDL（截止日期 / 待办承诺）检测器，专为中国大学生 / 科研工作者的日常聊天设计。

阅读用户给的"卡片正文"——它是从微信、QQ、邮件、Claude Code 等通道抽出的事实卡片。判断卡片是否含一个**对用户本人未来需要做某事的承诺/截止/约束/约会**。

【正例（应该判 has_ddl=true）】
1. "5月20号前把 PRB referee response 改完"          → due=YYYY-05-20, what="改 PRB referee response"
2. "周三组会前发 slides"                             → due=本周三, what="发 slides"
3. "下周一交一稿"                                    → due=下周一日期, what="交一稿"
4. "明早 9 点开始考试"                               → due=明天, what="参加考试"
5. "老师让我们 5/12 之前把数据交一下"                → due=YYYY-05-12, what="交数据给老师"
6. "周末前完成实验台调试"                            → due=本周六, what="完成实验台调试"
7. "5 月底之前把毕业论文初稿写完"                    → due=YYYY-05-31, what="毕业论文初稿"

【反例（应该判 has_ddl=false）】
1. "上次组会讨论的事情"             —— 过去事件
2. "我昨天看了那篇文章"             —— 已完成的过去
3. "anchor_message_ts: 2026-04-21"  —— 系统时间戳，不是承诺
4. "已经提交了，谢谢老师"            —— 已完成
5. "下次再说"                       —— 无具体时间无具体动作
6. 单纯八卦 / 闲聊 / 表情 / 信息分享，没有动作承诺

【相对时间解析】
卡片正文里我会同时给你 anchor_date（卡片产生的日期）。请把"明天 / 后天 / 周三 / 下周一 / 月底"这类相对表达解析成 YYYY-MM-DD 格式。如果实在解析不出来（例如只是"以后"），给 due_iso=null 但 has_ddl 仍可以为 true，置信度调低。

【recall-first 策略】
宁可多报一些边缘候选（has_ddl=true, confidence=0.4），让人类复核去掉，也不要漏掉真正的 DDL。但**不要**把过去事件、纯八卦、纯系统元数据误报。

【关键约束 — 不要幻觉日期】
1. 如果你必须"假设""推测""猜"具体日期才能填 due_iso，那就 due_iso=null 并把
   confidence 设为 ≤0.4，reason 里直说"日期不明"。**禁止**为了凑 ISO 日期而拍脑袋。
2. 如果消息只是别人 (老师/同学/TA) 在群里宣布"下次/下节课"做什么 (例如
   "下节课点名" / "下次开会讨论 X")，而**用户本人不需要做任何动作**，
   has_ddl=false。这是受众观察事件，不是个人 DDL。
3. 如果消息提到的 due_iso 距离 anchor_date 超过 60 天，且消息里没有显式年/月/日
   数字，has_ddl=false（说明你在外推）。

【输出格式】严格 JSON，单行，no prose, no markdown：
{"has_ddl": <true|false>, "due_iso": "YYYY-MM-DD" | null, "what": "<简短动作>", "who_for": "<对象，例如 老师/自己/合作者>", "confidence": <0.0-1.0>, "reason": "<不超过 30 字解释>"}

不要输出多个 JSON 对象。不要解释。仅一行 JSON。"""


def _build_user_prompt(payload: dict[str, Any], anchor: dt.date) -> str:
    narrative = (payload.get("narrative") or "").strip()
    quotes = payload.get("evidence_quotes") or []
    types = payload.get("types") or []
    salience = payload.get("salience") or 0.0
    when_end = payload.get("when_end") or ""
    quote_lines = "\n".join(f"  - {q}" for q in quotes[:6])
    return (
        f"anchor_date: {anchor.isoformat()}\n"
        f"types: {types}\n"
        f"salience: {salience}\n"
        f"when_end (raw, often only message ts): {when_end}\n\n"
        f"narrative:\n{narrative}\n\n"
        f"evidence_quotes:\n{quote_lines or '  (none)'}\n"
    )


def _parse_llm_output(content: str) -> dict[str, Any] | None:
    if not content:
        return None
    s = content.strip()
    if "```" in s:
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    try:
        start = s.index("{")
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(s[start : i + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return None


def _call_deepseek(
    payload: dict[str, Any], anchor: dt.date, client: httpx.Client,
    retries: int = 2, timeout: float = 45.0,
) -> tuple[dict[str, Any] | None, str | None]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None, "DEEPSEEK_API_KEY missing"
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(payload, anchor)},
        ],
        "max_tokens": 280,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = client.post(DEEPSEEK_URL, json=body, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            parsed = _parse_llm_output(content)
            if parsed is not None and "has_ddl" in parsed:
                return parsed, None
            last_err = f"unparsable_output: {content[:160]!r}"
        except (httpx.HTTPError, KeyError, IndexError) as e:
            last_err = repr(e)
        time.sleep(0.6 * (attempt + 1))
    return None, last_err


def _call_qwen_local(
    payload: dict[str, Any], anchor: dt.date, client: httpx.Client,
    retries: int = 1, timeout: float = 90.0,
) -> tuple[dict[str, Any] | None, str | None]:
    body = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "/no_think\n" + _build_user_prompt(payload, anchor)},
        ],
        "max_tokens": 280,
        "temperature": 0,
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = client.post(f"{QWEN_BASE}/v1/chat/completions", json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"].get("content") or ""
            parsed = _parse_llm_output(content)
            if parsed is not None and "has_ddl" in parsed:
                return parsed, None
            last_err = f"qwen_unparsable: {content[:160]!r}"
        except (httpx.HTTPError, KeyError, IndexError) as e:
            last_err = f"qwen_err:{e!r}"
        time.sleep(0.5 * (attempt + 1))
    return None, last_err


def call_llm(
    payload: dict[str, Any], anchor: dt.date, client: httpx.Client,
) -> dict[str, Any]:
    """Try primary provider, fall back to secondary; never raise."""
    if LLM_PROVIDER == "deepseek":
        result, err = _call_deepseek(payload, anchor, client)
        if result is not None:
            result.setdefault("_detector", "deepseek-chat")
            return result
        # fall back to qwen
        result, err2 = _call_qwen_local(payload, anchor, client)
        if result is not None:
            result.setdefault("_detector", "qwen3-14b-fallback")
            return result
        return {"has_ddl": False, "due_iso": None, "what": "", "who_for": "",
                "confidence": 0.0, "reason": f"primary={err}; fallback={err2}",
                "_detector": "error"}
    else:  # qwen primary
        result, err = _call_qwen_local(payload, anchor, client)
        if result is not None:
            result.setdefault("_detector", "qwen3-14b")
            return result
        return {"has_ddl": False, "due_iso": None, "what": "", "who_for": "",
                "confidence": 0.0, "reason": f"qwen_err:{err}",
                "_detector": "error"}


# ---------- Hindsight client ----------


def iter_cards(
    bank: str = DEFAULT_BANK,
    base: str = HINDSIGHT_BASE,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    page: int = PAGE,
    max_pages: int | None = None,
) -> Iterator[dict[str, Any]]:
    offset = 0
    pages = 0
    with httpx.Client(timeout=30.0) as client:
        while True:
            url = f"{base}/v1/default/banks/{bank}/memories/list"
            r = client.get(url, params={"limit": page, "offset": offset})
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            if not items:
                return
            for it in items:
                ts_raw = it.get("mentioned_at") or it.get("date") or ""
                if isinstance(ts_raw, str) and ts_raw:
                    try:
                        ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        ts = None
                    if since and ts and ts < since:
                        continue
                    if until and ts and ts > until:
                        continue
                yield it
            offset += page
            pages += 1
            if max_pages and pages >= max_pages:
                return
            if offset >= int(data.get("total") or 10**9):
                return


# ---------- Progress checkpoint ----------


class ProgressTracker:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.done: set[str] = set()
        if path.exists():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                self.done = set(d.get("done", []))
            except Exception:
                self.done = set()

    def is_done(self, card_id: str) -> bool:
        return card_id in self.done

    def mark(self, card_id: str) -> None:
        with self._lock:
            self.done.add(card_id)

    def flush(self) -> None:
        with self._lock:
            self.path.write_text(
                json.dumps({"done": sorted(self.done), "ts": dt.datetime.utcnow().isoformat()},
                           ensure_ascii=False),
                encoding="utf-8",
            )


# ---------- Per-card scan ----------


HEDGE_KEYWORDS = (
    "假设", "推测", "猜测", "估计", "或许", "可能", "推断", "未明确", "不确定",
    "probably", "perhaps", "assume", "guess", "maybe", "uncertain", "infer",
)
NON_USER_AUDIENCE = ("老师", "教师", "TA", "助教", "他人", "对方", "其他人")

WEEKDAY_ZH = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _verify_due_grounded(
    due_iso: str | None, anchor: dt.date, payload: dict[str, Any],
) -> tuple[str | None, str]:
    """Strict source-grounding for a proposed due date.

    Returns (verified_due_iso_or_None, evidence_string). The LLM-proposed date
    is only accepted if at least one of these holds in narrative + evidence_quotes:

      A) literal date substring matches: "5月15" / "5/15" / "2026-05-15" / "5-15"
      B) relative phrase consistent with the anchor → date delta:
         - 今天/今晚/today      → delta=0
         - 明天/明日/tomorrow   → delta=1
         - 后天                 → delta=2
         - 大后天               → delta=3
         - 周X / 下周X          → must match the resolved weekday + delta range
         - 本月/月底/this month → same month as anchor
         - 下个月/next month    → +1 month from anchor
         - X 天内 / X 周内      → delta inside the stated window
         - X 天后 / X 周后      → delta matches stated offset

    Any LLM-proposed date that fails BOTH A and B is treated as a hallucination
    and dropped (return None).
    """
    if not due_iso:
        return None, ""
    try:
        target = dt.date.fromisoformat(due_iso)
    except ValueError:
        return None, "bad_iso"

    narrative = payload.get("narrative") or ""
    quotes = "\n".join(payload.get("evidence_quotes") or [])
    haystack = (narrative + "\n" + quotes)

    # ---- A: literal absolute date substring ----
    surface_forms = [
        f"{target.year}-{target.month:02d}-{target.day:02d}",
        f"{target.year}-{target.month}-{target.day}",
        f"{target.year}/{target.month}/{target.day}",
        f"{target.year}/{target.month:02d}/{target.day:02d}",
        f"{target.year}年{target.month}月{target.day}",
        f"{target.month}月{target.day}日",
        f"{target.month}月{target.day}号",
        f"{target.month}/{target.day}",
        f"{target.month:02d}/{target.day:02d}",
        f"{target.month}-{target.day}",
    ]
    en_months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    surface_forms.extend([
        f"{en_months[target.month]} {target.day}",
        f"{en_months[target.month]}. {target.day}",
        f"{target.day} {en_months[target.month]}",
    ])
    for sf in surface_forms:
        if sf and sf in haystack:
            return due_iso, f"literal_match:{sf!r}"

    # ---- B: anchor-relative phrases consistent with delta ----
    delta = (target - anchor).days

    def _has(*kw: str) -> bool:
        return any(k in haystack for k in kw)

    if delta == 0 and _has("今天", "今晚", "今夜", "今早", "today", "tonight"):
        return due_iso, "rel:today"
    if delta == 1 and _has("明天", "明日", "明早", "明晚", "tomorrow"):
        return due_iso, "rel:tomorrow"
    if delta == 2 and "后天" in haystack and "大后天" not in haystack:
        return due_iso, "rel:dayafter"
    if delta == 3 and "大后天" in haystack:
        return due_iso, "rel:big_dayafter"

    # weekday phrases (周一..周日)
    for ch, wd in WEEKDAY_ZH.items():
        if f"周{ch}" in haystack or f"礼拜{ch}" in haystack or f"星期{ch}" in haystack:
            # this-week direction
            this_delta = (wd - anchor.weekday()) % 7
            if delta == this_delta:
                return due_iso, f"rel:thisweek_{ch}"
            if delta == this_delta + 7 and (f"下周{ch}" in haystack or f"下礼拜{ch}" in haystack):
                return due_iso, f"rel:nextweek_{ch}"

    # 本周末 / weekend
    if 0 <= delta <= 6 and _has("周末", "weekend"):
        # weekend Sat/Sun within current week
        if target.weekday() in (5, 6):
            return due_iso, "rel:weekend"

    # 本月 / 月底 / this month — must share month
    if _has("本月", "这个月", "month-end", "this month"):
        if target.year == anchor.year and target.month == anchor.month:
            return due_iso, "rel:thismonth"
    if "月底" in haystack:
        # within last 7 days of anchor's month
        last_of_month = (
            dt.date(anchor.year + (1 if anchor.month == 12 else 0),
                    1 if anchor.month == 12 else anchor.month + 1, 1)
            - dt.timedelta(days=1)
        )
        if target.year == anchor.year and target.month == anchor.month and \
                (last_of_month - target).days <= 7:
            return due_iso, "rel:monthend"

    # 下个月 / next month
    if _has("下个月", "下月", "next month"):
        nm = anchor.month + 1 if anchor.month < 12 else 1
        ny = anchor.year if anchor.month < 12 else anchor.year + 1
        if target.year == ny and target.month == nm:
            return due_iso, "rel:nextmonth"

    # X 天内 / X 周内 / X 天后
    import re as _re
    m = _re.search(r"(\d+)\s*(?:天|day)\s*(内|后|within|later|after)?", haystack)
    if m:
        n = int(m.group(1))
        scope = m.group(2) or "内"
        if scope in ("内", "within"):
            if 0 <= delta <= n:
                return due_iso, f"rel:within_{n}d"
        else:
            if delta == n:
                return due_iso, f"rel:after_{n}d"
    m = _re.search(r"(\d+)\s*(?:周|week)\s*(内|后|within|later|after)?", haystack)
    if m:
        n = int(m.group(1))
        scope = m.group(2) or "内"
        if scope in ("内", "within"):
            if 0 <= delta <= n * 7:
                return due_iso, f"rel:within_{n}w"
        else:
            if abs(delta - n * 7) <= 3:
                return due_iso, f"rel:after_{n}w"

    return None, "no_grounding"


def scan_card_llm(
    card: dict[str, Any], client: httpx.Client, horizon_days: int = 365,
) -> DdlCandidate | None:
    text = card.get("text") or ""
    payload = _strip_card_text(text) or {}
    if not payload:
        return None
    narrative = payload.get("narrative") or ""
    if not narrative.strip():
        return None
    # FIX 2026-05-09: anchor must be when_end (real message time) NOT
    # mentioned_at (the time the card was ingested into v5). Otherwise the
    # LLM resolves "下周" relative to ingestion time, e.g. card ingested
    # 2026-05-08 with message dated 2026-03-26 → "next class" → 2026-05-15
    # instead of late March.
    anchor_raw = (
        payload.get("when_end")
        or payload.get("when_start")
        or card.get("mentioned_at")
        or card.get("date")
        or ""
    )
    try:
        anchor = dt.datetime.fromisoformat(anchor_raw.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        anchor = dt.date.today()

    verdict = call_llm(payload, anchor, client)
    if not verdict or not verdict.get("has_ddl"):
        return None

    # FIX 2026-05-09: downgrade confidence when the LLM hedges in its own
    # reason field (e.g. "假设下周五"). Hedge means LLM made up the date.
    reason_text = str(verdict.get("reason") or "")
    if any(kw in reason_text for kw in HEDGE_KEYWORDS):
        verdict["confidence"] = min(float(verdict.get("confidence") or 0.5), 0.30)
        verdict["reason"] = "[hedge_demoted] " + reason_text

    # FIX 2026-05-09: if who_for is a non-user audience AND the user is not
    # explicitly the actor (speaker_role != "self"), this is an event the
    # user merely observes — not a personal DDL.
    who_for = str(verdict.get("who_for") or "")
    speaker_role = str(payload.get("speaker_role") or "")
    if any(role in who_for for role in NON_USER_AUDIENCE) and speaker_role != "self":
        verdict["confidence"] = min(float(verdict.get("confidence") or 0.5), 0.35)
        verdict["reason"] = "[non_user_actor] " + str(verdict.get("reason") or "")

    due_raw = verdict.get("due_iso")
    due_iso = None
    grounding = ""
    if isinstance(due_raw, str) and due_raw and due_raw.lower() != "null":
        # FIX 2026-05-09 (root cause): every due_iso must be groundable in
        # the source narrative + evidence_quotes. If the LLM's proposed
        # date has no literal substring match AND no consistent anchor-
        # relative phrase, treat it as a hallucination and drop the date.
        verified, grounding = _verify_due_grounded(due_raw, anchor, payload)
        if verified is None:
            # Drop the proposed date; demote to "no concrete due" with low conf.
            verdict["confidence"] = min(float(verdict.get("confidence") or 0.5), 0.30)
            verdict["reason"] = f"[ungrounded_date_dropped raw={due_raw}] " + str(verdict.get("reason") or "")
            due_iso = None
        else:
            try:
                d = dt.date.fromisoformat(verified)
            except ValueError:
                d = None
            today = dt.date.today()
            if d is None:
                due_iso = None
            elif d < today - dt.timedelta(days=14):
                return None  # past, drop entirely
            elif (d - today).days > horizon_days:
                return None  # too far in future
            else:
                due_iso = d.isoformat()

    tags = card.get("tags") or []
    src = next((t.split(":", 1)[1] for t in tags if t.startswith("src:")), "")
    if not src:
        src = next((t.split(":", 1)[1] for t in tags if t.startswith("source:")), "unknown")

    return DdlCandidate(
        card_id=card.get("id", ""),
        has_ddl=True,
        due_iso=due_iso,
        what=str(verdict.get("what") or "")[:200],
        who_for=str(verdict.get("who_for") or "")[:80],
        confidence=float(verdict.get("confidence") or 0.5),
        reason=str(verdict.get("reason") or "")[:200],
        source=src,
        salience=float(payload.get("salience") or 0.0),
        types=payload.get("types") or [],
        narrative_head=narrative[:240],
        mentioned_at=card.get("mentioned_at", ""),
        when_end=payload.get("when_end"),
        detector=str(verdict.get("_detector") or "deepseek-chat"),
        narrative_full=narrative[:2000],
        evidence_quotes=[str(q)[:300] for q in (payload.get("evidence_quotes") or [])][:10],
        speaker_role=str(payload.get("speaker_role") or ""),
        grounding_evidence=grounding,
        anchor_iso=anchor.isoformat(),
    )


# ---------- Scan driver ----------


def scan_all(
    bank: str = DEFAULT_BANK,
    since_days: int = 90,
    horizon_days: int = 365,
    max_pages: int | None = None,
    max_cards: int | None = None,
    workers: int = 4,
    out_path: Path | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    out_path = out_path or (OUT_DIR / "ddl_inbox.jsonl")
    progress = ProgressTracker(PROGRESS_PATH) if resume else ProgressTracker(Path(os.devnull))
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)

    counters = {"seen": 0, "skipped_done": 0, "no_payload": 0, "asked_llm": 0,
                "true_positive": 0, "false_negative": 0, "errors": 0}

    # FIX 2026-05-09: pagination overlap was producing duplicate (cid, card)
    # entries causing every card to be scanned twice — wasted half the API
    # budget and inflated the inbox.
    seen_ids: list[str] = []
    seen_set: set[str] = set()
    for card in iter_cards(bank=bank, since=since, max_pages=max_pages):
        cid = card.get("id") or ""
        if not cid or cid in seen_set:
            continue
        seen_set.add(cid)
        counters["seen"] += 1
        if resume and progress.is_done(cid):
            counters["skipped_done"] += 1
            continue
        seen_ids.append(cid)
        if max_cards and len(seen_ids) >= max_cards:
            break

    print(f"[scan] queued {len(seen_ids)} cards (skipped {counters['skipped_done']} done; total seen {counters['seen']})", flush=True)

    write_lock = threading.Lock()
    stats_lock = threading.Lock()

    def _worker(card: dict[str, Any]) -> None:
        with httpx.Client(timeout=90.0) as client:
            try:
                result = scan_card_llm(card, client, horizon_days=horizon_days)
            except Exception as e:
                with stats_lock:
                    counters["errors"] += 1
                print(f"[err] card={card.get('id','?')} {e!r}", flush=True)
                return
            with stats_lock:
                counters["asked_llm"] += 1
                if result:
                    counters["true_positive"] += 1
                else:
                    counters["false_negative"] += 1
            if result:
                with write_lock:
                    with out_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
            progress.mark(card.get("id", ""))

    cards_by_id = {}
    for card in iter_cards(bank=bank, since=since, max_pages=max_pages):
        cid = card.get("id") or ""
        if cid in seen_ids:
            cards_by_id[cid] = card

    flush_every = max(1, len(seen_ids) // 20) if seen_ids else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_worker, cards_by_id[cid]): cid for cid in seen_ids if cid in cards_by_id}
        done_count = 0
        t0 = time.time()
        for fut in as_completed(futures):
            done_count += 1
            if done_count % flush_every == 0:
                progress.flush()
                elapsed = time.time() - t0
                rate = done_count / elapsed if elapsed > 0 else 0
                print(f"[progress] {done_count}/{len(seen_ids)} ({rate:.2f} card/s) "
                      f"tp={counters['true_positive']} fn={counters['false_negative']}",
                      flush=True)
    progress.flush()

    summary = {
        "ok": True,
        "bank": bank,
        "since_days": since_days,
        "horizon_days": horizon_days,
        "counters": counters,
        "out": str(out_path),
        "progress": str(PROGRESS_PATH),
    }
    return summary


def cli(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser("ddl_scanner")
    p.add_argument("--bank", default=DEFAULT_BANK)
    p.add_argument("--since-days", type=int, default=90)
    p.add_argument("--horizon-days", type=int, default=365)
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--max-cards", type=int, default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--clean", action="store_true",
                   help="erase ddl_inbox.jsonl before scanning")
    args = p.parse_args(argv)

    out = args.out or (OUT_DIR / "ddl_inbox.jsonl")
    if args.clean:
        if out.exists():
            out.unlink()
        if PROGRESS_PATH.exists():
            PROGRESS_PATH.unlink()

    summary = scan_all(
        bank=args.bank,
        since_days=args.since_days,
        horizon_days=args.horizon_days,
        max_pages=args.max_pages,
        max_cards=args.max_cards,
        workers=args.workers,
        out_path=out,
        resume=not args.no_resume,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
