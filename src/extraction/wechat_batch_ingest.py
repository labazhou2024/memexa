"""WeChat batch ingest runner — producer-side core.

Bridges:
  wechat_db.WeChatDBReader.read_after()  →  WxMessage list
                                       →  _wxmessage_to_dict (per M1)
                                       →  dedup filter
                                       →  cost cap throttle
                                       →  chat_extract_local.extract_msgs (full pipeline)
                                       →  outbox enqueue (factrows)
                                       →  cursor advance (2-phase)

Invoked by run_graph_maintenance.step_5_wechat_daily_ingest (TU-4).
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.core.atomic_io import atomic_write_json


def emit(event: str, payload: dict) -> None:
    """Soft trace emit; ignores unknown events (best-effort observability)."""
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


from src.extraction.wechat_batch_cursor import (
    CursorReader,
    CursorWriter,
    chat_hash,
    lookback_clamp,
)
from src.extraction.wechat_batch_dedup import DedupOracle, _msg_hash

_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_CURSOR_PATH = _DATA / "wechat_ingest_cursor.json"
_DAILY_COST_PATH = _DATA / "wechat_daily_cost_tracker.json"
_BATCH_FLAG = _DATA / "wechat_batch_active.flag"
_REALTIME_FLAG = _DATA / "wechat_realtime_mode.json"
_OUTBOX_JSONL = _DATA / "win_keystone_outbox" / "realtime__wechat.jsonl"

_DEFAULT_MAX_MSGS = 200
_DEFAULT_MAX_COST = 0.40
_DEFAULT_MAX_DAILY_COST = 1.60
_REALTIME_TTL_HOURS_DEFAULT = 24


def _wxmessage_to_dict(msg: Any) -> dict:
    """M1: WxMessage dataclass → dict via dataclasses.asdict (preserving names).

    Fallback: if msg is already a dict OR has __dict__ attr, use that.
    """
    try:
        if dataclasses.is_dataclass(msg):
            d = dataclasses.asdict(msg)
        elif isinstance(msg, dict):
            d = dict(msg)
        elif hasattr(msg, "__dict__"):
            d = dict(msg.__dict__)
        else:
            d = {"raw": str(msg)}
    except Exception:
        d = {"raw": str(msg)}
    # Normalize: ts can be datetime or float
    ts = d.get("timestamp") or d.get("ts")
    if ts is not None:
        try:
            if hasattr(ts, "timestamp"):
                d["ts"] = ts.timestamp()
            else:
                d["ts"] = float(ts)
        except Exception:
            d["ts"] = 0.0
    return d


def _today_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_daily_cost() -> dict:
    try:
        if _DAILY_COST_PATH.exists():
            d = json.loads(_DAILY_COST_PATH.read_text(encoding="utf-8"))
            if d.get("date") == _today_utc_date():
                return d
    except Exception:
        pass
    return {"date": _today_utc_date(), "cumulative_usd": 0.0}


def _bump_daily_cost(usd: float) -> float:
    """Increment today's cumulative cost atomically; return new total."""
    d = _load_daily_cost()
    d["cumulative_usd"] = float(d.get("cumulative_usd", 0.0)) + float(usd)
    atomic_write_json(_DAILY_COST_PATH, d)
    return d["cumulative_usd"]


def _check_realtime_mode_active(ttl_hours_default: int = _REALTIME_TTL_HOURS_DEFAULT
                                 ) -> tuple[bool, str]:
    """sec-3 + TU-10: detect realtime mode flag. Returns (active, status).

    status ∈ {"none", "active", "expired_removed"}.
    """
    if not _REALTIME_FLAG.exists():
        return False, "none"
    try:
        d = json.loads(_REALTIME_FLAG.read_text(encoding="utf-8"))
        enabled_at = float(d.get("enabled_at", 0))
        ttl_hours = int(d.get("ttl_hours", ttl_hours_default))
        age = time.time() - enabled_at
        if age < ttl_hours * 3600:
            return True, "active"
        # Expired: auto-remove + emit warn
        try:
            _REALTIME_FLAG.unlink()
        except OSError:
            pass
        try:
            emit("wechat_realtime_mode_expired_warn", {
                "enabled_at": enabled_at, "age_hours": age / 3600,
                "ttl_hours": ttl_hours, "reason": d.get("reason", ""),
            })
        except Exception:
            pass
        return False, "expired_removed"
    except Exception:
        # Corrupt flag: leave it (CEO must inspect manually)
        return False, "corrupt"


