"""email_history_fetcher.py — Pull historical emails from configured accounts.

Goes beyond `email_batch_ingest.py` (which is incremental + cost-capped) by
supporting full historical backfill from any since-date with no extraction
cost (this module only fetches; the LLM extraction stage is deferred to
phase1_pipeline_email).

Per CEO directive 2026-05-08:
  - Backfill from 2026-01-01 to today
  - Both QQ + your-org accounts
  - Save raw payloads to disk; LLM stage runs later when GPU is allocated

Cursor file: data/cursors/email_<account>.json
  {
    "account": "qq_email" | "ustc_email",
    "last_uid_seen": "12345",
    "last_internal_date_iso": "2026-05-08T08:00:00+08:00",
    "n_total_fetched": 1234,
    "n_folders_scanned": ["INBOX", "Sent Messages"],
    "last_synced_at": "2026-05-08T17:30:00+08:00"
  }

Storage:
  data/raw_inputs/email/<account>/<yyyy-mm-dd>/<uid>.json
"""
from __future__ import annotations

import imaplib
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_RAW_DIR = _DATA / "raw_inputs" / "email"
_CURSOR_DIR = _DATA / "cursors"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def _emit(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def cursor_path(account: str) -> Path:
    return _CURSOR_DIR / f"email_{account}.json"


def load_cursor(account: str) -> Dict[str, Any]:
    p = cursor_path(account)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "account": account,
        "last_uid_seen": "",
        "last_internal_date_iso": "",
        "n_total_fetched": 0,
        "n_folders_scanned": [],
        "last_synced_at": "",
    }


def save_cursor(account: str, cursor: Dict[str, Any]) -> None:
    cursor["last_synced_at"] = datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )
    _atomic_write_json(cursor_path(account), cursor)


def _imap_search_since(conn: imaplib.IMAP4_SSL, since_date: str,
                        folder: str = "INBOX") -> List[bytes]:
    """Run IMAP SEARCH (SINCE date). Returns list of UID byte strings.

    `since_date` format: 'DD-Mon-YYYY' (e.g. '01-Jan-2026').
    """
    conn.select(folder, readonly=True)
    status, data = conn.uid("SEARCH", None, f'(SINCE "{since_date}")')
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _imap_fetch_msg_raw(
    conn: imaplib.IMAP4_SSL, uid: bytes
) -> Optional[Dict[str, Any]]:
    """UID FETCH the message. Return dict {raw_bytes, internal_date_raw}.

    INTERNALDATE may appear in any tuple's header or as a tail string in
    `data` (servers vary). We scan ALL elements, not just the first tuple.

    Returns None on failure.
    """
    status, data = conn.uid("FETCH", uid, "(RFC822 INTERNALDATE)")
    if status != "OK" or not data or data[0] is None:
        return None

    raw_bytes: Optional[bytes] = None
    internal_date: Optional[str] = None

    # Walk every element of data. Any bytes/string element may hold
    # INTERNALDATE; any tuple's [1] holds the RFC822 body.
    def _scan_for_internaldate(s: str) -> Optional[str]:
        if "INTERNALDATE" in s:
            after = s.split('INTERNALDATE "', 1)
            if len(after) > 1:
                return after[1].split('"', 1)[0]
        return None

    for elem in data:
        if isinstance(elem, tuple) and len(elem) >= 2:
            header, body = elem[0], elem[1]
            if isinstance(header, (bytes, bytearray)):
                hdr_s = bytes(header).decode("ascii", errors="replace")
                if internal_date is None:
                    internal_date = _scan_for_internaldate(hdr_s)
            if isinstance(body, (bytes, bytearray)) and raw_bytes is None:
                raw_bytes = bytes(body)
        elif isinstance(elem, (bytes, bytearray)):
            s = bytes(elem).decode("ascii", errors="replace")
            if internal_date is None:
                internal_date = _scan_for_internaldate(s)
        elif isinstance(elem, str):
            if internal_date is None:
                internal_date = _scan_for_internaldate(elem)

    if raw_bytes is None:
        return None
    return {
        "uid": uid.decode("ascii"),
        "raw_bytes": raw_bytes,
        "internal_date_raw": internal_date or "",
    }


