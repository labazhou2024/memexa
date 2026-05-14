"""Unified CEO anomaly notifier — kills the 'silent-drop' invariant violation.

Root Cluster 2 fix (2026-04-20 autopilot): every error-recovery path in the
reverse-flow pipeline previously swallowed errors into counters or returned
{"skipped": ...}. CEO edits could disappear at 3+ layers. This module provides
a single hook that routes all such anomalies to pending_approvals.json where
they surface on session_start_gate.

Call sites (6 total as of 2026-04-20):
  - memory_ingest_watcher cli_error (Haiku returncode != 0)
  - memory_ingest_watcher governance_reject (scan_and_ingest + drain_queue)
  - memory_ingest_watcher DLQ (ingest_deadletter.jsonl)
  - big_loop Q6.5 scan_error
  - governance_middleware _deep_consistency_check exception
  - promotion_engine veto (quarantine)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from filelock import FileLock

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get(
    "MEMEXA_DATA_DIR",
    str(Path(__file__).resolve().parent.parent / "data"),
))
_PENDING_FILE = _DATA_DIR / "pending_approvals.json"
_PENDING_LOCK = _DATA_DIR / "pending_approvals.lock"

# Valid anomaly types — the CEO briefing formats these.
VALID_TYPES = {
    "ingest_cli_error",        # Haiku CLI returncode != 0 while extracting memory edit
    "ingest_governance_reject",  # governance denied a CEO edit
    "ingest_dlq",              # file hit DLQ after >3 retries
    "q6_5_scan_error",         # BigLoop Q6.5 promotion scan exception
    "gov_consistency_error",   # _deep_consistency_check bare-except
    "promotion_veto",          # governance vetoed a promotion (quarantine)
}

# De-duplication TTL — don't re-notify for the same (type, key) within this window.
_DEDUPE_TTL_SECONDS = 3600  # 1 hour


def notify(
    anomaly_type: str,
    key: str,
    detail: str,
    *,
    source_file: Optional[str] = None,
    agent_name: str = "unknown",
    extra: Optional[Dict[str, Any]] = None,
) -> bool:
    """Enqueue a CEO-visible anomaly entry in pending_approvals.json.

    Args:
        anomaly_type: one of VALID_TYPES
        key: dedup key — e.g., file_path or pattern_id
        detail: 1-line human-readable description
        source_file: optional file that triggered the anomaly
        agent_name: caller identity (for audit)
        extra: additional context

    Returns:
        True if a new entry was written, False if de-duplicated or disabled.
    """
    if anomaly_type not in VALID_TYPES:
        logger.warning("_anomaly_notify: unknown type %s, dropping", anomaly_type)
        return False

    # Opt-out kill switch (for tests that specifically don't want notifications).
    if os.environ.get("MEMEXA_ANOMALY_NOTIFY_DISABLE") == "1":
        return False

    entry = {
        "id": f"anomaly_{anomaly_type}_{int(datetime.now(timezone.utc).timestamp())}",
        "type": anomaly_type,
        "key": key,
        "detail": detail[:500],
        "source_file": source_file,
        "agent_name": agent_name,
        "extra": dict(extra or {}),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "priority": "normal",  # CEO can downgrade to low_queue if noisy
    }

    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with FileLock(str(_PENDING_LOCK), timeout=5):
            queue = _load_queue()
            if _is_duplicate(queue, anomaly_type, key):
                return False
            queue.append(entry)
            _save_queue(queue)
        logger.info("_anomaly_notify: enqueued %s for key=%s", anomaly_type, key)
        return True
    except Exception as e:
        # NEVER raise — notifier must not compound the error.
        logger.warning("_anomaly_notify: enqueue failed: %s", e)
        return False


def _load_queue() -> list:
    if not _PENDING_FILE.exists():
        return []
    try:
        data = json.loads(_PENDING_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # legacy shape: {"pending": [...]}
        return list(data.get("pending") or data.get("approvals") or [])
    return []


def _save_queue(queue: list) -> None:
    tmp = _PENDING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(str(tmp), str(_PENDING_FILE))


def _is_duplicate(queue: list, anomaly_type: str, key: str) -> bool:
    """True if an entry with same (type,key) was enqueued within TTL window."""
    now = datetime.now(timezone.utc)
    for entry in queue:
        if entry.get("type") != anomaly_type or entry.get("key") != key:
            continue
        try:
            created = datetime.fromisoformat(entry.get("created_at", ""))
        except ValueError:
            continue
        if (now - created).total_seconds() < _DEDUPE_TTL_SECONDS:
            return True
    return False


# CLI helper for CEO inspection
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List pending anomalies")
    clear_p = sub.add_parser("clear", help="Remove anomaly by id")
    clear_p.add_argument("anomaly_id")
    args = ap.parse_args()

    if args.cmd == "list":
        q = _load_queue()
        anomalies = [e for e in q if str(e.get("type", "")).startswith(
            tuple(t.split("_")[0] for t in VALID_TYPES)) or e.get("type") in VALID_TYPES]
        for a in anomalies:
            print(f"{a.get('id','?')}  {a.get('type','?'):30s}  {a.get('key','')[:50]}")
            print(f"    {a.get('detail','')[:120]}")
        print(f"\nTotal: {len(anomalies)} anomalies")
        return 0

    if args.cmd == "clear":
        with FileLock(str(_PENDING_LOCK), timeout=5):
            q = _load_queue()
            q = [e for e in q if e.get("id") != args.anomaly_id]
            _save_queue(q)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
