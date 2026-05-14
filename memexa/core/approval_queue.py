"""
Approval Queue -- CEO async approval system with concurrent safety.

Agent cluster runs fully autonomous. When a decision requires human
taste/foresight/risk judgment, it gets queued for async CEO review.

Three levels:
  L1 Auto:  Execute directly, log to EventBus
  L2 Suspend: Queue for review, continue other work
  L3 Block:  Queue for review, pause related task chain

Concurrency: threading.Lock + filelock.FileLock for safe multi-agent access.
Atomic writes via atomic_io to prevent corruption on crash.
State machine validation prevents invalid status transitions.
"""

import html
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import filelock

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_QUEUE_FILE = _DATA_DIR / "pending_approvals.json"


def get_queue_path() -> Path:
    """P3 (2026-04-23): single authoritative path for pending_approvals queue.

    Before this, session_start_gate and approval_queue each resolved the
    path independently — session_start_gate used `_find_workspace()` which
    could fall back to os.getcwd() if the .claude marker was absent at
    runtime, diverging from approval_queue's `__file__`-anchored path
    (logic-reviewer B5). Callers must import this instead of re-deriving.
    """
    return _QUEUE_FILE


# Two-layer locking: in-process + cross-process
_thread_lock = threading.Lock()
_file_lock = filelock.FileLock(str(_QUEUE_FILE) + ".lock", timeout=10)

# Valid status transitions (state machine)
VALID_TRANSITIONS = {
    "pending": {"approved", "rejected", "deferred"},
    "deferred": {"approved", "rejected"},
    # approved, rejected are terminal
}

# Approval ID format
_APPROVAL_ID_PATTERN = re.compile(r"^apr_\d+_\d{3}$")

# Notes sanitization
MAX_NOTES_LENGTH = 1000


def _sanitize_text(text: str) -> str:
    """Escape HTML entities in user-controlled text to prevent XSS."""
    if not text:
        return text
    return html.escape(str(text)[:MAX_NOTES_LENGTH])


def _load() -> List[Dict]:
    """Load queue from file. Caller MUST hold locks."""
    if _QUEUE_FILE.exists():
        try:
            return json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(items: List[Dict]):
    """Save queue atomically. Caller MUST hold locks."""
    from .atomic_io import atomic_write_json
    atomic_write_json(_QUEUE_FILE, items, backup=True)


def submit_approval(
    level: str, category: str, title: str,
    context: str, proposal: str, *,
    evidence: List[str] = None, impact: str = "",
    alternatives: List[str] = None,
    blocked_tasks: List[str] = None,
) -> str:
    """Submit a decision for CEO async approval.

    Thread-safe and process-safe. All text fields are HTML-escaped.
    Returns: Approval ID
    """
    with _thread_lock:
        with _file_lock:
            items = _load()
            apr_id = f"apr_{int(time.time())}_{len(items):03d}"
            now = datetime.utcnow().isoformat() + "Z"
            item = {
                "id": apr_id, "level": level, "category": category,
                "title": _sanitize_text(title),
                "context": _sanitize_text(context),
                "proposal": _sanitize_text(proposal),
                "evidence": [_sanitize_text(e) for e in (evidence or [])],
                "impact": _sanitize_text(impact),
                "alternatives": [_sanitize_text(a) for a in (alternatives or [])],
                "created_at": now, "status": "pending",
                "ceo_response": None, "resolved_at": None,
                "blocked_tasks": blocked_tasks or [],
            }
            items.append(item)
            _save(items)

    try:
        from .event_bus import log_event
        log_event("approval_submitted", agent="approval_queue", details={
            "id": apr_id, "level": level, "title": title[:100],
        })
    except Exception:
        pass
    logger.info("[APPROVAL] %s submitted: %s (id=%s)", level, title, apr_id)
    return apr_id


def get_pending() -> List[Dict]:
    """Get all pending approvals. Thread-safe read."""
    with _thread_lock:
        with _file_lock:
            return [i for i in _load() if i["status"] == "pending"]


def get_all() -> List[Dict]:
    """Get all approvals. Thread-safe read."""
    with _thread_lock:
        with _file_lock:
            return _load()


def approve(apr_id: str, response: str = "") -> bool:
    """Approve a pending or deferred approval. Returns True if resolved."""
    return _resolve(apr_id, "approved", response)


def reject(apr_id: str, response: str = "") -> bool:
    """Reject a pending or deferred approval. Returns True if resolved."""
    return _resolve(apr_id, "rejected", response)


def defer(apr_id: str, response: str = "") -> bool:
    """Defer a pending approval. Returns True if resolved."""
    return _resolve(apr_id, "deferred", response)


def _resolve(apr_id: str, new_status: str, response: str) -> bool:
    """Resolve an approval with state machine validation.

    Thread-safe and process-safe. Validates:
    - Approval exists
    - Current status allows transition to new_status
    - Response text is sanitized

    Returns True if resolved, False if not found or invalid transition.
    """
    sanitized_response = _sanitize_text(response)

    with _thread_lock:
        with _file_lock:
            items = _load()
            for item in items:
                if item["id"] == apr_id:
                    current = item["status"]
                    allowed = VALID_TRANSITIONS.get(current, set())
                    if new_status not in allowed:
                        logger.warning(
                            "[APPROVAL] Invalid transition %s -> %s for %s",
                            current, new_status, apr_id,
                        )
                        return False
                    item["status"] = new_status
                    item["ceo_response"] = sanitized_response
                    item["resolved_at"] = datetime.utcnow().isoformat() + "Z"
                    _save(items)
                    try:
                        from .event_bus import log_event
                        log_event("approval_resolved", agent="ceo", details={
                            "id": apr_id, "status": new_status,
                        })
                    except Exception:
                        pass
                    return True
            return False


def is_approved(apr_id: str) -> bool:
    """Check if an approval is approved."""
    with _thread_lock:
        with _file_lock:
            for item in _load():
                if item["id"] == apr_id:
                    return item["status"] == "approved"
    return False


def get_blocked_tasks() -> List[str]:
    """Get all tasks blocked by pending L3 approvals."""
    blocked = []
    for item in get_pending():
        if item["level"] == "L3":
            blocked.extend(item.get("blocked_tasks", []))
    return blocked


def validate_approval_id(apr_id: str) -> bool:
    """Validate approval ID format."""
    return bool(_APPROVAL_ID_PATTERN.match(apr_id))


def format_briefing() -> str:
    """Format pending approvals as a briefing. All text is pre-sanitized."""
    pending = get_pending()
    if not pending:
        return "审批队列为空，无待决事项。"
    lines = [f"## 待审批事项 ({len(pending)} 项)\n"]
    for item in pending:
        icon = {"L2": "[挂起]", "L3": "[阻塞]"}.get(item["level"], "[?]")
        lines.append(f"### {icon} {item['title']}")
        lines.append(f"类别: {item['category']} | 时间: {item['created_at'][:10]}")
        lines.append(f"背景: {item['context'][:200]}")
        lines.append(f"建议: {item['proposal'][:200]}")
        if item["impact"]:
            lines.append(f"影响: {item['impact']}")
        if item["alternatives"]:
            lines.append(f"备选: {' / '.join(item['alternatives'])}")
        lines.append(f"ID: {item['id']}\n")
    return "\n".join(lines)


def _reset_for_testing(data_dir: Path, queue_file: Path) -> None:
    """Reset module state for test isolation."""
    global _DATA_DIR, _QUEUE_FILE, _file_lock
    _DATA_DIR = data_dir
    _QUEUE_FILE = queue_file
    _file_lock = filelock.FileLock(str(queue_file) + ".lock", timeout=10)
