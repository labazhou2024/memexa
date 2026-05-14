"""action_items hygiene — F2 fix (2026-04-24).

Problem: ``harness_state.json:action_items_for_user`` accumulated stale
and contradictory entries across sessions:

  - ``[ReplanStale:task_stale]`` / ``[ReplanStale:legacy_task]`` — phantom
    task_ids (no real task dir under ``.claude/harness/tasks/``), likely
    from test fixtures that set ``MEMEXA_TASK_DIR`` to a tmp location but
    wrote through to the real harness_state.
  - ``[URGENT] Tests failing: 0 failed.`` — self-contradictory output of
    a 0-case branch that wasn't guarded.
  - ``[MiniLoop] 25 commits since last loop`` — stale cached counter that
    outlived multiple mini-loop completions without a reset.

This module provides:

  1. ``is_stale(item)`` — regex-based classifier for the known bad shapes.
  2. ``clean(items)`` — partition into (kept, removed).
  3. ``clean_harness_state(path)`` — atomic read-modify-write purge.
  4. ``validate_real_task_id(tid)`` — helper callable by emitters to
     check a task_id has a corresponding real task directory before
     surfacing a ``[ReplanStale:*]`` item.

CLI: ``python -m src.core.action_items_hygiene clean --in-place PATH``
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

# Regex patterns for stale entry shapes. Keep each one narrow so we
# don't accidentally purge legitimate items that happen to share a prefix.
STALE_PATTERNS: Tuple[re.Pattern, ...] = (
    # Phantom task_ids seeded by tests that leaked into prod state.
    re.compile(r"^\[ReplanStale:(task_stale|legacy_task)\]"),
    # 0-failed URGENT message — always a bug since the emitter should
    # not fire when there are zero concrete failures.
    re.compile(r"^\[URGENT\] Tests failing: 0 failed\b"),
    # Stale MiniLoop cached counter — regenerated live from git on every
    # session after the emitter fix, so any existing text entry is by
    # definition out of date.
    re.compile(r"^\[MiniLoop\] \d+ commits since last loop\b"),
)

_KNOWN_PHANTOM_TASK_IDS = {"task_stale", "legacy_task"}


def is_stale(item: str) -> bool:
    """Return True if ``item`` matches any of the STALE_PATTERNS."""
    if not isinstance(item, str):
        return False
    return any(p.search(item) for p in STALE_PATTERNS)


def clean(items: List[str]) -> Tuple[List[str], List[str]]:
    """Partition ``items`` into (kept, removed).

    Ordering within ``kept`` is preserved. ``removed`` is ordered by
    first-occurrence in the input.
    """
    kept: List[str] = []
    removed: List[str] = []
    for x in items or []:
        if is_stale(x):
            removed.append(x)
        else:
            kept.append(x)
    return kept, removed


def validate_real_task_id(tid: str, tasks_root: Path | None = None) -> bool:
    """Gate for emitters: True iff ``tid`` is a real, non-phantom task.

    Rejects:
      - known phantom seeds (task_stale / legacy_task)
      - empty / None / whitespace-only
      - tid that does NOT correspond to a directory under tasks_root
    """
    if not isinstance(tid, str):
        return False
    tid_s = tid.strip()
    if not tid_s:
        return False
    if tid_s in _KNOWN_PHANTOM_TASK_IDS:
        return False
    # Priority (2026-04-24 reviewer F2-2 fix; round-2 LOG-F2-2 tighten):
    #   1. explicit `tasks_root` param (highest — used by tests)
    #   2. canonical __file__-derived path (production default)
    #   3. MEMEXA_TASK_DIR env var override (CI / operator escape hatch,
    #      only when canonical path is not on this host)
    #
    # LOG-F2-2 fix: when canonical is missing AND env empty, raise
    # LookupError instead of silently using a nonexistent path (which
    # caused every tid to return False in fresh/CI environments,
    # suppressing the filter entirely). Caller (heartbeat) treats
    # LookupError as "can't verify, skip gate" — fail-OPEN is safe here
    # because the scanner (`_scan_stale_replans`) only finds tasks under
    # a tasks_root it already decided exists; if it found any, we have
    # a valid tasks_root to check against.
    explicit = tasks_root is not None
    if tasks_root is None:
        canonical = (
            Path(__file__).resolve().parent.parent.parent.parent
            / ".claude" / "harness" / "tasks"
        )
        if canonical.is_dir():
            tasks_root = canonical
        else:
            env_root = os.environ.get("MEMEXA_TASK_DIR", "").strip()
            if env_root and Path(env_root).is_dir():
                tasks_root = Path(env_root)
            else:
                # Neither canonical nor env available — cannot verify.
                # Caller is responsible for deciding fail-open / closed.
                raise LookupError(
                    "validate_real_task_id: no tasks_root available "
                    "(canonical missing and MEMEXA_TASK_DIR unset/invalid)"
                )
    # SEC-2 fix (2026-04-24 security-reviewer): reject if the tid
    # resolves through a symlink/junction that escapes tasks_root.
    try:
        candidate = (tasks_root / tid_s)
        if not candidate.is_dir():
            return False
        # Atomic symlink check (SEC-R1-1 style): lstat doesn't follow.
        import stat as _stat
        st = os.lstat(str(candidate))
        if _stat.S_ISLNK(st.st_mode):
            return False
        # Containment: resolved candidate must be a direct child of tasks_root.
        real_root = Path(tasks_root).resolve(strict=False)
        real_cand = candidate.resolve(strict=False)
        if real_cand.parent != real_root:
            return False
        return True
    except OSError:
        return False


def clean_harness_state(path: Path) -> Tuple[int, int]:
    """Atomic purge of stale action_items from a harness_state.json file.

    Returns ``(kept_count, removed_count)``. No-op if key missing or the
    list is already clean.
    """
    from src.core._atomic_state import atomic_update_json

    counts = {"kept": 0, "removed": 0}

    def _mut(state):
        items = state.get("action_items_for_user") or []
        kept, removed = clean(items)
        counts["kept"] = len(kept)
        counts["removed"] = len(removed)
        if removed:
            state["action_items_for_user"] = kept
            state.setdefault("hygiene_log", []).append({
                "ts": __import__("time").time(),
                "removed_count": len(removed),
                "removed_sample": removed[:5],
            })
        return state

    atomic_update_json(path, _mut)
    return counts["kept"], counts["removed"]


def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="src.core.action_items_hygiene")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("clean", help="purge stale entries from a harness_state JSON")
    c.add_argument("--in-place", required=True, help="path to harness_state.json")

    v = sub.add_parser("check", help="check if any stale entries present (exit 1 if yes)")
    v.add_argument("path", help="path to harness_state.json")

    args = p.parse_args(argv)

    if args.cmd == "clean":
        target = Path(args.in_place)
        kept, removed = clean_harness_state(target)
        sys.stdout.write(
            f"clean: kept={kept} removed={removed} target={target}\n"
        )
        return 0

    if args.cmd == "check":
        try:
            d = json.loads(Path(args.path).read_text(encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"check: cannot read {args.path}: {e}\n")
            return 2
        items = d.get("action_items_for_user") or []
        bad = [x for x in items if is_stale(x)]
        if bad:
            sys.stdout.write(f"STALE: {len(bad)} entry(ies) in action_items_for_user\n")
            for x in bad:
                sys.stdout.write(f"  - {x[:120]}\n")
            return 1
        sys.stdout.write("OK: no stale entries\n")
        return 0

    p.error(f"unknown subcommand: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
