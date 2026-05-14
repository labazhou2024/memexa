"""Inject manifest_slice into input batches' prompt.json.

For each batch:
- Load existing prompt.json
- For each sender_wxid_hash, look up manifest persons
- Build redacted slice via ManifestStore.extraction_slice_for_batch
- Write back to prompt.json (idempotent)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

from memexa.core.identity_manifest import ManifestStore

logger = logging.getLogger("inject_slice")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=Path("data/l0_v5/input_batches"))
    parser.add_argument("--manifest-path", type=str, default="data/identity_manifest.yaml")
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reverse", action="store_true",
                        help="Process newest dates first")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    store = ManifestStore.load(args.manifest_path)
    logger.info(f"manifest stats: {store.stats()}")

    paths = sorted(args.src.rglob("prompt.json"), reverse=args.reverse)

    def _in_range(p: Path) -> bool:
        date_part = p.parent.parent.name  # data/l0_v5/input_batches/<date>/<batch_id>/prompt.json
        if args.start_date and date_part < args.start_date:
            return False
        if args.end_date and date_part > args.end_date:
            return False
        return True

    paths = [p for p in paths if _in_range(p)]
    if args.limit:
        paths = paths[: args.limit]

    n_processed = 0
    n_filled = 0
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"  skip {p}: {e}")
            continue

        sender_hashes = [s.get("wxid_hash", "") for s in data.get("sender_list", []) if s.get("wxid_hash")]
        room_hash = data.get("room_hash", "")
        window = data.get("batch_window_local", "")
        # Parse window "ISO ~ ISO"
        if " ~ " in window:
            time_window_iso = tuple(window.split(" ~ ", 1))
        else:
            time_window_iso = (window, window)

        slice_ = store.extraction_slice_for_batch(
            sender_wxid_hashes=sender_hashes,
            room_hash=room_hash,
            time_window_iso=time_window_iso,
        )

        # Update sender_list with alias_in_manifest_or_None and is_self
        for s in data.get("sender_list", []):
            wh = s.get("wxid_hash", "")
            person = store.lookup_person_by_wxid_hash(wh)
            if person:
                s["alias_in_manifest_or_None"] = person.primary_name
                s["is_self"] = bool(person.is_self)
            else:
                # Use sender_name as alias
                s["alias_in_manifest_or_None"] = s.get("sender_name") or None
                s["is_self"] = False

        data["manifest_slice"] = slice_
        if slice_["persons"] or slice_["public_figures"]:
            n_filled += 1

        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        n_processed += 1

    logger.info(f"injected manifest slices: {n_processed} (with content: {n_filled})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
