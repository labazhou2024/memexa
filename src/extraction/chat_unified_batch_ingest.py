"""Chat unified batch ingest orchestrator — backfill TU-6 (P1).

Drives 3 independent batches in series (intentional — single shared cost cap):
  1. WeChat (src.extraction.wechat_batch_ingest.BatchIngestRunner)
  2. QQ     (src.extraction.qq_batch_ingest.QQBatchIngestRunner)
  3. Email  (src.extraction.email_batch_ingest.EmailBatchIngestRunner)

Per-source realtime mode flags evaluated independently — if WeChat is in
realtime mode but QQ is not, only WeChat skips.

Flag stash protocol (per plan TU-6 action 5 + Stage 5 pre-commit protocol):
  - MEMEXA_ACTIVE_TASK_ID env pin REQUIRED for backfill driver invocations
    (long-running shells); checked at run() entry.
  - autopilot_active.json conflict detection: if flag.task_id != ENV task_id,
    callers (the backfill driver scripts) must stash flag → run → restore.
  - chat_unified itself does NOT touch the flag (read-only check + warn).

Wired into scripts/run_graph_maintenance.py step_5 to extend daily 6h batch.

Returns aggregated counters; never raises.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from src.core.atomic_io import atomic_write_json


def emit(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_HARNESS = _REPO.parent / ".claude" / "harness"
_AUTOPILOT_FLAG = _HARNESS / "autopilot_active.json"
_REALTIME_FLAG = _DATA / "chat_realtime_mode.json"

_DEFAULT_MAX_MSGS_PER_SOURCE = 200
_DEFAULT_MAX_COST_PER_SOURCE = 0.40
_DEFAULT_MAX_DAILY_COST = 1.60

_BACKFILL_TASK_ID = "20260501_180000_2026_backfill"


def _check_env_pin() -> tuple[bool, Optional[str]]:
    """Backfill discipline: env pin must equal backfill task id.

    Returns (ok, mismatch_value). For non-backfill production GraphMaintenance6h
    invocation, env pin not required (returns (True, None)).
    """
    env_tid = os.environ.get("MEMEXA_ACTIVE_TASK_ID")
    if not env_tid:
        return True, None  # not required outside backfill
    if env_tid != _BACKFILL_TASK_ID:
        return False, env_tid
    return True, env_tid


def _check_autopilot_flag_conflict() -> tuple[bool, Optional[str]]:
    """Detect autopilot flag owned by different task.

    Returns (conflict, owner_task_id). conflict=True → caller must stash.
    """
    if not _AUTOPILOT_FLAG.exists():
        return False, None
    try:
        d = json.loads(_AUTOPILOT_FLAG.read_text(encoding="utf-8"))
        owner = d.get("task_id") or ""
        env_tid = os.environ.get("MEMEXA_ACTIVE_TASK_ID")
        if env_tid and owner and owner != env_tid:
            return True, owner
        return False, owner
    except Exception:
        return False, None


def _per_source_realtime_status() -> dict:
    """Read per-source realtime mode states (passive observability only).

    Returns dict {wechat: bool, qq: bool, email: bool} or per-source 'none'.
    Flag schema (chat_realtime_mode.json):
      {"wechat": {"enabled_at": ts, "ttl_hours": 24}, "qq": ..., "email": ...}
    """
    out = {"wechat": False, "qq": False, "email": False}
    if not _REALTIME_FLAG.exists():
        return out
    try:
        d = json.loads(_REALTIME_FLAG.read_text(encoding="utf-8"))
    except Exception:
        return out
    now = time.time()
    for src in ("wechat", "qq", "email"):
        cfg = d.get(src) or {}
        enabled_at = float(cfg.get("enabled_at", 0))
        ttl = int(cfg.get("ttl_hours", 24))
        if enabled_at > 0 and (now - enabled_at) < ttl * 3600:
            out[src] = True
    return out


def run_unified_batch(
    max_msgs_per_source: int = _DEFAULT_MAX_MSGS_PER_SOURCE,
    max_cost_per_source: float = _DEFAULT_MAX_COST_PER_SOURCE,
    max_daily_cost_usd: float = _DEFAULT_MAX_DAILY_COST,
    skip_sources: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Run unified chat batch across wechat + qq + email.

    Args:
      max_msgs_per_source: per-batch message cap.
      max_cost_per_source: per-batch USD cap (cost cumulates against daily).
      max_daily_cost_usd: shared daily cumulative cap across sources.
      skip_sources: list ⊆ {"wechat", "qq", "email"} to skip this run.
      dry_run: pass through to each batch.

    Returns aggregated dict {sources: {wechat: <res>, qq: <res>, email: <res>},
    n_total_factrows, n_total_msgs_read, total_cost_usd, env_pin_ok,
    autopilot_flag_conflict}.
    """
    skip_sources = set(skip_sources or [])

    env_ok, env_value = _check_env_pin()
    flag_conflict, flag_owner = _check_autopilot_flag_conflict()
    realtime = _per_source_realtime_status()

    try:
        emit("chat_unified_started", {
            "skip_sources": list(skip_sources),
            "env_pin_ok": env_ok, "env_value": env_value,
            "autopilot_flag_conflict": flag_conflict,
            "autopilot_flag_owner": flag_owner,
            "realtime_status": realtime,
            "dry_run": dry_run,
        })
    except Exception:
        pass

    sources_results: dict = {}

    # WeChat
    if "wechat" in skip_sources or realtime.get("wechat"):
        sources_results["wechat"] = {"skipped": True,
                                      "reason": "skip_explicit_or_realtime"}
    else:
        sources_results["wechat"] = _run_wechat(
            max_msgs_per_source, max_cost_per_source,
            max_daily_cost_usd, dry_run,
        )

    # QQ
    if "qq" in skip_sources or realtime.get("qq"):
        sources_results["qq"] = {"skipped": True,
                                  "reason": "skip_explicit_or_realtime"}
    else:
        sources_results["qq"] = _run_qq(
            max_msgs_per_source, max_cost_per_source,
            max_daily_cost_usd, dry_run,
        )

    # Email
    if "email" in skip_sources or realtime.get("email"):
        sources_results["email"] = {"skipped": True,
                                     "reason": "skip_explicit_or_realtime"}
    else:
        sources_results["email"] = _run_email(
            max_msgs_per_source, max_cost_per_source,
            max_daily_cost_usd, dry_run,
        )

    n_total_msgs_read = sum(
        int(r.get("n_msgs_read", 0)) for r in sources_results.values()
    )
    n_total_factrows = sum(
        int(r.get("n_factrows", 0)) for r in sources_results.values()
    )
    total_cost = round(sum(
        float(r.get("cost_usd", 0)) for r in sources_results.values()
    ), 4)

    try:
        emit("chat_unified_completed", {
            "n_total_msgs_read": n_total_msgs_read,
            "n_total_factrows": n_total_factrows,
            "total_cost_usd": total_cost,
            "n_sources_run": sum(1 for r in sources_results.values()
                                  if not r.get("skipped")),
        })
    except Exception:
        pass

    return {
        "sources": sources_results,
        "n_total_msgs_read": n_total_msgs_read,
        "n_total_factrows": n_total_factrows,
        "total_cost_usd": total_cost,
        "env_pin_ok": env_ok,
        "env_pin_value": env_value,
        "autopilot_flag_conflict": flag_conflict,
        "autopilot_flag_owner": flag_owner,
    }


