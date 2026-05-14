"""V5-native email batch builder.

Reads raw email data from data/extract_archive_email_browser/<date>/<batch>/raw.json
(batches with prompt.json source_kind=="email" only; browser batches are skipped),
then writes V5-format prompt.json to:
  data/l0_v5/input_batches_email/<date>/<batch_id>/prompt.json

The output schema aligns with v5_wechat_batch_builder.py so v5 worker can
extract V2 envelope cards from email just like it does from WeChat batches.

Usage:
  python tools/v5_email_batch_builder.py \\
      --src data/extract_archive_email_browser \\
      --out data/l0_v5/input_batches_email \\
      --skip-existing

  python tools/v5_email_batch_builder.py \\
      --start 2026-01-01 --end 2026-05-11

  python tools/v5_email_batch_builder.py \\
      --max-batches 5 --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_SRC = ROOT / "data" / "extract_archive_email_browser"
DEFAULT_OUT = ROOT / "data" / "l0_v5" / "input_batches_email"
MANIFEST_PATH = ROOT / "data" / "identity_manifest.yaml"

BODY_TRUNCATE = 5000


# ---------------------------------------------------------------------------
# Hashing helpers (same approach as wechat builder)
# ---------------------------------------------------------------------------

def _email_hash(email_addr: str) -> str:
    """sha256(email_addr)[:16] — analogous to wxid_hash in wechat builder."""
    if not email_addr:
        return ""
    return hashlib.sha256(email_addr.strip().lower().encode("utf-8")).hexdigest()[:16]


def _room_hash(chat_room: str) -> str:
    """sha256(chat_room)[:32]."""
    return hashlib.sha256(chat_room.encode("utf-8", errors="replace")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Email address parsing
# ---------------------------------------------------------------------------

_ADDR_RE = re.compile(r"<([^>]+)>")
_BARE_RE = re.compile(r"[\w.+\-]+@[\w.\-]+")


def _parse_addr(field: str) -> tuple[str, str]:
    """Parse 'Display Name <addr@example.com>' -> (display_name, addr).

    Falls back to bare addr detection if angle brackets are absent.
    Returns ('', '') for empty/malformed strings.
    """
    if not field:
        return "", ""
    field = field.strip()
    m = _ADDR_RE.search(field)
    if m:
        addr = m.group(1).strip()
        # display name is everything before '<'
        name_part = field[: m.start()].strip().strip('"').strip("'").strip()
        return (name_part or addr, addr)
    # bare email without angle brackets
    m2 = _BARE_RE.search(field)
    if m2:
        addr = m2.group(0)
        return (addr, addr)
    return (field, "")


def _parse_addr_list(raw: str) -> list[tuple[str, str]]:
    """Split comma-separated address list into [(name, addr), ...].

    Handles 'Name <addr>, Name2 <addr2>' patterns.
    """
    if not raw:
        return []
    results = []
    # Split on comma that is NOT inside angle brackets
    parts = re.split(r",(?![^<]*>)", raw)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        name, addr = _parse_addr(part)
        if addr:
            results.append((name, addr))
    return results


# ---------------------------------------------------------------------------
# Manifest loading (reused pattern from wechat builder)
# ---------------------------------------------------------------------------

def load_manifest() -> dict[str, Any]:
    """Load identity_manifest.yaml; return empty skeleton on failure."""
    if not MANIFEST_PATH.exists():
        return {"persons": {}, "organizations": {}, "inanimate": {}, "public_figures": {}}
    try:
        import yaml
        d = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
        return {
            "persons": d.get("persons") or {},
            "organizations": d.get("organizations") or {},
            "inanimate": d.get("inanimate") or {},
            "public_figures": d.get("public_figures") or {},
        }
    except Exception as e:
        print(f"[warn] manifest load failed: {e}", file=sys.stderr)
        return {"persons": {}, "organizations": {}, "inanimate": {}, "public_figures": {}}


def _extract_self_emails(manifest: dict[str, Any]) -> set[str]:
    """Return lowercased email addresses that belong to is_self=True persons."""
    self_emails: set[str] = set()
    for person in manifest.get("persons", {}).values():
        if not (isinstance(person, dict) and person.get("is_self")):
            continue
        ids = person.get("identifiers") or {}
        for addr in ids.get("emails", []):
            if addr:
                self_emails.add(addr.strip().lower())
    return self_emails


# ---------------------------------------------------------------------------
# Core builder: one batch -> prompt_data dict
# ---------------------------------------------------------------------------

def _build_prompt(
    batch_id: str,
    emails: list[dict],
    self_emails: set[str],
    manifest_slice: dict[str, Any],
) -> dict[str, Any]:
    """Convert a list of email dicts into a v5 prompt dict."""

    # Collect all timestamps to derive batch_window_local
    timestamps: list[float] = []
    for email in emails:
        iso = email.get("date_iso") or email.get("internal_date_iso") or ""
        if iso:
            try:
                ts = dt.datetime.fromisoformat(iso).timestamp()
                timestamps.append(ts)
            except ValueError:
                pass

    batch_start_ts = min(timestamps) if timestamps else 0.0
    batch_end_ts = max(timestamps) if timestamps else 0.0

    def _ts_iso(ts: float) -> str:
        return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()

    batch_window_local = f"{_ts_iso(batch_start_ts)} ~ {_ts_iso(batch_end_ts)}"

    # Determine chat_room from first email's account + folder
    first = emails[0] if emails else {}
    account = first.get("account") or "unknown_email"
    folder = first.get("folder") or "INBOX"
    chat_room = f"email:{account}:{folder}"

    # Build sender_list: every unique person across from/to/cc
    seen_hashes: dict[str, str] = {}  # hash -> display_name

    def _register(name: str, addr: str) -> None:
        if not addr:
            return
        h = _email_hash(addr)
        if h and h not in seen_hashes:
            seen_hashes[h] = name or addr

    for email in emails:
        from_name, from_addr = _parse_addr(email.get("from_raw", ""))
        _register(from_name, from_addr)

        for name, addr in _parse_addr_list(email.get("to_raw", "")):
            _register(name, addr)

        for name, addr in _parse_addr_list(email.get("cc_raw", "")):
            _register(name, addr)

    sender_list = [
        {
            "wxid_hash": h,
            "sender_name": display,
            "alias_in_manifest_or_None": None,
            "is_self": (display.lower() in self_emails or h in {
                _email_hash(e) for e in self_emails
            }),
        }
        for h, display in seen_hashes.items()
    ]

    # Correct is_self using addr hash comparison (more robust)
    self_hashes = {_email_hash(e) for e in self_emails}
    for s in sender_list:
        s["is_self"] = s["wxid_hash"] in self_hashes

    # Build messages array: one email -> one message
    messages: list[dict] = []
    for email in emails:
        iso = email.get("date_iso") or email.get("internal_date_iso") or ""
        try:
            ts = dt.datetime.fromisoformat(iso).timestamp() if iso else 0.0
        except ValueError:
            ts = 0.0

        _, from_addr = _parse_addr(email.get("from_raw", ""))
        wxid_hash = _email_hash(from_addr)

        subject = email.get("subject") or ""
        to_raw = email.get("to_raw") or ""
        cc_raw = email.get("cc_raw") or ""
        body = (email.get("body_text") or "")[:BODY_TRUNCATE]

        parts = [f"邮件主题: {subject}"]
        if to_raw:
            parts.append(f"收件人: {to_raw}")
        if cc_raw:
            parts.append(f"抄送: {cc_raw}")
        parts.append(f"正文:\n{body}")
        content = "\n".join(parts)

        messages.append({
            "ts": ts,
            "wxid_hash": wxid_hash,
            "content": content,
        })

    # is_group_chat: True if >2 unique recipients across to+cc
    unique_recipients: set[str] = set()
    for email in emails:
        for _, addr in _parse_addr_list(email.get("to_raw", "")):
            unique_recipients.add(addr.lower())
        for _, addr in _parse_addr_list(email.get("cc_raw", "")):
            unique_recipients.add(addr.lower())
    is_group_chat = len(unique_recipients) > 2

    return {
        "batch_id": batch_id,
        "chat_room": chat_room,
        "room_hash": _room_hash(chat_room),
        "batch_window_local": batch_window_local,
        "sender_list": sender_list,
        "manifest_slice": manifest_slice,
        "messages": messages,
        "schema_v_input": "v5",
        "v5_native_builder": "v5_email_batch_builder.py",
        "source_kind": "email",
        "n_msgs": len(messages),
        "n_unique_senders": len(sender_list),
        "is_group_chat": is_group_chat,
    }


# ---------------------------------------------------------------------------
# Batch discovery
# ---------------------------------------------------------------------------

def _discover_batches(
    src_root: Path,
    start_filter: str | None,
    end_filter: str | None,
) -> list[tuple[str, str, Path, Path]]:
    """Yield (date_str, batch_id, raw_json_path, prompt_json_path) for email batches."""
    results = []
    for date_dir in sorted(src_root.iterdir()):
        if not date_dir.is_dir():
            continue
        date_str = date_dir.name
        if start_filter and date_str < start_filter:
            continue
        if end_filter and date_str > end_filter:
            continue
        for batch_dir in sorted(date_dir.iterdir()):
            if not batch_dir.is_dir():
                continue
            batch_id = batch_dir.name
            prompt_path = batch_dir / "prompt.json"
            raw_path = batch_dir / "raw.json"
            if not prompt_path.exists():
                continue
            # Check source_kind
            try:
                pdata = json.loads(prompt_path.read_text(encoding="utf-8"))
                if pdata.get("source_kind") != "email":
                    continue
            except Exception:
                continue
            results.append((date_str, batch_id, raw_path, prompt_path))
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(
        description="Convert email archive raw.json into v5-native batch prompt.json files."
    )
    p.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SRC,
        help="Root of email archive (default: data/extract_archive_email_browser)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output root (default: data/l0_v5/input_batches_email)",
    )
    p.add_argument("--start", help="Filter: only process dates >= YYYY-MM-DD")
    p.add_argument("--end", help="Filter: only process dates <= YYYY-MM-DD")
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip batches whose output prompt.json already exists",
    )
    p.add_argument("--max-batches", type=int, default=None, help="Limit number of batches processed")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan + sample without writing any files",
    )
    args = p.parse_args()

    src_root = args.src if args.src.is_absolute() else ROOT / args.src
    out_root = args.out if args.out.is_absolute() else ROOT / args.out

    print(f"[init] src={src_root}", flush=True)
    print(f"[init] out={out_root}", flush=True)

    # Discover batches
    batches = _discover_batches(src_root, args.start, args.end)
    print(f"[discover] {len(batches)} email batches found", flush=True)

    if args.max_batches:
        batches = batches[: args.max_batches]
        print(f"[limit] truncated to {len(batches)} batches (--max-batches)", flush=True)

    if args.dry_run:
        print(f"[dry-run] would process {len(batches)} batches:", flush=True)
        for date_str, batch_id, raw_path, _ in batches[:10]:
            print(f"  {date_str}/{batch_id}  raw={raw_path.exists()}", flush=True)
        if len(batches) > 10:
            print(f"  ... and {len(batches) - 10} more", flush=True)
        print(json.dumps({
            "dry_run": True,
            "batches_planned": len(batches),
            "start_filter": args.start,
            "end_filter": args.end,
            "src": str(src_root),
            "out": str(out_root),
        }, ensure_ascii=False))
        return 0

    # Load manifest once
    manifest_slice = load_manifest()
    self_emails = _extract_self_emails(manifest_slice)
    print(f"[manifest] {len(manifest_slice['persons'])} persons loaded, "
          f"{len(self_emails)} self emails: {sorted(self_emails)}", flush=True)

    written = 0
    skipped_existing = 0
    skipped_malformed = 0

    for date_str, batch_id, raw_path, _ in batches:
        out_dir = out_root / date_str / batch_id
        out_prompt = out_dir / "prompt.json"
        out_meta = out_dir / "meta.json"

        if args.skip_existing and out_prompt.exists():
            skipped_existing += 1
            continue

        # Load raw.json
        if not raw_path.exists():
            print(f"[warn] raw.json missing for {date_str}/{batch_id}, skipping", file=sys.stderr)
            skipped_malformed += 1
            continue

        try:
            emails = json.loads(raw_path.read_text(encoding="utf-8"))
            if not isinstance(emails, list):
                raise ValueError("raw.json is not a list")
        except Exception as exc:
            print(f"[warn] malformed raw.json {raw_path}: {exc}", file=sys.stderr)
            skipped_malformed += 1
            continue

        if not emails:
            print(f"[warn] empty email list in {date_str}/{batch_id}, skipping", file=sys.stderr)
            skipped_malformed += 1
            continue

        try:
            prompt_data = _build_prompt(batch_id, emails, self_emails, manifest_slice)
        except Exception as exc:
            print(f"[warn] build_prompt failed for {date_str}/{batch_id}: {exc}", file=sys.stderr)
            skipped_malformed += 1
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        out_prompt.write_text(
            json.dumps(prompt_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        # Write meta.json sidecar
        out_meta.write_text(
            json.dumps(
                {
                    "date": date_str,
                    "batch_id": batch_id,
                    "source_kind": "email",
                    "chat_room": prompt_data["chat_room"],
                    "n_msgs": prompt_data["n_msgs"],
                    "n_unique_senders": prompt_data["n_unique_senders"],
                    "is_group_chat": prompt_data["is_group_chat"],
                    "batch_window_local": prompt_data["batch_window_local"],
                    "schema_v_input": "v5",
                    "built_by": "v5_email_batch_builder",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        written += 1

    summary = {
        "ok": True,
        "start_filter": args.start,
        "end_filter": args.end,
        "total_batches_found": len(batches),
        "batches_written": written,
        "skipped_existing": skipped_existing,
        "skipped_malformed": skipped_malformed,
        "out": str(out_root),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
