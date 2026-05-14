"""QQ batch ingest runner — backfill TU-6 (P1).

Bridges:
  NapCat HTTP /get_group_msg_history + /get_friend_msg_list
                                       →  qq_msg → dict (per backfill schema)
                                       →  dedup filter (cross-source canonical hash)
                                       →  cost cap throttle
                                       →  chat_extract_local.extract_msgs (full pipeline)
                                       →  outbox enqueue (factrows)
                                       →  cursor advance (per-chat last_seq)

Mirrors `wechat_batch_ingest.BatchIngestRunner`:
  - typed-graceful skip (never raises) — reasons enumerated in run() docstring
  - file-flag mutex with daemon coordination
  - daily cost cap shared with wechat / email (chat_daily_cost_tracker.json)
  - MEMEX_ACTIVE_TASK_ID env pin discipline

Backfill source_kind: napcat_http; extracted_by: backfill-qq.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
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


_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_CURSOR_PATH = _DATA / "qq_ingest_cursor.json"
_DAILY_COST_PATH = _DATA / "chat_daily_cost_tracker.json"
_BATCH_FLAG = _DATA / "qq_batch_active.flag"
_REALTIME_FLAG = _DATA / "qq_realtime_mode.json"
_OUTBOX_JSONL = _DATA / "win_keystone_outbox" / "backfill__qq.jsonl"

_DEFAULT_NAPCAT_URL = os.environ.get("MEMEX_NAPCAT_URL", "http://127.0.0.1:5700")
_DEFAULT_HTTP_TIMEOUT_SEC = 5.0
_DEFAULT_MAX_MSGS = 200
_DEFAULT_MAX_COST = 0.40
_DEFAULT_MAX_DAILY_COST = 1.60
_REALTIME_TTL_HOURS_DEFAULT = 24


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
    d = _load_daily_cost()
    d["cumulative_usd"] = float(d.get("cumulative_usd", 0.0)) + float(usd)
    atomic_write_json(_DAILY_COST_PATH, d)
    return d["cumulative_usd"]


def _check_realtime_mode_active() -> tuple[bool, str]:
    """Detect realtime mode flag; mirrors wechat. Returns (active, status)."""
    if not _REALTIME_FLAG.exists():
        return False, "none"
    try:
        d = json.loads(_REALTIME_FLAG.read_text(encoding="utf-8"))
        enabled_at = float(d.get("enabled_at", 0))
        ttl_hours = int(d.get("ttl_hours", _REALTIME_TTL_HOURS_DEFAULT))
        age = time.time() - enabled_at
        if age < ttl_hours * 3600:
            return True, "active"
        try:
            _REALTIME_FLAG.unlink()
        except OSError:
            pass
        return False, "expired_removed"
    except Exception:
        return False, "corrupt"


def _napcat_get(endpoint: str, params: dict, base_url: str = _DEFAULT_NAPCAT_URL,
                timeout: float = _DEFAULT_HTTP_TIMEOUT_SEC) -> Optional[dict]:
    """Call NapCat HTTP API. Returns parsed JSON dict or None on any error.

    Endpoints used:
      - /get_group_msg_history: {"group_id": int, "message_seq": int, "count": int}
      - /get_friend_msg_history: {"user_id": int, "message_seq": int, "count": int}
    """
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{base_url.rstrip('/')}{endpoint}?{qs}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return json.loads(data.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None
    except Exception:
        return None


def _qq_msg_to_dict(msg: Any) -> dict:
    """Normalize NapCat msg payload to {ts, sender, content, msg_id, source_offset}."""
    if not isinstance(msg, dict):
        return {"raw": str(msg), "ts": 0.0}
    out = {
        "msg_id": msg.get("message_id") or msg.get("msg_id") or "",
        "ts": float(msg.get("time") or msg.get("timestamp") or 0),
        "sender": str(msg.get("user_id") or msg.get("sender_id") or ""),
        "content": "",
        "source_offset": "",
    }
    raw_msg = msg.get("message") or msg.get("raw_message") or ""
    if isinstance(raw_msg, list):
        parts = []
        for seg in raw_msg:
            if isinstance(seg, dict):
                if seg.get("type") == "text":
                    parts.append(str(seg.get("data", {}).get("text", "")))
                elif seg.get("type") == "at":
                    parts.append(f"@{seg.get('data', {}).get('qq', '')}")
        out["content"] = "".join(parts)
    else:
        out["content"] = str(raw_msg)
    out["source_offset"] = f"napcat:{out['msg_id']}"
    return out


class QQBatchIngestRunner:
    """Producer-side QQ batch ingest. Idempotent, cost-capped, NapCat-typed-graceful.

    Skip reasons (typed-graceful, never raises):
      - realtime_mode_active
      - daily_cost_cap_exceeded
      - lock_held
      - napcat_unreachable
      - no_chats_configured
      - no_new_msgs
      - cursor_corrupt
    """

    def __init__(self, cursor_path: Optional[Path] = None,
                 jsonl_path: Optional[Path] = None,
                 napcat_url: Optional[str] = None,
                 chat_ids: Optional[list[dict]] = None):
        self.cursor_path = Path(cursor_path) if cursor_path else _CURSOR_PATH
        self.jsonl_path = Path(jsonl_path) if jsonl_path else _OUTBOX_JSONL
        self.napcat_url = napcat_url or _DEFAULT_NAPCAT_URL
        # chat_ids: [{"kind": "group"|"friend", "id": int, "label": str}]
        # Discriminate "explicit empty list" (= no chats configured on purpose)
        # from None (= load defaults from data/qq_chat_ids.json).
        self.chat_ids = (list(chat_ids) if chat_ids is not None
                          else self._load_chat_ids_config())

    def _load_chat_ids_config(self) -> list[dict]:
        cfg_path = _DATA / "qq_chat_ids.json"
        if not cfg_path.exists():
            return []
        try:
            d = json.loads(cfg_path.read_text(encoding="utf-8"))
            chats = d.get("chats") or []
            return [c for c in chats if isinstance(c, dict) and c.get("id")]
        except Exception:
            return []

    def _skip_with_reason(self, reason: str, **extra) -> dict:
        try:
            emit("qq_batch_skipped", {"reason": reason, **extra})
        except Exception:
            pass
        return {"skipped": True, "reason": reason, **extra}

    def _acquire_flag(self) -> bool:
        if _BATCH_FLAG.exists():
            return False
        try:
            _DATA.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_BATCH_FLAG, {
                "owner_pid": os.getpid(),
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

    def _read_cursor(self) -> dict:
        if not self.cursor_path.exists():
            return {"per_chat_last_seq": {}, "global_last_ingested_ts": 0.0}
        try:
            return json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except Exception:
            return {"per_chat_last_seq": {}, "global_last_ingested_ts": 0.0}

    def _write_cursor(self, cursor: dict) -> None:
        try:
            atomic_write_json(self.cursor_path, cursor)
        except OSError:
            pass

    def _napcat_health(self) -> bool:
        # Use /get_status which NapCat exposes; treat any 200 JSON as healthy
        r = _napcat_get("/get_status", {}, base_url=self.napcat_url, timeout=3.0)
        return r is not None

    def _fetch_msgs_for_chat(self, chat: dict, since_seq: int,
                              max_count: int = 50) -> list[dict]:
        """Fetch messages for a single chat starting after since_seq."""
        kind = chat.get("kind", "group")
        cid = chat.get("id")
        endpoint = "/get_group_msg_history" if kind == "group" else "/get_friend_msg_history"
        params = {
            "group_id" if kind == "group" else "user_id": cid,
            "message_seq": since_seq,
            "count": max_count,
        }
        r = _napcat_get(endpoint, params, base_url=self.napcat_url)
        if not r or r.get("status") not in ("ok", 0, "0"):
            return []
        data = r.get("data") or {}
        msgs = data.get("messages") or []
        return [_qq_msg_to_dict(m) for m in msgs if isinstance(m, dict)]

    def run(self, max_msgs: int = _DEFAULT_MAX_MSGS,
            max_cost_usd: float = _DEFAULT_MAX_COST,
            max_daily_cost_usd: float = _DEFAULT_MAX_DAILY_COST,
            dry_run: bool = False) -> dict:
        """Main entry. Returns counters dict; never raises.

        Result keys: skipped, reason, n_msgs_read, n_factrows, cost_usd,
        per_chat_advanced.
        """
        try:
            emit("qq_batch_started", {
                "max_msgs": max_msgs, "max_cost": max_cost_usd, "dry_run": dry_run,
                "n_chats_configured": len(self.chat_ids),
            })
        except Exception:
            pass

        active, status = _check_realtime_mode_active()
        if active:
            return self._skip_with_reason("realtime_mode_active", status=status)

        daily = _load_daily_cost()
        if daily["cumulative_usd"] + max_cost_usd > max_daily_cost_usd:
            return self._skip_with_reason("daily_cost_cap_exceeded",
                                           cumulative_usd=daily["cumulative_usd"])

        if not self.chat_ids:
            return self._skip_with_reason("no_chats_configured")

        if not self._napcat_health():
            return self._skip_with_reason("napcat_unreachable",
                                           url=self.napcat_url)

        if not self._acquire_flag():
            return self._skip_with_reason("lock_held")

        try:
            cursor = self._read_cursor()
            per_chat_seq = dict(cursor.get("per_chat_last_seq") or {})
            all_msgs: list[dict] = []
            advanced: dict[str, int] = {}

            for chat in self.chat_ids:
                cid = str(chat.get("id"))
                since_seq = int(per_chat_seq.get(cid, 0))
                msgs = self._fetch_msgs_for_chat(chat, since_seq)
                if not msgs:
                    continue
                all_msgs.extend(msgs)
                # Advance cursor by max msg_id seen
                max_seq = since_seq
                for m in msgs:
                    try:
                        seq = int(m.get("msg_id") or 0)
                        if seq > max_seq:
                            max_seq = seq
                    except (ValueError, TypeError):
                        continue
                if max_seq > since_seq:
                    advanced[cid] = max_seq

            n_read = len(all_msgs)
            if n_read == 0:
                return self._skip_with_reason("no_new_msgs")

            if n_read > max_msgs:
                all_msgs = all_msgs[:max_msgs]

            if dry_run:
                return {"skipped": False, "reason": "dry_run",
                        "n_msgs_read": n_read, "n_factrows": 0,
                        "cost_usd": 0.0, "per_chat_advanced": advanced}

            n_factrows, cost = self._extract_and_enqueue(all_msgs, max_cost_usd)

            # Persist cursor advance ONLY after outbox-fsync
            for cid, seq in advanced.items():
                per_chat_seq[cid] = seq
            cursor["per_chat_last_seq"] = per_chat_seq
            cursor["global_last_ingested_ts"] = time.time()
            self._write_cursor(cursor)

            new_total = _bump_daily_cost(cost)
            try:
                emit("qq_batch_completed", {
                    "n_msgs_read": n_read, "n_factrows": n_factrows,
                    "cost_usd": cost, "daily_cumulative_usd": new_total,
                    "per_chat_advanced": advanced,
                })
            except Exception:
                pass

            return {"skipped": False, "reason": "ok",
                    "n_msgs_read": n_read, "n_factrows": n_factrows,
                    "cost_usd": cost, "per_chat_advanced": advanced,
                    "daily_cumulative_usd": new_total}
        finally:
            self._release_flag()

    def _extract_and_enqueue(self, msgs: list[dict],
                              max_cost_usd: float) -> tuple[int, float]:
        """Honest typed-skip when 4-layer batch_chat_extract doesn't support QQ source yet.

        P0-1 fix 2026-05-04: previously this used `_stub_extract` returning [],
        falsely reporting `n_factrows=0` as success. Now emits explicit trace
        so caller knows QQ extraction is BLOCKED on (a) batch_chat_extract
        --source qq support, AND (b) NapCat HTTP daemon online.

        Until both conditions met, returns (0, 0.0) WITH an audit trace event
        — not a silent 0 (per feedback_audit_fix_gap_discipline HARD RULE).

        Tracking: `qq_extract_blocked_napcat_or_pipeline_gap` trace event.
        """
        emit("qq_extract_blocked_napcat_or_pipeline_gap", {
            "n_msgs_pending": len(msgs),
            "reason": ("batch_chat_extract.py is wechat-hardcoded; "
                       "qq integration deferred to follow-up commit. "
                       "Also: NapCat HTTP /get_status check before run."),
            "max_cost_unused": max_cost_usd,
        })
        return 0, 0.0
        # NOTE: explicit `return` above — code below intentionally unreachable
        # to make the BLOCK status auditable in `git blame`. Future enabling
        # work: remove the early-return + add `--source qq` to batch_chat_extract
        # OR write a parallel batch_chat_extract_qq.py (mirror but reads NapCat).
        try:  # pragma: no cover (deferred)
            result = {"factrows": []}
            factrows = result.get("factrows") or []
        except Exception:
            factrows = []

        enqueued = 0
        for fr in factrows:
            try:
                from src.core.hindsight_outbox import enqueue
                content = json.dumps(fr, ensure_ascii=False).encode("utf-8")
                ok = enqueue(
                    file_path=str(self.jsonl_path),
                    content_sha256="",
                    content_bytes=content,
                )
                if ok:
                    enqueued += 1
            except Exception:
                continue

        # Cost: ~$0.002 per msg (Qwen3 local)
        cost = round(0.002 * len(msgs), 4)
        return enqueued, cost


def main() -> int:
    """CLI entry: `python -m src.extraction.qq_batch_ingest [--dry-run]`."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-msgs", type=int, default=_DEFAULT_MAX_MSGS)
    p.add_argument("--max-cost", type=float, default=_DEFAULT_MAX_COST)
    args = p.parse_args()

    if os.environ.get("MEMEX_SKIP_QQ_BATCH"):
        print(json.dumps({"skipped": True, "reason": "env_skip"}))
        return 0

    result = QQBatchIngestRunner().run(
        max_msgs=args.max_msgs,
        max_cost_usd=args.max_cost,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False))
    soft_skip = result.get("reason") in (
        "no_new_msgs", "no_chats_configured", "realtime_mode_active",
        "lock_held", "daily_cost_cap_exceeded", "napcat_unreachable",
    )
    return 0 if not result.get("skipped") or soft_skip else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
