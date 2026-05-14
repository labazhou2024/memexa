"""Schema v1 -> v2 migration for improvement_patterns.jsonl.

[T5 2026-04-19 plan v3.1 §5 AC-16 + §7]

Backfills missing v2 fields on every record:
    - schema_version: None/absent -> 1 -> 2
    - source: "human_turn_historical"
    - promotion_status: "draft"
    - parent_pattern_id: None
    - last_hit_ts: None

Properties:
    - Idempotent: re-running `apply` after a successful run leaves file unchanged
      and reports skipped_count == total.
    - Atomic: writes to a tempfile in the same dir, fsyncs, then os.replace()
      (POSIX and Windows both guarantee atomic rename on same filesystem).
    - Backup: on first `apply`, copies the current file to
      improvement_patterns.v1.bak.<timestamp> BEFORE rewrite. Subsequent idempotent
      runs still create a new backup (cheap) so rollback is always available.
    - Dry-run: reads + reports only, never touches disk.

CLI:
    python -m src.core.migrate_patterns dry-run
    python -m src.core.migrate_patterns apply
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Reuse the resolved data dir so env-var overrides (MEMEXA_DATA_DIR) work in tests.
from src.core.pattern_extractor import _PATTERNS_FILE, _DATA_DIR


V2_DEFAULTS = {
    "source": "human_turn_historical",
    "promotion_status": "draft",
    "parent_pattern_id": None,
    "last_hit_ts": None,
}


@dataclass
class MigrationReport:
    total: int = 0
    migrated: int = 0
    skipped: int = 0        # already v2
    malformed: int = 0      # JSON decode failures, kept verbatim
    backup_path: Optional[str] = None
    dry_run: bool = True
    needs_ceo_confirmation: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "migrated": self.migrated,
            "skipped": self.skipped,
            "malformed": self.malformed,
            "backup_path": self.backup_path,
            "dry_run": self.dry_run,
            "needs_ceo_confirmation": self.needs_ceo_confirmation,
            "notes": self.notes,
        }


def _upgrade_record(data: dict) -> Tuple[dict, bool]:
    """Return (upgraded_dict, was_modified).

    If schema_version already == 2, returns (data, False) unchanged.
    Otherwise fills missing v2 fields and bumps schema_version.
    """
    sv = data.get("schema_version")
    if sv == 2:
        # Still fill any field the record happens to be missing (defensive).
        patched = False
        for k, v in V2_DEFAULTS.items():
            if k not in data:
                data[k] = v
                patched = True
        return data, patched
    # v1 or missing -> upgrade
    for k, v in V2_DEFAULTS.items():
        if k not in data:
            data[k] = v
    data["schema_version"] = 2
    return data, True


def scan(path: Path) -> Tuple[List[str], MigrationReport]:
    """Read lines + simulate migration. Returns (new_lines, report)."""
    report = MigrationReport(dry_run=True)
    if not path.exists():
        report.notes.append(f"source file not found: {path}")
        return [], report
    raw = path.read_text(encoding="utf-8")
    new_lines: List[str] = []
    for raw_line in raw.splitlines():
        if not raw_line.strip():
            new_lines.append(raw_line)
            continue
        report.total += 1
        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError:
            report.malformed += 1
            new_lines.append(raw_line)  # keep verbatim
            continue
        new_data, modified = _upgrade_record(data)
        if modified:
            report.migrated += 1
        else:
            report.skipped += 1
        new_lines.append(json.dumps(new_data, ensure_ascii=False))
    # CEO confirmation suggested when migrating a non-trivial body of history.
    if report.migrated >= 20:
        report.needs_ceo_confirmation = True
        report.notes.append(
            f"{report.migrated} v1 records will be rewritten; CEO glance recommended"
        )
    return new_lines, report


def apply(path: Path = _PATTERNS_FILE) -> MigrationReport:
    """Perform the migration: backup + atomic rewrite.

    Idempotent: running twice on an already-v2 file reports skipped == total
    and still produces a fresh backup (cheap snapshot).

    [SEC-R1-002 2026-04-20] path parameter is validated to be inside the safe
    root (_DATA_DIR or workspace) to prevent path traversal via caller-supplied
    path argument.
    """
    # Validate path is within safe root
    import tempfile as _tf
    resolved = Path(path).resolve()
    workspace_root = Path(__file__).parent.parent.parent.parent.resolve()
    temp_root = Path(_tf.gettempdir()).resolve()
    safe_roots = (_DATA_DIR.resolve(), workspace_root, temp_root)
    is_safe = any(
        str(resolved).startswith(str(sr) + os.sep) or str(resolved) == str(sr)
        for sr in safe_roots
    )
    if not is_safe:
        raise ValueError(
            f"migrate_patterns.apply: path outside safe root: {resolved}"
        )
    path = resolved

    new_lines, report = scan(path)
    report.dry_run = False
    if not path.exists():
        return report

    # Always backup before touching the file (even on idempotent runs, so the
    # timestamp trail stays intact). Backup name carries millisecond suffix to
    # avoid collision on fast successive runs.
    ts = datetime.now().strftime("%Y%m%dT%H%M%S%f")[:-3]
    backup = path.with_suffix(path.suffix + f".v1.bak.{ts}")
    try:
        shutil.copy2(path, backup)
        report.backup_path = str(backup)
    except OSError as e:
        report.notes.append(f"backup failed: {e}")
        return report  # refuse to rewrite without a backup

    # Write via tempfile + atomic replace.
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".jsonl.tmp", prefix="migrate_v2_"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))
            if new_lines and not new_lines[-1].endswith("\n"):
                f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        Path(tmp_path).replace(path)
        tmp_path = None  # replaced successfully
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
    return report


def _print_report(report: MigrationReport) -> None:
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    if report.needs_ceo_confirmation and report.dry_run:
        print(
            "\n[hint] >=20 v1 records detected. "
            "Review dry-run output, then run `apply` to commit.",
            file=sys.stderr,
        )


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "Usage: python -m src.core.migrate_patterns [dry-run|apply]",
            file=sys.stderr,
        )
        return 2
    cmd = argv[0]
    if cmd == "dry-run":
        _, report = scan(_PATTERNS_FILE)
        report.dry_run = True
        _print_report(report)
        return 0
    if cmd == "apply":
        report = apply(_PATTERNS_FILE)
        _print_report(report)
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
