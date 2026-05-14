"""
Staleness Audit (T9, plan v3.1, 2026-04-20)
============================================

Manages aging of pending_approvals entries and generates CEO bulk briefings
when items have been silent for >= 14 CEO-active-days.

Key invariant: promotions_silently_dropped_by_staleness == 0 ALWAYS.
Stale items are DEMOTED to low_queue (priority field), never removed.

CEO-active-day clock
--------------------
All "age" values use count_ceo_active_days(window_days=delta_calendar_days)
from session_heartbeat.  Wall-clock days are used only to bound the window
passed to count_ceo_active_days; the returned integer is the CEO-active-day age.

Demotion / Flagging thresholds
-------------------------------
  * age >= 3 CEO-active-days AND priority != 'low_queue'  -> demote to low_queue
  * age >= 14 CEO-active-days                              -> flag for bulk briefing

CLI
---
    python -m src.core.staleness_audit audit            # single pass, print summary
    python -m src.core.staleness_audit briefing         # generate + print path
    python -m src.core.staleness_audit metrics          # pretty status
    python -m src.core.staleness_audit apply <file>     # parse marks, call promote/reject
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Lazy imports with graceful fallback (mirrors promotion_engine pattern)
# ---------------------------------------------------------------------------
try:
    from src.core.promotion_engine import promote, reject  # type: ignore
    from src.core.promotion_engine import _load_pending, _save_pending  # type: ignore
except Exception:  # pragma: no cover
    def promote(*a, **k): return {"ok": False, "reason": "promotion_engine_unavailable"}  # type: ignore[no-redef]
    def reject(*a, **k): return False  # type: ignore[no-redef]
    def _load_pending() -> List[Dict[str, Any]]: return []  # type: ignore[no-redef]
    def _save_pending(items): pass  # type: ignore[no-redef]

try:
    from src.core.session_heartbeat import count_ceo_active_days  # type: ignore
    from src.core.session_heartbeat import ceo_days_since as _sh_ceo_days_since  # type: ignore
    _has_sh_helper = True
except Exception:  # pragma: no cover
    _has_sh_helper = False
    def count_ceo_active_days(window_days: int = 30, **_k) -> int:  # type: ignore[no-redef]
        return 0
    def _sh_ceo_days_since(ts_iso: str, now=None) -> int:  # type: ignore[no-redef]
        return 0

try:
    from src.core.trace_sink import write_trace_event  # type: ignore
except Exception:  # pragma: no cover
    def write_trace_event(*a, **k): return False  # type: ignore[no-redef]

# H4 (2026-04-20): shared filelock around pending_approvals.json so that
# concurrent promote()/audit_pending()/apply_bulk_ratings() do not lose
# updates. See memex/core/_pending_io.py.
try:
    from src.core._pending_io import (
        pending_approvals_lock as _pending_lock,
    )
except Exception:  # pragma: no cover
    from contextlib import contextmanager
    @contextmanager
    def _pending_lock(pending_file):  # type: ignore[no-redef]
        yield

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

def _resolve_data_dir() -> Path:
    default = Path(__file__).parent.parent / "data"
    env_override = os.environ.get("MEMEX_DATA_DIR")
    if env_override:
        try:
            p = Path(env_override).resolve()
            if not p.is_dir():
                return default
            workspace_root = Path(__file__).parent.parent.parent.parent.resolve()
            import tempfile as _t
            temp_root = Path(_t.gettempdir()).resolve()
            for allowed in (workspace_root, temp_root):
                try:
                    p.relative_to(allowed)
                    return p
                except ValueError:
                    continue
            return default
        except (OSError, ValueError):
            return default
    return default


_DATA_DIR = _resolve_data_dir()
_PENDING_APPROVALS_FILE = _DATA_DIR / "pending_approvals.json"
_BRIEFINGS_DIR = _DATA_DIR / "cto_briefings"

# Workspace root for harness_state.json (4 levels up from this file)
_WORKSPACE_ROOT = Path(__file__).parent.parent.parent.parent
_HARNESS_STATE_FILE = _WORKSPACE_ROOT / ".claude" / "config" / "harness_state.json"

_DEMOTE_THRESHOLD_CEO_DAYS = 3
_BRIEFING_THRESHOLD_CEO_DAYS = 14
_BRIEFING_MAX_ROWS = 30

_TERMINAL_STATUSES = {"approved", "rejected", "closed", "completed", "promoted_to_memory", "ceo_rejected"}

# ---------------------------------------------------------------------------
# Helper: parse ISO-8601 timestamp
# ---------------------------------------------------------------------------

def _parse_ts(raw: str) -> Optional[datetime]:
    """Tolerant ISO-8601 parse. Returns UTC-aware datetime or None."""
    if not raw:
        return None
    try:
        s = raw.replace("Z", "")
        if "+" in s[10:]:
            s = s.split("+", 1)[0]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# CEO-active-day helper
# ---------------------------------------------------------------------------

def ceo_days_since(ts_iso: str, now: Optional[datetime] = None) -> int:
    """Return the number of CEO-active-days between ts_iso and now.

    Delegates to session_heartbeat.ceo_days_since when available (preferred).
    Falls back to a local implementation using count_ceo_active_days directly.

    Uses count_ceo_active_days(window_days=delta_calendar_days) where
    delta_calendar_days = (now - ts_iso).days + 1.
    Returns 0 if ts_iso is unparseable.
    """
    if _has_sh_helper:
        return _sh_ceo_days_since(ts_iso, now=now)
    _now = now or _utc_now()
    enqueued = _parse_ts(ts_iso)
    if enqueued is None:
        return 0
    delta_days = max(1, (_now - enqueued).days + 1)
    try:
        return int(count_ceo_active_days(window_days=delta_days))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------

def _is_active(item: Dict[str, Any]) -> bool:
    """Item counts as active (not yet terminally resolved)."""
    status = (item.get("status") or "pending").lower()
    return status not in _TERMINAL_STATUSES


def audit_pending(now_active_days: Optional[int] = None) -> dict:
    """Single-pass audit over pending_approvals.json.

    For each pending row:
      - age_active_days = CEO-active-days since enqueued_at
      - if age >= 3 AND priority != 'low_queue' -> demote to 'low_queue'
      - if age >= 14 -> flag for bulk briefing

    Returns:
      {total, demoted_to_low_queue, flagged_for_briefing, active,
       promotions_silently_dropped_by_staleness=0}

    The `promotions_silently_dropped_by_staleness` counter is ALWAYS 0.
    Items are demoted, never removed.
    """
    # H4: load-mutate-save under shared filelock with promotion_engine.
    with _pending_lock(_PENDING_APPROVALS_FILE):
        items = _load_pending()
        now = _utc_now()

        total = len(items)
        active_count = 0
        demoted = 0
        flagged = 0
        changed = False

        for item in items:
            if not _is_active(item):
                continue
            active_count += 1

            ts_key = item.get("enqueued_at") or item.get("created_at") or ""
            if now_active_days is not None:
                age = now_active_days
            else:
                age = ceo_days_since(ts_key, now=now)

            # Store age_active_days on item for downstream consumers (briefing)
            item["age_active_days"] = age

            # Demote to low_queue if stale (never re-demote already-low items)
            if age >= _DEMOTE_THRESHOLD_CEO_DAYS:
                current_priority = item.get("priority") or "high_queue"
                if current_priority != "low_queue":
                    item["priority"] = "low_queue"
                    item["demoted_at"] = _utc_now_iso()
                    demoted += 1
                    changed = True

            # Flag for bulk briefing (separate from demotion; independent threshold)
            if age >= _BRIEFING_THRESHOLD_CEO_DAYS:
                flagged += 1

        if changed:
            _save_pending(items)

    if changed:
        write_trace_event("hook_outcome", {
            "hook": "staleness_audit.audit",
            "demoted_to_low_queue": demoted,
            "flagged_for_briefing": flagged,
            "total": total,
        })

    return {
        "total": total,
        "active": active_count,
        "demoted_to_low_queue": demoted,
        "flagged_for_briefing": flagged,
        "promotions_silently_dropped_by_staleness": 0,
    }


# ---------------------------------------------------------------------------
# Briefing generation
# ---------------------------------------------------------------------------

def generate_cto_briefing(
    output_path: Optional[Path] = None,
    max_items: int = _BRIEFING_MAX_ROWS,
) -> Path:
    """Generate cto_briefing_<YYYY-MM-DD>.md with bulk-rating grid.

    Selects active items with age_active_days >= 14 (runs audit first to
    ensure age_active_days is populated). Sorts by age descending; caps at
    max_items rows.

    Each row: id | preview | source | age_days | enqueued | Action column
    CEO fills in: "approve 5", "reject <reason>", "defer", or a 1-5 score.

    Does NOT touch pending_approvals state (read-only after audit).
    Returns the written path.
    """
    # Run audit to populate age_active_days
    audit_pending()

    items = _load_pending()
    now = _utc_now()

    # Collect items aged >= 14 CEO-active-days
    aged: List[Dict[str, Any]] = []
    for item in items:
        if not _is_active(item):
            continue
        age = item.get("age_active_days")
        if age is None:
            ts_key = item.get("enqueued_at") or item.get("created_at") or ""
            age = ceo_days_since(ts_key, now=now)
            item["age_active_days"] = age
        if age >= _BRIEFING_THRESHOLD_CEO_DAYS:
            aged.append(item)

    # Sort by age descending (oldest first = most urgent)
    aged.sort(key=lambda x: x.get("age_active_days", 0), reverse=True)
    aged = aged[:max_items]

    today = now.strftime("%Y-%m-%d")
    if output_path is None:
        _BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
        output_path = _BRIEFINGS_DIR / f"cto_briefing_{today}.md"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = [
        f"# CTO Briefing -- Stale Approvals -- {today}",
        "",
        f"> Generated by staleness_audit.py | Items shown: {len(aged)} / {len([x for x in items if _is_active(x)])} active",
        "> Fill the **Action** column: `approve [score 1-5]` / `reject <reason>` / `defer`",
        "> Then run: `python -m src.core.staleness_audit apply <this_file>`",
        "",
        "| id | preview | source | age_days | enqueued | Action (approve/reject/defer + score 1-5) |",
        "|----|---------|--------|----------|----------|------------------------------------------|",
    ]

    for item in aged:
        item_id = (item.get("id") or "")[:24]
        preview_raw = (
            item.get("pattern_preview")
            or item.get("summary")
            or item.get("title")
            or ""
        )
        # SEC-R1 S5: escape "|" in all table-cell fields so markdown table
        # column alignment is not broken when pattern content contains pipes.
        preview = preview_raw[:80].replace("|", r"\|").replace("\n", " ")
        source = (item.get("type") or item.get("source") or "unknown")[:20]
        source = source.replace("|", r"\|")
        age_days = item.get("age_active_days", 0)
        enqueued = (item.get("enqueued_at") or item.get("created_at") or "")[:10]
        lines.append(
            f"| {item_id} | {preview} | {source} | {age_days} | {enqueued} |  |"
        )

    lines.extend(["", "<!-- end of briefing -->", ""])
    output_path.write_text("\n".join(lines), encoding="utf-8")

    # Record briefing path in harness_state for next-session reminder
    _record_briefing_in_harness(str(output_path), len(aged))

    write_trace_event("hook_outcome", {
        "hook": "staleness_audit.generate_briefing",
        "path": str(output_path),
        "rows": len(aged),
        "date": today,
    })

    return output_path


def _record_briefing_in_harness(path: str, row_count: int) -> bool:
    """Append latest briefing info to harness_state.json so CEO is reminded.

    Uses _atomic_state.atomic_update_json to avoid non-atomic RMW corruption
    under concurrent hooks (SEC-R1-004 fix, 2026-04-20 Cluster 4).
    """
    from src.core._atomic_state import atomic_update_json
    if not _HARNESS_STATE_FILE.exists():
        return False

    def _mutate(data):
        if not isinstance(data, dict):
            return None  # no-op
        pending = data.get("pending_briefings") or []
        # Remove older entries for same date (idempotent).
        suffix = path[-13:] if len(path) >= 13 else path
        pending = [b for b in pending
                   if not b.get("path", "").endswith(suffix)]
        pending.append({
            "path": path,
            "rows": row_count,
            "generated_at": _utc_now_iso(),
            "applied": False,
        })
        data["pending_briefings"] = pending
        return data

    lock = _HARNESS_STATE_FILE.with_suffix(_HARNESS_STATE_FILE.suffix + ".lock")
    return atomic_update_json(
        path=_HARNESS_STATE_FILE,
        mutator=_mutate,
        lock_path=lock,
        lock_timeout=5.0,
    )


# ---------------------------------------------------------------------------
# Apply bulk ratings
# ---------------------------------------------------------------------------

def apply_bulk_ratings(ratings: List[dict]) -> dict:
    """Apply CEO's marks from a completed briefing.

    ratings = [
        {"id": "apr_xxx", "action": "approve", "score": 5, "target_filename": "feedback_xxx.md"},
        {"id": "apr_yyy", "action": "reject",  "score": None},
        {"id": "apr_zzz", "action": "defer"},
        {"id": "apr_www", "action": "rate", "score": 4},
    ]

    For approve: calls promotion_engine.promote(pattern_id, target_filename).
    For reject:  calls promotion_engine.reject(pattern_id, reason).
    For defer:   marks pending row status="deferred", no promotion_engine call.
    For rate:    records score in the pending row, no state change.

    Returns {applied, errors, summary}.
    """
    applied = 0
    errors: List[str] = []
    now_iso = _utc_now_iso()

    # H4: Group the defer/rate mutations inside the shared lock. promote()
    # and reject() each take their own lock internally, so we must NOT hold
    # our lock across those calls (would deadlock on non-reentrant lock).
    # Strategy: (1) pre-pass under lock collects defer/rate mutations;
    # (2) lock-free pass executes promote/reject side-effects.

    # Pre-pass: determine actions and apply defer/rate under lock.
    promote_work: List[tuple] = []   # (item_id, pattern_id, target_fn)
    reject_work: List[tuple] = []    # (item_id, pattern_id, reason)

    with _pending_lock(_PENDING_APPROVALS_FILE):
        items = _load_pending()
        id_map: Dict[str, Dict[str, Any]] = {it.get("id", ""): it for it in items}
        items_dirty = False

        for rating in ratings:
            item_id = rating.get("id", "")
            action = (rating.get("action") or "").lower()
            score = rating.get("score")
            target = rating.get("target_filename")

            item = id_map.get(item_id)
            if item is None:
                errors.append(f"{item_id}: not found in pending_approvals")
                continue
            if not _is_active(item):
                errors.append(f"{item_id}: already terminal (status={item.get('status')})")
                continue

            pattern_id = item.get("pattern_id") or item_id

            if action == "approve":
                target_fn = target or item.get("suggested_target") or f"feedback_{pattern_id[:20]}.md"
                promote_work.append((item_id, pattern_id, target_fn))

            elif action == "reject":
                reason = rating.get("reason") or f"ceo_reject_via_briefing_score_{score}"
                reject_work.append((item_id, pattern_id, reason))

            elif action == "defer":
                item["status"] = "deferred"
                item["deferred_at"] = now_iso
                if score is not None:
                    item["ceo_score"] = score
                items_dirty = True
                applied += 1

            elif action == "rate":
                if score is not None:
                    item["ceo_score"] = int(score)
                    item["rated_at"] = now_iso
                    items_dirty = True
                    applied += 1
                else:
                    errors.append(f"{item_id}: rate action requires score")

            else:
                errors.append(f"{item_id}: unknown action '{action}'")

        if items_dirty:
            _save_pending(items)

    # Lock-free pass: promote()/reject() each take their own lock internally.
    for item_id, pattern_id, target_fn in promote_work:
        result = promote(pattern_id, target_fn)
        if result.get("ok"):
            applied += 1
        else:
            errors.append(f"{item_id}: promote failed: {result.get('reason')}")

    for item_id, pattern_id, reason in reject_work:
        ok = reject(pattern_id, reason)
        if ok:
            applied += 1
        else:
            errors.append(f"{item_id}: reject returned False (pattern_id={pattern_id})")

    write_trace_event("hook_outcome", {
        "hook": "staleness_audit.apply_bulk_ratings",
        "applied": applied,
        "errors": len(errors),
    })

    return {
        "applied": applied,
        "errors": errors,
        "summary": f"applied={applied} errors={len(errors)}",
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def get_staleness_metrics() -> dict:
    """Return {total_pending, high_queue, low_queue, ever_dropped:0,
              briefings_generated, latest_briefing_at}."""
    items = _load_pending()
    active = [it for it in items if _is_active(it)]
    high_queue = sum(
        1 for it in active
        if (it.get("priority") or "high_queue") != "low_queue"
    )
    low_queue = sum(
        1 for it in active
        if (it.get("priority") or "high_queue") == "low_queue"
    )

    briefings: List[Path] = []
    if _BRIEFINGS_DIR.exists():
        briefings = sorted(_BRIEFINGS_DIR.glob("cto_briefing_*.md"))

    latest_at = ""
    if briefings:
        latest_at = briefings[-1].stem.replace("cto_briefing_", "")

    return {
        "total_pending": len(active),
        "high_queue": high_queue,
        "low_queue": low_queue,
        "ever_dropped": 0,  # invariant: never drop
        "briefings_generated": len(briefings),
        "latest_briefing_at": latest_at,
    }


# ---------------------------------------------------------------------------
# Briefing parser (for CLI apply)
# ---------------------------------------------------------------------------

def _parse_briefing_file(briefing_path: Path) -> List[dict]:
    """Parse a completed briefing markdown, extract rows with non-empty Action.

    Expected table format (from generate_cto_briefing):
      | id | preview | source | age_days | enqueued | Action (...) |

    Returns list of rating dicts ready for apply_bulk_ratings.
    """
    text = briefing_path.read_text(encoding="utf-8")
    ratings: List[dict] = []
    in_table = False
    header_passed = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_table = False
            header_passed = False
            continue
        # Detect header row
        if "id" in stripped and "preview" in stripped and "action" in stripped.lower():
            in_table = True
            header_passed = False
            continue
        # Separator row
        if stripped.replace("|", "").replace("-", "").replace(" ", "") == "":
            if in_table:
                header_passed = True
            continue
        if not in_table or not header_passed:
            continue

        # Parse data row: | id | preview | source | age_days | enqueued | action |
        parts = [p.strip() for p in stripped.split("|")]
        # parts[0] is empty (before first |), parts[-1] may be empty (after last |)
        parts = [p for p in parts if p != "" or parts.index(p) > 0]
        # Re-split cleanly
        cells = [p.strip() for p in stripped.strip("|").split("|")]
        if len(cells) < 6:
            continue
        item_id = cells[0].strip()
        action_raw = cells[5].strip()
        if not action_raw:
            continue  # CEO left blank; skip

        rating = _parse_action_cell(action_raw)
        if rating is None:
            continue
        rating["id"] = item_id
        ratings.append(rating)

    return ratings


def _parse_action_cell(raw: str) -> Optional[dict]:
    """Parse action cell like 'approve 5', 'reject lazy', 'defer', 'rate 3'.

    Returns dict or None if unparseable.
    """
    raw = raw.strip()
    if not raw:
        return None
    parts = raw.split(None, 1)
    verb = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if verb == "approve":
        score = None
        if rest.isdigit():
            score = int(rest)
        return {"action": "approve", "score": score}
    if verb == "reject":
        return {"action": "reject", "reason": rest or "ceo_reject"}
    if verb == "defer":
        return {"action": "defer"}
    if verb == "rate":
        if rest.isdigit():
            return {"action": "rate", "score": int(rest)}
        return None
    # Single digit treated as score (rate action)
    if raw.isdigit():
        return {"action": "rate", "score": int(raw)}
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: List[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: staleness_audit {audit | briefing | metrics | apply <briefing_file>}",
            file=sys.stderr,
        )
        return 2

    cmd = argv[1]

    if cmd == "audit":
        result = audit_pending()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if cmd == "briefing":
        path = generate_cto_briefing()
        print(str(path))
        return 0

    if cmd == "metrics":
        m = get_staleness_metrics()
        print(f"total_pending    : {m['total_pending']}")
        print(f"high_queue       : {m['high_queue']}")
        print(f"low_queue        : {m['low_queue']}")
        print(f"ever_dropped     : {m['ever_dropped']}  (invariant: always 0)")
        print(f"briefings_gen    : {m['briefings_generated']}")
        print(f"latest_briefing  : {m['latest_briefing_at'] or 'none'}")
        return 0

    if cmd == "apply":
        if len(argv) < 3:
            print("apply requires <briefing_file>", file=sys.stderr)
            return 2
        bp = Path(argv[2])
        if not bp.exists():
            print(f"file not found: {bp}", file=sys.stderr)
            return 1
        ratings = _parse_briefing_file(bp)
        if not ratings:
            print("no actionable rows found in briefing file")
            return 0
        result = apply_bulk_ratings(ratings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["errors"] else 3

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli(sys.argv))
