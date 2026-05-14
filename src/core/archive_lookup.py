"""Cross-source archive lookup for memory queries (L0 v5).

Spec: docs/l0_v5/MASTER_PLAN.md §1 (Layer 3 — 原文反查) + §4.5

Given a card's `batch_id` + `source`, return path to original raw content
(prompt.json for wechat, .eml for email, schedule entry for schedule, ...).

This module is the LAST mile of the query → graph → card → 原文 chain.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


SOURCE_ARCHIVE_ROOTS: Dict[str, Path] = {
    # source name → root dir relative to memex workspace
    "wechat": Path("data/extract_archive"),
    "email": Path("archive/email"),
    "sms": Path("archive/sms"),
    "schedule": Path(""),  # special: schedule_data.json[batch_id]
    "git": Path(""),       # special: git rev-parse
    "doc": Path("archive/docs"),
    "folder": Path("archive/folders"),
    "probe": Path("data/probe_archive"),
}


class ArchiveNotFound(FileNotFoundError):
    """Raised when card can't be reverse-mapped to original archive."""


def _try_wechat(batch_id: str, when_start: Optional[str]) -> Optional[Path]:
    """Search data/extract_archive for batch with this batch_id.

    Layout: data/extract_archive/<YYYY-MM-DD>/<batch_id>/prompt.json
    """
    root = SOURCE_ARCHIVE_ROOTS["wechat"]
    if not root.exists():
        return None

    # Best path: use when_start to narrow date
    if when_start:
        try:
            d = datetime.fromisoformat(when_start.replace("Z", "+00:00"))
            day = d.strftime("%Y-%m-%d")
            cand = root / day / batch_id / "prompt.json"
            if cand.exists():
                return cand
            # Fallback: try day ± 1 (timezone edge)
            for offset in (-1, 1):
                from datetime import timedelta
                day2 = (d + timedelta(days=offset)).strftime("%Y-%m-%d")
                cand2 = root / day2 / batch_id / "prompt.json"
                if cand2.exists():
                    return cand2
        except (ValueError, TypeError):
            pass

    # Fallback: scan all dates (slow)
    for date_dir in root.iterdir():
        if date_dir.is_dir():
            cand = date_dir / batch_id / "prompt.json"
            if cand.exists():
                return cand

    return None


def _try_email(batch_id: str) -> Optional[Path]:
    root = SOURCE_ARCHIVE_ROOTS["email"]
    if not root.exists():
        return None
    cand = root / batch_id / "raw.eml"
    if cand.exists():
        return cand
    cand = root / f"{batch_id}.eml"
    if cand.exists():
        return cand
    return None


