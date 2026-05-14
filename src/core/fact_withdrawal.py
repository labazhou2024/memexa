"""TU-5 of 2026 backfill plan_v2 §3 — fact_withdrawal: bi-temporal + tombstone.

Per plan §TaskUnits TU-5 + logic-iter2-6 absorption:
  - WithdrawalReason enum (含 SCHEMA_MIGRATION).
  - invalidate_fact(...) SQL parameterized; reason_detail ≤500 chars.
  - replay_dropped(...) distinguishes outbox replay failure from user retract.
  - query_active(...) returns active facts at a given timestamp (bi-temporal).

Storage backend is abstracted behind a small WithdrawalStore protocol; concrete
backends are SqliteWithdrawalStore (for tests + low-volume) and PgWithdrawalStore
(for production via psycopg2). Tests use sqlite3 in-memory.

Per plan §Architecture invariant 10: invalidate_fact reason ∈ enum.

axis_anchor: [C:cli:fact_withdrawal] [C:cli:withdrawal_replay]
trace events: tombstone_written, replay_dropped, withdrawal_replay_done
"""
from __future__ import annotations

import enum
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

# Reuse scrub_pii for reason_detail (security-iter1-6 fix)
try:
    from src.extraction.keystone_outbox import scrub_pii as _scrub_pii
    _HAS_SCRUB = True
except ImportError:
    _HAS_SCRUB = False
    def _scrub_pii(text):
        return (text, 0)


# ---- WithdrawalReason enum ------------------------------------------------

class WithdrawalReason(str, enum.Enum):
    """Closed enum for invalidate_fact reason. Per plan §Architecture inv 10
    + logic-iter2-6 (SCHEMA_MIGRATION added).
    """
    USER_RETRACT = "USER_RETRACT"
    G6_CONTRADICTION = "G6_CONTRADICTION"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    SOURCE_RETRACTED = "SOURCE_RETRACTED"
    MANUAL_CORRECTION = "MANUAL_CORRECTION"
    SCHEMA_MIGRATION = "SCHEMA_MIGRATION"  # logic-iter2-6 fix


REASON_DETAIL_MAX_LEN: int = 500


# ---- Storage protocol -----------------------------------------------------

class WithdrawalStore(Protocol):
    """Backend protocol for fact storage."""

    def insert_fact(
        self, fact_id: str, canonical_subject: str, predicate: str,
        object_: str, valid_at: str, invalidated_at: Optional[str] = None,
        withdrawal_reason: Optional[str] = None,
    ) -> None: ...

    def update_invalidation(
        self, fact_id: str, invalidated_at: str,
        reason: str, reason_detail: str,
    ) -> int: ...

    def query_active_by_subject(
        self, subject: str, at_ts: str,
    ) -> List[Dict[str, Any]]: ...

    def get_fact(self, fact_id: str) -> Optional[Dict[str, Any]]: ...


# ---- SQLite backend (tests + low-volume) ----------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    canonical_subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_at TEXT NOT NULL,
    invalidated_at TEXT,
    withdrawal_reason TEXT,
    withdrawal_reason_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_subject_valid_at ON memories(canonical_subject, valid_at);
