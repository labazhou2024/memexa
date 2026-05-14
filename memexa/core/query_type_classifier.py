"""Query-type log instrumentation (no gating).

U2 of long_term_plan_v3_FINAL.md (chat-graph). 6-class deterministic
regex+keyword classifier + atomic-append jsonl log writer. Wired into
keyword_router.main() as a non-blocking UserPromptSubmit side-effect.

The log feeds CEO visibility (per CEO directive 2026-05-01); it does
NOT gate plans (10/10 LIVE recall on commit 58855c0 已实证 contact-query
utility). Future U12 promotion may consume this log.

Classes
-------
status            "现在/当前/状况/在做什么/进展如何"
progress          "做到哪了/进度/完成度/如何/到哪步"
ddl               "DDL/截止/什么时候交/期限/到期"
contact_fact      "<name>/谁/电话/学号/邮箱/联系方式"
cross_aggregate   "全部/统计/汇总/平均/总共/列出"
lifelog           "上周/昨天/最近/历史/记得/上次"

Privacy
-------
- session_id: stored as sha256[:8] only.
- prompt: stored as sha256[:16] + length only. NEVER raw text.

CLI
---
    python -m memexa.core.query_type_classifier classify "<prompt>"
    python -m memexa.core.query_type_classifier acc <corpus.jsonl>

Env kill-switch: ``MEMEXA_QUERY_TYPE_LOG=0`` disables hook side-effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

LABELS = (
    "status",
    "progress",
    "ddl",
    "contact_fact",
    "cross_aggregate",
    "lifelog",
)

_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    # ddl: high-precision deadline anchors
    ("ddl", re.compile(r"\b(ddl|deadline)\b", re.I), 5),
    ("ddl", re.compile(r"截止|期限|到期|交[作业稿件论文报告]|[何什][时么]时候.*[交完成截]"), 5),
    ("ddl", re.compile(r"还有几天|剩多少天|多少天.*交"), 4),
    # cross_aggregate: list/stats/sum verbs
    ("cross_aggregate", re.compile(r"全部|所有|统计|汇总|累计|平均|总[共计数和量]|多少个|几个"), 4),
    ("cross_aggregate", re.compile(r"列[出表][一-龥]*|分布|占比|比例"), 3),
    # contact_fact: explicit contact-info nouns
    ("contact_fact", re.compile(r"电话|手机[号]?|微信号|qq\s*号|邮箱|联系方式|学号|工号|地址(?!栏)", re.I), 5),
    ("contact_fact", re.compile(r"谁(?:是|的|有)|是谁|哪[位个]"), 3),
    # lifelog: temporal recall anchors
    # `历史(?!记录|纪录)` excludes "浏览历史记录" / "登录历史纪录" UI strings.
    ("lifelog", re.compile(r"昨[天日]|前[天日]|上[周个]?(?:周|月)|最近|之前|上次|历史(?!记录|纪录)|记[得不]|回忆"), 4),
    ("lifelog", re.compile(r"\b(yesterday|last\s+(?:week|month|time))\b", re.I), 4),
    # progress: completion/state-of-work
    ("progress", re.compile(r"做到[哪那][里步]|进度|完成度|完成[了到]|搞[到完]|到哪步|进展(?:如何|怎么样|到哪)"), 4),
    ("progress", re.compile(r"\b(progress|how\s+far|status\s+of)\b", re.I), 3),
    # status: present-tense state (lower priority than progress)
    ("status", re.compile(r"现在|当前|目前|此刻|正在"), 3),
    ("status", re.compile(r"在(?:做|忙|搞|写|看)什么|状态(?:如何|怎么样)|状况"), 3),
)

_CLEAN_RE = re.compile(r"```[\s\S]*?```|https?://\S+|<[^>]+>")


def _clean(prompt: str) -> str:
    return _CLEAN_RE.sub(" ", prompt or "").strip()


def classify(prompt: str) -> str:
    """Return one of LABELS for the given prompt; unmatched → 'status'."""
    text = _clean(prompt)
    if not text:
        return "status"
    scores: dict[str, int] = {}
    for label, pat, weight in _RULES:
        if pat.search(text):
            scores[label] = scores.get(label, 0) + weight
    if not scores:
        return "status"
    # Tie-break by LABELS order so output is deterministic.
    best = max(scores.values())
    for label in LABELS:
        if scores.get(label, 0) == best:
            return label
    return "status"


def _log_path() -> Path:
    here = Path(__file__).resolve()
    # memexa/core/query_type_classifier.py → memexa/data/
    return here.parents[2] / "data" / "query_type_log.jsonl"


def log_query_type(
    prompt: str,
    session_id: str = "",
    label: str | None = None,
    ts: float | None = None,
) -> str:
    """Append one classification event to data/query_type_log.jsonl.

    Returns the chosen label. Errors are caller's to suppress
    (see keyword_router wiring, which wraps in try/except).
    """
    # str() guard: hook stdin JSON could deliver non-string session_id
    # (int/dict/None); .encode() would crash. Coerce defensively.
    sid = str(session_id) if session_id is not None else ""
    raw = str(prompt) if prompt is not None else ""
    label = label or classify(raw)
    record = {
        "ts": ts if ts is not None else time.time(),
        "session_id_hash": hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8],
        "label": label,
        "prompt_len": len(raw),
        "prompt_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
    }
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    # trace event (best-effort; do NOT raise)
    try:
        from memexa.core import trace_sink

        trace_sink.emit("query_type_logged", {"label": label, "prompt_len": record["prompt_len"]})
    except Exception:
        pass
    return label


def accuracy(corpus_lines: Iterable[str]) -> tuple[float, int, int]:
    """Score classifier against a jsonl corpus of {prompt,label}."""
    total = 0
    correct = 0
    for raw in corpus_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        gold = obj.get("label")
        prompt = obj.get("prompt", "")
        if gold not in LABELS:
            continue
        total += 1
        if classify(prompt) == gold:
            correct += 1
    if total == 0:
        return 0.0, 0, 0
    return correct / total, correct, total


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: query_type_classifier {classify <prompt>|acc <corpus.jsonl>}")
        return 0
    cmd = argv[0]
    if cmd == "classify":
        if len(argv) < 2:
            print("classify needs a prompt arg", file=sys.stderr)
            return 2
        print(classify(argv[1]))
        return 0
    if cmd == "acc":
        if len(argv) < 2:
            print("acc needs a corpus.jsonl path", file=sys.stderr)
            return 2
        path = Path(argv[1])
        if not path.is_file():
            print(f"corpus not found: {path}", file=sys.stderr)
            return 2
        with open(path, "r", encoding="utf-8") as fh:
            acc, ok, n = accuracy(fh)
        print(json.dumps({"accuracy": round(acc, 4), "correct": ok, "total": n}))
        return 0
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
