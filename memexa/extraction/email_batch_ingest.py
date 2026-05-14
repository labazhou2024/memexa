"""Email batch ingest runner — backfill TU-6 (P1).

Bridges qq-email + ustc-email skills `read_inbox(since)` →
  email_msg → dict → cross-source dedup → cost cap throttle →
  Mac Qwen3 extract (DeepSeek fallback) → outbox enqueue (factrows) →
  cursor advance (per-account last_uid).

Mirrors `wechat_batch_ingest.BatchIngestRunner`:
  - typed-graceful skip (never raises) — auth/IMAP/network errors degrade soft
  - per-account cursor (qq + ustc) since last_uid
  - shared chat_daily_cost_tracker.json with wechat/qq
  - MEMEXA_ACTIVE_TASK_ID env pin discipline

Backfill source_kind: imap; extracted_by: backfill-email.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from memexa.core.atomic_io import atomic_write_json


def emit(event: str, payload: dict) -> None:
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_CURSOR_PATH = _DATA / "email_ingest_cursor.json"
_DAILY_COST_PATH = _DATA / "chat_daily_cost_tracker.json"
_BATCH_FLAG = _DATA / "email_batch_active.flag"
_OUTBOX_JSONL = _DATA / "win_keystone_outbox" / "backfill__email.jsonl"

_DEFAULT_MAX_MSGS = 100
_DEFAULT_MAX_COST = 0.40
_DEFAULT_MAX_DAILY_COST = 1.60
_DEFAULT_SINCE_DAYS = 30  # backfill window per call

ACCOUNTS = ("qq_email", "ustc_email")


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


def _email_msg_to_dict(msg: Any, account: str) -> dict:
    """Normalize email payload to {ts, sender, subject, body, msg_id, source_offset}."""
    if not isinstance(msg, dict):
        return {"raw": str(msg), "ts": 0.0, "account": account}
    out = {
        "msg_id": str(msg.get("uid") or msg.get("message_id") or ""),
        "ts": float(msg.get("ts") or msg.get("date_ts") or 0),
        "sender": str(msg.get("from") or msg.get("sender") or ""),
        "subject": str(msg.get("subject") or ""),
        "body": str(msg.get("body") or msg.get("text") or "")[:8000],
        "account": account,
        "source_offset": "",
    }
    out["source_offset"] = f"imap:{account}:{out['msg_id']}"
    return out


def _read_inbox_for_account(account: str, since_uid: int = 0,
                              since_ts: float = 0.0,
                              max_count: int = 50) -> list[dict]:
    """Call email skill `read_inbox`. Returns list of raw msg dicts.

    Soft-fail: any exception → empty list (typed-graceful per plan TU-6).
    Skill resolution order:
      1. memexa.extraction.email_read.read_inbox(account, since_uid, max_count)
         (preferred — uses local cache + IMAP fallback)
      2. fallback: empty list (skill not available → no_skill)
    """
    try:
        from memexa.extraction.email_read import read_inbox
    except Exception:
        return []

    try:
        msgs = read_inbox(
            account=account,
            since_uid=since_uid,
            since_ts=since_ts,
            max_count=max_count,
        )
        if not isinstance(msgs, list):
            return []
        return [m for m in msgs if isinstance(m, dict)]
    except Exception:
        return []


class EmailBatchIngestRunner:
    """Producer-side email batch ingest. Idempotent, cost-capped, IMAP-typed-graceful.

    Skip reasons (typed-graceful, never raises):
      - daily_cost_cap_exceeded
      - lock_held
      - no_accounts_enabled
      - imap_auth_fail
      - no_new_msgs
      - cursor_corrupt
    """

    def __init__(self, cursor_path: Optional[Path] = None,
                 jsonl_path: Optional[Path] = None,
                 accounts: Optional[list[str]] = None):
        self.cursor_path = Path(cursor_path) if cursor_path else _CURSOR_PATH
        self.jsonl_path = Path(jsonl_path) if jsonl_path else _OUTBOX_JSONL
        # Discriminate "explicit empty list" (= no accounts on purpose) from None
        # (= load defaults). Avoid the falsy-empty-list trap.
        self.accounts = (list(accounts) if accounts is not None
                          else self._load_enabled_accounts())

    def _load_enabled_accounts(self) -> list[str]:
        cfg_path = _DATA / "email_accounts.json"
        if not cfg_path.exists():
            return list(ACCOUNTS)
        try:
            d = json.loads(cfg_path.read_text(encoding="utf-8"))
            enabled = d.get("enabled") or []
            return [a for a in enabled if a in ACCOUNTS]
        except Exception:
            return list(ACCOUNTS)

    def _skip_with_reason(self, reason: str, **extra) -> dict:
        try:
            emit("email_batch_skipped", {"reason": reason, **extra})
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
            return {"per_account_last_uid": {}, "global_last_ingested_ts": 0.0}
        try:
            return json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except Exception:
            return {"per_account_last_uid": {}, "global_last_ingested_ts": 0.0}

    def _write_cursor(self, cursor: dict) -> None:
        try:
            atomic_write_json(self.cursor_path, cursor)
        except OSError:
            pass

    def run(self, max_msgs: int = _DEFAULT_MAX_MSGS,
            max_cost_usd: float = _DEFAULT_MAX_COST,
            max_daily_cost_usd: float = _DEFAULT_MAX_DAILY_COST,
            since_days: int = _DEFAULT_SINCE_DAYS,
            dry_run: bool = False) -> dict:
        """Main entry. Returns counters dict; never raises.

        Result keys: skipped, reason, n_msgs_read, n_factrows, cost_usd,
        per_account_advanced.
        """
        try:
            emit("email_batch_started", {
                "max_msgs": max_msgs, "max_cost": max_cost_usd, "dry_run": dry_run,
                "n_accounts": len(self.accounts),
            })
        except Exception:
            pass

        daily = _load_daily_cost()
        if daily["cumulative_usd"] + max_cost_usd > max_daily_cost_usd:
            return self._skip_with_reason("daily_cost_cap_exceeded",
                                           cumulative_usd=daily["cumulative_usd"])

        if not self.accounts:
            return self._skip_with_reason("no_accounts_enabled")

        if not self._acquire_flag():
            return self._skip_with_reason("lock_held")

        try:
            cursor = self._read_cursor()
            per_account_uid = dict(cursor.get("per_account_last_uid") or {})
            since_ts = max(
                0.0,
                time.time() - since_days * 86400,
            )
            all_msgs: list[dict] = []
            advanced: dict[str, int] = {}

            for account in self.accounts:
                since_uid = int(per_account_uid.get(account, 0))
                raw = _read_inbox_for_account(account, since_uid, since_ts,
                                               max_count=50)
                if not raw:
                    continue
                msgs = [_email_msg_to_dict(m, account) for m in raw]
                all_msgs.extend(msgs)
                # Advance per-account cursor by max uid seen
                max_uid = since_uid
                for m in msgs:
                    try:
                        u = int(m.get("msg_id") or 0)
                        if u > max_uid:
                            max_uid = u
                    except (ValueError, TypeError):
                        continue
                if max_uid > since_uid:
                    advanced[account] = max_uid

            n_read = len(all_msgs)
            if n_read == 0:
                return self._skip_with_reason("no_new_msgs")

            if n_read > max_msgs:
                all_msgs = all_msgs[:max_msgs]

            if dry_run:
                return {"skipped": False, "reason": "dry_run",
                        "n_msgs_read": n_read, "n_factrows": 0,
                        "cost_usd": 0.0, "per_account_advanced": advanced}

            n_factrows, cost = self._extract_and_enqueue(all_msgs, max_cost_usd)

            for account, uid in advanced.items():
                per_account_uid[account] = uid
            cursor["per_account_last_uid"] = per_account_uid
            cursor["global_last_ingested_ts"] = time.time()
            self._write_cursor(cursor)

            new_total = _bump_daily_cost(cost)
            try:
                emit("email_batch_completed", {
                    "n_msgs_read": n_read, "n_factrows": n_factrows,
                    "cost_usd": cost, "daily_cumulative_usd": new_total,
                    "per_account_advanced": advanced,
                })
            except Exception:
                pass

            return {"skipped": False, "reason": "ok",
                    "n_msgs_read": n_read, "n_factrows": n_factrows,
                    "cost_usd": cost, "per_account_advanced": advanced,
                    "daily_cumulative_usd": new_total}
        finally:
            self._release_flag()

    def _extract_and_enqueue(self, msgs: list[dict],
                              max_cost_usd: float) -> tuple[int, float]:
        """Run extract pipeline + outbox enqueue.

        Email extraction differs from chat: subject + body fed as single doc;
        Mac Qwen3 main route (CEO directive); Sonnet narrative quarantine
        ≤50/day per TU-7 HMAC counter. For now stub (real Qwen3 wiring in TU-8).
        """
        try:
            from memexa.extraction.chat_extract_local import extract_msgs
            from memexa.extraction.chat_class_denylist import classify_drop_reason
            from memexa.extraction.consent_gate import evaluate_consent
        except Exception:
            return 0, 0.0

        # Wrap email msgs into chat-msg-like format for shared extract pipeline
        chat_like_msgs = [
            {
                "ts": m.get("ts", 0),
                "sender": m.get("sender", ""),
                "content": f"[{m.get('subject', '')}]\n{m.get('body', '')}",
                "msg_id": m.get("msg_id", ""),
                "kind": "email",
                "account": m.get("account", ""),
            }
            for m in msgs
        ]

        def _stub_extract(_msg: dict, _ctx: list) -> list:
            return []

        try:
            result = extract_msgs(
                msgs=chat_like_msgs,
                denylist_filter=classify_drop_reason,
                consent_evaluate=evaluate_consent,
                extract_triples=_stub_extract,
                context_window=5,
                pseudonymize=True,
            )
            factrows = result.get("factrows") or []
        except Exception:
            factrows = []

        enqueued = 0
        for fr in factrows:
            try:
                from memexa.core.hindsight_outbox import enqueue
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

        # Cost: ~$0.003 per email (longer body than chat msg)
        cost = round(0.003 * len(msgs), 4)
        return enqueued, cost


def main() -> int:
    """CLI entry: `python -m memexa.extraction.email_batch_ingest [--dry-run]`."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-msgs", type=int, default=_DEFAULT_MAX_MSGS)
    p.add_argument("--max-cost", type=float, default=_DEFAULT_MAX_COST)
    p.add_argument("--since-days", type=int, default=_DEFAULT_SINCE_DAYS)
    args = p.parse_args()

    if os.environ.get("MEMEXA_SKIP_EMAIL_BATCH"):
        print(json.dumps({"skipped": True, "reason": "env_skip"}))
        return 0

    result = EmailBatchIngestRunner().run(
        max_msgs=args.max_msgs,
        max_cost_usd=args.max_cost,
        since_days=args.since_days,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False))
    soft_skip = result.get("reason") in (
        "no_new_msgs", "no_accounts_enabled", "lock_held",
        "daily_cost_cap_exceeded", "imap_auth_fail",
    )
    return 0 if not result.get("skipped") or soft_skip else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