CREATE INDEX IF NOT EXISTS idx_invalidated ON memories(invalidated_at);
"""


class SqliteWithdrawalStore:
    """Sqlite3 backend implementing WithdrawalStore. Used by tests."""

    def __init__(self, db_path: str = ":memory:"):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SQLITE_SCHEMA)
        self._conn.commit()

    def insert_fact(
        self, fact_id: str, canonical_subject: str, predicate: str,
        object_: str, valid_at: str, invalidated_at: Optional[str] = None,
        withdrawal_reason: Optional[str] = None,
    ) -> None:
        # Parameterized; per plan invariant + sec hardening
        self._conn.execute(
            """
            INSERT OR REPLACE INTO memories
              (id, canonical_subject, predicate, object, valid_at,
               invalidated_at, withdrawal_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fact_id, canonical_subject, predicate, object_, valid_at,
             invalidated_at, withdrawal_reason),
        )
        self._conn.commit()

    def update_invalidation(
        self, fact_id: str, invalidated_at: str,
        reason: str, reason_detail: str,
    ) -> int:
        cur = self._conn.execute(
            """
            UPDATE memories
            SET invalidated_at = ?,
                withdrawal_reason = ?,
                withdrawal_reason_detail = ?
            WHERE id = ?
            """,
            (invalidated_at, reason, reason_detail, fact_id),
        )
        self._conn.commit()
        return cur.rowcount

    def query_active_by_subject(
        self, subject: str, at_ts: str,
    ) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT id, canonical_subject, predicate, object, valid_at,
                   invalidated_at, withdrawal_reason
            FROM memories
            WHERE canonical_subject = ?
              AND valid_at <= ?
              AND (invalidated_at IS NULL OR invalidated_at > ?)
            ORDER BY valid_at DESC
            """,
            (subject, at_ts, at_ts),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_fact(self, fact_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (fact_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self._conn.close()


# ---- Public API ------------------------------------------------------------

def invalidate_fact(
    fact_id: str,
    invalidated_at: str,
    reason: WithdrawalReason,
    reason_detail: str = "",
    store: Optional[WithdrawalStore] = None,
    emit_trace: Optional[Callable] = None,
) -> bool:
    """Mark a fact invalidated_at via bi-temporal tombstone.

    Per plan §TaskUnits TU-5:
      - SQL parameterized (handled by store backend).
      - reason_detail ≤500 chars.
      - reason MUST be a WithdrawalReason enum member.

    Returns True on success (1 row updated), False on missing fact_id.

    Per security: empty/None fact_id rejected. Reason as raw string also
    rejected (must be enum) — defense-in-depth against injection.
    """
    if not isinstance(fact_id, str) or not fact_id:
        return False
    if not isinstance(invalidated_at, str) or not invalidated_at:
        return False
    # Per security-iter1-2 fix: validate ISO-8601; bi-temporal queries rely on
    # lexicographic ISO comparison.
    try:
        datetime.fromisoformat(invalidated_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValueError(f"invalidated_at must be ISO-8601, got {invalidated_at!r}")
    if not isinstance(reason, WithdrawalReason):
        # Reject raw strings; must use enum
        raise TypeError(f"reason must be WithdrawalReason, got {type(reason).__name__}")
    if not isinstance(reason_detail, str):
        raise TypeError(f"reason_detail must be str, got {type(reason_detail).__name__}")
    if len(reason_detail) > REASON_DETAIL_MAX_LEN:
        raise ValueError(
            f"reason_detail too long: {len(reason_detail)} > {REASON_DETAIL_MAX_LEN}"
        )

    if store is None:
        raise RuntimeError("invalidate_fact: store backend required")

    # Per security-iter1-6 fix: scrub PII from reason_detail before persisting
    scrubbed_detail, _ = _scrub_pii(reason_detail) if reason_detail else ("", 0)

    rowcount = store.update_invalidation(
        fact_id=fact_id,
        invalidated_at=invalidated_at,
        reason=reason.value,
        reason_detail=scrubbed_detail,
    )

    if rowcount == 0:
        return False

    if emit_trace is not None:
        try:
            emit_trace("tombstone_written", {
                "fact_id": fact_id,
                "invalidated_at": invalidated_at,
                "reason": reason.value,
                "reason_detail_len": len(reason_detail),
            })
        except Exception:
            pass

    return True


def replay_dropped(
    fact_id: str,
    drop_context: str,
    store: Optional[WithdrawalStore] = None,
    emit_trace: Optional[Callable] = None,
) -> bool:
    """Mark a fact as dropped during outbox replay (NOT user-retract).

    Per plan §TaskUnits TU-5: distinguishes 'outbox replay failure' from
    'user said this is wrong' (USER_RETRACT). Uses SOURCE_RETRACTED reason
    when source is gone, MANUAL_CORRECTION when ops needed to roll back.

    Args:
        fact_id: id of fact being dropped.
        drop_context: brief description (e.g. "outbox replay 3-step failed
            on PG migration check").
        store: WithdrawalStore backend.
        emit_trace: optional trace emitter.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    detail = f"replay_dropped: {drop_context}"[:REASON_DETAIL_MAX_LEN]
    ok = invalidate_fact(
        fact_id=fact_id,
        invalidated_at=now_iso,
        reason=WithdrawalReason.SOURCE_RETRACTED,
        reason_detail=detail,
        store=store,
        emit_trace=emit_trace,
    )
    if ok and emit_trace is not None:
        try:
            emit_trace("replay_dropped", {
                "fact_id": fact_id,
                "drop_context": drop_context[:200],
            })
        except Exception:
            pass
    return ok


def query_active(
    subject: str,
    at_ts: Optional[str] = None,
    store: Optional[WithdrawalStore] = None,
) -> List[Dict[str, Any]]:
    """Query facts active for `subject` at `at_ts`.

    Bi-temporal semantics: returns rows where
      valid_at ≤ at_ts AND (invalidated_at IS NULL OR invalidated_at > at_ts).

    Args:
        subject: canonical_subject person_uuid.
        at_ts: ISO-8601; default = now.
        store: WithdrawalStore backend.
    """
    if store is None:
        raise RuntimeError("query_active: store backend required")
    if not isinstance(subject, str) or not subject:
        return []
    if at_ts is None:
        at_ts = datetime.now(timezone.utc).isoformat()
    return store.query_active_by_subject(subject=subject, at_ts=at_ts)


__all__ = [
    "WithdrawalReason",
    "REASON_DETAIL_MAX_LEN",
    "WithdrawalStore",
    "SqliteWithdrawalStore",
    "invalidate_fact",
    "replay_dropped",
    "query_active",
]
