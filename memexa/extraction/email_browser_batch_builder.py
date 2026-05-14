"""email_browser_batch_builder.py — Convert raw email/browser items
   into batches with prompt.json files compatible with phase1_pipeline.

Layout produced (mirrors data/extract_archive/<date>/<batch_sha>/):

    data/extract_archive_email_browser/
        <YYYY-MM-DD>/
            <batch_sha>/
                meta.json     — batch identity + source_kind
                raw.json      — list of source items in this batch
                prompt.json   — pre-built LLM prompt + classifier metadata
                # Stage A/B/C/D will write qwen.done / 27b.done / pair.done /
                # ingest.done sentinels HERE later.

This makes the batches "look like" wechat batches to the existing
phase1_pipeline, which already knows how to run Stage A/B/C/D given a
batch directory containing prompt.json. Only difference: source_kind +
prompt_id are 'email' / 'browser_session' / 'browser_search', so caller
can route to the right Stage A model + Stage B prompt.

Stop-condition: this module ONLY builds the queue. It does NOT call any
LLM. After build, run `phase1_pipeline_email_browser.py` (next module)
when GPU is allocated.

Public API:
    build_email_batches(account, since_iso, batch_size_emails=5)
      -> {'batches_created': N, 'archive_dir': Path, 'batch_dirs': [...]}

    build_browser_batches(browser_id, since_iso, session_max_minutes=30,
                          session_max_visits=30)
      -> same shape

    build_browser_search_batches(browser_id, since_iso, batch_size=50)
      -> same shape
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_RAW_EMAIL = _DATA / "raw_inputs" / "email"
_ARCHIVE = _DATA / "extract_archive_email_browser"
_CURSOR_DIR = _DATA / "cursors"


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def _emit(event: str, payload: dict) -> None:
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def _batch_sha(source_kind: str, key: str, ts: str) -> str:
    """Deterministic batch id from source + identifier + timestamp."""
    h = hashlib.sha256(f"{source_kind}|{key}|{ts}".encode("utf-8")).hexdigest()
    return h[:16]


def _write_batch(
    archive_root: Path,
    source_kind: str,
    prompt_id: str,
    when_start: datetime,
    items: List[Dict[str, Any]],
    context: Dict[str, Any],
    batch_key: str,
) -> Path:
    """Materialize one batch dir with meta.json + raw.json + prompt.json.

    Returns batch dir path.
    """
    from memexa.extraction.email_browser_prompts import get_prompt_builder

    date_dir = archive_root / when_start.strftime("%Y-%m-%d")
    sha = _batch_sha(source_kind, batch_key, when_start.isoformat())
    bdir = date_dir / sha
    bdir.mkdir(parents=True, exist_ok=True)

    # Resume-friendly: skip if all 3 already exist.
    if all((bdir / f).exists() for f in ("meta.json", "raw.json", "prompt.json")):
        return bdir

    meta = {
        "batch_sha": sha,
        "source_kind": source_kind,
        "prompt_id": prompt_id,
        "when_start_iso": when_start.isoformat(),
        "n_items": len(items),
        "context": context,
        "built_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    _atomic_write_json(bdir / "meta.json", meta)
    _atomic_write_json(bdir / "raw.json", items)

    builder = get_prompt_builder(prompt_id)
    prompt_text = builder(items, context)
    _atomic_write_json(bdir / "prompt.json", {
        "prompt": prompt_text,
        "prompt_id": prompt_id,
        "source_kind": source_kind,
        "classified_type": "informative",  # placeholder for Stage B router
        "classifier_confidence": 1.0,
        "routing_key": prompt_id,
        "memory_summary": "",
    })

    return bdir


# ────────────────────────────────────────────────────────────────────────
# Email batches: 1 thread or ≤N emails by sender per batch
# ────────────────────────────────────────────────────────────────────────

def build_email_batches(
    account: str,
    since_iso: str = "2026-01-01",
    batch_size_emails: int = 5,
) -> Dict[str, Any]:
    """Walk data/raw_inputs/email/<account>/ and build batches.

    Strategy:
      - Walk by day; within a day group by sender (from_raw); slice to
        batch_size_emails per batch.
      - This keeps thread continuity (same sender → likely same topic) +
        bounds prompt size.
    """
    src_root = _RAW_EMAIL / account
    if not src_root.exists():
        return {"batches_created": 0, "archive_dir": None, "batch_dirs": [],
                 "skipped": 0, "warning": f"no raw inputs at {src_root}"}

    since_dt = datetime.fromisoformat(since_iso + "T00:00:00+00:00") \
        if "T" not in since_iso else datetime.fromisoformat(since_iso)

    out_dirs: List[Path] = []
    skipped = 0

    for day_dir in sorted(src_root.glob("*/*")):  # day_dir = <date>/<folder_slug>
        # Actually layout is <date>/<folder_slug>/<uid>.json — group emails
        # within a day across all folders by sender.
        pass

    # Cleaner walk: glob 2-deep for *.json, group manually.
    by_day_sender: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for fp in src_root.rglob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        date_iso = data.get("internal_date_iso", "") or data.get("date_iso", "")
        if not date_iso:
            skipped += 1
            continue
        try:
            dt = datetime.fromisoformat(date_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            skipped += 1
            continue
        if dt < since_dt:
            continue
        day = dt.strftime("%Y-%m-%d")
        sender = (data.get("from_raw", "") or "unknown")[:80]
        by_day_sender.setdefault((day, sender), []).append(data)

    for (day, sender), emails in by_day_sender.items():
        emails.sort(key=lambda e: e.get("internal_date_iso", "") or "")
        for i in range(0, len(emails), batch_size_emails):
            slice_ = emails[i : i + batch_size_emails]
            try:
                first_dt = datetime.fromisoformat(
                    slice_[0].get("internal_date_iso", "")
                )
            except ValueError:
                first_dt = datetime.now(timezone.utc)
            ctx = {
                "account": account,
                "folder": slice_[0].get("folder", "INBOX"),
                "sender": sender,
                "n_emails": len(slice_),
            }
            bdir = _write_batch(
                _ARCHIVE,
                source_kind="email",
                prompt_id="email_v1",
                when_start=first_dt,
                items=slice_,
                context=ctx,
                batch_key=f"{account}|{sender}|{day}|{i}",
            )
            out_dirs.append(bdir)

    _emit("email_browser_batches_built", {
        "source_kind": "email", "account": account,
        "n_batches": len(out_dirs), "skipped": skipped,
    })
    return {
        "batches_created": len(out_dirs),
        "archive_dir": str(_ARCHIVE),
        "batch_dirs": [str(p) for p in out_dirs],
        "skipped": skipped,
    }


# ────────────────────────────────────────────────────────────────────────
# Browser session batches: cluster visits into 30-min sessions, ≤30 visits
# ────────────────────────────────────────────────────────────────────────

def build_browser_batches(
    browser_id: str,
    since_iso: str = "2026-01-01",
    until_iso: Optional[str] = None,
    session_idle_min: int = 5,
    session_max_minutes: int = 30,
    session_max_visits: int = 30,
    skip_internal_urls: bool = True,
) -> Dict[str, Any]:
    """Read browser visits, cluster into sessions, write batches.

    Clustering rule:
      - Two consecutive visits split into separate sessions if gap > session_idle_min.
      - A session caps at session_max_minutes total span OR session_max_visits.
    """
    from memexa.extraction.browser_history_reader import BrowserHistoryReader

    since_dt = datetime.fromisoformat(since_iso + "T00:00:00+00:00") \
        if "T" not in since_iso else datetime.fromisoformat(since_iso)
    until_dt = None
    if until_iso:
        until_dt = datetime.fromisoformat(until_iso + "T00:00:00+00:00") \
            if "T" not in until_iso else datetime.fromisoformat(until_iso)

    reader = BrowserHistoryReader()
    visits_dc = reader.read_visits(
        browser_id,
        since_unix=since_dt.timestamp(),
        until_unix=until_dt.timestamp() if until_dt else None,
        max_rows=100000,
    )

    # filter internal URLs
    visits = [asdict(v) for v in visits_dc]
    if skip_internal_urls:
        def _ok(u: str) -> bool:
            if u.startswith(("about:", "chrome://", "edge://", "lenovo://")):
                return False
            return True
        visits = [v for v in visits if _ok(v.get("url", ""))]

    visits.sort(key=lambda v: v["visit_time_unix"])

    # cluster
    sessions: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    last_t: Optional[float] = None
    cur_start_t: Optional[float] = None
    for v in visits:
        t = v["visit_time_unix"]
        if cur_start_t is None:
            cur_start_t = t
        idle = t - last_t if last_t else 0
        span = t - cur_start_t
        too_idle = idle > session_idle_min * 60
        too_long = span > session_max_minutes * 60
        too_big = len(cur) >= session_max_visits
        if cur and (too_idle or too_long or too_big):
            sessions.append(cur)
            cur = []
            cur_start_t = t
        cur.append(v)
        last_t = t
    if cur:
        sessions.append(cur)

    out_dirs: List[Path] = []
    for sidx, sess in enumerate(sessions):
        first_dt = datetime.fromtimestamp(
            sess[0]["visit_time_unix"], tz=timezone.utc
        ).astimezone()
        last_dt = datetime.fromtimestamp(
            sess[-1]["visit_time_unix"], tz=timezone.utc
        ).astimezone()
        span_min = max(1, int((last_dt - first_dt).total_seconds() / 60))
        ctx = {
            "browser_id": browser_id,
            "profile": sess[0].get("profile", ""),
            "session_idx": sidx,
            "session_start_iso": first_dt.isoformat(timespec="seconds"),
            "session_end_iso": last_dt.isoformat(timespec="seconds"),
            "span_minutes": span_min,
            "n_visits": len(sess),
        }
        bdir = _write_batch(
            _ARCHIVE,
            source_kind="browser_session",
            prompt_id="browser_session_v1",
            when_start=first_dt,
            items=sess,
            context=ctx,
            batch_key=f"{browser_id}|sess|{first_dt.isoformat()}",
        )
        out_dirs.append(bdir)

    _emit("email_browser_batches_built", {
        "source_kind": "browser_session", "browser_id": browser_id,
        "n_sessions": len(sessions), "n_visits": len(visits),
    })
    return {
        "batches_created": len(out_dirs),
        "archive_dir": str(_ARCHIVE),
        "batch_dirs": [str(p) for p in out_dirs],
        "n_visits_clustered": len(visits),
        "n_sessions": len(sessions),
    }


def build_browser_search_batches(
    browser_id: str,
    since_iso: str = "2026-01-01",
    batch_size: int = 50,
) -> Dict[str, Any]:
    """Read keyword searches, batch by N per LLM call."""
    from memexa.extraction.browser_history_reader import BrowserHistoryReader
    from dataclasses import asdict as _asdict

    since_dt = datetime.fromisoformat(since_iso + "T00:00:00+00:00") \
        if "T" not in since_iso else datetime.fromisoformat(since_iso)

    reader = BrowserHistoryReader()
    sd = reader.read_searches(browser_id, since_unix=since_dt.timestamp(),
                                max_rows=20000)
    items = [_asdict(s) for s in sd]
    items.sort(key=lambda s: s["visit_time_unix"])

    out_dirs: List[Path] = []
    for i in range(0, len(items), batch_size):
        slice_ = items[i : i + batch_size]
        first_dt = datetime.fromtimestamp(
            slice_[0]["visit_time_unix"], tz=timezone.utc
        ).astimezone()
        ctx = {
            "browser_id": browser_id,
            "batch_idx": i // batch_size,
            "n_searches": len(slice_),
        }
        bdir = _write_batch(
            _ARCHIVE,
            source_kind="browser_search",
            prompt_id="browser_search_v1",
            when_start=first_dt,
            items=slice_,
            context=ctx,
            batch_key=f"{browser_id}|search|{i}",
        )
        out_dirs.append(bdir)

    _emit("email_browser_batches_built", {
        "source_kind": "browser_search", "browser_id": browser_id,
        "n_searches": len(items), "n_batches": len(out_dirs),
    })
    return {
        "batches_created": len(out_dirs),
        "archive_dir": str(_ARCHIVE),
        "batch_dirs": [str(p) for p in out_dirs],
        "n_searches": len(items),
    }


# ────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────

def _cli(argv: List[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="email_browser_batch_builder")
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("email")
    pe.add_argument("--account", choices=["qq_email", "ustc_email", "all"],
                     default="all")
    pe.add_argument("--since", default="2026-01-01")
    pe.add_argument("--batch-size", type=int, default=5)

    pb = sub.add_parser("browser-session")
    pb.add_argument("--browser", required=True,
                     choices=["chrome", "edge", "lenovo"])
    pb.add_argument("--since", default="2026-01-01")
    pb.add_argument("--until", default=None)
    pb.add_argument("--session-max-min", type=int, default=30)
    pb.add_argument("--session-max-visits", type=int, default=30)

    ps = sub.add_parser("browser-search")
    ps.add_argument("--browser", required=True,
                     choices=["chrome", "edge", "lenovo"])
    ps.add_argument("--since", default="2026-01-01")
    ps.add_argument("--batch-size", type=int, default=50)

    args = p.parse_args(argv[1:])

    if args.cmd == "email":
        accounts = (["qq_email", "ustc_email"] if args.account == "all"
                     else [args.account])
        for acc in accounts:
            r = build_email_batches(acc, since_iso=args.since,
                                      batch_size_emails=args.batch_size)
            print(f"{acc}: {r['batches_created']} batches "
                  f"(skipped {r['skipped']})")
        return 0

    if args.cmd == "browser-session":
        r = build_browser_batches(
            args.browser, since_iso=args.since, until_iso=args.until,
            session_max_minutes=args.session_max_min,
            session_max_visits=args.session_max_visits,
        )
        print(f"{args.browser} sessions: {r['batches_created']} batches "
              f"({r['n_visits_clustered']} visits → {r['n_sessions']} sessions)")
        return 0

    if args.cmd == "browser-search":
        r = build_browser_search_batches(
            args.browser, since_iso=args.since, batch_size=args.batch_size
        )
        print(f"{args.browser} searches: {r['batches_created']} batches "
              f"({r['n_searches']} searches)")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
