"""memory_header_sync — keep MEMORY.md stats in sync with disk + graph.

The MEMORY.md header claims "Memory file count: N LIVE. Graph: X Episodes /
Y Facts / Z Entities". These drift as new files land and the graph ingests
without corresponding header updates. This module computes the real counts
+ rewrites the single stats line in-place.

Usage:

    python -m src.core.memory_header_sync --dry-run   # show diff
    python -m src.core.memory_header_sync --apply     # rewrite
"""
from __future__ import annotations


import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional
from src.core._path_resolver import memory_dir


_MEMORY_DIR = memory_dir()
_INDEX_FILE = _MEMORY_DIR / "MEMORY.md"

_STATS_LINE_RE = re.compile(
    r"^>\s*Memory file count:.*$", re.MULTILINE
)


def _count_files(memory_dir: Path = _MEMORY_DIR) -> int:
    """logic-iter1-7 fix: exclusion set must match _EXCLUDED_FILENAMES used
    by _safe_glob_memory_dir (excludes both MEMORY.md and MEMORY.md.bak)."""
    if not memory_dir.is_dir():
        return -1
    excluded = {"MEMORY.md", "MEMORY.md.bak"}
    return sum(1 for p in memory_dir.glob("*.md") if p.name not in excluded)


def _graph_stats() -> tuple[int, int, int]:
    """Returns (episodes, facts, entities). (-1,-1,-1) if unreachable."""
    try:
        # 2026-04-30 daemon repair: v2 stats returns Hindsight bank metadata.
        # v1 episode/fact/entity counts are now mapped from v2's bank /stats:
        #   total_documents → episodes, total_links → facts (proxy), total_nodes → entities
        # This preserves the (episodes, facts, entities) tuple contract.
        import httpx, os
        base = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
        bank = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")
        r = httpx.get(f"{base}/v1/default/banks/{bank}/stats", timeout=5.0)
        d = r.json()
        return (
            int(d.get("total_documents", 0)),
            int(d.get("total_links", 0)),
            int(d.get("total_nodes", 0)),
        )
    except Exception:
        return (-1, -1, -1)


def _build_stats_line(file_count: int, episodes: int, facts: int,
                      entities: int, today_iso: Optional[str] = None) -> str:
    today = today_iso or date.today().isoformat()
    return (
        f"> Memory file count: {file_count} LIVE. "
        f"Graph ({today}): {episodes} Episodes / "
        f"{facts} Facts / {entities} Entities."
    )


def sync_header(index_file: Path = _INDEX_FILE,
                memory_dir: Path = _MEMORY_DIR,
                apply: bool = False) -> dict:
    """Compute real counts, diff against existing MEMORY.md header, optionally rewrite.

    Returns dict: {old_line, new_line, changed, applied}.
    """
    if not index_file.exists():
        return {"error": f"MEMORY.md not found at {index_file}"}

    text = index_file.read_text(encoding="utf-8")
    m = _STATS_LINE_RE.search(text)
    old_line = m.group(0) if m else ""

    fc = _count_files(memory_dir)
    ep, fa, en = _graph_stats()
    new_line = _build_stats_line(fc, ep, fa, en)

    changed = old_line.strip() != new_line.strip()
    applied = False
    if apply and changed:
        if m:
            new_text = text[:m.start()] + new_line + text[m.end():]
        else:
            # No existing line — insert after title block (after first blank line)
            parts = text.split("\n\n", 1)
            if len(parts) == 2:
                new_text = parts[0] + "\n\n" + new_line + "\n\n" + parts[1]
            else:
                new_text = text + "\n" + new_line + "\n"
        # Backup first
        bak = index_file.with_suffix(".md.bak")
        bak.write_text(text, encoding="utf-8")
        index_file.write_text(new_text, encoding="utf-8")
        applied = True

    return {
        "old_line": old_line,
        "new_line": new_line,
        "changed": changed,
        "applied": applied,
        "file_count": fc,
        "graph": {"episodes": ep, "facts": fa, "entities": en},
    }


# --- TU-4 (memory_tech_debt_cleanup) extensions: --regen-from-fs ----

_UNINDEXED_SECTION_HEADING = "## Unindexed (auto-detected)"
_UNINDEXED_SECTION_RE = re.compile(
    r"^## Unindexed \(auto-detected\)[\s\S]*?(?=^## |\Z)",
    re.MULTILINE,
)
_ENTRY_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+\.md)\)")
# Files excluded from canonical glob (per BLOCKER-V2, NR-15)
_EXCLUDED_FILENAMES = {"MEMORY.md", "MEMORY.md.bak"}


