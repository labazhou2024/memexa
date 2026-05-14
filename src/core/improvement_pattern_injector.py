"""improvement_pattern_injector — inject Inherited Lessons §0.1 into plan_v0 (U19 G6).

Reads memexa/data/improvement_patterns.jsonl, filters by task_type, sorts by
helpful_count + recency, caps by UTF-8 byte size, formats as markdown table,
and injects as `## §0.1 Inherited Lessons` section before first `## ` heading
(respects YAML frontmatter).

CLI:
    python -m src.core.improvement_pattern_injector inject <plan_path> --task-type DEVELOP
    python -m src.core.improvement_pattern_injector list --task-type DEVELOP
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "load_patterns",
    "filter_by_task_type",
    "format_inherited_lessons_section",
    "inject_inherited_lessons",
]

# TU-3 (learning_pip 2026-04-30): path drift bug fix.
# Previous: parents[2] resolved to `memexa/` -> `memexa/data/` (stale, 28 entries).
# Fix: parents[1] = `memexa/memexa/` -> `memexa/memexa/data/` (live KB, 2k+ entries).
# Aligns with plan_retro_gate._kb_path() and pattern_extractor._PATTERNS_FILE.
# Per HARD RULE feedback_writer_reader_schema_contract.
_MEMEXA_ROOT = Path(__file__).resolve().parents[1]  # memexa/memexa/
_DEFAULT_JSONL = _MEMEXA_ROOT / "data" / "improvement_patterns.jsonl"


def load_patterns(jsonl_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = Path(jsonl_path) if jsonl_path else _DEFAULT_JSONL
    if not p.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if isinstance(entry, dict):
                out.append(entry)
        except json.JSONDecodeError:
            continue
    return out


def filter_by_task_type(patterns: List[Dict[str, Any]], task_type: str,
                        top_n: int = 10, byte_cap_utf8: int = 2048) -> List[Dict[str, Any]]:
    """Filter by task_type (CASE-INSENSITIVE); sort helpful_count desc + recorded_at desc.

    TU-3 (learning_pip 2026-04-30): case-insensitive comparison closes RC-C.
    Historical KB stores task_type='develop' (lowercase) but autopilot v2.0 enum
    is 'DEVELOP' (uppercase) — exact match misses 100% of historical RPs.
    Per HARD RULE feedback_writer_reader_schema_contract.

    logic-iter1-3 fix: recorded_at DESC (newer first); reverse sort key.
    logic-iter1-5 fix: byte_cap STRICT - first oversized entry returned only when running_bytes==0
    AND the entry alone fits; otherwise dropped.
    """
    target = (task_type or "").strip().lower()
    filtered = [p for p in patterns
                if (p.get("task_type") or "").strip().lower() == target]
    # TU-3 (learning_pip): coerce recorded_at to string (allows None / float / str)
    def _ra_str(p: Dict[str, Any]) -> str:
        ra = p.get("recorded_at") or p.get("ts") or p.get("created_at") or ""
        return str(ra) if ra is not None else ""

    # Stable resort with proper reversed string sort:
    filtered.sort(
        key=lambda p: (-int(p.get("helpful_count") or 0), _ra_str(p)),
        reverse=False,
    )
    # Reverse only the recorded_at within same helpful_count groups by re-bucketing:
    by_help: Dict[int, List[Dict[str, Any]]] = {}
    for p in filtered:
        hc = -int(p.get("helpful_count") or 0)
        by_help.setdefault(hc, []).append(p)
    rebuilt: List[Dict[str, Any]] = []
    for hc in sorted(by_help.keys()):
        rebuilt.extend(sorted(by_help[hc], key=_ra_str, reverse=True))
    filtered = rebuilt

    out: List[Dict[str, Any]] = []
    running_bytes = 0
    for entry in filtered:
        encoded = json.dumps(entry, ensure_ascii=False).encode("utf-8")
        if running_bytes + len(encoded) > byte_cap_utf8:
            # logic-iter1-5 fix: NO bypass for first entry - if it alone exceeds cap, drop it
            break
        out.append(entry)
        running_bytes += len(encoded)
        if len(out) >= top_n:
            break
    return out


def format_inherited_lessons_section(patterns: List[Dict[str, Any]]) -> str:
    if not patterns:
        return ""
    lines = ["## §0.1 Inherited Lessons (auto-injected from improvement_patterns.jsonl)\n"]
    lines.append("| ID | Task type | Lesson |")
    lines.append("|---|---|---|")
    for entry in patterns:
        # security-iter1-7 fix: pipe-escape ALL columns, not only lesson
        eid = str(entry.get("id") or entry.get("pattern_id") or "<no-id>")[:60].replace("|", "\\|").replace("\n", " ")
        task_type = str(entry.get("task_type") or "?")[:20].replace("|", "\\|").replace("\n", " ")
        lesson = str(entry.get("lesson") or entry.get("issue") or "")[:200].replace("\n", " ").replace("|", "\\|")
        lines.append(f"| {eid} | {task_type} | {lesson} |")
    lines.append("")
    return "\n".join(lines)


_FRONTMATTER_RE = re.compile(r"^---\r?\n[\s\S]*?\r?\n---\r?\n", re.MULTILINE)  # logic-iter1-4: CRLF-tolerant
_FIRST_H2_RE = re.compile(r"^## ", re.MULTILINE)
_EXISTING_INJECT_RE = re.compile(r"^## §?0\.1\s+Inherited Lessons", re.MULTILINE)


def inject_inherited_lessons(plan_text: str, task_type: str,
                             jsonl_path: Optional[Path] = None) -> str:
    """Inject §0.1 Inherited Lessons before first `## ` heading. Respects YAML frontmatter.

    No-op cases:
      - plan already contains `## §0.1 Inherited Lessons` (or `## 0.1 Inherited Lessons`)
      - filter_by_task_type yields zero patterns
    """
    if _EXISTING_INJECT_RE.search(plan_text):
        return plan_text
    patterns = filter_by_task_type(load_patterns(jsonl_path), task_type)
    if not patterns:
        return plan_text
    section = format_inherited_lessons_section(patterns) + "\n"

    # Per logic-iter1-3 fix: respect YAML frontmatter (insert AFTER closing ---)
    fm_m = _FRONTMATTER_RE.match(plan_text)
    if fm_m:
        head = plan_text[:fm_m.end()]
        rest = plan_text[fm_m.end():]
    else:
        head, rest = "", plan_text
    h2_m = _FIRST_H2_RE.search(rest)
    if h2_m:
        return head + rest[:h2_m.start()] + section + rest[h2_m.start():]
    return head + section + rest


def emit_injected(task_type: str, patterns_count: int, byte_size: int,
                  task_id: Optional[str] = None) -> bool:
    payload = {"task_type": task_type, "patterns_count": patterns_count, "byte_size": byte_size}
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("improvement_pattern_injected", payload)
        return True
    except Exception:
        if task_id:
            try:
                from src.core.task_dir_layout import append_trace
                return append_trace(task_id, "improvement_pattern_injected", payload)
            except Exception:
                pass
        return False


def _cli(argv: List[str]) -> int:
    if len(argv) < 2:
        print("Usage: improvement_pattern_injector {inject|list} [args]", file=sys.stderr)
        return 64
    cmd = argv[1]
    if cmd == "inject":
        if len(argv) < 3:
            print("Usage: improvement_pattern_injector inject <plan_path> [--task-type T]",
                  file=sys.stderr)
            return 64
        plan_path = Path(argv[2])
        task_type = "DEVELOP"
        if "--task-type" in argv:
            i = argv.index("--task-type")
            if i + 1 < len(argv):
                task_type = argv[i + 1]
        if not plan_path.is_file():
            print(f"plan not found: {plan_path}", file=sys.stderr)
            return 2
        text = plan_path.read_text(encoding="utf-8")
        new_text = inject_inherited_lessons(text, task_type)
        if new_text != text:
            plan_path.write_text(new_text, encoding="utf-8")
            print(f"injected; new size {len(new_text)} bytes")
            count = len(filter_by_task_type(load_patterns(), task_type))
            emit_injected(task_type, count, len(new_text) - len(text))
            return 0
        print("no-op (already injected or no matching patterns)")
        return 0
    if cmd == "list":
        task_type = "DEVELOP"
        if "--task-type" in argv:
            i = argv.index("--task-type")
            if i + 1 < len(argv):
                task_type = argv[i + 1]
        patterns = filter_by_task_type(load_patterns(), task_type)
        for p in patterns:
            print(f"{p.get('id', '<no-id>')}: {(p.get('lesson') or p.get('issue', ''))[:80]}")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
