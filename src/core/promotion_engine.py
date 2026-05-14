"""
Promotion Engine (T7, plan v3.1, 2026-04-20)
============================================

ReasoningBank-style KB -> memory promotion pipeline.

Responsibility
--------------
Scan the pattern knowledge base (improvement_patterns.jsonl) for entries
whose reward signal has crossed the promotion threshold, queue them for
CEO review as approval items, and on CEO approval write them out to
memory/*.md via the governance_middleware pre_write_check choke point.

The engine NEVER auto-promotes. Every memory/feedback_*.md write requires
an explicit CEO action (promote / reject). Taste-path guard in
governance_middleware enforces this at the sink even if upstream logic
drifts: feedback_*.md writes with source not in
_TASTE_PATH_ALLOWED_SOURCES are rejected.

We pass source="ceo_approved_promotion" (M3 fix 2026-04-20) so downstream
lineage auditors can tell KB-derived promotions apart from raw CEO
hand-edits (source="ceo_edit"); both are treated as taste-path-allowed.

Thresholds (plan v3.1 §5 AC-12, §3 B ReasoningBank promotion criteria)
----------------------------------------------------------------------
A pattern is promotable when ALL of:
  * helpful_count >= 5
  * outdated_reports == 0        (loose proxy for "no downvote")
  * source != "autodream"         (autodream alone is not a human source)
  * promotion_status == "draft"   (not already queued / promoted / rejected)
  * age_in_ceo_active_days >= 14  (CEO-active-day clock from session_heartbeat)

CLI
---
    python -m src.core.promotion_engine scan
        Find + enqueue every pattern that meets the thresholds.

    python -m src.core.promotion_engine list-pending
        Pretty-print the pending_approvals queue.

    python -m src.core.promotion_engine promote <pattern_id> <filename>
        CEO approves. <filename> is the basename under memory/
        (e.g. feedback_writing_style.md). Writes via governance
        with source="ceo_edit".

    python -m src.core.promotion_engine reject <pattern_id> <reason>
        CEO rejects. Pattern stays in KB, status set to ceo_rejected.

Idempotency & provenance
------------------------
* enqueue_for_ceo_review skips if the pattern is already queued
  (promotion_status != "draft").
* promote() refuses to run if promotion_status is already promoted_to_memory
  or if the pattern is in ceo_rejected.
* Every promote()/reject() is traced via trace_sink hook_outcome events.
* Written memory/*.md files carry frontmatter:
      distilled_from_pattern_id: <pid>
      promoted_by: ceo
      promoted_at: <ISO-ts>
  so the lineage is auditable from the file alone.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Local imports
try:
    from src.core import pattern_extractor as _pe
except Exception:  # pragma: no cover - dependency is mandatory in prod
    _pe = None  # type: ignore[assignment]

try:
    from src.core.governance_middleware import pre_write_check  # type: ignore
except Exception:  # pragma: no cover
    pre_write_check = None  # type: ignore[assignment]

try:
    from src.core.session_heartbeat import count_ceo_active_days  # type: ignore
except Exception:  # pragma: no cover
    def count_ceo_active_days(window_days: int = 30, **_k):  # type: ignore[no-redef]
        return 0

try:
    from src.core.signal_bootstrap import (  # type: ignore
        natural_rating_count,
        natural_rating_fraction,
    )
except Exception:  # pragma: no cover
    def natural_rating_count(window_days: int = 30) -> int:  # type: ignore[no-redef]
        return 0
    def natural_rating_fraction(window_days: int = 30) -> float:  # type: ignore[no-redef]
        return 0.0

try:
    from src.core.trace_sink import write_trace_event  # type: ignore
except Exception:  # pragma: no cover
    def write_trace_event(*a, **k):  # type: ignore[no-redef]
        return False


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

def _resolve_data_dir() -> Path:
    """Mirror pattern_extractor._resolve_data_dir (MEMEXA_DATA_DIR honored)."""
    default = Path(__file__).parent.parent / "data"
    env_override = os.environ.get("MEMEXA_DATA_DIR")
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

# H4 (2026-04-20): shared filelock wrapper around pending_approvals.json.
# Both promotion_engine and staleness_audit must wrap load-mutate-save
# sequences with `_pending_lock(_PENDING_APPROVALS_FILE)` to avoid lost
# updates.
try:
    from src.core._pending_io import (
        pending_approvals_lock as _pending_lock,
    )
except Exception:  # pragma: no cover
    from contextlib import contextmanager
    @contextmanager
    def _pending_lock(pending_file):  # type: ignore[no-redef]
        yield

# Memory root (workspace-relative; not memexa/memexa/data). The governance
# target_path expected by pre_write_check is a string relative path like
# "memory/feedback_xxx.md", not an absolute one. The actual file write is
# done inside promote() via a workspace-root join.
_WORKSPACE_ROOT = Path(__file__).parent.parent.parent.parent
_MEMORY_DIR_NAME = "memory"

_MIN_HELPFUL = 5
_MAX_AGE_WINDOW_DAYS = 3650  # 10y cap for count_ceo_active_days() window
_MIN_AGE_CEO_ACTIVE_DAYS = 14

# M3 fix (2026-04-20): governance now accepts 'ceo_approved_promotion' as a
# distinct taste-path whitelist entry. This is semantically more correct than
# reusing 'ceo_edit' (which implies a direct CEO edit of memory/*.md).
_TASTE_SOURCE = "ceo_approved_promotion"

# Patterns that should NEVER be auto-promoted (taste path lives here)
_TASTE_PATH_RE = re.compile(r"^feedback_[a-z0-9_]+\.md$", re.I)
_PROJECT_PATH_RE = re.compile(r"^project_[a-z0-9_]+\.md$", re.I)


# ---------------------------------------------------------------------------
# pending_approvals JSON I/O (append-only list, dedup by pattern_id)
# ---------------------------------------------------------------------------

def _load_pending() -> List[Dict[str, Any]]:
    if not _PENDING_APPROVALS_FILE.exists():
        return []
    try:
        data = json.loads(_PENDING_APPROVALS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_pending(items: List[Dict[str, Any]]) -> None:
    _PENDING_APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_APPROVALS_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _has_pending_for_pattern(items: List[Dict[str, Any]], pattern_id: str) -> bool:
    for it in items:
        if it.get("pattern_id") == pattern_id and it.get("status") in (
            None, "pending", "deferred",
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(raw: str) -> Optional[datetime]:
    """Tolerant ISO-8601 parse. Strips Z / trailing fractional. Returns None on fail."""
    if not raw:
        return None
    try:
        s = raw.replace("Z", "")
        # Drop timezone offset for naive comparison (good enough for day counting).
        if "+" in s[10:]:
            s = s.split("+", 1)[0]
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _age_in_ceo_active_days(
    pattern: Any,
    now: Optional[datetime] = None,
    _count_fn=None,
) -> int:
    """Approximate "CEO-active-days since pattern created".

    We rely on count_ceo_active_days() for the total CEO-active-day count
    over a rolling window. Since the signal_heartbeat API gives us a count
    over window_days, the cleanest approximation is:

        age_in_ceo_days ~= count_ceo_active_days(window_days=N)

    where N = ceil(days between pattern.created_at and now). If the pattern
    was created fewer than N calendar days ago, that window exactly covers
    the pattern's lifetime, so the CEO-active-day count within that window
    is the pattern's CEO-active age.
    """
    now = now or datetime.utcnow()
    created_raw = getattr(pattern, "created_at", "") or ""
    created = _parse_ts(created_raw)
    if created is None:
        return 0
    delta_days = max(1, (now - created).days + 1)
    # Cap to prevent absurd windows.
    delta_days = min(delta_days, _MAX_AGE_WINDOW_DAYS)
    fn = _count_fn or count_ceo_active_days
    try:
        return int(fn(window_days=delta_days))
    except Exception:
        return 0


def _slugify_tags(tags: List[str]) -> str:
    """Produce a short snake_case slug from pattern tags (used in target name)."""
    if not tags:
        return "pattern"
    # Keep only [a-z0-9_] and join with '_'. Limit length to ~40 chars.
    cleaned: List[str] = []
    for t in tags:
        if not t:
            continue
        s = re.sub(r"[^a-z0-9]+", "_", str(t).lower()).strip("_")
        if s and s not in cleaned:
            cleaned.append(s)
    if not cleaned:
        return "pattern"
    slug = "_".join(cleaned)[:40].strip("_")
    return slug or "pattern"


def _suggest_target(pattern: Any) -> str:
    """Suggested memory/*.md filename (relative to memory/)."""
    slug = _slugify_tags(getattr(pattern, "tags", []) or [])
    # Default: feedback_*.md (taste path). Engineers can override at promote time.
    return f"feedback_{slug}.md"


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_promotable(
    now: Optional[datetime] = None,
    _count_ceo_active_days_fn=None,
) -> List[Dict[str, Any]]:
    """Return candidates meeting the promotion thresholds.

    Each candidate dict has:
        id, tags, rule, why, source, helpful_count, age_days, suggested_target.
    """
    if _pe is None:
        return []
    try:
        entries = _pe.load_all_patterns()
    except Exception:
        return []

    candidates: List[Dict[str, Any]] = []
    for e in entries:
        helpful = int(getattr(e, "helpful_count", 0) or 0)
        if helpful < _MIN_HELPFUL:
            continue
        # "downvote_count" proxy: outdated_reports. Governance stores no
        # explicit downvote column; outdated_reports is the reverse signal.
        outdated = int(getattr(e, "outdated_reports", 0) or 0)
        if outdated > 0:
            continue
        source = getattr(e, "source", "auto_extracted")
        if source == "autodream":
            continue
        status = getattr(e, "promotion_status", "draft")
        if status != "draft":
            continue
        age = _age_in_ceo_active_days(
            e, now=now, _count_fn=_count_ceo_active_days_fn,
        )
        if age < _MIN_AGE_CEO_ACTIVE_DAYS:
            continue
        candidates.append({
            "id": e.id,
            "tags": list(getattr(e, "tags", []) or []),
            "rule": getattr(e, "fact", ""),
            "why": getattr(e, "recommendation", ""),
            "source": source,
            "helpful_count": helpful,
            "age_days": age,
            "suggested_target": _suggest_target(e),
        })
    return candidates


def enqueue_for_ceo_review(
    candidate: Dict[str, Any],
    target: Optional[str] = None,
) -> bool:
    """Append one candidate to pending_approvals.json.

    Idempotent: if a non-terminal approval row already exists for the
    same pattern_id, returns False without duplicating.

    Also flips the pattern's promotion_status draft -> ceo_pending via
    pattern_extractor._atomic_rewrite_patterns so subsequent scans skip it.
    """
    pattern_id = candidate.get("id")
    if not pattern_id:
        return False

    final_target = target or candidate.get("suggested_target") or _suggest_target_from_dict(candidate)
    apr_id = f"apr_prom_{uuid.uuid4().hex[:10]}"
    now = _utc_now_iso()

    try:
        nat_count = int(natural_rating_count(window_days=30))
    except Exception:
        nat_count = 0
    try:
        nat_frac = float(natural_rating_fraction(window_days=30))
    except Exception:
        nat_frac = 0.0

    preview = (candidate.get("rule") or "")[:180]
    row = {
        "id": apr_id,
        "pattern_id": pattern_id,
        "pattern_preview": preview,
        "suggested_target": final_target,
        "enqueued_at": now,
        "ac15_context": {
            "natural_rating_count": nat_count,
            "fraction": round(nat_frac, 4),
        },
        "status": "pending",
        "level": "L3",
        "type": "kb_promotion",
        "helpful_count": candidate.get("helpful_count", 0),
        "age_days": candidate.get("age_days", 0),
        "tags": candidate.get("tags", []),
    }

    # H4: load-mutate-save under shared filelock.
    with _pending_lock(_PENDING_APPROVALS_FILE):
        items = _load_pending()
        if _has_pending_for_pattern(items, pattern_id):
            return False
        items.append(row)
        _save_pending(items)

    # Flip pattern status so repeat scans skip it. (Separate file, no race.)
    if _pe is not None:
        _set_pattern_status(pattern_id, "ceo_pending")

    write_trace_event("hook_outcome", {
        "hook": "promotion_engine.enqueue",
        "pattern_id": pattern_id,
        "apr_id": apr_id,
        "target": final_target,
    })
    return True


def _suggest_target_from_dict(candidate: Dict[str, Any]) -> str:
    tags = candidate.get("tags") or []
    slug = _slugify_tags(list(tags))
    return f"feedback_{slug}.md"


def _set_pattern_status(pattern_id: str, new_status: str) -> int:
    """Atomically flip promotion_status on the matching pattern."""
    if _pe is None:
        return 0
    now_iso = datetime.utcnow().isoformat() + "Z"

    def mutator(data: dict) -> dict:
        if data.get("id") == pattern_id:
            data["promotion_status"] = new_status
            data["updated_at"] = now_iso
            data["_mutated"] = True
        return data
    try:
        return int(_pe._atomic_rewrite_patterns(mutator))
    except Exception:
        return 0


def promote(
    pattern_id: str,
    target_path: str,
    approved_by: str = "ceo",
    _governance_fn=None,
) -> Dict[str, Any]:
    """CEO-approved promotion: write memory/<target> via governance.

    Arguments
    ---------
    pattern_id
        The pattern.id being promoted.
    target_path
        Filename (not a path) under memory/. Can start with "feedback_" or
        "project_". Caller may also pass "memory/feedback_xxx.md"; we
        normalize by stripping a leading "memory/" token.
    approved_by
        Audit label, defaults to "ceo".

    Returns
    -------
    {"ok": True, "written_path": str, "audit_id": str} on success,
    {"ok": False, "reason": str} otherwise. Governance rejection (taste
    guard, consistency veto, etc.) rolls the pattern status back to draft.
    """
    if _pe is None:
        return {"ok": False, "reason": "pattern_extractor_unavailable"}

    # Locate the pattern in current KB.
    try:
        entries = _pe.load_all_patterns()
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "reason": f"load_failed:{exc!r}"}
    entry = next((e for e in entries if e.id == pattern_id), None)
    if entry is None:
        return {"ok": False, "reason": "pattern_not_found"}

    status = getattr(entry, "promotion_status", "draft")
    if status == "promoted_to_memory":
        return {"ok": False, "reason": "already_promoted"}
    if status == "ceo_rejected":
        return {"ok": False, "reason": "pattern_rejected"}

    # Normalize target_path: accept bare filename OR "memory/<fn>".
    tp = target_path.replace("\\", "/").strip()
    if tp.startswith("memory/"):
        tp = tp[len("memory/"):]
    if "/" in tp or ".." in tp.split("/"):
        return {"ok": False, "reason": "invalid_target_path"}
    if not (tp.endswith(".md")):
        return {"ok": False, "reason": "target_must_be_md"}
    if not (_TASTE_PATH_RE.match(tp) or _PROJECT_PATH_RE.match(tp)):
        return {"ok": False, "reason": "target_not_whitelisted"}

    # Compose memory file content. frontmatter exposes lineage.
    now = _utc_now_iso()
    content = _render_promoted_markdown(entry, pattern_id, now, approved_by)

    gov_fn = _governance_fn or pre_write_check
    if gov_fn is None:
        return {"ok": False, "reason": "governance_unavailable"}

    req = {
        "tier": "L3",
        "source": _TASTE_SOURCE,  # ceo_edit — reuses T6 whitelist
        "parent_pattern_id": None,
        "content": content,
        "why": f"ceo_promotion:{pattern_id}",
        "agent_name": "promotion_engine",
        "target_path": f"memory/{tp}",
    }
    try:
        decision = gov_fn(req)
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "reason": f"governance_error:{exc!r}"}

    if not decision.get("allowed"):
        # Governance veto — do NOT advance status.
        write_trace_event("hook_outcome", {
            "hook": "promotion_engine.promote_rejected_by_governance",
            "pattern_id": pattern_id,
            "reason": decision.get("reason"),
            "audit_id": decision.get("audit_id"),
        })
        # F8 (LOG-R1-005): previously status was rolled back to "draft",
        # causing next Q6.5 scan to re-find and re-enqueue → infinite loop.
        # Use "veto_quarantine" so find_promotable (status != "draft") skips
        # it; CEO can manually reset with `promotion_engine reset <id>`.
        _set_pattern_status(pattern_id, "veto_quarantine")
        _drop_pending_row(pattern_id)
        try:
            from src.core._anomaly_notify import notify as _notify
            _notify(
                "promotion_veto",
                key=pattern_id,
                detail=f"reason={decision.get('reason')} audit={decision.get('audit_id')}",
                agent_name="promotion_engine.promote",
                extra={"audit_id": decision.get("audit_id")},
            )
        except Exception:
            pass
        return {
            "ok": False,
            "reason": f"governance_rejected:{decision.get('reason')}",
            "audit_id": decision.get("audit_id"),
        }

    # Governance approved the payload. Do the actual disk write.
    sanitized = decision.get("sanitized_content") or content
    abs_path = _WORKSPACE_ROOT / _MEMORY_DIR_NAME / tp
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(sanitized, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": f"write_failed:{exc!r}"}

    # Flip status, log, and mark the pending row terminal.
    _set_pattern_status(pattern_id, "promoted_to_memory")
    _mark_pending_row(pattern_id, "approved", approved_by=approved_by,
                      notes=f"written:{tp}")

    write_trace_event("hook_outcome", {
        "hook": "promotion_engine.promote",
        "pattern_id": pattern_id,
        "written_path": str(abs_path),
        "audit_id": decision.get("audit_id"),
        "approved_by": approved_by,
    })
    return {
        "ok": True,
        "written_path": str(abs_path),
        "audit_id": decision.get("audit_id", ""),
    }


def reject(
    pattern_id: str,
    reason: str,
    rejected_by: str = "ceo",
) -> bool:
    """CEO rejects. Status -> ceo_rejected. Keep pattern in KB."""
    if _pe is None:
        return False
    mutated = _set_pattern_status(pattern_id, "ceo_rejected")
    _mark_pending_row(pattern_id, "rejected", approved_by=rejected_by, notes=reason)
    write_trace_event("hook_outcome", {
        "hook": "promotion_engine.reject",
        "pattern_id": pattern_id,
        "reason": reason,
        "rejected_by": rejected_by,
    })
    return mutated > 0


def _drop_pending_row(pattern_id: str) -> bool:
    # H4: load-mutate-save under shared filelock.
    with _pending_lock(_PENDING_APPROVALS_FILE):
        items = _load_pending()
        filtered = [
            it for it in items
            if it.get("pattern_id") != pattern_id
            or it.get("status") not in ("pending", "deferred", None)
        ]
        if len(filtered) == len(items):
            return False
        _save_pending(filtered)
    return True


def _mark_pending_row(
    pattern_id: str,
    new_status: str,
    *,
    approved_by: str = "",
    notes: str = "",
) -> bool:
    # H4: load-mutate-save under shared filelock.
    with _pending_lock(_PENDING_APPROVALS_FILE):
        items = _load_pending()
        touched = False
        now = _utc_now_iso()
        for it in items:
            if it.get("pattern_id") != pattern_id:
                continue
            if it.get("status") not in ("pending", "deferred", None):
                continue
            it["status"] = new_status
            it["decided_at"] = now
            if approved_by:
                it["decided_by"] = approved_by
            if notes:
                it["notes"] = notes
            touched = True
        if touched:
            _save_pending(items)
    return touched


def cli_list_pending() -> None:
    """Pretty-print pending promotion approvals."""
    items = [it for it in _load_pending() if it.get("type") == "kb_promotion"]
    if not items:
        print("No pending promotion approvals.")
        return
    print(f"{'APPROVAL_ID':<24} {'PATTERN':<12} {'STATUS':<10} {'TARGET':<32} HELPFUL AGE")
    print("-" * 100)
    for it in items:
        apr = (it.get("id") or "")[:23]
        pid = (it.get("pattern_id") or "")[:11]
        st = (it.get("status") or "")[:9]
        tgt = (it.get("suggested_target") or "")[:31]
        hc = it.get("helpful_count", 0)
        age = it.get("age_days", 0)
        print(f"{apr:<24} {pid:<12} {st:<10} {tgt:<32} {hc:>7} {age:>3}")


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _render_promoted_markdown(
    entry: Any,
    pattern_id: str,
    now_iso: str,
    approved_by: str,
) -> str:
    """Render the memory/*.md file body.

    Frontmatter carries the provenance fields governance_middleware checks.
    The body is the rule + why + tags, suitable for CEO to edit further.
    """
    tags = list(getattr(entry, "tags", []) or [])
    fact = (getattr(entry, "fact", "") or "").strip()
    rec = (getattr(entry, "recommendation", "") or "").strip()
    name_from_slug = _slugify_tags(tags)

    # SEC-R1 S4: sanitize frontmatter fields against YAML injection.
    # pattern_id or approved_by containing "\n---\n" would truncate the
    # frontmatter block and inject arbitrary YAML keys.
    # Step 1: replace all non-whitelisted chars with "_"
    # Step 2: collapse runs of 3+ dashes (which render as YAML doc-end "---")
    _safe_pattern_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(pattern_id))
    _safe_pattern_id = re.sub(r"-{3,}", "--", _safe_pattern_id)
    _safe_approved_by = re.sub(r"[^a-zA-Z0-9_.@-]", "_", str(approved_by))[:64]
    _safe_approved_by = re.sub(r"-{3,}", "--", _safe_approved_by)

    fm_lines = [
        "---",
        f"name: {name_from_slug}",
        f"distilled_from_pattern_id: {_safe_pattern_id}",
        f"promoted_by: {_safe_approved_by}",
        f"promoted_at: {now_iso}",
        f"source_signal: helpful_count>={_MIN_HELPFUL}",
        "---",
    ]
    body_lines = [
        "",
        f"# {name_from_slug.replace('_', ' ').title()}",
        "",
        "**Rule (distilled from repeated success signal)**",
        "",
        fact or "_(no rule captured)_",
        "",
    ]
    if rec:
        body_lines.extend([
            "**Why / how to apply**",
            "",
            rec,
            "",
        ])
    if tags:
        body_lines.append("Tags: " + ", ".join(str(t) for t in tags))
        body_lines.append("")
    return "\n".join(fm_lines + body_lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: List[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: promotion_engine "
            "[scan | list-pending | promote <pid> <target> | reject <pid> <reason>]",
            file=sys.stderr,
        )
        return 2
    cmd = argv[1]
    if cmd == "scan":
        found = find_promotable()
        enq = 0
        for c in found:
            if enqueue_for_ceo_review(c):
                enq += 1
        print(f"scanned: {len(found)} candidate(s); enqueued: {enq}")
        return 0
    if cmd == "list-pending":
        cli_list_pending()
        return 0
    if cmd == "promote":
        if len(argv) < 4:
            print("promote requires <pattern_id> <target>", file=sys.stderr)
            return 2
        res = promote(argv[2], argv[3])
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 3
    if cmd == "reject":
        if len(argv) < 4:
            print("reject requires <pattern_id> <reason>", file=sys.stderr)
            return 2
        ok = reject(argv[2], argv[3])
        print(json.dumps({"ok": ok}, ensure_ascii=False))
        return 0 if ok else 3
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli(sys.argv))
