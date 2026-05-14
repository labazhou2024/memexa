"""
Auto Scheduler — turn DDL candidates from ddl_scanner into Calendar events.

Pipeline:
  ddl_inbox.jsonl  ─►  dedup + score + conflict-check  ─►  three buckets:
    1. AUTO_PENDING   conf ≥ AUTO_THRESH AND salience ≥ 0.6  → CEO must still
                      explicitly approve before any calendar write happens.
    2. REVIEW         AUTO_THRESH > conf ≥ MIN_THRESH         → CEO review queue
    3. DISCARDED      conf < MIN_THRESH                         → audit log

Approval is a SEPARATE command. We never auto-write to Calendar without an
explicit `auto_scheduler.py approve --commit` invocation.

Output files (data/calendar_planning/):
  pending_calendar_events.json     # AUTO_PENDING + REVIEW combined
  discarded_low_conf.jsonl         # audit trail
  approved_events.jsonl            # after `approve --commit` runs
  approval_history.jsonl           # CEO action log
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
PLAN_DIR = ROOT / "data" / "calendar_planning"
PLAN_DIR.mkdir(parents=True, exist_ok=True)

INBOX_PATH = PLAN_DIR / "ddl_inbox.jsonl"
PENDING_PATH = PLAN_DIR / "pending_calendar_events.json"
DISCARD_PATH = PLAN_DIR / "discarded_low_conf.jsonl"
APPROVED_PATH = PLAN_DIR / "approved_events.jsonl"
HISTORY_PATH = PLAN_DIR / "approval_history.jsonl"

AUTO_THRESH = float(os.environ.get("MEMEX_DDL_AUTO_THRESH", "0.75"))
MIN_THRESH = float(os.environ.get("MEMEX_DDL_MIN_THRESH", "0.40"))
DEFAULT_HORIZON_DAYS = int(os.environ.get("MEMEX_DDL_HORIZON", "60"))

# Sources that represent the user's real life (personal DDLs come from these).
USER_SOURCES = {"wechat", "qq", "email", "browser_search", "browser_session"}
# Sources that are system / dev work — auto_pending demoted to review.
SYSTEM_SOURCES = {"claude_code", "md_legacy", "stop_hook", "doc"}


@dataclass
class PlannedEvent:
    plan_id: str
    bucket: str  # "auto_pending" | "review"
    summary: str
    due_iso: str
    start_iso: str
    end_iso: str
    confidence: float
    salience: float
    priority: int  # 0 highest .. 3 lowest
    why: str
    source_card_ids: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # wechat/qq/...
    types: list[str] = field(default_factory=list)
    raw_what: str = ""
    raw_who_for: str = ""
    narrative_head: str = ""
    detector: str = ""
    ddl_only: bool = True  # False = anchor / event with start+end
    conflict_warn: str = ""


# ---------- helpers ----------


def _norm_what(what: str) -> str:
    s = (what or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\(\)（）【】《》\[\]]+", "", s)
    return s


def _stable_id(due_iso: str, what_norm: str) -> str:
    h = hashlib.sha1(f"{due_iso}|{what_norm}".encode("utf-8")).hexdigest()[:14]
    return f"plan_{h}"


def _priority(salience: float, confidence: float, days_to_due: int, types: list[str]) -> int:
    score = salience * 0.5 + confidence * 0.3
    if any(t in {"directive", "decision"} for t in types):
        score += 0.10
    if days_to_due <= 1:
        score += 0.20
    elif days_to_due <= 3:
        score += 0.10
    elif days_to_due > 30:
        score -= 0.10
    if score >= 0.85:
        return 0
    if score >= 0.65:
        return 1
    if score >= 0.45:
        return 2
    return 3


def _compose_summary(what: str, sources: Iterable[str], detector: str) -> str:
    label = (what or "").strip() or "(待办)"
    if len(label) > 50:
        label = label[:50] + "..."
    return f"📌 {label}"


def _compose_notes(events: list[dict]) -> str:
    lines = ["[memex auto-planned] DDL aggregated from graph cards."]
    for e in events[:5]:
        snippet = (e.get("narrative_head") or e.get("reason") or "")[:160]
        cid = e.get("card_id", "")[:12]
        lines.append(f"- card={cid} src={e.get('source','')} sal={e.get('salience',0):.2f} "
                     f"conf={e.get('confidence',0):.2f}")
        if snippet:
            lines.append(f"  ╰─ {snippet}")
    if len(events) > 5:
        lines.append(f"... {len(events) - 5} more cards merged into this DDL")
    lines.append("")
    lines.append(f"detector: {events[0].get('detector','')}")
    lines.append(f"reason: {events[0].get('reason','')[:200]}")
    return "\n".join(lines)


# ---------- pipeline ----------


def load_inbox(path: Path = INBOX_PATH) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def dedup_and_score(
    inbox: list[dict], horizon_days: int = DEFAULT_HORIZON_DAYS
) -> tuple[list[PlannedEvent], list[dict]]:
    """Group by (due_iso, normalized what). Discard low-conf. Return (events, discarded)."""
    today = dt.date.today()
    discarded: list[dict] = []

    groups: dict[str, list[dict]] = {}
    for c in inbox:
        if not c.get("has_ddl"):
            continue
        due = c.get("due_iso")
        if not due:
            # cards without resolvable date go to review with synthetic due = anchor + 7d
            try:
                anchor = dt.datetime.fromisoformat((c.get("mentioned_at") or "").replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                anchor = today
            due = (anchor + dt.timedelta(days=7)).isoformat()
            c = {**c, "due_iso": due, "_imputed_due": True}
        try:
            d = dt.date.fromisoformat(due)
        except ValueError:
            discarded.append({"reason": "bad_due_iso", **c})
            continue
        if d < today - dt.timedelta(days=2):
            discarded.append({"reason": "past_due", **c})
            continue
        if (d - today).days > horizon_days:
            discarded.append({"reason": "beyond_horizon", **c})
            continue
        if (c.get("confidence") or 0) < MIN_THRESH:
            discarded.append({"reason": "low_conf", **c})
            continue
        key = f"{due}|{_norm_what(c.get('what',''))}"
        groups.setdefault(key, []).append(c)

    planned: list[PlannedEvent] = []
    for key, members in groups.items():
        members_sorted = sorted(members, key=lambda x: -float(x.get("confidence") or 0))
        head = members_sorted[0]
        due_iso = head.get("due_iso", "")
        d = dt.date.fromisoformat(due_iso)
        days_to_due = (d - today).days

        max_conf = max(float(m.get("confidence") or 0) for m in members)
        max_sal = max(float(m.get("salience") or 0) for m in members)
        avg_conf = sum(float(m.get("confidence") or 0) for m in members) / len(members)
        types_union = sorted({t for m in members for t in (m.get("types") or [])})

        prio = _priority(max_sal, max_conf, days_to_due, types_union)

        # default DDL = all-day on the day, 09:00–10:00 fallback if not all_day
        # We default to "all_day=true" so the user sees it as a deadline marker.
        start_dt = dt.datetime.combine(d, dt.time(9, 0))
        end_dt = dt.datetime.combine(d, dt.time(10, 0))

        sources = sorted({m.get("source", "") for m in members if m.get("source")})
        any_user_src = any(s in USER_SOURCES for s in sources)
        all_system_src = sources and all(s in SYSTEM_SOURCES for s in sources)

        if max_conf >= AUTO_THRESH and max_sal >= 0.6 and not head.get("_imputed_due") and any_user_src:
            bucket = "auto_pending"
        elif all_system_src and max_sal < 0.7:
            # downgrade noisy claude_code / md_legacy commitments
            bucket = "review_system"
        else:
            bucket = "review"
        ev = PlannedEvent(
            plan_id=_stable_id(due_iso, _norm_what(head.get("what", ""))),
            bucket=bucket,
            summary=_compose_summary(head.get("what", ""), sources, head.get("detector", "")),
            due_iso=due_iso,
            start_iso=start_dt.isoformat(timespec="minutes"),
            end_iso=end_dt.isoformat(timespec="minutes"),
            confidence=round(max_conf, 3),
            salience=round(max_sal, 3),
            priority=prio,
            why=head.get("reason", "")[:200],
            source_card_ids=[m.get("card_id", "") for m in members],
            sources=sources,
            types=types_union,
            raw_what=head.get("what", ""),
            raw_who_for=head.get("who_for", ""),
            narrative_head=head.get("narrative_head", "")[:240],
            detector=head.get("detector", ""),
            ddl_only=True,
        )
        planned.append(ev)

    planned.sort(key=lambda x: (x.priority, x.due_iso))
    return planned, discarded


def conflict_check(events: list[PlannedEvent], lookahead_days: int = 60) -> list[PlannedEvent]:
    """Mark events that look like duplicates of existing Calendar items."""
    try:
        from memex.integrations.mac_calendar import read_events, DEFAULT_PLAN_CALENDAR
    except ImportError:
        return events
    try:
        existing = read_events(
            calendars=[DEFAULT_PLAN_CALENDAR, "个人"],
            start=dt.datetime.now() - dt.timedelta(days=2),
            end=dt.datetime.now() + dt.timedelta(days=lookahead_days),
        )
    except Exception as e:
        # Best effort; don't fail the whole pipeline if SSH glitches.
        for ev in events:
            ev.conflict_warn = f"conflict_check_skipped: {e!r}"
        return events
    existing_keys = {
        (ev.calendar, ev.summary.strip(), ev.start_iso[:10]) for ev in existing
    }
    for ev in events:
        if ("memex-自动规划", ev.summary.strip(), ev.due_iso) in existing_keys:
            ev.conflict_warn = "duplicate_in_plan_calendar"
        elif ("个人", ev.summary.strip(), ev.due_iso) in existing_keys:
            ev.conflict_warn = "duplicate_in_personal_calendar"
    return events


def write_pending(events: list[PlannedEvent], path: Path = PENDING_PATH) -> None:
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "total": len(events),
        "auto_pending_count": sum(1 for e in events if e.bucket == "auto_pending"),
        "review_count": sum(1 for e in events if e.bucket == "review"),
        "review_system_count": sum(1 for e in events if e.bucket == "review_system"),
        "events": [asdict(e) for e in events],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_discarded(rows: list[dict], path: Path = DISCARD_PATH) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------- approval / commit ----------


def approve_and_write(
    plan_ids: list[str] | None = None,
    commit: bool = False,
    bucket_filter: str | None = None,
    pending_path: Path = PENDING_PATH,
) -> dict:
    """Push approved events to Mac Calendar.

    plan_ids = None means "all events whose bucket matches bucket_filter".
    commit=False is a dry run.
    """
    if not pending_path.exists():
        return {"ok": False, "error": "no pending file"}
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    todo: list[dict] = []
    skip: list[dict] = []
    for e in pending["events"]:
        if plan_ids and e["plan_id"] not in plan_ids:
            continue
        if bucket_filter and e["bucket"] != bucket_filter:
            continue
        if e.get("conflict_warn", "").startswith("duplicate_"):
            skip.append({"reason": "duplicate", **e})
            continue
        todo.append(e)
    actions: list[dict] = []
    if not todo:
        return {"ok": True, "to_write": 0, "actions": [], "skipped": skip}

    if not commit:
        return {"ok": True, "dry_run": True, "to_write": len(todo),
                "would_write": [{"plan_id": e["plan_id"], "summary": e["summary"],
                                 "due_iso": e["due_iso"]} for e in todo],
                "skipped": skip}

    from memex.integrations.mac_calendar import (
        ensure_plan_calendar, create_event, DEFAULT_PLAN_CALENDAR,
    )
    ensure_plan_calendar()
    for e in todo:
        try:
            start = dt.datetime.fromisoformat(e["start_iso"])
            end = dt.datetime.fromisoformat(e["end_iso"])
            uid = create_event(
                calendar=DEFAULT_PLAN_CALENDAR,
                summary=e["summary"],
                start=start,
                end=end,
                notes=_compose_notes_from_event(e),
                all_day=True,
                uid=e["plan_id"],
            )
            actions.append({"plan_id": e["plan_id"], "ok": True, "uid": uid,
                            "summary": e["summary"], "due_iso": e["due_iso"]})
        except Exception as ex:
            actions.append({"plan_id": e["plan_id"], "ok": False,
                            "error": repr(ex)[:200],
                            "summary": e["summary"]})

    # Append to approved_events.jsonl + approval_history.jsonl
    with APPROVED_PATH.open("a", encoding="utf-8") as f:
        for a in actions:
            if a.get("ok"):
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "commit": True,
            "to_write": len(todo),
            "actions": actions,
            "skipped_count": len(skip),
        }, ensure_ascii=False) + "\n")

    return {"ok": True, "committed": True,
            "written": sum(1 for a in actions if a.get("ok")),
            "failed": sum(1 for a in actions if not a.get("ok")),
            "skipped": len(skip),
            "actions": actions}


def _compose_notes_from_event(e: dict) -> str:
    lines = [
        f"[memex auto-planned]",
        f"plan_id: {e.get('plan_id')}",
        f"due: {e.get('due_iso')}  conf={e.get('confidence')}  sal={e.get('salience')}",
        f"why: {e.get('why', '')[:200]}",
        f"narrative: {e.get('narrative_head', '')[:240]}",
        f"sources: {', '.join(e.get('sources') or [])}",
        f"merged cards: {len(e.get('source_card_ids') or [])}",
        f"detector: {e.get('detector', '')}",
    ]
    return "\n".join(lines)


# ---------- CLI ----------


def cli(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser("auto_scheduler")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan", help="dedup + score inbox into pending file")
    pl.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    pl.add_argument("--no-conflict-check", action="store_true")
    pl.add_argument("--inbox", type=Path, default=INBOX_PATH)
    sh = sub.add_parser("show", help="print pending events")
    sh.add_argument("--bucket", choices=["auto_pending", "review", "review_system", "all"], default="all")
    ap = sub.add_parser("approve", help="commit selected events to Calendar")
    ap.add_argument("--plan-ids", nargs="*", default=None)
    ap.add_argument("--bucket", choices=["auto_pending", "review"], default=None)
    ap.add_argument("--commit", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "plan":
        inbox = load_inbox(args.inbox)
        events, discarded = dedup_and_score(inbox, horizon_days=args.horizon_days)
        if not args.no_conflict_check:
            events = conflict_check(events, lookahead_days=args.horizon_days)
        write_pending(events)
        write_discarded(discarded)
        print(json.dumps({
            "ok": True,
            "inbox_size": len(inbox),
            "planned": len(events),
            "auto_pending": sum(1 for e in events if e.bucket == "auto_pending"),
            "review": sum(1 for e in events if e.bucket == "review"),
            "discarded": len(discarded),
            "pending_path": str(PENDING_PATH),
        }, ensure_ascii=False))
        return 0
    if args.cmd == "show":
        if not PENDING_PATH.exists():
            print("no pending file")
            return 1
        d = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        rows = [e for e in d["events"]
                if args.bucket == "all" or e["bucket"] == args.bucket]
        print(f"# {len(rows)} events  (auto_pending={d['auto_pending_count']}, "
              f"review={d['review_count']})")
        for e in rows:
            warn = f"  ⚠ {e['conflict_warn']}" if e.get("conflict_warn") else ""
            print(f"[{e['bucket']:13s}] p{e['priority']} due={e['due_iso']} "
                  f"conf={e['confidence']:.2f} sal={e['salience']:.2f} | {e['summary']}{warn}")
            print(f"    raw_what={e['raw_what']!r}")
            print(f"    src={e['sources']}  cards={len(e['source_card_ids'])}")
        return 0
    if args.cmd == "approve":
        result = approve_and_write(
            plan_ids=args.plan_ids,
            commit=args.commit,
            bucket_filter=args.bucket,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(cli())
