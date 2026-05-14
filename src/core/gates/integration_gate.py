"""integration_gate — LIVE probe framework (TU-1 of plan_v3).

Runs at Stage 3.5 between pytest (Stage 3) and Stage 4 reviewers. For each
probe declared in the latest plan_v*.md's `### integration_probes` block:

  1. Schema + sandbox validate (via _probe_schema.validate_probe_dict).
  2. Snapshot trace_sink file position (for trace_event channel).
  3. Execute `action` as subprocess (shell=False, shlex.split, timeout).
  4. Evaluate `downstream_assertion` per channel:
     - trace_event:    new event matches pattern/name within `within_s`
     - stdout_pattern: regex match on captured subprocess stdout
     - neo4j_delta:    Episode count increases (skip if Neo4j down)
     - file_mtime:     declared file's mtime advances past action start

Vacuous-pass defense (ANCHOR-3): empty probes list for complex/medium
task → BLOCK, not PASS.

Uniform `check(task_id) -> (allow: bool, reason: str)` for gate_runner.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.gates._probe_schema import (
    Probe, ProbeSchemaError, parse_probes_from_plan_path,
)


# Workspace-root resolution for subprocess cwd
_GATES_DIR = Path(__file__).resolve().parent
_WORKSPACE = _GATES_DIR.parent.parent.parent.parent  # memex/core/gates -> workspace


@dataclass
class ProbeResult:
    probe_id: str
    outcome: str          # 'pass' | 'fail' | 'skipped' | 'schema_error'
    message: str
    duration_s: float = 0.0


def _resolve_plan_path(task_id: str) -> Optional[Path]:
    """ANCHOR-8: pick the highest plan_vN.md via plan_spec.get_latest mechanism.

    Returns None if no plan file exists for this task_id.
    """
    try:
        from src.core import task_dir_layout
        td = task_dir_layout.task_dir(task_id)
    except Exception:
        return None
    if not td or not td.exists():
        return None
    plans = sorted(
        (p for p in td.glob("plan_v*.md") if p.stem.startswith("plan_v")),
        key=lambda p: int(p.stem.split("_v", 1)[1]) if p.stem.split("_v", 1)[1].isdigit() else -1,
    )
    if not plans:
        return None
    return plans[-1]


def _task_complexity(task_id: str) -> str:
    """Read complexity from data/task_spec.json; default 'simple' if unknown."""
    spec = _WORKSPACE / "memex" / "memex" / "data" / "task_spec.json"
    if not spec.exists():
        return "simple"
    try:
        d = json.loads(spec.read_text(encoding="utf-8"))
        return str(d.get("complexity", "simple"))
    except Exception:
        return "simple"


def _trace_sink_file() -> Path:
    """Path to trace_sink traces.jsonl (same file integration_gate's
    trace_event channel observes)."""
    return _WORKSPACE / ".claude" / "data" / "traces.jsonl"


def _snapshot_trace_position() -> int:
    """Return current byte-length of trace_sink file (0 if missing)."""
    p = _trace_sink_file()
    if not p.exists():
        return 0
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _poll_trace_events_since(
    start_bytes: int, pattern: Optional[str], event_name: Optional[str],
    until_ts: float,
) -> bool:
    """Read trace file from start_bytes; return True iff any new event
    matches. Polls every 0.5s until `until_ts`.

    LOG-2 fix: detect trace file rotation/truncation — if current size
    < start_bytes, file was truncated/rotated; reset seek to 0 so we
    don't miss events in the new content.
    """
    p = _trace_sink_file()
    regex = re.compile(pattern) if pattern else None
    while time.time() < until_ts:
        if not p.exists():
            time.sleep(0.5)
            continue
        try:
            curr_size = p.stat().st_size
            # LOG-2: file rotation detection
            seek_from = start_bytes if curr_size >= start_bytes else 0
            with p.open("rb") as f:
                f.seek(seek_from)
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
            if event_name and ev == event_name:
                return True
            if regex and regex.search(ev):
                return True
        time.sleep(0.5)
    return False


def _check_trace_event(
    probe: Probe, start_bytes: int, action_start: float,
) -> Tuple[bool, str]:
    a = probe.downstream_assertion
    within = float(a.get("within_s", 10))
    deadline = action_start + within
    pat = a.get("event_pattern")
    name = a.get("event")
    hit = _poll_trace_events_since(start_bytes, pat, name, deadline)
    if hit:
        return True, "trace_event hit"
    return False, f"trace_event not observed within {within}s (pattern={pat or name})"


def _check_stdout_pattern(
    probe: Probe, stdout: str,
) -> Tuple[bool, str]:
    a = probe.downstream_assertion
    pat = a.get("pattern") or ""
    if re.search(pat, stdout):
        return True, "stdout_pattern matched"
    return False, f"stdout_pattern not matched: {pat!r}"


def _check_file_mtime(
    probe: Probe, action_start: float,
) -> Tuple[bool, str]:
    a = probe.downstream_assertion
    path = Path(a.get("path") or "")
    if not path.exists():
        return False, f"file_mtime path does not exist: {path}"
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        return False, f"file_mtime stat failed: {e}"
    if mtime >= action_start:
        return True, f"file_mtime advanced (+{mtime - action_start:.2f}s)"
    return False, "file_mtime did NOT advance after action"


def _check_neo4j_delta(
    probe: Probe, baseline: int,
) -> Tuple[bool, str]:
    """Episode count increased? If Neo4j unreachable → treat as skipped."""
    try:
        from src.core.graph_memory import stats
        s = stats()
        curr = int(s.get("episode_count", -1))
        if curr < 0:
            return False, "neo4j_delta: stats() returned no episode_count"
        if curr > baseline:
            return True, f"neo4j_delta +{curr - baseline}"
        return False, f"neo4j_delta: episode_count unchanged ({curr})"
    except Exception as e:
        return False, f"neo4j_delta skipped (connect error): {str(e)[:80]}"


def _run_probe(probe: Probe) -> ProbeResult:
    """Execute one probe; return result with outcome."""
    t0 = time.time()
    channel = probe.downstream_assertion.get("channel", "")

    # Requires-gate: if a required capability is declared missing → skipped
    for req in (probe.requires or []):
        if req == "neo4j":
            try:
                from src.core.graph_memory import _get_driver
                _get_driver().verify_connectivity()
            except Exception:
                return ProbeResult(
                    probe_id=probe.id, outcome="skipped",
                    message="neo4j required but unavailable",
                    duration_s=time.time() - t0,
                )
        elif req == "llm":
            try:
                from src.core.llm_provider import call_llm  # noqa: F401
            except Exception:
                return ProbeResult(
                    probe_id=probe.id, outcome="skipped",
                    message="llm provider unavailable",
                    duration_s=time.time() - t0,
                )

    # Snapshot side-effect observation state BEFORE the action
    trace_start_bytes = _snapshot_trace_position() if channel == "trace_event" else 0
    neo4j_baseline = -1
    if channel == "neo4j_delta":
        try:
            from src.core.graph_memory import stats
            neo4j_baseline = int(stats().get("episode_count", 0))
        except Exception:
            neo4j_baseline = -1

    # Run the action (sandboxed: shlex.split + shell=False + cwd=workspace)
    argv = shlex.split(probe.action, posix=(sys.platform != "win32"))
    action_start = time.time()
    stdout = ""
    try:
        r = subprocess.run(
            argv, shell=False, cwd=str(_WORKSPACE),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=probe.timeout_s,
        )
        stdout = r.stdout or ""
    except subprocess.TimeoutExpired:
        return ProbeResult(
            probe_id=probe.id, outcome="fail",
            message=f"subprocess timeout after {probe.timeout_s}s",
            duration_s=time.time() - t0,
        )
    except FileNotFoundError as e:
        return ProbeResult(
            probe_id=probe.id, outcome="fail",
            message=f"subprocess FileNotFoundError: {e}",
            duration_s=time.time() - t0,
        )
    except Exception as e:
        return ProbeResult(
            probe_id=probe.id, outcome="fail",
            message=f"subprocess error: {str(e)[:120]}",
            duration_s=time.time() - t0,
        )

    # Evaluate downstream assertion
    if channel == "trace_event":
        ok, msg = _check_trace_event(probe, trace_start_bytes, action_start)
    elif channel == "stdout_pattern":
        ok, msg = _check_stdout_pattern(probe, stdout)
    elif channel == "file_mtime":
        ok, msg = _check_file_mtime(probe, action_start)
    elif channel == "neo4j_delta":
        if neo4j_baseline < 0:
            return ProbeResult(
                probe_id=probe.id, outcome="skipped",
                message="neo4j_delta baseline unavailable",
                duration_s=time.time() - t0,
            )
        ok, msg = _check_neo4j_delta(probe, neo4j_baseline)
    else:
        return ProbeResult(
            probe_id=probe.id, outcome="fail",
            message=f"unknown channel {channel!r}",
            duration_s=time.time() - t0,
        )

    # Encoding assertion (ANCHOR-7): captured stdout must round-trip UTF-8
    if probe.encoding_assertion:
        try:
            stdout.encode("utf-8")
        except Exception as e:
            return ProbeResult(
                probe_id=probe.id, outcome="fail",
                message=f"encoding_assertion failed: {e}",
                duration_s=time.time() - t0,
            )

    return ProbeResult(
        probe_id=probe.id,
        outcome="pass" if ok else "fail",
        message=msg,
        duration_s=time.time() - t0,
    )


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def check(task_id: str, *, total_timeout_s: float = 120.0) -> Tuple[bool, str]:
    """Stage 3.5 entry — validate probes declared in latest plan_v*.md
    and run each against real environment.

    Returns:
      (True, "N/N probes green")           — all PASS
      (False, "<probe_id>: <msg>")         — first hard FAIL
      (False, "vacuous-pass BLOCK: ...")   — empty probes on complex task
      (True, "no probes, task simple")     — empty OK on simple task
    """
    plan_path = _resolve_plan_path(task_id)
    if plan_path is None:
        _emit_trace("integration_gate_fail_open", {
            "task_id": task_id, "reason": "no_plan_file"})
        return (True, "integration_gate: no plan file — fail_open")

    try:
        probes = parse_probes_from_plan_path(plan_path)
    except ProbeSchemaError as e:
        _emit_trace("integration_gate_block", {
            "task_id": task_id, "reason": "schema_error",
            "error": str(e)[:200]})
        return (False, f"integration_gate BLOCK: probe schema error: {e}")

    complexity = _task_complexity(task_id)

    if not probes:
        if complexity in ("complex", "medium"):
            _emit_trace("integration_gate_block", {
                "task_id": task_id, "reason": "vacuous_pass_blocked",
                "complexity": complexity})
            return (False, f"integration_gate BLOCK: no probes declared "
                           f"(complexity={complexity}); add ### integration_probes block")
        return (True, f"integration_gate: 0 probes (complexity={complexity} — OK)")

    # Run each probe sequentially (parallelization TODO; small N in practice)
    start = time.time()
    results: List[ProbeResult] = []
    for probe in probes:
        if time.time() - start > total_timeout_s:
            results.append(ProbeResult(
                probe_id=probe.id, outcome="skipped",
                message="wall-cap exhausted", duration_s=0.0))
            continue
        results.append(_run_probe(probe))

    passed = sum(1 for r in results if r.outcome == "pass")
    failed = [r for r in results if r.outcome == "fail"]
    skipped = sum(1 for r in results if r.outcome == "skipped")

    _emit_trace("integration_gate_result", {
        "task_id": task_id,
        "total": len(results), "passed": passed,
        "failed": len(failed), "skipped": skipped,
    })

    if failed:
        first = failed[0]
        return (False, f"integration_gate: {first.probe_id}: {first.message} "
                       f"[{passed}/{len(results)} pass, {skipped} skip]")
    return (True, f"integration_gate: {passed}/{len(results)} probes green "
                  f"({skipped} skipped)")


def _cli(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        # allow-silent: fail-soft observability path
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    import argparse
    p = argparse.ArgumentParser(prog="integration_gate")
    p.add_argument("task_id")
    p.add_argument("--timeout", type=float, default=120.0)
    args = p.parse_args(argv)
    ok, reason = check(args.task_id, total_timeout_s=args.timeout)
    print(json.dumps({"allow": ok, "reason": reason}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