def _parse_imap_date(s: str) -> Optional[datetime]:
    """Parse '08-May-2026 16:29:07 +0800' → datetime."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def _safe_email_parse(raw_bytes: bytes) -> Dict[str, Any]:
    """Parse RFC822 → JSON-friendly dict."""
    import email as email_lib
    from memexa.qq_email import (
        _decode_header, _extract_body, _extract_attachments, _parse_date,
    )
    msg = email_lib.message_from_bytes(raw_bytes)

    def _hdr(name: str) -> str:
        """Get header as decoded string. Handles QQ's Header objects."""
        v = msg.get(name, "")
        if v is None:
            return ""
        # Some servers (QQ) return email.header.Header objects directly
        # for non-ASCII headers. Force str() and decode again.
        return _decode_header(str(v))

    subject = _hdr("Subject")
    from_raw = _hdr("From")
    to_raw = _hdr("To")
    cc_raw = _hdr("Cc")
    date_raw = _hdr("Date")
    parsed_dt = _parse_date(date_raw)
    text_body, html_body = _extract_body(msg)
    attachments = _extract_attachments(msg)
    return {
        "subject": str(subject),
        "from_raw": str(from_raw),
        "to_raw": str(to_raw),
        "cc_raw": str(cc_raw),
        "date_header_raw": str(date_raw),
        "date_iso": parsed_dt.isoformat() if parsed_dt else "",
        "body_text": str(text_body or ""),
        "body_html_preview": str(html_body or "")[:1000],
        "body_html_len": len(html_body or ""),
        "attachments": [str(a) for a in attachments],
    }