def _run_wechat(max_msgs: int, max_cost: float, max_daily: float,
                 dry_run: bool) -> dict:
    try:
        from src.extraction.wechat_batch_ingest import BatchIngestRunner
    except Exception as e:
        return {"skipped": True, "reason": "wechat_module_unavailable",
                "error": type(e).__name__}
    try:
        return BatchIngestRunner().run(
            max_msgs=max_msgs,
            max_cost_usd=max_cost,
            max_daily_cost_usd=max_daily,
            dry_run=dry_run,
        )
    except Exception as e:
        return {"skipped": True, "reason": "wechat_run_error",
                "error": type(e).__name__}


def _run_qq(max_msgs: int, max_cost: float, max_daily: float,
             dry_run: bool) -> dict:
    try:
        from src.extraction.qq_batch_ingest import QQBatchIngestRunner
    except Exception as e:
        return {"skipped": True, "reason": "qq_module_unavailable",
                "error": type(e).__name__}
    try:
        return QQBatchIngestRunner().run(
            max_msgs=max_msgs,
            max_cost_usd=max_cost,
            max_daily_cost_usd=max_daily,
            dry_run=dry_run,
        )
    except Exception as e:
        return {"skipped": True, "reason": "qq_run_error",
                "error": type(e).__name__}


def _run_email(max_msgs: int, max_cost: float, max_daily: float,
                dry_run: bool) -> dict:
    try:
        from src.extraction.email_batch_ingest import EmailBatchIngestRunner
    except Exception as e:
        return {"skipped": True, "reason": "email_module_unavailable",
                "error": type(e).__name__}
    try:
        return EmailBatchIngestRunner().run(
            max_msgs=max_msgs,
            max_cost_usd=max_cost,
            max_daily_cost_usd=max_daily,
            dry_run=dry_run,
        )
    except Exception as e:
        return {"skipped": True, "reason": "email_run_error",
                "error": type(e).__name__}


