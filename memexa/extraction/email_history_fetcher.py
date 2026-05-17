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
        from memexa.core.trace_sink import write_trace_event
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


def _decode_mime_header(raw: Any) -> str:
    """RFC2047 -> Unicode string. Handles email.header.Header objects too.

    v0.1.1: stdlib-only replacement for the deleted ``memexa.qq_email``
    helpers. Servers like Tencent Exmail wrap non-ASCII headers in
    ``=?utf-8?B?...?=`` or ``=?gb18030?Q?...?=`` MIME encodings;
    decode_header returns ``[(bytes_or_str, charset), ...]`` chunks
    which we reassemble.
    """
    from email.header import decode_header, Header
    if raw is None:
        return ""
    if isinstance(raw, Header):
        raw = str(raw)
    if not isinstance(raw, str):
        raw = str(raw)
    out = []
    for chunk, charset in decode_header(raw):
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_email_body(msg) -> tuple:
    """Walk a parsed email.Message; return (text_body, html_body) as str."""
    text_parts, html_parts = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            cdisp = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in cdisp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, TypeError):
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                text_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except (LookupError, TypeError):
                body = payload.decode("utf-8", errors="replace")
            if "html" in (msg.get_content_type() or "").lower():
                html_parts.append(body)
            else:
                text_parts.append(body)
    return ("\n".join(text_parts), "\n".join(html_parts))


def _extract_email_attachments(msg) -> List[str]:
    """Return list of attachment filenames (no payload data)."""
    names = []
    if not msg.is_multipart():
        return names
    for part in msg.walk():
        cdisp = str(part.get("Content-Disposition", "")).lower()
        if "attachment" not in cdisp:
            continue
        fname = part.get_filename()
        if fname:
            names.append(_decode_mime_header(fname))
    return names


def _parse_rfc822_date(raw: str) -> Optional[datetime]:
    """Parse 'Tue, 17 May 2026 08:32:11 +0800' -> datetime, or None."""
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw)
    except Exception:
        return None


def _safe_email_parse(raw_bytes: bytes) -> Dict[str, Any]:
    """Parse RFC822 -> JSON-friendly dict.

    v0.1.1: stdlib-only implementation. The v0.1.0 version imported
    ``_decode_header``, ``_extract_body``, ``_extract_attachments``,
    and ``_parse_date`` from ``memexa.qq_email`` -- a module that
    does not exist in OSS, so every email parse crashed with
    ``ModuleNotFoundError`` after the IMAP fetch succeeded. Now uses
    only Python's stdlib ``email`` package.
    """
    import email as email_lib
    msg = email_lib.message_from_bytes(raw_bytes)

    def _hdr(name: str) -> str:
        return _decode_mime_header(msg.get(name, ""))

    subject = _hdr("Subject")
    from_raw = _hdr("From")
    to_raw = _hdr("To")
    cc_raw = _hdr("Cc")
    date_raw = _hdr("Date")
    parsed_dt = _parse_rfc822_date(date_raw)
    text_body, html_body = _extract_email_body(msg)
    attachments = _extract_email_attachments(msg)
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


class EmailConfigMissing(Exception):
    """Raised when an email account config is incomplete (missing host /
    user / password env var, or env var is empty)."""