def _safe_glob_memory_dir(memory_dir: Path) -> list[Path]:
    """Path-containment guard (SEC-4): every glob result MUST resolve under memory_dir.

    Prevents NTFS junction / symlink escape into other dirs.
    """
    if not memory_dir.is_dir():
        return []
    canonical_root = memory_dir.resolve()
    out = []
    for p in memory_dir.glob("*.md"):
        if p.name in _EXCLUDED_FILENAMES:
            continue
        try:
            resolved = p.resolve()
        except OSError:
            continue
        # path-containment: resolved file must be inside resolved memory_dir
        try:
            resolved.relative_to(canonical_root)
        except ValueError:
            continue  # outside allowed parent — skip
        out.append(p)
    return sorted(out, key=lambda x: x.name)


def _list_indexed_files(text: str) -> set[str]:
    """Extract every `(*.md)` link from MEMORY.md body."""
    return {Path(m.group(2)).name for m in _ENTRY_LINK_RE.finditer(text)}


def _build_unindexed_section(orphans: list[Path]) -> str:
    """Build the ## Unindexed section content. Sorted; idempotent."""
    if not orphans:
        return ""
    lines = [
        _UNINDEXED_SECTION_HEADING,
        "",
        "> Auto-generated by `memory_header_sync --regen-from-fs`. "
        "Files in memory/ not yet promoted to a curated section above. "
        "CEO may move them under a categorized heading; do not edit this section by hand.",
        "",
    ]
    for p in sorted(orphans, key=lambda x: x.name):
        lines.append(f"- [{p.stem}]({p.name}) -- (auto-detected; not yet curated)")
    return "\n".join(lines) + "\n"


def regen_from_fs(index_file: Path = _INDEX_FILE,
                  memory_dir: Path = _MEMORY_DIR,
                  apply: bool = False) -> dict:
    """Regenerate MEMORY.md index by syncing stats + appending unindexed orphans.

    Precedence rules (per plan_v2 NR-2):
    - count = filesystem ground truth (via _safe_glob_memory_dir)
    - existing entries preserved verbatim (no body deletion)
    - orphans (files in fs not in any link) appended to Unindexed section
    - Unindexed section regenerated idempotently (existing → replaced)

    Idempotent: a second run produces zero diff.
    """
    if not index_file.exists():
        return {"error": f"MEMORY.md not found at {index_file}"}

    text = index_file.read_text(encoding="utf-8")
    files = _safe_glob_memory_dir(memory_dir)
    fs_count = len(files)
    # Strip the auto-generated Unindexed section BEFORE computing orphans,
    # so links inside that section don't count as "curated indexed" on re-runs.
    base_text = _UNINDEXED_SECTION_RE.sub("", text).rstrip() + "\n"
    indexed = _list_indexed_files(base_text)
    orphans = [p for p in files if p.name not in indexed]
    unindexed_block = _build_unindexed_section(orphans)
    if unindexed_block:
        new_body = base_text + "\n" + unindexed_block
    else:
        new_body = base_text
    # Sync stats line. Idempotency: if existing line has SAME numbers,
    # preserve its date (avoid cosmetic-only diff across day boundaries).
    ep, fa, en = _graph_stats()
    m = _STATS_LINE_RE.search(new_body)
    preserve_date = None
    if m:
        existing = m.group(0)
        # Try to preserve the existing date if all 4 numbers are unchanged
        nums_match = re.search(
            r"Memory file count:\s*(\d+).*Graph\s*\(([^)]+)\):\s*(\d+)\s*Episodes\s*/\s*(\d+)\s*Facts\s*/\s*(\d+)\s*Entities",
            existing,
        )
        if nums_match:
            ex_fs, ex_date, ex_ep, ex_fa, ex_en = nums_match.groups()
            if (int(ex_fs) == fs_count and int(ex_ep) == ep
                    and int(ex_fa) == fa and int(ex_en) == en):
                preserve_date = ex_date
    new_stats_line = _build_stats_line(fs_count, ep, fa, en, today_iso=preserve_date)
    if m:
        new_body = new_body[:m.start()] + new_stats_line + new_body[m.end():]
    body_changed = (new_body != text)
    applied = False
    if apply and body_changed:
        bak = index_file.with_suffix(".md.bak")
        bak.write_text(text, encoding="utf-8")
        index_file.write_text(new_body, encoding="utf-8")
        applied = True
    return {
        "fs_count": fs_count,
        "indexed_count": len(indexed),
        "orphan_count": len(orphans),
        "orphans": [p.name for p in orphans],
        "changed": body_changed,
        "applied": applied,
        "graph": {"episodes": ep, "facts": fa, "entities": en},
    }


def _cli(argv: Optional[list] = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="src.core.memory_header_sync")
    p.add_argument("--regen-from-fs", action="store_true",
                   help="(TU-4) regen body via filesystem orphan detection + stats sync")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true",
                   help="rewrite the stats line (backup to MEMORY.md.bak)")
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="show diff without rewriting (default)")
    args = p.parse_args(argv)

    if args.regen_from_fs:
        r = regen_from_fs(apply=args.apply)
    else:
        r = sync_header(apply=args.apply)
    import json
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if "error" not in r else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
