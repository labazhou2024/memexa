"""memory_tier0_audit — compute Tier-0 cold-start byte budget.

Tier-0 files loaded at every SessionStart per CLAUDE.md §7.1:
  - MEMORY.md
  - user_profile.md
  - constraints.md
  - All feedback_*.md internally tagged HARD RULE

Target budget: ~80 kB / ~20k tokens. Over-budget Tier-0 inflates context
for every session; this tool reports actual vs target for ops review.
"""
from __future__ import annotations


import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from memexa.core._path_resolver import memory_dir


_MEMORY_DIR = memory_dir()
_TARGET_BYTES = 80_000  # ≈ 20k tokens at 4 chars/token


def _tier0_files(memory_dir: Path = _MEMORY_DIR) -> List[Path]:
    """Same definition as CLAUDE.md §7.1 cold-start protocol."""
    files = []
    for name in ("MEMORY.md", "user_profile.md", "constraints.md"):
        p = memory_dir / name
        if p.exists():
            files.append(p)
    # HARD RULE files: feedback_*.md with 'HARD RULE' tag (excl. candidate-only)
    for p in sorted(memory_dir.glob("feedback_*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        total = text.count("HARD RULE")
        candidates = text.count("HARD RULE candidate")
        if total > candidates:
            files.append(p)
    return files


def compute_tier0_bytes(memory_dir: Path = _MEMORY_DIR,
                        target_bytes: int = _TARGET_BYTES) -> dict:
    files = _tier0_files(memory_dir)
    breakdown = []
    total = 0
    for f in files:
        try:
            sz = f.stat().st_size
        except OSError:
            sz = 0
        total += sz
        breakdown.append({"file": f.name, "bytes": sz})
    # Sort largest-first for top-5 visibility
    breakdown.sort(key=lambda x: -x["bytes"])
    return {
        "total_bytes": total,
        "target_bytes": target_bytes,
        "over_budget_bytes": max(0, total - target_bytes),
        "file_count": len(files),
        "top5_largest": breakdown[:5],
        "tokens_approx": total // 4,
        "utilization_pct": round(100 * total / target_bytes, 1) if target_bytes > 0 else -1,
    }


def _cli(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="memexa.core.memory_tier0_audit")
    p.add_argument("--target-kb", type=int, default=80,
                   help="target budget in kB (default 80 = 20k tokens)")
    args = p.parse_args(argv)
    r = compute_tier0_bytes(target_bytes=args.target_kb * 1000)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
