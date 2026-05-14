"""dual_write_audit.py — 7-day double-write consistency audit.

U2 (2026-04-26): During the 7-day Hindsight rollout window, both old
Neo4j and new Hindsight outbox should be receiving every memory write.
This script computes:

  - neo4j_24h_count: Cypher count of (:Fact) nodes with created_at in
    last 24h (skipped if Neo4j down).
  - outbox_done_24h_count: count of `status=done` entries in outbox
    JSONL with last_attempt_at in last 24h.
  - diff = abs(outbox_done_24h_count - neo4j_24h_count)
  - status:
      OK                 — diff <= threshold (default 50)
      WARN_BREACH        — diff > threshold
      EXPECTED_FIRST_DAY — outbox_done < 5 AND neo4j > 100 (rollout day-0)
      NEO4J_DOWN         — Cypher unreachable, outbox-only report

JSON output to stdout + dated artifact at
`<workspace>/.claude/harness/data/dual_write_audit_<date>.json`.

Per logic-reviewer / coverage MED-1: this swaps Hindsight-side counting
(impossible without /list endpoint) for outbox-completion-count, which
is the locally authoritative ledger for "what we asked Hindsight to
ingest". Drift discovered here is meaningful: it tells us how many
memory writes the legacy pipe accepted vs how many the new pipe
asked Hindsight to accept.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 50
_DEFAULT_FIRST_DAY_OUTBOX_THRESHOLD = 5
_DEFAULT_FIRST_DAY_NEO4J_THRESHOLD = 100
_WINDOW_SEC = 86400.0  # 24h


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _audit_artifact_path(date_str: Optional[str] = None) -> Path:
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return (_workspace_root() / ".claude" / "harness" / "data"
            / f"dual_write_audit_{date_str}.json")


# --------------------------------------------------------------------------
# Outbox window count
# --------------------------------------------------------------------------


def _count_outbox_done_in_window(outbox_dir: Optional[str] = None,
                                 window_sec: float = _WINDOW_SEC,
                                 now_fn=time.time) -> int:
    """Count `status=done` outbox entries with last_attempt_at >= now - window."""
    try:
        from memexa.core.hindsight_outbox import (
            _resolve_outbox_dir, _iter_all_entries, _outbox_path,
            STATUS_DONE, OutboxDirRejected,
        )
    except Exception as e:
        logger.warning("outbox import failed: %s", e)
        return 0
    try:
        target = _resolve_outbox_dir(outbox_dir)
    except OutboxDirRejected as e:
        logger.warning("outbox_dir rejected: %s", e)
        return 0
    if not target.exists():
        return 0
    threshold = now_fn() - window_sec
    cnt = 0
    try:
        for entry in _iter_all_entries(_outbox_path(target)):
            if entry.status == STATUS_DONE:
                ts = entry.last_attempt_at or entry.enqueued_at
                if ts and ts >= threshold:
                    cnt += 1
    except Exception as e:
        logger.warning("outbox count failed: %s", e)
    return cnt


# --------------------------------------------------------------------------
# Neo4j window count
# --------------------------------------------------------------------------


def _count_neo4j_facts_24h(mode: str = "live") -> Optional[int]:
    """Return Neo4j Fact count in last 24h, or None on Neo4j down.

    mode='mock' returns a fixed mock value (10 by default; override via
    MEMEXA_AUDIT_MOCK_NEO4J_COUNT env).
    mode='down' simulates Neo4j unreachable (returns None).
    mode='live' attempts real Cypher.
    """
    if mode == "mock":
        try:
            return int(os.environ.get("MEMEXA_AUDIT_MOCK_NEO4J_COUNT", "10"))
        except ValueError:
            return 10
    if mode == "down":
        return None
    # live mode
    try:
        from neo4j import GraphDatabase  # type: ignore
    except ImportError:
        logger.warning("neo4j package not installed")
        return None
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD")
    if not pwd:
        logger.warning("NEO4J_PASSWORD not set")
        return None
    try:
        with GraphDatabase.driver(uri, auth=(user, pwd)) as driver:
            with driver.session() as session:
                result = session.run(
                    "MATCH (f:Fact) "
                    "WHERE f.created_at > datetime() - duration({days: 1}) "
                    "RETURN count(f) AS n"
                )
                rec = result.single()
                if rec is None:
                    return 0
                return int(rec["n"])
    except Exception as e:
        logger.warning("neo4j cypher failed: %s", e)
        return None


# --------------------------------------------------------------------------
# Audit logic
# --------------------------------------------------------------------------


def _decide_status(neo4j_count: Optional[int],
                   outbox_done: int,
                   threshold: int) -> str:
    if neo4j_count is None:
        return "NEO4J_DOWN"
    if (outbox_done < _DEFAULT_FIRST_DAY_OUTBOX_THRESHOLD
            and neo4j_count >= _DEFAULT_FIRST_DAY_NEO4J_THRESHOLD):
        return "EXPECTED_FIRST_DAY"
    diff = abs(outbox_done - neo4j_count)
    return "WARN_BREACH" if diff > threshold else "OK"


def run_audit(neo4j_mode: str = "live",
              outbox_dir: Optional[str] = None,
              threshold: Optional[int] = None,
              write_artifact: bool = True,
              now_fn=time.time) -> Dict[str, Any]:
    """Compute audit result and (optionally) persist dated JSON artifact.

    Returns the result dict with these keys:
    - neo4j_24h_count (int or None when NEO4J_DOWN)
    - outbox_done_24h_count (int)
    - diff (int)
    - threshold (int)
    - breach (bool)
    - status (str)
    - timestamp (ISO 8601 UTC)
    """
    if threshold is None:
        try:
            threshold = int(os.environ.get("MEMEXA_AUDIT_THRESHOLD",
                                            str(_DEFAULT_THRESHOLD)))
        except ValueError:
            threshold = _DEFAULT_THRESHOLD

    neo4j_count = _count_neo4j_facts_24h(mode=neo4j_mode)
    outbox_done = _count_outbox_done_in_window(outbox_dir=outbox_dir,
                                                now_fn=now_fn)
    diff = abs((neo4j_count or 0) - outbox_done) if neo4j_count is not None else 0
    status = _decide_status(neo4j_count, outbox_done, threshold)
    breach = (status == "WARN_BREACH")

    result: Dict[str, Any] = {
        "neo4j_24h_count": neo4j_count,
        "outbox_done_24h_count": outbox_done,
        "diff": diff,
        "threshold": threshold,
        "breach": breach,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if write_artifact:
        try:
            ap = _audit_artifact_path()
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            result["artifact_path"] = str(ap)
        except Exception as e:
            logger.warning("audit artifact write failed: %s", e)

    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("dual_write_audit_run", {
            "status": result["status"],
            "diff": result["diff"],
            "neo4j_24h_count": result.get("neo4j_24h_count"),
            "outbox_done_24h_count": result["outbox_done_24h_count"],
        })
    except Exception:
        pass  # allow-silent

    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cli(argv):
    parser = argparse.ArgumentParser(prog="memexa.core.dual_write_audit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_r = sub.add_parser("run", help="run audit and emit JSON")
    p_r.add_argument("--neo4j-mode",
                     choices=("live", "mock", "down"), default="live")
    p_r.add_argument("--outbox-dir", default=None)
    p_r.add_argument("--threshold", type=int, default=None)
    p_r.add_argument("--no-artifact", action="store_true",
                     help="skip writing dated json artifact")

    args = parser.parse_args(argv[1:])
    if args.cmd == "run":
        result = run_audit(
            neo4j_mode=args.neo4j_mode,
            outbox_dir=args.outbox_dir,
            threshold=args.threshold,
            write_artifact=not args.no_artifact,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] != "WARN_BREACH" else 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
