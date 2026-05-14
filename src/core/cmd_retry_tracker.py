"""Rule 10 support: two-tier budget tracker for non-retryable Bash commands.

Design (plan_v1 §3):
  - Append every Bash command to `.claude/harness/flags/cmd_history.jsonl`.
  - On each command, check_budget(cmd) compares it against a seed regex
    table; if the count within either window (short) OR daily cap exceeds
    the per-pattern max_n, return DENY.
  - Canonical rule (plan_v1 L1 fix): DENY when count >= max_n (count
    includes current request). E.g. max_n=3 means 3rd invocation is blocked.
  - Rotate-after-append semantic (I1 fix): append first, then rotate if
    file exceeds 1MB. check_budget reads recent records from the jsonl +
    the archive if archive is <1h old, so the window is never lost.
  - Concurrent safety (L3 fix): append + rotate are guarded by
    `_file_lock.locked_open` (msvcrt on Windows, fcntl on POSIX).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from src.core._file_lock import locked_open
from src.core.sanitize import sanitize_for_log

# ---------------------------------------------------------------------------
# Seed pattern table
# Canonical semantic: block when count >= max_n  (count includes current call)
# window_s: short window ; daily_cap: 24-hour total
# memory_file: cited in deny reason so LLM gets a pointer
# ---------------------------------------------------------------------------
_BUDGET_TABLE: List[Tuple[re.Pattern, int, int, int, str]] = [
    # (regex, max_n_window, window_s, max_n_daily, memory_file_cite)
    (re.compile(r"\bwsl(\.exe)?\s+(-d|--distribution)\s"), 3, 600, 5, "feedback_wsl_poll_zombie_trap.md"),
    (re.compile(r"\bwsl(\.exe)?\s+--list\b"), 4, 600, 8, "feedback_wsl_poll_zombie_trap.md"),
    (re.compile(r"\bdocker\s+desktop\s+(start|restart|engine\s+use)\b"), 2, 600, 4, "feedback_wsl_poll_zombie_trap.md"),
    (re.compile(r"Start-Process.*Docker Desktop\.exe"), 2, 600, 3, "feedback_probe_before_gui_recommend.md"),
]

_HISTORY_MAX_BYTES = 1 * 1024 * 1024   # 1 MB
_ARCHIVE_MAX_AGE_S = 3600              # 1 h — older archives ignored


def _flags_dir() -> Path:
    # cmd_retry_tracker.py sits at memex/memex/core/
    # .claude sits at workspace root = parent of memex
    d = Path(__file__).resolve().parent.parent.parent.parent / ".claude" / "harness" / "flags"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _history_path() -> Path:
    return _flags_dir() / "cmd_history.jsonl"


def _archive_path(ts: Optional[int] = None) -> Path:
    ts = ts if ts is not None else int(time.time())
    return _flags_dir() / f"cmd_history.jsonl.archive.{ts}"


@dataclass
class BudgetDecision:
    allow: bool
    reason: str = ""
    memory_file: str = ""
    match_pattern: str = ""


def _record_from_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except Exception:
        return None


def _tail_records(path: Path, max_lines: int = 100) -> List[dict]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-max_lines:]
        return [r for r in (_record_from_line(l) for l in tail) if r]
    except Exception:
        return []


def _load_window_records() -> List[dict]:
    """Read recent history + any fresh archive (<1h old)."""
    records = _tail_records(_history_path(), max_lines=100)
    # Find the most recent archive file (if any) and merge its tail.
    now = time.time()
    for entry in sorted(_flags_dir().glob("cmd_history.jsonl.archive.*"), reverse=True):
        try:
            ts = int(entry.name.rsplit(".", 1)[-1])
        except Exception:
            continue
        if now - ts < _ARCHIVE_MAX_AGE_S:
            records = _tail_records(entry, max_lines=100) + records
            break  # Only the newest fresh archive.
    return records


def _rotate_if_needed() -> None:
    h = _history_path()
    try:
        sz = h.stat().st_size
    except OSError:
        return
    if sz < _HISTORY_MAX_BYTES:
        return
    try:
        dest = _archive_path()
        os.replace(str(h), str(dest))  # atomic on Windows & POSIX
        # Re-create empty file so subsequent appenders don't race on missing file.
        h.touch(exist_ok=True)
    except Exception:
        pass


def check_budget(cmd: str, now: Optional[float] = None) -> BudgetDecision:
    """Check whether `cmd` should be DENIED before appending to history.

    Canonical rule: DENY when (count of matches including this one) >= max_n
    in either the short window or the 24-hour daily cap.

    On ALLOW: caller MUST call `_append(cmd)` to record the command.
    """
    now = now if now is not None else time.time()
    records = _load_window_records()

    for pattern, max_window, window_s, max_daily, mem in _BUDGET_TABLE:
        if not pattern.search(cmd):
            continue
        # Count prior matches of the SAME pattern within the windows.
        prior_window = 0
        prior_daily = 0
        for r in records:
            cmd_r = r.get("cmd", "")
            ts_r = r.get("ts", 0)
            if not pattern.search(cmd_r):
                continue
            if now - ts_r <= window_s:
                prior_window += 1
            if now - ts_r <= 86400:
                prior_daily += 1
        # Including CURRENT call → count = prior + 1
        this_count_window = prior_window + 1
        this_count_daily = prior_daily + 1
        # S1 fix: pattern.pattern is a static regex literal we control; no
        # user-data interpolation here, so deny reason is safe from injection.
        if this_count_window >= max_window:
            return BudgetDecision(
                allow=False,
                reason=(
                    f"Rule 10: pattern {pattern.pattern!r} ran {this_count_window}x "
                    f"within {window_s}s (max={max_window}). See memory/{mem}."
                ),
                memory_file=mem,
                match_pattern=pattern.pattern,
            )
        if this_count_daily >= max_daily:
            return BudgetDecision(
                allow=False,
                reason=(
                    f"Rule 10: pattern {pattern.pattern!r} ran {this_count_daily}x in 24h "
                    f"(daily cap={max_daily}). See memory/{mem}."
                ),
                memory_file=mem,
                match_pattern=pattern.pattern,
            )
        # Matched but under budget — allow.
        return BudgetDecision(allow=True, match_pattern=pattern.pattern)

    # No pattern matched — allow without tracking.
    return BudgetDecision(allow=True)


def _append(cmd: str, now: Optional[float] = None) -> None:
    """Append one record. Must be called on ALLOW from check_budget."""
    now = now if now is not None else time.time()
    record = {"ts": now, "cmd": sanitize_for_log(cmd)}
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with locked_open(_history_path(), "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
    # Rotate after append (I1 fix): check_budget reads archive if <1h old so
    # the window is not lost; putting rotate after write also ensures the
    # just-appended record is the LAST one before rotation.
    _rotate_if_needed()


def record_and_check(cmd: str, now: Optional[float] = None) -> BudgetDecision:
    """Atomic: sanitize → check_budget → append if allowed.

    S5 fix: sanitize cmd at entry so NUL/CR/LF cannot corrupt JSONL or smuggle
    instructions into reflected deny reasons.
    """
    cmd = sanitize_for_log(cmd)
    decision = check_budget(cmd, now=now)
    if decision.allow:
        _append(cmd, now=now)
    return decision
