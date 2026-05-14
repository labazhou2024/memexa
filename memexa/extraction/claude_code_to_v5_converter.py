"""Convert Claude Code conversation transcripts (JSONL) to L0 v5 batch prompts (prompt.json).

CEO directive trace: memory/project_l0_phase_g_ustc_handoff_2026_05_06.md §G.3 (第三优先) + §H.6 (优先级2)
Plan ref: docs/l0_v5/CLAUDE_CODE_HISTORY_PLAN.md Stage 1-4

Usage:
  python -m memexa.extraction.claude_code_to_v5_converter \\
    --transcripts-root "~/.claude/projects" \\
    --output data/l0_v5/input_batches_claude \\
    --start-date 2026-01-01 --end-date 2026-05-07 \\
    --self-name "Alice"

Output layout:
  <output>/<YYYY-MM-DD>/<batch_id>/prompt.json
  <output>/<YYYY-MM-DD>/<batch_id>/.done

Batch ID: sha256("claude_code:" + cwd_hash + ":" + first_turn_ts)[:16]
Schema: v5 input (same shape as WeChat/QQ input_batches; source="claude_code")

This module is standalone — do NOT modify any existing l0_v5 converters.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("claude_code_to_v5")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOISE_TYPES = frozenset({
    "queue-operation",
    "permission-mode",
    "last-prompt",
    "file-history-snapshot",
    "system",
})

# Batch cutting thresholds
IDLE_GAP_SECONDS = 7200       # 2 hours
MAX_TURNS_PER_BATCH = 30
MAX_BYTES_PER_BATCH = 100_000  # 100 KB total content
MIN_TURNS_PER_BATCH = 3

# Tool-use content prefix chars limit
TOOL_INPUT_CHARS = 150
TOOL_RESULT_CHARS = 80

# ---------------------------------------------------------------------------
# PII Redaction (requirement §5, per handoff §G.4)
# ---------------------------------------------------------------------------

_PII_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # API keys — ordered most-specific first
    (re.compile(r'sk-ant-[A-Za-z0-9_\-]{10,}'), '[API_KEY_REDACTED]'),
    (re.compile(r'sk-or-[A-Za-z0-9_\-]{10,}'), '[API_KEY_REDACTED]'),
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), '[API_KEY_REDACTED]'),
    # GitHub tokens
    (re.compile(r'ghp_[a-zA-Z0-9]{36,}'), '[GITHUB_TOKEN_REDACTED]'),
    # Generic credential assignments
    (re.compile(r'(?i)(password|token)\s*=\s*[^\s\'"]{4,}'), '[REDACTED]'),
    # Home paths — generalised: any Windows / Unix user-home prefix
    (re.compile(r'C:\\\\Users\\\\[^\\\\/]+', re.IGNORECASE), '~user'),
    (re.compile(r'C:/Users/[^/]+', re.IGNORECASE), '~user'),
    (re.compile(r'/Users/[^/]+'), '~user'),
    (re.compile(r'/home/[^/]+'), '~user'),
]


def _redact(text: str) -> str:
    """Apply all PII redaction patterns to a text string."""
    for pat, replacement in _PII_PATTERNS:
        text = pat.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def _cwd_to_encoded(cwd: str) -> str:
    """Encode a CWD path to directory-safe ASCII (mirrors Claude Code project naming).

    e.g. ``~/workspace/project`` -> ``-Users-username-workspace-project``
    (Claude Code replaces ``:``, ``/``, and ``\\`` with ``-``).
    """
    # Normalise separators
    s = cwd.replace("\\", "/")
    # Replace filesystem-unsafe chars with dashes (same pattern Claude Code uses)
    # Claude Code replaces :/\\ with -- or - per character
    s = re.sub(r'[:/\\]', '-', s)
    # Collapse triple+ dashes (from e.g. path with spaces that became --)
    # Actually keep as-is to match Claude Code naming; only replace special chars
    return s


def _room_hash(encoded_cwd: str) -> str:
    return _sha256_hex(encoded_cwd)[:32]


def _wxid_hash(key: str) -> str:
    return _sha256_hex(key)[:16]


def _batch_id(cwd_hash: str, first_turn_ts: str) -> str:
    """sha256("claude_code:" + cwd_hash + ":" + first_turn_ts)[:16]"""
    raw = f"claude_code:{cwd_hash}:{first_turn_ts}"
    return _sha256_hex(raw)[:16]


# ---------------------------------------------------------------------------
# JSONL Reader / Normalizer (Stage 1)
# ---------------------------------------------------------------------------

def _is_system_prompt(content: Any) -> bool:
    """True if content is a system-reminder injected string (noise)."""
    if isinstance(content, str):
        return "<system-reminder>" in content or content.startswith("You are Claude Code")
    return False


def _parse_turn(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse one JSONL line into a normalised turn dict, or None if noise."""
    t = obj.get("type", "")
    if t in NOISE_TYPES:
        return None

    # attachment type: drop hook-injected content
    if t == "attachment":
        att = obj.get("attachment", {})
        if isinstance(att, dict):
            hook_event = att.get("hookEvent", "")
            if hook_event in ("SessionStart:startup", "UserPromptSubmit"):
                return None
        # attachment with no useful conversation content → drop
        return None

    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None

    role = msg.get("role", "")
    if role not in ("user", "assistant"):
        return None

    content = msg.get("content")
    if content is None or content == "" or content == []:
        return None

    # Drop assistant turns that only have thinking blocks (unrecoverable)
    if role == "assistant" and isinstance(content, list):
        non_thinking = [c for c in content if isinstance(c, dict) and c.get("type") != "thinking"]
        if not non_thinking:
            return None
        content = non_thinking

    # Drop system prompt injections in user content (string form)
    if role == "user" and isinstance(content, str) and _is_system_prompt(content):
        return None

    # Drop user turns that are pure tool_result with no real content
    if role == "user" and isinstance(content, list):
        non_tr = [c for c in content if isinstance(c, dict) and c.get("type") != "tool_result"]
        if not non_tr and all(
            isinstance(c, dict) and (c.get("content") == "" or c.get("content") is None)
            for c in content
        ):
            return None

    ts_raw = obj.get("timestamp", "")
    try:
        ts_dt = _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        ts = ts_dt.isoformat()
    except Exception:
        ts = ts_raw

    return {
        "ts": ts,
        "ts_raw": ts_raw,
        "role": role,
        "user_type": obj.get("userType", "external"),
        "is_sidechain": obj.get("isSidechain", False),
        "cwd": obj.get("cwd", ""),
        "git_branch": obj.get("gitBranch", ""),
        "session_id": obj.get("sessionId", ""),
        "uuid": obj.get("uuid", ""),
        "parent_uuid": obj.get("parentUuid"),
        "content": content,
        "version": obj.get("version", ""),
    }


