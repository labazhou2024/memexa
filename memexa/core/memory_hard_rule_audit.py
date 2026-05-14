"""memory_hard_rule_audit — reconcile HARD RULE labels between
`memory/feedback_*.md` file content and MEMORY.md index labels.

Today's audit found 3 files internally tagged `HARD RULE` but presented
as regular feedback in MEMORY.md index. LLM reads the index
(Tier-0) without the HARD RULE flag → doesn't escalate the rule's
enforcement weight.

This module:
  - diff: enumerate mismatches
  - apply: add/remove `**HARD RULE: <title>**` prefix to match disk tags
"""
from __future__ import annotations


import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from memexa.core._path_resolver import memory_dir


_MEMORY_DIR = memory_dir()
_INDEX_FILE = _MEMORY_DIR / "MEMORY.md"


def _disk_hard_rule_files(memory_dir: Path = _MEMORY_DIR) -> List[str]:
    """Return basename (no .md) of each feedback_*.md containing 'HARD RULE'.

    Excludes files where the only mention is 'HARD RULE candidate' (which is
    a different state).
    """
    results = []
    for p in sorted(memory_dir.glob("feedback_*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        # Must contain 'HARD RULE' as a rule marker, NOT only 'candidate'
        if "HARD RULE" in text:
            # Count non-candidate occurrences
            candidates = text.count("HARD RULE candidate")
            total = text.count("HARD RULE")
            if total > candidates:
                results.append(p.stem)
    return results


def _index_hard_rule_entries(index_file: Path = _INDEX_FILE) -> List[Tuple[int, str, str]]:
    """Return (line_no, full_line, basename) for each MEMORY.md entry.

    Entry format: `- [Title](basename.md) -- desc` or `- **[HARD RULE: ...](basename.md)** ...`.
    """
    if not index_file.exists():
        return []
    entries = []
    for i, line in enumerate(index_file.read_text(encoding="utf-8").splitlines()):
        m = re.search(r"\(([a-zA-Z0-9_]+)\.md\)", line)
        if m and line.lstrip().startswith(("- ", "- **")):
            entries.append((i, line, m.group(1)))
    return entries


def _entry_has_hard_rule_label(line: str) -> bool:
    """Check if MEMORY.md line explicitly labels entry as HARD RULE.

    Three acceptable label shapes:
      - `**HARD RULE: ...**`
      - `**HARD RULE candidate: ...**`
      - starts with `**[HARD RULE`
    """
    return ("HARD RULE:" in line) or ("HARD RULE candidate:" in line)


def diff_labels(memory_dir: Path = _MEMORY_DIR,
                index_file: Path = _INDEX_FILE) -> dict:
    """Return mismatch dict:
    {
      "disk_only": [<basenames tagged HARD RULE on disk but not labeled in index>],
      "index_only": [<entries labeled HARD RULE in index but file lacks tag>],
      "disk_count": N, "index_count": M,
    }
    """
    disk = set(_disk_hard_rule_files(memory_dir))
    index_entries = _index_hard_rule_entries(index_file)

    index_labeled = {
        basename for (_, line, basename) in index_entries
        if _entry_has_hard_rule_label(line)
    }

    disk_only = sorted(disk - index_labeled)
    index_only = sorted(index_labeled - disk)

    return {
        "disk_count": len(disk),
        "index_count": len(index_labeled),
        "disk_only": disk_only,
        "index_only": index_only,
        "match_count": len(disk & index_labeled),
    }


def apply_fixes(memory_dir: Path = _MEMORY_DIR,
                index_file: Path = _INDEX_FILE,
                dry_run: bool = True) -> dict:
    """Add `**HARD RULE: <title>**` prefix to index entries for files
    tagged HARD RULE on disk but not labeled in index.

    dry_run=True (default) — returns the diff without writing.
    dry_run=False — writes MEMORY.md.bak backup + applies edits.
    """
    d = diff_labels(memory_dir, index_file)
    if not d["disk_only"]:
        d["applied"] = False
        return d

    text = index_file.read_text(encoding="utf-8")
    new_text = text
    edits = []
    for basename in d["disk_only"]:
        # Find the line referencing this basename
        pat = re.compile(
            r"^(-\s+)(\[[^\]]+\]\(" + re.escape(basename) + r"\.md\))"
            r"(.*)$",
            re.MULTILINE,
        )
        m = pat.search(new_text)
        if not m:
            continue
        # Extract title from the [Title] bracket
        title_m = re.search(r"\[([^\]]+)\]", m.group(2))
        title = title_m.group(1) if title_m else basename
        # If title already starts with HARD RULE, skip
        if "HARD RULE" in title:
            continue
        # Build new line: - **[HARD RULE: <title>](basename.md)** <rest>
        new_link = f"[HARD RULE: {title}]({basename}.md)"
        new_line = f"{m.group(1)}**{new_link}**{m.group(3)}"
        new_text = new_text[:m.start()] + new_line + new_text[m.end():]
        edits.append({"basename": basename, "title": title})

    applied = False
    if not dry_run and edits:
        bak = index_file.with_suffix(".md.bak")
        bak.write_text(text, encoding="utf-8")
        index_file.write_text(new_text, encoding="utf-8")
        applied = True

    d["edits"] = edits
    d["applied"] = applied
    return d


def _cli(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="memexa.core.memory_hard_rule_audit")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("diff", help="show mismatches (read-only)")
    pa = sub.add_parser("apply", help="add HARD RULE labels to index entries")
    pa.add_argument("--dry-run", action="store_true", default=False)

    args = p.parse_args(argv)
    if args.cmd == "diff":
        r = diff_labels()
    elif args.cmd == "apply":
        r = apply_fixes(dry_run=args.dry_run)
    else:
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
