"""browser_history_reader.py — Read Chromium-based browser History DBs.

Supported browsers (auto-detect installed):
  - Google Chrome
  - Microsoft Edge
  - Lenovo SLBrowser

All Chromium variants share the same SQLite schema (urls + visits +
keyword_search_terms). We hard-copy the History file before opening to
sidestep the SQLite write-lock the running browser holds.

Per CEO directive 2026-05-08:
  - Process Chrome too even though Default profile is currently empty
    (browser may be reinstalled / activated later)
  - NO privacy filter — full URLs including query strings
  - Cover all profiles (Default + Profile 1, 2, ...)

Public API:
    BrowserHistoryReader.list_browsers() -> dict[browser_id, profile_dirs]
    BrowserHistoryReader.read_visits(
        browser_id, since_utc, until_utc=None, max_rows=10000
    ) -> list[VisitEvent]
    BrowserHistoryReader.read_searches(...) -> list[SearchEvent]

Cursor file: data/cursors/browser_<browser_id>.json
  {
    "browser_id": "edge",
    "last_visit_time_chromium": 13362567893456789,  # us since 1601-01-01 UTC
    "last_visit_time_iso": "2026-05-08T08:19:28+08:00",
    "n_total_processed": 12043,
    "last_synced_at": "2026-05-08T17:30:00+08:00"
  }
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


# Chromium stores time as microseconds since 1601-01-01 UTC ("WebKit time").
# Unix epoch 1970-01-01 = 11644473600 seconds after WebKit epoch.
_CHROMIUM_EPOCH_DIFF_S = 11644473600


def chromium_us_to_unix(us: int) -> float:
    """Convert Chromium WebKit microseconds → Unix epoch seconds (float)."""
    return us / 1_000_000.0 - _CHROMIUM_EPOCH_DIFF_S


def unix_to_chromium_us(ts: float) -> int:
    """Convert Unix epoch seconds → Chromium WebKit microseconds."""
    return int((ts + _CHROMIUM_EPOCH_DIFF_S) * 1_000_000)


# Browser registry. Each entry: (browser_id, display_name, %LOCALAPPDATA% path).
# Profile dirs are auto-discovered (Default + Profile *).
_BROWSER_REGISTRY = [
    ("chrome",  "Google Chrome",      "Google/Chrome/User Data"),
    ("edge",    "Microsoft Edge",     "Microsoft/Edge/User Data"),
    ("lenovo",  "Lenovo SLBrowser",   "Lenovo/SLBrowser/User Data"),
    # Future-proof: add Brave/Vivaldi/QQBrowser/etc. here when discovered.
]


@dataclass
class VisitEvent:
    """One browser visit, normalized across Chromium variants."""
    browser_id: str
    profile: str
    url: str
    title: str
    visit_time_unix: float    # seconds since Unix epoch (UTC)
    visit_time_iso: str       # ISO 8601 with timezone
    visit_duration_s: float   # 0 if not recorded
    transition: int           # Chromium PAGE_TRANSITION_* enum
    transition_label: str     # human-readable: "link" / "typed" / "auto_bookmark" / ...
    visit_count: int          # cumulative for this URL across history
    typed_count: int          # cumulative typed
    is_from_search: bool
    search_term: Optional[str] = None
    referrer_url: Optional[str] = None


@dataclass
class SearchEvent:
    """One keyword search (extracted from urls.keyword_search_terms)."""
    browser_id: str
    profile: str
    keyword: str              # search term
    url: str                  # the search-results URL
    visit_time_unix: float
    visit_time_iso: str


# Chromium PAGE_TRANSITION enum (lower 8 bits of transition column).
# https://source.chromium.org/chromium/chromium/src/+/main:ui/base/page_transition_types.h
_TRANSITION_LABELS = {
    0: "link",
    1: "typed",
    2: "auto_bookmark",
    3: "auto_subframe",
    4: "manual_subframe",
    5: "generated",          # search box
    6: "auto_toplevel",
    7: "form_submit",
    8: "reload",
    9: "keyword",
    10: "keyword_generated",
}


def _transition_label(transition_int: int) -> str:
    """Map Chromium transition int → human label."""
    core = transition_int & 0xFF
    return _TRANSITION_LABELS.get(core, f"unknown_{core}")


def _is_search_transition(transition_int: int) -> bool:
    return (transition_int & 0xFF) in (5, 9, 10)


class BrowserHistoryReader:
    """Read Chromium-based browser histories without disturbing live browser."""

    def __init__(
        self,
        local_appdata: Optional[Path] = None,
        copy_dir: Optional[Path] = None,
    ):
        self.local_appdata = local_appdata or Path(
            os.environ.get("LOCALAPPDATA", "")
        )
        # Where to copy History files for safe reading
        self.copy_dir = copy_dir or (
            Path(tempfile.gettempdir()) / "memex_browser_copies"
        )
        self.copy_dir.mkdir(parents=True, exist_ok=True)

    # ----- discovery -----

    def list_browsers(self) -> List[Tuple[str, str, List[Path]]]:
        """Return [(browser_id, display_name, [profile_history_paths])].

        Only lists browsers actually present on disk with non-empty History.
        """
        out: List[Tuple[str, str, List[Path]]] = []
        for browser_id, display, rel in _BROWSER_REGISTRY:
            base = self.local_appdata / rel
            if not base.exists():
                continue
            profile_paths: List[Path] = []
            for entry in base.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name not in ("Default",) and not entry.name.startswith("Profile "):
                    continue
                hist = entry / "History"
                if hist.exists():
                    # Include even 0-byte files; caller decides whether to skip.
                    profile_paths.append(hist)
            if profile_paths:
                out.append((browser_id, display, profile_paths))
        return out

    # ----- safe-copy + read -----

    def _safe_copy_history(self, hist_path: Path) -> Path:
        """Copy History (and journal sibling) to copy_dir. Return copy path."""
        # Use a per-browser/profile name so concurrent readers don't collide.
        slug = (
            str(hist_path.parent.parent.name) + "_" + hist_path.parent.name
        ).replace(" ", "_")
        dst = self.copy_dir / f"{slug}_History.db"
        shutil.copy2(hist_path, dst)
        # Journal must be copied too if present (Chromium uses both)
        jrn = hist_path.parent / "History-journal"
        if jrn.exists():
            shutil.copy2(jrn, self.copy_dir / f"{slug}_History.db-journal")
        return dst

    def _open_ro(self, db_path: Path) -> sqlite3.Connection:
        """Open SQLite DB; falls back to non-uri if URI mode not allowed."""
        # Note: Windows + non-ASCII paths break URI mode. Use plain str path.
        c = sqlite3.connect(str(db_path), timeout=5.0)
        c.row_factory = sqlite3.Row
        return c

    # ----- public read methods -----

    def read_visits(
        self,
        browser_id: str,
        since_unix: float = 0.0,
        until_unix: Optional[float] = None,
        max_rows: int = 10000,
    ) -> List[VisitEvent]:
        """Read visits for a browser_id within [since, until)."""
        browsers = {bid: paths for bid, _, paths in self.list_browsers()}
        if browser_id not in browsers:
            return []

        out: List[VisitEvent] = []
        until_us = unix_to_chromium_us(until_unix) if until_unix else None
        since_us = unix_to_chromium_us(since_unix)

        for hist in browsers[browser_id]:
            if hist.stat().st_size == 0:
                continue
            profile = hist.parent.name
            try:
                copy = self._safe_copy_history(hist)
            except OSError:
                continue
            try:
                conn = self._open_ro(copy)
            except sqlite3.OperationalError:
                continue
            try:
                # Schema: visits joined to urls.
                # transition is bitfield; visit_duration is microseconds.
                sql = """
                SELECT v.visit_time, v.transition, v.visit_duration,
                       u.url, u.title, u.visit_count, u.typed_count,
                       v.from_visit
                FROM visits v
                JOIN urls u ON u.id = v.url
                WHERE v.visit_time >= ?
                """
                params: List[object] = [since_us]
                if until_us is not None:
                    sql += " AND v.visit_time < ?"
                    params.append(until_us)
                sql += " ORDER BY v.visit_time ASC LIMIT ?"
                params.append(max_rows)
                for row in conn.execute(sql, params):
                    vt_us = int(row["visit_time"])
                    vt_unix = chromium_us_to_unix(vt_us)
                    vt_iso = datetime.fromtimestamp(
                        vt_unix, tz=timezone.utc
                    ).astimezone().isoformat(timespec="seconds")
                    transition = int(row["transition"]) if row["transition"] is not None else 0
                    out.append(VisitEvent(
                        browser_id=browser_id,
                        profile=profile,
                        url=row["url"] or "",
                        title=row["title"] or "",
                        visit_time_unix=vt_unix,
                        visit_time_iso=vt_iso,
                        visit_duration_s=(int(row["visit_duration"] or 0)) / 1_000_000.0,
                        transition=transition,
                        transition_label=_transition_label(transition),
                        visit_count=int(row["visit_count"] or 0),
                        typed_count=int(row["typed_count"] or 0),
                        is_from_search=_is_search_transition(transition),
                        search_term=None,  # filled by read_searches if needed
                        referrer_url=None,
                    ))
            except sqlite3.DatabaseError as exc:
                print(f"[browser_reader] {browser_id}/{profile}: {exc!r}", file=sys.stderr)
            finally:
                conn.close()
        return out

    def read_searches(
        self,
        browser_id: str,
        since_unix: float = 0.0,
        until_unix: Optional[float] = None,
        max_rows: int = 5000,
    ) -> List[SearchEvent]:
        """Read keyword search terms for a browser_id."""
        browsers = {bid: paths for bid, _, paths in self.list_browsers()}
        if browser_id not in browsers:
            return []
        out: List[SearchEvent] = []
        since_us = unix_to_chromium_us(since_unix)
        until_us = unix_to_chromium_us(until_unix) if until_unix else None

        for hist in browsers[browser_id]:
            if hist.stat().st_size == 0:
                continue
            profile = hist.parent.name
            try:
                copy = self._safe_copy_history(hist)
            except OSError:
                continue
            try:
                conn = self._open_ro(copy)
            except sqlite3.OperationalError:
                continue
            try:
                sql = """
                SELECT k.term, u.url, u.last_visit_time
                FROM keyword_search_terms k
                JOIN urls u ON u.id = k.url_id
                WHERE u.last_visit_time >= ?
                """
                params: List[object] = [since_us]
                if until_us is not None:
                    sql += " AND u.last_visit_time < ?"
                    params.append(until_us)
                sql += " ORDER BY u.last_visit_time ASC LIMIT ?"
                params.append(max_rows)
                for row in conn.execute(sql, params):
                    vt_us = int(row["last_visit_time"])
                    vt_unix = chromium_us_to_unix(vt_us)
                    vt_iso = datetime.fromtimestamp(
                        vt_unix, tz=timezone.utc
                    ).astimezone().isoformat(timespec="seconds")
                    out.append(SearchEvent(
                        browser_id=browser_id,
                        profile=profile,
                        keyword=row["term"] or "",
                        url=row["url"] or "",
                        visit_time_unix=vt_unix,
                        visit_time_iso=vt_iso,
                    ))
            except sqlite3.DatabaseError:
                pass
            finally:
                conn.close()
        return out

    def cleanup_copies(self) -> int:
        """Remove temp History copies. Return number of files removed."""
        n = 0
        for f in self.copy_dir.glob("*_History.db*"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        return n


# ----- CLI -----

def _cli(argv: List[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="browser_history_reader")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list installed browsers")

    p_visits = sub.add_parser("visits", help="read visit events")
    p_visits.add_argument("--browser", required=True,
                          choices=[b[0] for b in _BROWSER_REGISTRY])
    p_visits.add_argument("--since", default="2026-01-01",
                          help="ISO date (default 2026-01-01)")
    p_visits.add_argument("--until", default=None)
    p_visits.add_argument("--max", type=int, default=20)

    p_search = sub.add_parser("searches")
    p_search.add_argument("--browser", required=True,
                          choices=[b[0] for b in _BROWSER_REGISTRY])
    p_search.add_argument("--since", default="2026-01-01")
    p_search.add_argument("--max", type=int, default=20)

    args = p.parse_args(argv[1:])
    r = BrowserHistoryReader()

    if args.cmd == "list":
        for bid, name, paths in r.list_browsers():
            print(f"{bid:8s} {name:25s}  profiles={len(paths)}")
            for hp in paths:
                sz = hp.stat().st_size
                print(f"           {hp}  ({sz} B)")
        return 0

    if args.cmd == "visits":
        since_unix = datetime.fromisoformat(args.since).replace(
            tzinfo=timezone.utc
        ).timestamp() if "T" not in args.since else \
            datetime.fromisoformat(args.since).timestamp()
        until_unix = None
        if args.until:
            until_unix = datetime.fromisoformat(args.until).replace(
                tzinfo=timezone.utc
            ).timestamp()
        events = r.read_visits(args.browser, since_unix=since_unix,
                                until_unix=until_unix, max_rows=args.max)
        print(f"# {len(events)} visits since {args.since}")
        for e in events:
            print(f"{e.visit_time_iso}  [{e.transition_label:12s}]  "
                  f"{e.title[:50]:50s}  {e.url[:100]}")
        return 0

    if args.cmd == "searches":
        since_unix = datetime.fromisoformat(args.since).replace(
            tzinfo=timezone.utc
        ).timestamp()
        events = r.read_searches(args.browser, since_unix=since_unix,
                                  max_rows=args.max)
        print(f"# {len(events)} searches since {args.since}")
        for e in events:
            print(f"{e.visit_time_iso}  '{e.keyword}'  {e.url[:80]}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
