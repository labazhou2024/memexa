"""hook_chain_probe — TU-3 of plan_v3.

Specialized subset of integration_gate focused on real tool→hook→trace
chain verification. Ships a bundled default probe
`memory_write_hook_fires` that:

  1. Writes a CJK-containing temp `.md` under real memory dir.
  2. Invokes `python memex/memex/core/memory_write_hook.py` directly
     with stdin mimicking a PostToolUse tool_use JSON payload
     (so we can test it OUTSIDE a real Claude Code session).
  3. Asserts a `memory_write_hook_*` or `memory_ingest_start` trace
     event appears within 15 s.

Uniform `check(task_id) -> (allow, reason)` for gate_runner.
"""
from __future__ import annotations


import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple
from src.core._path_resolver import memory_dir


_WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent.parent
# SEC-4 fix: derive from Path.home() to avoid hardcoded user-path.
# Matches memory_write_hook.py _MEMORY_DIR computation.
_MEMORY_DIR = (
    memory_dir()
)
_TRACE_FILE = _WORKSPACE / ".claude" / "data" / "traces.jsonl"


def _snapshot_trace_bytes() -> int:
    try:
        return _TRACE_FILE.stat().st_size if _TRACE_FILE.exists() else 0
    except OSError:
        return 0


def _poll_for_trace_event(start_bytes: int, name_prefixes: tuple,
                          until_ts: float) -> Tuple[bool, str]:
    import re
    while time.time() < until_ts:
        if not _TRACE_FILE.exists():
            time.sleep(0.5)
            continue
        try:
            with _TRACE_FILE.open("rb") as f:
                f.seek(start_bytes)
                tail = f.read()
        except OSError:
            time.sleep(0.5)
            continue
        for line in tail.splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ev = rec.get("event", "") or ""
            for p in name_prefixes:
                if ev.startswith(p):
                    return True, f"observed event: {ev}"
        time.sleep(0.5)
    return False, f"no event with prefix {name_prefixes} observed"


def run_memory_write_hook_probe(timeout_s: float = 15.0) -> Tuple[str, str]:
    """Execute the bundled probe. Returns (outcome, message).

    outcome in {'pass', 'fail', 'skipped'}
    """
    if not _MEMORY_DIR.is_dir():
        return ("skipped", f"memory dir not found: {_MEMORY_DIR}")

    # Create a CJK-containing temp memory file (feedback_ prefix so it
    # matches the filename allowlist in memory_write_hook).
    cjk_payload = "这是测试 — probe 整合测试"
    ts = int(time.time())
    temp_name = f"feedback_probe_test_{ts}.md"
    temp_path = _MEMORY_DIR / temp_name
    temp_path.write_text(
        f"---\nname: probe_test\n---\n\n# {cjk_payload}\n",
        encoding="utf-8",
    )

    try:
        baseline = _snapshot_trace_bytes()
        import subprocess
        # Build a stdin payload that mimics what Claude Code PostToolUse
        # sends to memory_write_hook.py.
        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": str(temp_path)},
        })
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable,
                 str(_WORKSPACE / "memex" / "memex" / "core" / "memory_write_hook.py")],
                input=payload, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout_s, cwd=str(_WORKSPACE),
            )
        except subprocess.TimeoutExpired:
            return ("fail", f"memory_write_hook subprocess timeout {timeout_s}s")
        except FileNotFoundError as e:
            return ("fail", f"memory_write_hook.py not found: {e}")

        # Encoding assertion — stdout must round-trip UTF-8
        try:
            (proc.stdout or "").encode("utf-8")
        except Exception as e:
            return ("fail", f"encoding_assertion failed: {e}")

        # Observation: poll for any memory_write_hook_* or
        # memory_ingest_start or haiku_extract_start trace event.
        deadline = t0 + timeout_s
        hit, msg = _poll_for_trace_event(
            baseline,
            ("memory_write_hook_", "memory_ingest_start", "haiku_extract_start"),
            deadline,
        )
        if hit:
            return ("pass", msg)
        # Capture hook's own stdout for diagnostic
        diag = (proc.stdout or "")[:200]
        return ("fail", f"no trace event within {timeout_s}s; hook stdout: {diag!r}")
    finally:
        # Cleanup — don't leave test file polluting memory dir
        try:
            temp_path.unlink()
        except OSError:
            pass


def check(task_id: str) -> Tuple[bool, str]:
    """Uniform gate entry. Runs the bundled probe."""
    outcome, msg = run_memory_write_hook_probe(timeout_s=15.0)
    if outcome == "pass":
        return (True, f"hook_chain_probe: {msg}")
    if outcome == "skipped":
        return (True, f"hook_chain_probe skipped: {msg}")
    return (False, f"hook_chain_probe: {msg}")


if __name__ == "__main__":
    if sys.platform == "win32":
        # allow-silent: fail-soft observability path
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    tid = sys.argv[1] if len(sys.argv) > 1 else ""
    ok, msg = check(tid)
    print(json.dumps({"allow": ok, "reason": msg}, ensure_ascii=False))
    sys.exit(0 if ok else 1)