def stash_autopilot_flag(target_task_id: str = _BACKFILL_TASK_ID,
                          backup_suffix: str = "BACKFILL_PHASE_BACKUP"
                          ) -> tuple[bool, Optional[Path]]:
    """Stash autopilot flag if owned by different task.

    Per plan TU-6 step 5 + Stage 5 pre-commit protocol. Caller (driver
    scripts and pre-commit shell) uses this before backfill long-runs to
    avoid pretool_gate Rule 16 collision.

    Returns (stashed, backup_path).
    """
    if not _AUTOPILOT_FLAG.exists():
        return False, None
    try:
        d = json.loads(_AUTOPILOT_FLAG.read_text(encoding="utf-8"))
        if d.get("task_id") == target_task_id:
            return False, None
        backup = _AUTOPILOT_FLAG.with_suffix(
            _AUTOPILOT_FLAG.suffix + "." + backup_suffix
        )
        # If a stale backup already exists from a crashed prior run, leave it
        # but DO NOT overwrite — emit warn instead.
        if backup.exists():
            try:
                emit("flag_stash_skip_existing_backup", {
                    "backup_path": str(backup),
                })
            except Exception:
                pass
            return False, backup
        _AUTOPILOT_FLAG.rename(backup)
        try:
            emit("flag_stash_done", {
                "stashed_owner": d.get("task_id"),
                "backup_path": str(backup),
            })
        except Exception:
            pass
        return True, backup
    except Exception:
        return False, None


def restore_autopilot_flag(backup_suffix: str = "BACKFILL_PHASE_BACKUP"
                             ) -> bool:
    """Restore autopilot flag from backup. Returns True iff restore happened."""
    backup = _AUTOPILOT_FLAG.with_suffix(
        _AUTOPILOT_FLAG.suffix + "." + backup_suffix
    )
    if not backup.exists():
        return False
    try:
        if _AUTOPILOT_FLAG.exists():
            # Current flag in place; preserve backup but don't crash
            try:
                emit("flag_restore_skip_existing_flag", {
                    "current_flag": str(_AUTOPILOT_FLAG),
                    "backup_path": str(backup),
                })
            except Exception:
                pass
            return False
        backup.rename(_AUTOPILOT_FLAG)
        try:
            emit("flag_restore_done", {"restored_from": str(backup)})
        except Exception:
            pass
        return True
    except Exception:
        return False


def main() -> int:
    """CLI: `python -m src.extraction.chat_unified_batch_ingest [--dry-run]`."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-msgs", type=int, default=_DEFAULT_MAX_MSGS_PER_SOURCE)
    p.add_argument("--max-cost", type=float, default=_DEFAULT_MAX_COST_PER_SOURCE)
    p.add_argument("--skip", action="append", default=[],
                   choices=["wechat", "qq", "email"])
    args = p.parse_args()

    result = run_unified_batch(
        max_msgs_per_source=args.max_msgs,
        max_cost_per_source=args.max_cost,
        skip_sources=args.skip,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