def fetch_account(
    account: str,
    since_iso: str = "2026-01-01",
    folders: Optional[List[str]] = None,
    max_per_folder: int = 99999,
) -> Dict[str, int]:
    """Fetch all emails for `account` since `since_iso`.

    Args:
        account: 'qq_email' or 'ustc_email'.
        since_iso: ISO date '2026-01-01' or '2026-01-01T00:00:00+08:00'.
        folders: list of IMAP folder names. None = ['INBOX', 'Sent Messages'].
        max_per_folder: cap to avoid runaway downloads.

    Returns:
        {'fetched': N, 'skipped': N, 'errors': N, 'folders': [...]}
    """
    if account == "qq_email":
        from memexa.qq_email import QQEmailClient
        client = QQEmailClient()
    elif account == "ustc_email":
        from memexa.ustc_email import RemoteEmailClient
        client = RemoteEmailClient()
    else:
        raise ValueError(f"unknown account: {account!r}")

    if folders is None:
        # your-org and QQ use slightly different folder names; try common ones.
        folders = ["INBOX", "Sent Messages", "Sent", "&XfJSIJZk-"]  # last is QQ "已发送"

    # Convert since_iso → IMAP date format (DD-Mon-YYYY)
    if "T" in since_iso:
        since_dt = datetime.fromisoformat(since_iso)
    else:
        since_dt = datetime.fromisoformat(since_iso + "T00:00:00").replace(
            tzinfo=timezone.utc
        )
    imap_since = since_dt.strftime("%d-%b-%Y")

    cursor = load_cursor(account)
    # Resume: if last_internal_date_iso later than since_iso, advance start
    if cursor["last_internal_date_iso"]:
        try:
            cur_dt = datetime.fromisoformat(cursor["last_internal_date_iso"])
            if cur_dt > since_dt:
                imap_since = cur_dt.strftime("%d-%b-%Y")
        except ValueError:
            pass

    counters = {"fetched": 0, "skipped": 0, "errors": 0, "folders_ok": 0}
    folders_actually_scanned: List[str] = []

    conn = client._imap_connect()
    try:
        for folder in folders:
            try:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    continue
                folders_actually_scanned.append(folder)
                counters["folders_ok"] += 1
            except imaplib.IMAP4.error:
                continue

            try:
                uids = _imap_search_since(conn, imap_since, folder=folder)
            except imaplib.IMAP4.error as e:
                _emit("email_search_fail",
                      {"account": account, "folder": folder, "err": str(e)[:200]})
                counters["errors"] += 1
                continue

            uids = uids[-max_per_folder:]  # cap
            for uid in uids:
                uid_str = uid.decode("ascii")
                # Dedup by checking existing path (account + uid + folder hash).
                # Lightweight: use uid in filename; folder collisions resolved
                # by storing per-folder subdir.
                folder_slug = "".join(
                    c if c.isalnum() else "_" for c in folder
                )[:32]
                # We don't know date yet, write to temp date dir and rename.
                try:
                    raw = _imap_fetch_msg_raw(conn, uid)
                except imaplib.IMAP4.error:
                    counters["errors"] += 1
                    continue
                if raw is None:
                    counters["errors"] += 1
                    continue

                parsed_dt = _parse_imap_date(raw["internal_date_raw"])

                # Parse email body now (need Date header as fallback for date)
                try:
                    parsed = _safe_email_parse(raw["raw_bytes"])
                except Exception as e:
                    counters["errors"] += 1
                    _emit("email_parse_fail",
                          {"account": account, "uid": uid_str,
                           "err": str(e)[:200]})
                    continue

                # Fallback chain for date: INTERNALDATE → email.Date header → now
                if parsed_dt is None and parsed.get("date_iso"):
                    try:
                        parsed_dt = datetime.fromisoformat(parsed["date_iso"])
                    except ValueError:
                        parsed_dt = None
                if parsed_dt is None:
                    parsed_dt = datetime.now(timezone.utc)

                # IMAP SEARCH SINCE is date-only and timezone-server-local.
                # Trust it — don't apply naive UTC client filter (rejected
                # 48/50 valid messages on QQ INBOX in initial test).

                day = parsed_dt.strftime("%Y-%m-%d")
                target = (
                    _RAW_DIR / account / day / folder_slug
                    / f"{uid_str}.json"
                )
                if target.exists():
                    counters["skipped"] += 1
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)

                # `parsed` was already populated earlier (during date fallback)
                payload = {
                    "account": account,
                    "folder": folder,
                    "uid": uid_str,
                    "internal_date_raw": raw["internal_date_raw"],
                    "internal_date_iso": parsed_dt.isoformat(),
                    "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    **parsed,
                }
                _atomic_write_json(target, payload)
                counters["fetched"] += 1

                # Update cursor incrementally so kill-resume works.
                cursor["last_uid_seen"] = uid_str
                cursor["last_internal_date_iso"] = parsed_dt.isoformat()
                cursor["n_total_fetched"] = (
                    cursor.get("n_total_fetched", 0) + 1
                )
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    cursor["n_folders_scanned"] = sorted(set(
        cursor.get("n_folders_scanned", []) + folders_actually_scanned
    ))
    save_cursor(account, cursor)

    _emit("email_history_fetched", {
        "account": account, "since": since_iso, **counters,
    })
    return counters


def _cli(argv: List[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="email_history_fetcher")
    p.add_argument("--account", choices=["qq_email", "ustc_email", "all"],
                    default="all")
    p.add_argument("--since", default="2026-01-01")
    p.add_argument("--folders", nargs="*", default=None)
    p.add_argument("--max-per-folder", type=int, default=99999)
    args = p.parse_args(argv[1:])

    accounts = ["qq_email", "ustc_email"] if args.account == "all" else [args.account]
    overall = {"fetched": 0, "skipped": 0, "errors": 0}
    for acc in accounts:
        print(f"=== fetching {acc} since {args.since} ===")
        try:
            r = fetch_account(acc, since_iso=args.since,
                               folders=args.folders,
                               max_per_folder=args.max_per_folder)
            print(f"  {acc}: {r}")
            for k in ("fetched", "skipped", "errors"):
                overall[k] += r.get(k, 0)
        except Exception as e:
            print(f"  {acc} FAIL: {type(e).__name__}: {e}")
            overall["errors"] += 1
    print(f"=== overall: {overall} ===")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