def read_session(path: Path) -> List[Dict[str, Any]]:
    """Read one JSONL session file, return normalised turn list (noise dropped)."""
    turns: List[Dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                turn = _parse_turn(obj)
                if turn is not None:
                    turns.append(turn)
    except OSError as e:
        logger.warning(f"Cannot read {path}: {e}")
    return turns


# ---------------------------------------------------------------------------
# Tool block flattening (requirement §4)
# ---------------------------------------------------------------------------

def _flatten_content(content: Any, role: str) -> str:
    """Flatten a message content (str or list of blocks) to a single string."""
    if isinstance(content, str):
        return _redact(content)

    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")

        if btype == "text":
            text = block.get("text", "")
            if text:
                if role == "assistant":
                    parts.append(f"[Claude] {_redact(text)}")
                else:
                    parts.append(_redact(text))

        elif btype == "thinking":
            # Encrypted — skip entirely
            pass

        elif btype == "tool_use":
            name = block.get("name", "?")
            inp = block.get("input", {})
            # Pick most informative input field
            if name in ("Bash",):
                raw = inp.get("command", inp.get("description", str(inp)))
            elif name in ("Read",):
                raw = inp.get("file_path", str(inp))
            elif name in ("Write", "Edit"):
                raw = inp.get("file_path", str(inp))
            elif name in ("Grep", "Glob"):
                raw = inp.get("pattern", str(inp))
            else:
                raw = json.dumps(inp, ensure_ascii=False) if inp else ""
            snippet = _redact(raw[:TOOL_INPUT_CHARS])
            parts.append(f"[Claude tool_use:{name}] {snippet}")

        elif btype == "tool_result":
            is_error = block.get("is_error", False)
            if is_error:
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = " ".join(
                        c.get("text", "") for c in result_content if isinstance(c, dict)
                    )
                snippet = _redact(str(result_content)[:TOOL_RESULT_CHARS])
                parts.append(f"[tool result ERR: {snippet}]")
            else:
                # Skip successful tool results — too noisy
                parts.append("[tool result OK]")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Segmenter / Batch Cutter (Stage 2 logic, simplified for this converter)
# ---------------------------------------------------------------------------

def _ts_epoch(ts_raw: str) -> float:
    """Parse ISO timestamp to epoch float; 0.0 on failure."""
    try:
        dt = _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


_TASK_HEADER_RE = re.compile(
    r'^(请你|帮我|现在|新任务|task\s*[:：]|Build\s|Implement\s|Fix\s|Create\s)',
    re.IGNORECASE
)


def _is_task_header(turn: Dict[str, Any]) -> bool:
    """True if user turn starts with a strong task header pattern."""
    if turn.get("role") != "user":
        return False
    content = turn.get("content", "")
    text = content if isinstance(content, str) else ""
    if not text and isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
    return bool(_TASK_HEADER_RE.match(text.strip()))


def segment_session(turns: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Cut normalised turns into work-unit batches.

    Cut rules (priority descending):
      (a) CWD change → hard cut
      (b) Idle gap >2h → hard cut
      (c) Task header (user prompt matching pattern) → soft cut if ≥10 turns since last cut
      Hard limit: 30 turns or 100 KB per batch
    """
    if not turns:
        return []

    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_bytes = 0
    turns_since_cut = 0

    for i, turn in enumerate(turns):
        cwd = turn.get("cwd", "")
        ts_raw = turn.get("ts_raw", turn.get("ts", ""))

        # Hard cut conditions
        should_cut = False
        if current:
            prev = current[-1]
            prev_cwd = prev.get("cwd", "")
            prev_ts = prev.get("ts_raw", prev.get("ts", ""))

            # (a) CWD change
            if cwd and prev_cwd and cwd != prev_cwd:
                should_cut = True

            # (b) Idle gap
            if not should_cut:
                gap = _ts_epoch(ts_raw) - _ts_epoch(prev_ts)
                if gap > IDLE_GAP_SECONDS:
                    should_cut = True

            # (c) Task header (soft cut — only if ≥10 turns since last cut)
            if not should_cut and turns_since_cut >= 10 and _is_task_header(turn):
                should_cut = True

            # Hard limit: batch too large
            turn_bytes = len(json.dumps(turn, ensure_ascii=False))
            if len(current) >= MAX_TURNS_PER_BATCH or (current_bytes + turn_bytes) > MAX_BYTES_PER_BATCH:
                should_cut = True

        if should_cut and len(current) >= MIN_TURNS_PER_BATCH:
            batches.append(current)
            current = []
            current_bytes = 0
            turns_since_cut = 0
        elif should_cut and current:
            # Too small — flush anyway to avoid losing data (will be filtered later)
            batches.append(current)
            current = []
            current_bytes = 0
            turns_since_cut = 0

        current.append(turn)
        current_bytes += len(json.dumps(turn, ensure_ascii=False))
        turns_since_cut += 1

    if current:
        batches.append(current)

    return batches


# ---------------------------------------------------------------------------
# v5 Schema Builder (Stage 4)
# ---------------------------------------------------------------------------

def build_prompt_json(
    batch_turns: List[Dict[str, Any]],
    self_name: str,
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """Convert a list of turns into a v5 prompt.json dict.

    Returns None if batch has no usable content.
    """
    if not batch_turns:
        return None

    # Determine CWD from first turn that has one
    cwd = ""
    for t in batch_turns:
        if t.get("cwd"):
            cwd = t["cwd"]
            break

    encoded_cwd = _cwd_to_encoded(cwd) if cwd else "unknown"
    room_h = _room_hash(encoded_cwd)
    chat_room = encoded_cwd  # human-readable = encoded CWD (ASCII-safe)

    first_ts = batch_turns[0].get("ts", "")
    last_ts = batch_turns[-1].get("ts", "")
    first_ts_raw = batch_turns[0].get("ts_raw", first_ts)

    # Batch ID: sha256("claude_code:" + cwd_hash + ":" + first_turn_ts)[:16]
    # Use cwd_hash as sha256(encoded_cwd)[:16]
    cwd_hash_short = _sha256_hex(encoded_cwd)[:16]
    bid = _batch_id(cwd_hash_short, first_ts_raw)

    # Sender list: exactly 2 entries — self + claude
    self_wxid_hash = _wxid_hash(f"self_{encoded_cwd}")
    claude_wxid_hash = _wxid_hash("claude_assistant_claude_code")

    sender_list = [
        {
            "wxid_hash": self_wxid_hash,
            "alias_in_manifest_or_None": self_name,
            "is_self": True,
        },
        {
            "wxid_hash": claude_wxid_hash,
            "alias_in_manifest_or_None": "Claude",
            "is_self": False,
        },
    ]

    # Build messages list
    messages: List[Dict[str, Any]] = []
    for turn in batch_turns:
        role = turn.get("role", "")
        ts = turn.get("ts", "")
        content = turn.get("content")

        if not content:
            continue

        if role == "user":
            wxid_hash = self_wxid_hash
            # User turns: content may be str (normal prompt) or list (tool_result blocks)
            text = _flatten_content(content, role="user")
        elif role == "assistant":
            wxid_hash = claude_wxid_hash
            text = _flatten_content(content, role="assistant")
        else:
            continue

        if not text.strip():
            continue

        messages.append({
            "ts": ts,
            "wxid_hash": wxid_hash,
            "content": text,
        })

    if not messages:
        return None

    prompt_json: Dict[str, Any] = {
        "batch_id": bid,
        "source": "claude_code",
        "chat_room": chat_room,
        "room_hash": room_h,
        "room_tier": 2,
        "batch_window_local": f"{first_ts} ~ {last_ts}",
        "sender_list": sender_list,
        "manifest_slice": {},
        "messages": messages,
        "chinese_calendar_window": None,
        "user_calendar_window": None,
        "schema_v_input": "v5",
        # metadata
        "session_id": session_id,
        "encoded_cwd": encoded_cwd,
        "cwd": cwd,
    }

    return prompt_json


# ---------------------------------------------------------------------------
# Date filter helpers
# ---------------------------------------------------------------------------

def _session_date(turns: List[Dict[str, Any]], fallback_mtime: float) -> str:
    """Return YYYY-MM-DD string for the session's first turn."""
    for t in turns:
        ts = t.get("ts", "")
        if ts:
            try:
                dt = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    # Fallback to file mtime
    dt = _dt.datetime.fromtimestamp(fallback_mtime, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _in_date_range(date_str: str, start: Optional[str], end: Optional[str]) -> bool:
    if start and date_str < start:
        return False
    if end and date_str > end:
        return False
    return True


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def iter_project_dirs(transcripts_root: Path) -> Iterator[Tuple[str, Path]]:
    """Yield (encoded_cwd, project_dir) for each project directory."""
    try:
        for item in sorted(transcripts_root.iterdir()):
            if item.is_dir():
                yield item.name, item
    except OSError as e:
        logger.error(f"Cannot list transcripts root {transcripts_root}: {e}")


def convert_all(
    transcripts_root: Path,
    output_root: Path,
    start_date: Optional[str],
    end_date: Optional[str],
    self_name: str,
    max_sessions: Optional[int] = None,
) -> Tuple[int, int, int]:
    """Run full conversion pipeline.

    Returns (sessions_processed, batches_written, batches_skipped).
    """
    sessions_processed = 0
    batches_written = 0
    batches_skipped = 0

    for encoded_cwd, project_dir in iter_project_dirs(transcripts_root):
        logger.info(f"Project: {encoded_cwd}")

        jsonl_files = sorted(project_dir.glob("*.jsonl"))
        for jf in jsonl_files:
            if max_sessions is not None and sessions_processed >= max_sessions:
                logger.info(f"Reached max_sessions={max_sessions}, stopping.")
                return sessions_processed, batches_written, batches_skipped

            mtime = jf.stat().st_mtime
            session_id = jf.stem  # UUID without .jsonl

            turns = read_session(jf)
            if not turns:
                continue

            # Date filter — use first turn timestamp
            date_str = _session_date(turns, mtime)
            if not _in_date_range(date_str, start_date, end_date):
                continue

            sessions_processed += 1

            # Segment session into work-unit batches
            batches = segment_session(turns)

            for batch_turns in batches:
                if len(batch_turns) < MIN_TURNS_PER_BATCH:
                    batches_skipped += 1
                    continue

                prompt = build_prompt_json(batch_turns, self_name=self_name, session_id=session_id)
                if prompt is None:
                    batches_skipped += 1
                    continue

                bid = prompt["batch_id"]

                # Output: <output>/<date>/<batch_id>/prompt.json
                out_dir = output_root / date_str / bid
                out_dir.mkdir(parents=True, exist_ok=True)

                # Idempotent: skip if .done sentinel exists
                done_sentinel = out_dir / ".done"
                if done_sentinel.exists():
                    batches_skipped += 1
                    continue

                out_path = out_dir / "prompt.json"
                out_path.write_text(
                    json.dumps(prompt, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                # Write .done sentinel
                done_sentinel.write_text(f"done at {_dt.datetime.utcnow().isoformat()}Z\n")
                batches_written += 1

                logger.debug(f"  batch {bid} → {out_dir}")

    return sessions_processed, batches_written, batches_skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Claude Code JSONL transcripts to L0 v5 prompt.json batches."
    )
    parser.add_argument(
        "--transcripts-root",
        type=Path,
        default=Path(os.path.expanduser("~/.claude/projects")),
        help="Root directory containing Claude Code project dirs (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/l0_v5/input_batches_claude"),
        help="Output root for batches (default: data/l0_v5/input_batches_claude)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Inclusive start date filter",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Inclusive end date filter",
    )
    parser.add_argument(
        "--self-name",
        type=str,
        default="Alice",
        help="Display name for self (CEO) in sender_list",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=None,
        help="Limit total sessions processed (for dry-run testing)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    transcripts_root = args.transcripts_root
    if not transcripts_root.exists():
        logger.error(f"transcripts-root not found: {transcripts_root}")
        return 2

    args.output.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"Starting conversion: root={transcripts_root} out={args.output} "
        f"dates={args.start_date}~{args.end_date} self={args.self_name}"
    )

    sessions, written, skipped = convert_all(
        transcripts_root=transcripts_root,
        output_root=args.output,
        start_date=args.start_date,
        end_date=args.end_date,
        self_name=args.self_name,
        max_sessions=args.max_sessions,
    )

    logger.info(
        f"Done: sessions_processed={sessions} batches_written={written} batches_skipped={skipped}"
    )
    print(f"sessions_processed={sessions} batches_written={written} batches_skipped={skipped}")

    # Print sample from first written batch
    if written > 0:
        for date_dir in sorted(args.output.iterdir()):
            if not date_dir.is_dir():
                continue
            for batch_dir in sorted(date_dir.iterdir()):
                pj = batch_dir / "prompt.json"
                if pj.exists():
                    data = json.loads(pj.read_text(encoding="utf-8"))
                    print(f"\nSample batch: {batch_dir}")
                    print(f"  prompt.json keys: {list(data.keys())}")
                    print(f"  room_hash: {data.get('room_hash','?')}")
                    print(f"  messages_count: {len(data.get('messages',[]))}")
                    print(f"  batch_window: {data.get('batch_window_local','?')}")
                    return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
