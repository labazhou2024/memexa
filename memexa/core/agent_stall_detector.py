"""Agent stall detector -- 2026-04-21 plan feedback loop bug-1 fix.

Prevents agent death-loops (e.g. chief-researcher chief 13min → 14 bytes
output) from silently burning minutes and token budget.

Two entry points, designed to be wired into Claude Code hooks:

1. `post_check()` — PostToolUse handler for Agent tool. Computes
   output_bytes / duration ratio. If < 50 B/min AND duration > 300s,
   writes a stall flag + emits trace event.

2. `pre_check(subagent_type)` — callable from PreToolUse hook (or from
   pretool_gate main). If a stall flag exists for the same subagent_type
   and is <30 min old, BLOCK the spawn.

Flags live at `.claude/harness/flags/agent_stall_<subagent_type>`. Each
flag file contains a one-line JSON with the stall event details.

Manual reset: `python -m memexa.core.agent_stall_detector clear`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# Bytes-per-minute threshold. Normal research agents emit >500 B/min.
# First chief-researcher stall was 14 B / 800s = 1.05 B/min. Threshold 50
# gives generous headroom without false-positive on short quiet spells.
STALL_BPM_THRESHOLD = 50.0

# Minimum duration before the ratio check fires. Short agents legitimately
# return 0 bytes quickly (e.g. "nothing to do" → exit 0 in 30s).
STALL_MIN_DURATION_SEC = 300.0

# Flag TTL. A stall flag for subagent_type X blocks spawns for this long
# before auto-clearing. Operator can also run `clear` CLI to reset sooner.
STALL_FLAG_TTL_SEC = 300.0  # 5 min (W1-5 2026-05-04: was 1800/30min — over-blocked council/reviewer iter retry; per logic-reviewer L-3 pre-edit grep 1800 in tests/ found 0 stall references → safe)


def _flags_dir() -> Path:
    """Return .claude/harness/flags/, creating if missing."""
    # agent_stall_detector.py sits at memexa/memexa/core/
    # .claude sits at workspace root = parent of memexa
    d = (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".claude" / "harness" / "flags"
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trace_path() -> Path:
    """Return the agent_stall_trace.jsonl path.

    Test-isolation override (2026-04-24 F1 fix): honor env var
    ``MEMEXA_AGENT_STALL_TRACE_PATH`` when set AND its parent dir exists
    AND it's in an allowed location. Without this gate, the pollution
    test in tests/test_plan_feedback_loop.py (which raises OSError from
    _flags_dir) triggered _emit_trace to write into the production trace
    file, polluting 21+ bogus "cannot mkdir" entries.

    SEC-1 fix (2026-04-24 security-reviewer round 2): only honor override
    if it resolves to either (a) pytest's tmp directory or (b) under the
    workspace root. Rejects arbitrary system paths like C:/Windows/Temp
    to prevent env-var-based path traversal writes.
    """
    override = os.environ.get("MEMEXA_AGENT_STALL_TRACE_PATH", "").strip()
    if override:
        p = Path(override)
        if p.parent.exists() and _is_allowed_trace_path(p):
            return p
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".claude" / "harness" / "agent_stall_trace.jsonl"
    )


def _is_allowed_trace_path(p: Path) -> bool:
    """True if override path is under workspace root OR under pytest tmp.

    Accepts:
      - Workspace root: ``Path(__file__).parent.parent.parent.parent``
      - System tmp: ``tempfile.gettempdir()`` (pytest tmp_path_factory
        creates dirs under here on Windows and POSIX).
    Rejects anything else (e.g. C:/Windows, /etc, arbitrary user dirs).
    """
    import tempfile
    try:
        real_p = p.resolve(strict=False)
        workspace = (
            Path(__file__).resolve().parent.parent.parent.parent
        )
        tmp_root = Path(tempfile.gettempdir()).resolve()
        for allowed in (workspace, tmp_root):
            try:
                real_p.relative_to(allowed)
                return True
            except ValueError:
                continue
        return False
    except OSError:
        return False


def _emit_trace(event: str, payload: dict) -> None:
    """Append one JSONL line for post-hoc debugging. Fail-soft."""
    try:
        record = {"ts": time.time(), "event": event, **payload}
        with _trace_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _safe_sanitize(name: str) -> str:
    """Strip characters unsafe in filesystem paths. Dots excluded to
    prevent any '..' pattern from surviving (defense in depth vs
    filesystem traversal)."""
    allow = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    return "".join(c if c in allow else "_" for c in (name or "unknown"))[:64]


def post_check(subagent_type: str, duration_sec: float, output_bytes: int) -> bool:
    """Called from PostToolUse after Agent tool returns.

    Returns True iff a stall was detected (and flag was written).
    Never raises.
    """
    try:
        if duration_sec is None or duration_sec <= 0:
            return False
        if duration_sec < STALL_MIN_DURATION_SEC:
            return False
        bpm = (output_bytes or 0) / (duration_sec / 60.0)
        if bpm >= STALL_BPM_THRESHOLD:
            return False

        # Stall detected — write flag
        sanitized = _safe_sanitize(subagent_type)
        flag_path = _flags_dir() / f"agent_stall_{sanitized}"
        stall_record = {
            "subagent_type": subagent_type,
            "duration_sec": duration_sec,
            "output_bytes": output_bytes,
            "bpm": round(bpm, 3),
            "ts": time.time(),
        }
        flag_path.write_text(
            json.dumps(stall_record, ensure_ascii=False),
            encoding="utf-8",
        )
        _emit_trace("agent_stall_detected", stall_record)
        return True
    except Exception as e:
        _emit_trace("agent_stall_post_check_error", {"err": str(e)[:200]})
        return False


def pre_check(subagent_type: str) -> Tuple[bool, Optional[str]]:
    """Called before spawning an Agent.

    Returns (allow, block_reason):
      - (True, None) if no recent stall flag for this subagent_type
      - (False, human-readable reason) if blocked
    Stale flags (> TTL) are auto-cleared.
    """
    try:
        sanitized = _safe_sanitize(subagent_type)
        flag_path = _flags_dir() / f"agent_stall_{sanitized}"
        if not flag_path.exists():
            return True, None

        age = time.time() - flag_path.stat().st_mtime
        if age > STALL_FLAG_TTL_SEC:
            # Auto-clear stale flag — 2026-04-22 migrated to safe_unlink
            # (baseline exemption shrinkage; same semantics, adds symlink guard)
            from memexa.core._safe_fs import safe_unlink
            safe_unlink(flag_path, _flags_dir())
            return True, None

        # Still fresh → block
        try:
            info = json.loads(flag_path.read_text(encoding="utf-8"))
        except Exception:
            info = {}
        mins_left = round((STALL_FLAG_TTL_SEC - age) / 60, 1)
        reason = (
            f"agent_stall: prior {subagent_type} spawn produced "
            f"{info.get('output_bytes', '?')} bytes in "
            f"{round(info.get('duration_sec', 0), 1)}s "
            f"({info.get('bpm', '?')} B/min). Flag auto-clears in "
            f"~{mins_left} min, or run `python -m "
            f"memexa.core.agent_stall_detector clear {sanitized}`."
        )
        return False, reason
    except Exception as e:
        _emit_trace("agent_stall_pre_check_error", {"err": str(e)[:200]})
        # fail-open on error
        return True, None


def clear_flag(subagent_type: Optional[str] = None) -> int:
    """Manually clear one or all stall flags. Returns count cleared.

    2026-04-22: migrated to _safe_fs.safe_unlink for symlink-safety."""
    from memexa.core._safe_fs import safe_unlink
    d = _flags_dir()
    count = 0
    if subagent_type:
        sanitized = _safe_sanitize(subagent_type)
        p = d / f"agent_stall_{sanitized}"
        if safe_unlink(p, d):
            count = 1
    else:
        for p in d.glob("agent_stall_*"):
            if safe_unlink(p, d):
                count += 1
    return count


def _cli():
    args = sys.argv[1:]
    if not args:
        print("usage: python -m memexa.core.agent_stall_detector "
              "<clear|list|check> [subagent_type]", file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "clear":
        target = args[1] if len(args) > 1 else None
        n = clear_flag(target)
        print(f"cleared {n} flag(s)")
        return 0
    if cmd == "list":
        for p in _flags_dir().glob("agent_stall_*"):
            try:
                body = p.read_text(encoding="utf-8")
            except Exception:
                body = "(unreadable)"
            print(f"{p.name}: {body}")
        return 0
    if cmd == "check":
        if len(args) < 2:
            print("usage: check <subagent_type>", file=sys.stderr)
            return 2
        allow, reason = pre_check(args[1])
        print(json.dumps({"allow": allow, "reason": reason}, ensure_ascii=False))
        return 0 if allow else 1
    if cmd == "mark":
        # Manual stall marking. For when Claude sees a stalled agent in the
        # conversation stream and wants to block next spawn.
        # usage: mark <subagent_type> <duration_sec> <output_bytes>
        if len(args) < 4:
            print("usage: mark <subagent_type> <duration_sec> <output_bytes>",
                  file=sys.stderr)
            return 2
        try:
            dur = float(args[2])
            bts = int(args[3])
        except (ValueError, TypeError):
            print("mark: duration_sec and output_bytes must be numeric",
                  file=sys.stderr)
            return 2
        marked = post_check(args[1], dur, bts)
        print(json.dumps({"marked": marked,
                          "subagent": args[1],
                          "duration_sec": dur,
                          "output_bytes": bts}, ensure_ascii=False))
        return 0 if marked else 1
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
