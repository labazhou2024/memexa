"""V5-native browser batch builder.

Reads raw visits from data/extract_archive_email_browser/<date>/<batch>/raw.json
(list of browser visit dicts, source_kind in {browser_session, browser_search})
and writes V5-format prompt.json + meta.json sidecar to
data/l0_v5/input_batches_browser/<date>/<batch_id>/

Output schema aligns with v5_wechat_batch_builder.py (same field names).
Key differences from wechat builder:
  - chat_room  = "browser:<browser_id>:<profile>"
  - room_hash  = sha256(chat_room)[:32]
  - sender_list is always [self], n_unique_senders = 1
  - messages built from visit records, not WeChat DB

Usage:
  python tools/v5_browser_batch_builder.py \\
      --src data/extract_archive_email_browser \\
      --out data/l0_v5/input_batches_browser \\
      --skip-existing

  python tools/v5_browser_batch_builder.py \\
      --start 2026-01-01 --end 2026-05-11

  python tools/v5_browser_batch_builder.py \\
      --max-batches 5 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_SRC = ROOT / "data" / "extract_archive_email_browser"
DEFAULT_OUT = ROOT / "data" / "l0_v5" / "input_batches_browser"
MANIFEST_PATH = ROOT / "data" / "identity_manifest.yaml"

# source_kinds this builder handles
BROWSER_KINDS = {"browser_session", "browser_search"}

# Field trim limits (chars) — keeps prompt size sane
_TRIM = 200


def _room_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:32]


def load_manifest() -> dict[str, Any]:
    """Load identity_manifest.yaml, return trimmed dict safe for JSON."""
    if not MANIFEST_PATH.exists():
        return {
            "persons": {},
            "organizations": {},
            "inanimate": {},
            "public_figures": {},
        }
    try:
        import yaml  # type: ignore[import]

        d = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
        return {
            "persons": d.get("persons") or {},
            "organizations": d.get("organizations") or {},
            "inanimate": d.get("inanimate") or {},
            "public_figures": d.get("public_figures") or {},
        }
    except Exception as exc:
        print(f"[warn] manifest load fail: {exc}", file=sys.stderr)
        return {
            "persons": {},
            "organizations": {},
            "inanimate": {},
            "public_figures": {},
        }


def _trim(s: str | None, n: int = _TRIM) -> str:
    """Return s trimmed to n chars; empty string if None."""
    if not s:
        return ""
    return s[:n]


def _build_message_from_visit(visit: dict[str, Any]) -> dict[str, Any]:
    """Convert one visit dict to a v5-format message entry."""
    title = _trim(visit.get("title") or "")
    url = _trim(visit.get("url") or "")
    referrer = _trim(visit.get("referrer_url") or "") or None
    search_term = visit.get("search_term") or visit.get("keyword") or ""
    duration_s = visit.get("visit_duration_s", 0.0)
    transition_label = visit.get("transition_label") or ""
    ts = visit.get("visit_time_unix") or 0.0

    content_lines = [
        f"访问 {title}",
        f"URL: {url}",
        f"搜索词: {search_term}",
        f"停留: {duration_s}秒",
        f"来源: {transition_label}",
        f"上游: {_trim(referrer) if referrer else '直接打开'}",
    ]
    return {
        "ts": ts,
        "wxid_hash": "self",
        "content": "\n".join(content_lines),
    }


def _build_message_from_search(item: dict[str, Any]) -> dict[str, Any]:
    """Convert one browser_search item (keyword + url) to a v5 message."""
    keyword = _trim(item.get("keyword") or "")
    url = _trim(item.get("url") or "")
    ts = item.get("visit_time_unix") or 0.0

    content_lines = [
        f"搜索: {keyword}",
        f"URL: {url}",
    ]
    return {
        "ts": ts,
        "wxid_hash": "self",
        "content": "\n".join(content_lines),
    }


def process_batch(
    date_str: str,
    batch_dir: Path,
    out_root: Path,
    manifest_slice: dict[str, Any],
    skip_existing: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Process one archive batch directory.

    Returns a result dict with keys: status, batch_id, n_msgs, source_kind, path.
    """
    # Determine source_kind from prompt.json if available
    source_kind = "browser_session"  # default
    prompt_src = batch_dir / "prompt.json"
    if prompt_src.exists():
        try:
            pd = json.loads(prompt_src.read_text(encoding="utf-8"))
            sk = pd.get("source_kind", "")
            if sk in BROWSER_KINDS:
                source_kind = sk
            elif sk:
                # Not a browser batch — skip
                return {"status": "skip_not_browser", "batch_id": batch_dir.name}
        except Exception:
            pass  # raw.json check will handle bad cases

    raw_path = batch_dir / "raw.json"
    if not raw_path.exists():
        return {"status": "skip_no_raw", "batch_id": batch_dir.name}

    try:
        raw: list[dict[str, Any]] = json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[warn] bad raw.json {raw_path}: {exc}", file=sys.stderr)
        return {"status": "error_bad_raw", "batch_id": batch_dir.name}

    if not isinstance(raw, list) or not raw:
        return {"status": "skip_empty_raw", "batch_id": batch_dir.name}

    # Confirm this is a browser raw: must have browser_id field
    first = raw[0]
    if "browser_id" not in first:
        return {"status": "skip_not_browser_raw", "batch_id": batch_dir.name}

    # Detect source_kind from raw content if not already set
    if "keyword" in first and "title" not in first:
        source_kind = "browser_search"
    else:
        source_kind = "browser_session"

    browser_id = _trim(first.get("browser_id") or "unknown")
    profile = _trim(first.get("profile") or "Default")
    chat_room = f"browser:{browser_id}:{profile}"
    room_hash = _room_hash(chat_room)

    # Build messages
    messages: list[dict[str, Any]] = []
    for item in raw:
        if source_kind == "browser_search":
            messages.append(_build_message_from_search(item))
        else:
            messages.append(_build_message_from_visit(item))

    if not messages:
        return {"status": "skip_no_messages", "batch_id": batch_dir.name}

    # Determine batch_window_local
    times_iso = [
        item.get("visit_time_iso") or ""
        for item in raw
        if item.get("visit_time_iso")
    ]
    if times_iso:
        earliest = min(times_iso)
        latest = max(times_iso)
        batch_window_local = f"{earliest} ~ {latest}"
    else:
        batch_window_local = ""

    batch_id = batch_dir.name

    # Output path
    out_dir = out_root / date_str / batch_id
    out_path = out_dir / "prompt.json"

    if skip_existing and out_path.exists():
        return {"status": "skipped_existing", "batch_id": batch_id}

    sender_list = [
        {
            "wxid_hash": "self",
            "sender_name": "\u7528\u6237",  # 用户
            "alias_in_manifest_or_None": None,
            "is_self": True,
        }
    ]

    prompt_data: dict[str, Any] = {
        "batch_id": batch_id,
        "chat_room": chat_room,
        "room_hash": room_hash,
        "batch_window_local": batch_window_local,
        "sender_list": sender_list,
        "manifest_slice": manifest_slice,
        "messages": messages,
        "schema_v_input": "v5",
        "v5_native_builder": "v5_browser_batch_builder.py",
        "n_msgs": len(messages),
        "n_unique_senders": 1,
        "is_group_chat": False,
    }

    meta_data: dict[str, Any] = {
        "date": date_str,
        "batch_id": batch_id,
        "source_kind": source_kind,
        "n_msgs": len(messages),
        "browser_id": browser_id,
        "profile": profile,
        "chat_room": chat_room,
        "batch_window_local": batch_window_local,
        "schema_v_input": "v5",
        "built_by": "v5_browser_batch_builder",
    }

    if dry_run:
        return {
            "status": "dry_run",
            "batch_id": batch_id,
            "n_msgs": len(messages),
            "source_kind": source_kind,
            "would_write": str(out_path),
        }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(prompt_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    meta_path = out_dir / "meta.json"
    meta_path.write_text(
        json.dumps(meta_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "status": "written",
        "batch_id": batch_id,
        "n_msgs": len(messages),
        "source_kind": source_kind,
        "path": str(out_path),
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description="Convert browser archive to v5 batches")
    p.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SRC,
        help="Root of extract_archive_email_browser (default: %(default)s)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output root (default: %(default)s)",
    )
    p.add_argument("--start", default=None, help="Earliest date YYYY-MM-DD (inclusive)")
    p.add_argument("--end", default=None, help="Latest date YYYY-MM-DD (inclusive)")
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip batches whose prompt.json already exists in out",
    )
    p.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Stop after processing this many batches (for testing)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without writing any files",
    )
    args = p.parse_args()

    src_root: Path = args.src
    out_root: Path = args.out

    if not src_root.exists():
        print(
            json.dumps({"ok": False, "error": f"src not found: {src_root}"}),
            flush=True,
        )
        return 1

    print(f"[init] loading manifest...", flush=True)
    manifest_slice = load_manifest()
    print(
        f"[init] manifest: {len(manifest_slice['persons'])} persons",
        flush=True,
    )

    # Enumerate date dirs
    date_dirs = sorted(
        d for d in src_root.iterdir() if d.is_dir() and d.name[:4].isdigit()
    )

    if args.start:
        date_dirs = [d for d in date_dirs if d.name >= args.start]
    if args.end:
        date_dirs = [d for d in date_dirs if d.name <= args.end]

    print(f"[scan] {len(date_dirs)} date dirs in range", flush=True)

    counters = {
        "written": 0,
        "skipped_existing": 0,
        "skipped_not_browser": 0,
        "skipped_other": 0,
        "errors": 0,
        "dry_run": 0,
    }
    total_msgs = 0
    processed = 0

    for date_dir in date_dirs:
        batch_dirs = sorted(b for b in date_dir.iterdir() if b.is_dir())
        for batch_dir in batch_dirs:
            if args.max_batches is not None and processed >= args.max_batches:
                break

            result = process_batch(
                date_str=date_dir.name,
                batch_dir=batch_dir,
                out_root=out_root,
                manifest_slice=manifest_slice,
                skip_existing=args.skip_existing,
                dry_run=args.dry_run,
            )
            status = result.get("status", "")

            if status == "written":
                counters["written"] += 1
                total_msgs += result.get("n_msgs", 0)
                processed += 1
                print(
                    f"  [write] {date_dir.name}/{batch_dir.name} "
                    f"({result['n_msgs']} msgs, {result['source_kind']})",
                    flush=True,
                )
            elif status == "dry_run":
                counters["dry_run"] += 1
                total_msgs += result.get("n_msgs", 0)
                processed += 1
                print(
                    f"  [dry]   {date_dir.name}/{batch_dir.name} "
                    f"({result['n_msgs']} msgs, {result['source_kind']}) "
                    f"-> {result['would_write']}",
                    flush=True,
                )
            elif status == "skipped_existing":
                counters["skipped_existing"] += 1
            elif status.startswith("skip_not_browser"):
                counters["skipped_not_browser"] += 1
            elif status.startswith("error"):
                counters["errors"] += 1
                print(f"  [err]   {date_dir.name}/{batch_dir.name}: {status}", flush=True)
            else:
                counters["skipped_other"] += 1

        if args.max_batches is not None and processed >= args.max_batches:
            break

    summary: dict[str, Any] = {
        "ok": True,
        "dry_run": args.dry_run,
        "src": str(src_root),
        "out": str(out_root),
        "date_range": f"{args.start or 'all'} ~ {args.end or 'all'}",
        "batches_written": counters["written"],
        "batches_dry_run": counters["dry_run"],
        "batches_skipped_existing": counters["skipped_existing"],
        "batches_skipped_not_browser": counters["skipped_not_browser"],
        "batches_skipped_other": counters["skipped_other"],
        "batches_errors": counters["errors"],
        "total_msgs": total_msgs,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