def _load_accounts_from_identity(identity_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load ``email.accounts`` from ``~/.memexa/identity.yaml``.

    v0.1.1 generic-IMAP rewrite: replaces the hard-coded
    ``qq_email``/``ustc_email`` clients that v0.1.0 shipped (those
    referenced ``memexa.qq_email`` / ``memexa.ustc_email`` modules
    that do not exist in OSS — the path was broken at runtime).

    Schema::

        email:
          accounts:
            primary:                    # arbitrary account name
              host: imap.example.com
              port: 993
              user: alice@example.com
              password_env: MEMEXA_IMAP_PASSWORD
              folders: [INBOX, Sent]    # optional, defaults to [INBOX, Sent]
              since_days: 90            # optional, defaults to 90
    """
    if identity_path is None:
        identity_path = Path(
            os.environ.get("MEMEXA_CONFIG_DIR", str(Path.home() / ".memexa"))
        ).expanduser() / "identity.yaml"
    if not identity_path.exists():
        raise EmailConfigMissing(
            f"identity.yaml not found at {identity_path} -- "
            "run `memexa init email` to scaffold one"
        )
    try:
        import yaml  # type: ignore
    except ImportError:
        raise EmailConfigMissing(
            "PyYAML required to read identity.yaml -- pip install memexa "
            "should have installed it; check your environment"
        )
    with identity_path.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    accounts = (data.get("email") or {}).get("accounts") or {}
    if not isinstance(accounts, dict) or not accounts:
        raise EmailConfigMissing(
            f"identity.yaml has no email.accounts block, or it is empty "
            f"(at {identity_path}) -- run `memexa init email`"
        )
    return accounts


def _generic_imap_connect(account_config: Dict[str, Any]) -> imaplib.IMAP4_SSL:
    """Open an IMAP4-SSL connection using a generic config dict.

    v0.1.1: replaces ``client._imap_connect()`` on the now-deleted
    hard-coded ``QQEmailClient`` / ``RemoteEmailClient`` objects.
    """
    host = account_config.get("host", "").strip()
    port = int(account_config.get("port", 993))
    user = account_config.get("user", "").strip()
    pw_env = account_config.get("password_env", "MEMEXA_IMAP_PASSWORD")
    password = os.environ.get(pw_env, "").strip()
    if not host or not user:
        raise EmailConfigMissing(
            f"account config missing host or user (got host={host!r} user={user!r})"
        )
    if not password:
        raise EmailConfigMissing(
            f"env var {pw_env} is empty or unset -- "
            f"export {pw_env}='<your-IMAP-app-specific-password>' first"
        )
    conn = imaplib.IMAP4_SSL(host, port)
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error as e:
        raise EmailConfigMissing(
            f"IMAP login failed for {user}@{host}:{port} -- "
            f"check {pw_env} value and provider's IMAP enablement: {e}"
        )
    return conn


def fetch_account(
    account_name: str,
    account_config: Optional[Dict[str, Any]] = None,
    since_iso: Optional[str] = None,
    folders: Optional[List[str]] = None,
    max_per_folder: int = 99999,
) -> Dict[str, int]:
    """Fetch all emails for ``account_name`` since ``since_iso``.

    Args:
        account_name: label, used for cursor file + raw_dir partitioning
                      (e.g. ``"primary"``, ``"ustc"``).
        account_config: dict with ``host`` / ``port`` / ``user`` /
                        ``password_env`` / ``folders`` / ``since_days``.
                        If None, looked up from identity.yaml by name.
        since_iso: ISO date string. None = use ``account_config['since_days']``
                   relative to today.
        folders: explicit folder list. None = use ``account_config['folders']``.
        max_per_folder: cap to avoid runaway downloads.

    Returns:
        ``{'fetched': N, 'skipped': N, 'errors': N, 'folders_ok': N}``
    """
    if account_config is None:
        accounts = _load_accounts_from_identity()
        if account_name not in accounts:
            raise EmailConfigMissing(
                f"account {account_name!r} not found in identity.yaml; "
                f"available: {sorted(accounts.keys())}"
            )
        account_config = accounts[account_name]

    if folders is None:
        folders = account_config.get("folders") or ["INBOX", "Sent"]

    if since_iso is None:
        since_days = int(account_config.get("since_days", 90))
        since_dt = datetime.now(timezone.utc) - timedelta(days=since_days)
        since_iso = since_dt.strftime("%Y-%m-%d")

    # Convert since_iso → IMAP date format (DD-Mon-YYYY)
    if "T" in since_iso:
        since_dt = datetime.fromisoformat(since_iso)
    else:
        since_dt = datetime.fromisoformat(since_iso + "T00:00:00").replace(
            tzinfo=timezone.utc
        )
    imap_since = since_dt.strftime("%d-%b-%Y")

    cursor = load_cursor(account_name)
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

    # v0.1.1: generic IMAP client (replaces removed memexa.qq_email /
    # memexa.ustc_email lookup)
    conn = _generic_imap_connect(account_config)
    account = account_name  # back-compat alias for the rest of this function
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
    """v0.1.1 generic IMAP CLI.

    Reads accounts from ``~/.memexa/identity.yaml`` (email.accounts block)
    or honors ``MEMEXA_CONFIG_DIR``. Run ``memexa init email`` first if
    no accounts are configured.
    """
    import argparse
    p = argparse.ArgumentParser(
        prog="email_history_fetcher",
        description="IMAP fetch — pulls raw email payloads to "
                    "data/raw_inputs/email/<account>/<date>/<folder>/<uid>.json "
                    "for downstream extraction.",
    )
    p.add_argument(
        "--account",
        default="all",
        help="account name from identity.yaml (e.g. 'primary'), or 'all' "
             "for every configured account",
    )
    p.add_argument(
        "--since",
        default=None,
        help="ISO date 'YYYY-MM-DD'; if omitted, uses each account's "
             "configured since_days relative to today",
    )
    p.add_argument(
        "--folders", nargs="*", default=None,
        help="explicit folder list; overrides identity.yaml config",
    )
    p.add_argument("--max-per-folder", type=int, default=99999)
    p.add_argument(
        "--identity", type=Path, default=None,
        help="path to identity.yaml (default: ~/.memexa/identity.yaml)",
    )
    args = p.parse_args(argv[1:])

    try:
        all_accounts = _load_accounts_from_identity(args.identity)
    except EmailConfigMissing as e:
        print(f"[fail] {e}", file=sys.stderr)
        print("\nHint: run `memexa init email` to scaffold an account.",
              file=sys.stderr)
        return 1

    if args.account == "all":
        account_names = sorted(all_accounts.keys())
    else:
        if args.account not in all_accounts:
            print(f"[fail] account {args.account!r} not in identity.yaml; "
                  f"available: {sorted(all_accounts.keys())}", file=sys.stderr)
            return 1
        account_names = [args.account]

    overall = {"fetched": 0, "skipped": 0, "errors": 0}
    rc = 0
    for acc in account_names:
        cfg = all_accounts[acc]
        host = cfg.get("host", "(no host)")
        user = cfg.get("user", "(no user)")
        print(f"=== fetching {acc} ({user}@{host}) ===")
        try:
            r = fetch_account(
                acc, account_config=cfg,
                since_iso=args.since, folders=args.folders,
                max_per_folder=args.max_per_folder,
            )
            print(f"  {acc}: {r}")
            for k in ("fetched", "skipped", "errors"):
                overall[k] += r.get(k, 0)
        except EmailConfigMissing as e:
            print(f"  {acc} CONFIG FAIL: {e}", file=sys.stderr)
            overall["errors"] += 1
            rc = 1
        except Exception as e:
            print(f"  {acc} FAIL: {type(e).__name__}: {e}", file=sys.stderr)
            overall["errors"] += 1
            rc = 1
    print(f"=== overall: {overall} ===")
    return rc


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