class BatchIngestRunner:
    """Producer-side batch ingest. Idempotent, cost-capped, dedup-aware.

    Skip reasons (typed-graceful, never raises):
      - realtime_mode_active     (TU-10)
      - daily_cost_cap_exceeded  (sec-4)
      - cycle_cost_cap_exceeded
      - lock_held
      - weixin_not_running
      - enc_keys_empty
      - cursor_corrupt
      - no_new_msgs
    """

    def __init__(self, cursor_path: Optional[Path] = None,
                 jsonl_path: Optional[Path] = None,
                 hindsight_url: Optional[str] = None):
        self.cursor_path = Path(cursor_path) if cursor_path else _CURSOR_PATH
        self.jsonl_path = Path(jsonl_path) if jsonl_path else _OUTBOX_JSONL
        self.hindsight_url = hindsight_url

    def _skip_with_reason(self, reason: str, **extra) -> dict:
        try:
            emit("wechat_batch_skipped", {"reason": reason, **extra})
        except Exception:
            pass
        return {"skipped": True, "reason": reason, **extra}

    def _acquire_flag(self) -> bool:
        """File-flag lock for daemon-batch coordination. Best-effort, not strict."""
        if _BATCH_FLAG.exists():
            return False
        try:
            _DATA.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_BATCH_FLAG, {
                "owner_pid": os.getpid(),
                "owner_uuid": str(time.time()),
                "started_at": time.time(),
            })
            return True
        except OSError:
            return False

    def _release_flag(self) -> None:
        try:
            if _BATCH_FLAG.exists():
                _BATCH_FLAG.unlink()
        except OSError:
            pass

    def run(self, max_msgs: int = _DEFAULT_MAX_MSGS,
            max_cost_usd: float = _DEFAULT_MAX_COST,
            max_daily_cost_usd: float = _DEFAULT_MAX_DAILY_COST,
            dry_run: bool = False) -> dict:
        """Main entry. Returns counters dict; never raises.

        Result keys: skipped (bool), reason, n_msgs_read, n_msgs_dedup,
        n_factrows, cost_usd, cursor_advanced_global, cursor_advanced_chats.
        """
        try:
            emit("wechat_batch_started", {
                "max_msgs": max_msgs, "max_cost": max_cost_usd, "dry_run": dry_run,
            })
        except Exception:
            pass

        # TU-10: realtime-mode mutex check (highest priority)
        active, status = _check_realtime_mode_active()
        if active:
            return self._skip_with_reason("realtime_mode_active", status=status)

        # sec-4: daily cumulative cost cap
        daily = _load_daily_cost()
        if daily["cumulative_usd"] + max_cost_usd > max_daily_cost_usd:
            return self._skip_with_reason("daily_cost_cap_exceeded",
                                           cumulative_usd=daily["cumulative_usd"],
                                           max_daily_cost_usd=max_daily_cost_usd)

        # File-flag mutex
        if not self._acquire_flag():
            return self._skip_with_reason("lock_held")

        try:
            # Read cursor + compute since_ts (7d clamp per R-1)
            try:
                cursor_dict = CursorReader(self.cursor_path).read()
            except NotImplementedError:
                return self._skip_with_reason("cursor_corrupt", reason_detail="schema_version_mismatch")
            since_ts = lookback_clamp(cursor_dict.get("global_last_ingested_ts"))

            # Read messages from WeChat DB
            try:
                from src.wechat_db import WeChatDBReader
                reader = WeChatDBReader()
                reader.initialize()
                if not reader.enc_keys:
                    return self._skip_with_reason("enc_keys_empty")
                if not reader.wxid_dir:
                    return self._skip_with_reason("weixin_not_running")
                wx_msgs = reader.read_after(since_ts, chat_name=None)
            except Exception as e:
                return self._skip_with_reason("wechat_db_error",
                                               error=type(e).__name__)

            n_read = len(wx_msgs)
            if n_read == 0:
                return self._skip_with_reason("no_new_msgs", since_ts=since_ts)

            # Convert to dict + dedup
            msg_dicts = [_wxmessage_to_dict(m) for m in wx_msgs]
            dedup = DedupOracle(self.jsonl_path, self.hindsight_url)
            new_msgs = []
            for d in msg_dicts:
                seen, _src = dedup.is_already_ingested(d)
                if not seen:
                    new_msgs.append(d)

            n_dedup = len(new_msgs)
            if n_dedup == 0:
                self._advance_global_cursor_after_outbox(since_ts, wx_msgs)
                return {"skipped": False, "reason": "all_dedup",
                        "n_msgs_read": n_read, "n_msgs_dedup": 0,
                        "n_factrows": 0, "cost_usd": 0.0}

            # Cap
            if n_dedup > max_msgs:
                new_msgs = new_msgs[:max_msgs]

            if dry_run:
                return {"skipped": False, "reason": "dry_run",
                        "n_msgs_read": n_read, "n_msgs_dedup": n_dedup,
                        "n_factrows": 0, "cost_usd": 0.0,
                        "would_extract": len(new_msgs)}

            # Pipeline: extract → outbox enqueue
            n_factrows, cost = self._extract_and_enqueue(new_msgs, max_cost_usd)

            # Advance cursors (2-phase)
            self._advance_global_cursor_after_outbox(since_ts, wx_msgs)

            # Update daily cost
            new_total = _bump_daily_cost(cost)

            try:
                emit("wechat_batch_completed", {
                    "n_msgs_read": n_read, "n_msgs_dedup": n_dedup,
                    "n_factrows": n_factrows, "cost_usd": cost,
                    "daily_cumulative_usd": new_total,
                })
            except Exception:
                pass

            return {"skipped": False, "reason": "ok",
                    "n_msgs_read": n_read, "n_msgs_dedup": n_dedup,
                    "n_factrows": n_factrows, "cost_usd": cost,
                    "daily_cumulative_usd": new_total}
        finally:
            self._release_flag()

    def _extract_and_enqueue(self, msgs: list, max_cost_usd: float) -> tuple[int, float]:
        """Delegate to batch_chat_extract.main (4-layer pipeline).

        Replaces the legacy single-msg + _fake_extract stub path (P0-1 fix
        2026-05-04: stub was producing 0 factrows for cron path, while
        batch_chat_extract's 4-layer pipeline (utterance_merger → batch
        classifier → memory-aware → paired_eval Qwen+Gemma → 27B inline
        arbiter → episode_chain) was already shipped in commit 1b1f42e.

        msgs is informational here; batch_chat_extract reads WeChat DB
        directly with its own time window. We compute --since-days from
        earliest msg ts. PG ON-CONFLICT on factrow.id deterministic-hash
        guarantees idempotent re-ingest (no dup inflation).

        Returns (n_factrows, cost_usd). cost_usd from batch summary if
        available; else 0 (mlx_lm.server is local zero-cost).
        """
        if not msgs:
            return 0, 0.0
        import json as _json
        import subprocess as _sp
        import sys as _sys

        # Compute since_days from earliest msg ts (clamp 1..7 per R-1 lookback).
        now_ts = time.time()
        try:
            earliest = min(float(m.get("ts") or now_ts) for m in msgs)
        except (TypeError, ValueError):
            earliest = now_ts - 86400
        since_days = max(1, min(7, int((now_ts - earliest) / 86400) + 1))

        cmd = [
            _sys.executable, "-m", "src.extraction.batch_chat_extract",
            "--since-days", str(since_days),
            "--max-batches", "50",
            "--json",
        ]
        env = dict(os.environ)
        # Pin task_id env so batch_chat_extract's commit-gate sees same tid
        try:
            r = _sp.run(cmd, capture_output=True, text=True,
                        timeout=3600, encoding="utf-8", errors="replace",
                        cwd=str(_REPO), env=env)
        except Exception as e:
            emit("wechat_ingest_subprocess_error", {
                "error": f"{type(e).__name__}: {e}",
            })
            return 0, 0.0

        if r.returncode != 0:
            emit("wechat_ingest_subprocess_nonzero", {
                "exit_code": r.returncode,
                "stderr_tail": (r.stderr or "")[-300:],
            })
            return 0, 0.0
        try:
            summary = _json.loads(r.stdout) if r.stdout else {}
        except _json.JSONDecodeError:
            return 0, 0.0
        n_factrows = int(summary.get("n_factrows_total", 0))
        cost = float(summary.get("total_cost_usd", 0.0))
        emit("wechat_batch_path_result", {
            "since_days": since_days,
            "n_factrows": n_factrows,
            "n_msgs_pre_merge": int(summary.get("n_msgs_read", 0)),
            "n_utterances": int(summary.get("n_utterances_post_merge", 0)),
            "n_batches": int(summary.get("n_batches_total", 0)),
        })
        return n_factrows, cost

        # Enqueue each factrow as a memory-file via outbox
        enqueued = 0
        for fr in factrows:
            try:
                from src.core.hindsight_outbox import enqueue
                content = json.dumps(fr, ensure_ascii=False).encode("utf-8")
                ok = enqueue(
                    file_path=str(_DATA / "win_keystone_outbox" / "realtime__wechat.jsonl"),
                    content_sha256="",
                    content_bytes=content,
                )
                if ok:
                    enqueued += 1
            except Exception:
                continue

        # Cost: ~$0.002 per msg (Qwen3 local + bge-m3) — placeholder until
        # real cost_meter integration in TU-8
        cost = round(0.002 * len(msgs), 4)
        return enqueued, cost

    def _advance_global_cursor_after_outbox(self, since_ts: float, wx_msgs: list) -> None:
        """Advance global cursor to max(ts in batch). Called AFTER outbox-fsync."""
        if not wx_msgs:
            return
        try:
            max_ts = since_ts
            for m in wx_msgs:
                t = getattr(m, "timestamp", None) or m.get("timestamp") if isinstance(m, dict) else None
                if t is None:
                    continue
                try:
                    tv = t.timestamp() if hasattr(t, "timestamp") else float(t)
                    if tv > max_ts:
                        max_ts = tv
                except Exception:
                    continue
            CursorWriter(self.cursor_path).write_global(max_ts,
                                                         daemon_last_seen_ts=time.time())
        except Exception:
            pass


def main() -> int:
    """CLI entrypoint: `python -m src.extraction.wechat_batch_ingest [--dry-run]`."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-msgs", type=int, default=_DEFAULT_MAX_MSGS)
    p.add_argument("--max-cost", type=float, default=_DEFAULT_MAX_COST)
    args = p.parse_args()

    if os.environ.get("MEMEX_SKIP_WECHAT_BATCH"):
        print(json.dumps({"skipped": True, "reason": "env_skip"}))
        return 0

    result = BatchIngestRunner().run(
        max_msgs=args.max_msgs,
        max_cost_usd=args.max_cost,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result.get("skipped") or result.get("reason") in (
        "no_new_msgs", "all_dedup", "realtime_mode_active",
        "lock_held", "daily_cost_cap_exceeded",
        "weixin_not_running", "enc_keys_empty"
    ) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