def _try_schedule(batch_id: str) -> Optional[Dict[str, Any]]:
    """Schedule lives in schedule_data.json keyed by event id (= batch_id)."""
    sched_path = Path("../schedule_data.json")
    if not sched_path.exists():
        sched_path = Path("schedule_data.json")
    if not sched_path.exists():
        return None
    try:
        data = json.loads(sched_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # Schedule data structure varies; try common keys
    if isinstance(data, dict):
        if batch_id in data:
            return data[batch_id]
        if "events" in data:
            events = data["events"]
            if isinstance(events, list):
                for ev in events:
                    if ev.get("id") == batch_id:
                        return ev
            elif isinstance(events, dict) and batch_id in events:
                return events[batch_id]
    return None


def _try_git(batch_id: str) -> Optional[Dict[str, Any]]:
    """Git commit lookup."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "show", "--no-patch", "--format=%H%n%an%n%ae%n%aI%n%s", batch_id],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    parts = r.stdout.strip().split("\n", 4)
    if len(parts) < 5:
        return None
    return {
        "commit": parts[0],
        "author_name": parts[1],
        "author_email": parts[2],
        "author_date": parts[3],
        "subject": parts[4],
    }


def _try_doc(batch_id: str) -> Optional[Path]:
    root = SOURCE_ARCHIVE_ROOTS["doc"]
    if not root.exists():
        return None
    for cand in (
        root / batch_id / "raw.txt",
        root / f"{batch_id}.md",
        root / f"{batch_id}.txt",
    ):
        if cand.exists():
            return cand
    return None


def lookup_archive(
    source: str,
    batch_id: str,
    when_start: Optional[str] = None,
) -> Dict[str, Any]:
    """Reverse-map a card identifier to its archive entry.

    Returns dict with keys:
      - source
      - batch_id
      - archive_uri (str — file path or descriptor)
      - archive_kind (str — 'file', 'json_entry', 'git_commit')
      - content_preview (str | None — first ~500 chars when applicable)
      - raw (dict | None — parsed content for json/git)

    Raises ArchiveNotFound if cannot resolve.
    """
    out: Dict[str, Any] = {
        "source": source,
        "batch_id": batch_id,
        "archive_uri": None,
        "archive_kind": None,
        "content_preview": None,
        "raw": None,
    }

    if source == "wechat":
        path = _try_wechat(batch_id, when_start)
        if path:
            out["archive_uri"] = str(path)
            out["archive_kind"] = "file"
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                out["raw"] = doc
                # preview = first message
                msgs = doc.get("messages", [])
                if msgs:
                    first = msgs[0]
                    out["content_preview"] = (
                        f"[{first.get('ts','?')}]: {first.get('content','')[:300]}"
                    )
            except Exception as e:
                logger.warning(f"failed to parse {path}: {e}")
            return out

    elif source == "email":
        path = _try_email(batch_id)
        if path:
            out["archive_uri"] = str(path)
            out["archive_kind"] = "file"
            try:
                out["content_preview"] = path.read_text(encoding="utf-8")[:500]
            except Exception:
                pass
            return out

    elif source == "schedule":
        entry = _try_schedule(batch_id)
        if entry:
            out["archive_uri"] = f"schedule_data.json#{batch_id}"
            out["archive_kind"] = "json_entry"
            out["raw"] = entry
            out["content_preview"] = json.dumps(entry, ensure_ascii=False)[:500]
            return out

    elif source == "git":
        entry = _try_git(batch_id)
        if entry:
            out["archive_uri"] = f"git#{batch_id}"
            out["archive_kind"] = "git_commit"
            out["raw"] = entry
            out["content_preview"] = (
                f"{entry['author_name']} <{entry['author_email']}> "
                f"@ {entry['author_date']}\n{entry['subject']}"
            )
            return out

    elif source == "doc":
        path = _try_doc(batch_id)
        if path:
            out["archive_uri"] = str(path)
            out["archive_kind"] = "file"
            try:
                out["content_preview"] = path.read_text(encoding="utf-8")[:500]
            except Exception:
                pass
            return out

    raise ArchiveNotFound(
        f"Cannot resolve archive for source={source} batch_id={batch_id}"
    )


def lookup_archives_bulk(
    cards: List[Dict[str, Any]],
    fail_silently: bool = True,
) -> List[Dict[str, Any]]:
    """Look up archives for many cards.

    Each input dict needs `source`, `batch_id`, optionally `when_start`.
    Output items have `archive_resolved` flag + ArchiveLookup result merged in.
    """
    results: List[Dict[str, Any]] = []
    for c in cards:
        out: Dict[str, Any] = dict(c)
        try:
            archive = lookup_archive(
                source=c.get("source", "wechat"),
                batch_id=c.get("batch_id", ""),
                when_start=c.get("when_start"),
            )
            out.update(archive)
            out["archive_resolved"] = True
        except ArchiveNotFound as e:
            if fail_silently:
                out["archive_resolved"] = False
                out["archive_error"] = str(e)
            else:
                raise
        results.append(out)
    return results


__all__ = [
    "lookup_archive",
    "lookup_archives_bulk",
    "ArchiveNotFound",
    "SOURCE_ARCHIVE_ROOTS",
]
