"""
plan_spec.py — Plan-as-artifact lifecycle for industrial autopilot (2026-04-21).

Structures plan.md files into PlanSpec (AxisAnchor, AcceptanceCriterion, ...)
and tracks revisions via append-only revisions.jsonl. Reuses
task_dir_layout for storage and _atomic_state for safe RMW.

Also hosts the evidence-side helpers consumed by session_gate rule-8
and task_complete_gate.check_industrial_termination:
  - probe_evidence_store(task_id) -> StoreHealth
  - load_evidence(task_id) -> Dict[ac_id, EvidenceEntry]
  - probe_live_evidence(task_id, spec) -> List[str]
  - ac_density, depth_ratio

NEVER raises from gate-facing helpers when data source is degraded —
returns (True, reason) or fail-open equivalent, emitting
`gate_data_source_unhealthy` trace events so CEO briefing surfaces
them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

try:
    from filelock import FileLock, Timeout as FilelockTimeout
except ImportError:
    FileLock = None  # type: ignore
    FilelockTimeout = Exception  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

Mutability = Literal["immutable", "append_only", "mutable"]


@dataclass
class AxisAnchor:
    id: str
    text: str
    mutability: Mutability = "immutable"


@dataclass
class AcceptanceCriterion:
    id: str
    text: str
    verify_cmd: str  # required non-empty
    signal_hint: Optional[str] = None
    status: Literal["pending", "verified", "failed"] = "pending"


@dataclass
class ReviewerVerdict:
    reviewer: str  # agent role or "ceo"
    round: int
    verdict: Literal["APPROVED", "REVISE_REQUIRED", "REJECTED"]
    timestamp: str
    notes: str = ""


@dataclass
class PlanSpec:
    task_id: str
    version: int
    parent_sha: Optional[str]
    revision_reason: Optional[str]
    axis_anchors: List[AxisAnchor]
    acceptance_criteria: List[AcceptanceCriterion]
    line_count: int
    code_lines_target: int
    reviewer_verdicts: List[ReviewerVerdict] = field(default_factory=list)


@dataclass
class EvidenceEntry:
    ac_id: str
    verify_cmd: str
    stdout_sha: str
    exit_code: int
    stderr_excerpt: str
    duration_ms: int
    ts: str


@dataclass
class StoreHealth:
    healthy: bool
    reason: str  # "ok" | "file_missing" | "parse_error" | "truncated" | "lock_timeout" | "task_missing"


# ---------------------------------------------------------------------------
# Path helpers (delegate to task_dir_layout)
# ---------------------------------------------------------------------------


def _task_dir(task_id: str) -> Path:
    from src.core.task_dir_layout import task_dir as _td
    return _td(task_id)


def _plan_path(task_id: str, version: int) -> Path:
    return _task_dir(task_id) / f"plan_v{version}.md"


def _revisions_path(task_id: str) -> Path:
    return _task_dir(task_id) / "revisions.jsonl"


def _evidence_path(task_id: str) -> Path:
    return _task_dir(task_id) / "evidence.jsonl"


def _anchors_path(task_id: str) -> Path:
    return _task_dir(task_id) / "axis_anchors.json"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_ANCHOR_LINE_RE = re.compile(
    r"^\s*[-*]\s*\*?\*?ANCHOR-(\d+)\*?\*?\s*[:：]\s*(.+?)$",
    re.MULTILINE,
)
# AC block: look for lines with "**AC-X-N**" or "AC-X-N:" then
# optional verify_cmd / signal_hint blocks. Lookahead must accept the
# common markdown prefixes (whitespace, "-" bullet, "*" bullet, number
# + period) before the next AC header — otherwise bullet-prefix lists
# like `- **AC-1**: ...` collapse into a single match.
# 2026-04-24 plan_v1 AC-4 fix: replaced `\s*` with `[\s\-*0-9.]*`.
_AC_HEADER_RE = re.compile(
    r"\*\*(AC-[A-Za-z0-9_-]+)\*\*\s*(?:\(([^)]*)\))?\s*[:：]\s*"
    r"(.+?)(?=\n[\s\-*0-9.]*\*\*AC-|\Z)",
    re.DOTALL,
)
# verify_cmd block: terminate at next verify_cmd-or-signal_hint OR at an
# AC/section header (blank line + **AC-|## heading) OR at text end.
_VERIFY_CMD_RE = re.compile(
    r"verify_cmd\s*[:：]\s*(.+?)"
    r"(?=\n[\s\-*0-9.]*(?:signal_hint|verify_cmd)\s*[:：]|\n\s*\n|\Z)",
    re.DOTALL,
)
_SIGNAL_HINT_RE = re.compile(r"signal_hint\s*[:：]\s*([^\n]+)")


def _parse_plan_markdown(text: str, task_id: str, version: int) -> PlanSpec:
    """Lightweight parser. Extracts anchors + ACs.

    AC verify_cmd extraction is best-effort (markdown headings vary);
    unparseable ACs get an empty verify_cmd which parser-caller should
    reject.
    """
    anchors: List[AxisAnchor] = []
    seen_anchors: set = set()
    for m in _ANCHOR_LINE_RE.finditer(text):
        anchor_id = f"ANCHOR-{m.group(1)}"
        if anchor_id in seen_anchors:
            continue
        seen_anchors.add(anchor_id)
        raw_text = m.group(2).strip()
        # Strip markdown emphasis from anchor text
        raw_text = re.sub(r"\*\*|\*", "", raw_text).strip()
        anchors.append(AxisAnchor(id=anchor_id, text=raw_text[:500]))

    acs: List[AcceptanceCriterion] = []
    seen_acs: set = set()
    # Extract each AC block
    for m in _AC_HEADER_RE.finditer(text):
        ac_id = m.group(1).strip()
        if ac_id in seen_acs:
            continue
        seen_acs.add(ac_id)
        body = (m.group(3) or "").strip()
        vm = _VERIFY_CMD_RE.search(body)
        sm = _SIGNAL_HINT_RE.search(body)
        verify_cmd = (vm.group(1).strip() if vm else "").strip("`\n ")
        signal_hint = (sm.group(1).strip() if sm else None)
        # Remove verify_cmd/signal_hint segments from body to get clean text
        text_only = _VERIFY_CMD_RE.sub("", body)
        text_only = _SIGNAL_HINT_RE.sub("", text_only).strip()[:500]
        acs.append(AcceptanceCriterion(
            id=ac_id,
            text=text_only,
            verify_cmd=verify_cmd,
            signal_hint=signal_hint,
        ))

    line_count = text.count("\n") + 1
    # code_lines_target: heuristic — search for "LOC target" marker, else 0
    ct_m = re.search(r"(?:code[_ ]lines[_ ]target|LOC\s*target)\s*[:：=]\s*(\d+)", text)
    code_lines_target = int(ct_m.group(1)) if ct_m else 0

    return PlanSpec(
        task_id=task_id,
        version=version,
        parent_sha=None,
        revision_reason=None,
        axis_anchors=anchors,
        acceptance_criteria=acs,
        line_count=line_count,
        code_lines_target=code_lines_target,
    )


# ---------------------------------------------------------------------------
# Public API — spec load/save
# ---------------------------------------------------------------------------


def load_plan(task_id: str, version: Optional[int] = None) -> PlanSpec:
    """Load plan_v<N>.md into PlanSpec.

    version=None → latest. Raises FileNotFoundError if task dir or plan
    file absent.
    """
    d = _task_dir(task_id)
    if not d.is_dir():
        raise FileNotFoundError(f"task dir not found: {d}")
    if version is None:
        # 2026-04-27 Phase 2 cleanup: U5 introduced plan_v_latest.md pointer
        # which the prior `int(p.stem.split("_v")[1])` blind-parse choked on
        # ("latest" -> ValueError -> depth/ac_audit fail_open every commit
        # since 2f9d453). Filter to integer-suffix versions only; pointer
        # files and any other non-int suffix are silently skipped here so
        # that load_plan returns the highest legitimate plan_v<N>.md.
        # Closes action_item #5 (CEO 2026-04-27).
        # NOTE: glob("plan_v*.md") already guarantees stem.startswith("plan_v"),
        # so an extra startswith() guard would be dead code (logic-iter1-2).
        versions = []
        for p in d.glob("plan_v*.md"):
            suffix = p.stem.split("_v", 1)[1]
            try:
                versions.append(int(suffix))
            except ValueError:
                # plan_v_latest.md, plan_v_draft.md, plan_v.md (empty),
                # plan_v1e3.md (not int), etc. — not a versioned plan,
                # skip silently. Liberal accept on int-stem only.
                continue
        versions.sort()
        if not versions:
            raise FileNotFoundError(f"no plan_v*.md in {d}")
        version = versions[-1]
    path = _plan_path(task_id, version)
    if not path.exists():
        raise FileNotFoundError(str(path))
    text = path.read_text(encoding="utf-8")
    return _parse_plan_markdown(text, task_id, version)


def get_latest(task_id: str) -> PlanSpec:
    """Alias for load_plan(task_id, version=None)."""
    return load_plan(task_id, version=None)


def list_revisions(task_id: str) -> List[Dict[str, Any]]:
    """Return revision ledger entries (append-only jsonl)."""
    p = _revisions_path(task_id)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def save_revision(task_id: str, new_spec: PlanSpec, reason: str,
                  reviewer_verdict: Optional[ReviewerVerdict] = None) -> int:
    """Append revision entry to revisions.jsonl. Returns new version integer.

    Does NOT write plan_v<N>.md — caller writes markdown separately. This
    helper records metadata only (reason, verdict, sha of plan content).
    """
    p = _revisions_path(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "version": new_spec.version,
        "parent_sha": new_spec.parent_sha,
        "reason": reason,
        "timestamp": _now_iso(),
        "reviewer_verdict": asdict(reviewer_verdict) if reviewer_verdict else None,
        "line_count": new_spec.line_count,
        "ac_count": len(new_spec.acceptance_criteria),
        "anchor_count": len(new_spec.axis_anchors),
    }
    # Atomic-ish append: write under filelock if available.
    lock_obj = _file_lock(p)
    if lock_obj is not None:
        with lock_obj:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    else:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return new_spec.version


def _now_iso() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _file_lock(path: Path):
    if FileLock is None:
        return None
    try:
        return FileLock(str(path) + ".lock", timeout=5.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def ac_density(spec: PlanSpec, actual_code_lines: int) -> float:
    """ACs per 100 LOC. Returns inf when actual_code_lines == 0 (sentinel)."""
    if actual_code_lines <= 0:
        return float("inf")
    return (len(spec.acceptance_criteria) / actual_code_lines) * 100.0


def depth_ratio(spec: PlanSpec, actual_code_lines: int) -> float:
    """plan_lines / code_lines. Returns inf when actual_code_lines == 0."""
    if actual_code_lines <= 0:
        return float("inf")
    return spec.line_count / actual_code_lines


# ---------------------------------------------------------------------------
# Evidence helpers (shared with session_gate + task_complete_gate)
# ---------------------------------------------------------------------------


def probe_evidence_store(task_id: str) -> StoreHealth:
    """ANCHOR-5 compliance probe. Distinguishes "work did not run" from
    "storage is broken". Gates use the result to decide between strict
    check (healthy) and fail-open + trace event (unhealthy).
    """
    d = _task_dir(task_id)
    if not d.is_dir():
        return StoreHealth(healthy=False, reason="task_missing")

    # Acceptable: spec has no ACs → probe trivially healthy (caller
    # then hits ANCHOR-9 zero-AC guard separately if applicable).
    try:
        spec = get_latest(task_id)
    except FileNotFoundError:
        # No plan yet → evidence not expected; healthy
        return StoreHealth(healthy=True, reason="ok_no_plan")

    if len(spec.acceptance_criteria) == 0:
        return StoreHealth(healthy=True, reason="ok_zero_acs")

    p = _evidence_path(task_id)
    if not p.exists():
        # File absent but ACs exist → treat as UNHEALTHY so genuine run
        # absence does NOT silently fail to block (ANCHOR-5).
        # Gates that see file_missing must distinguish this: if probe
        # says file_missing AND the gate sees the absence as "no work
        # done", gate fails-open. The alternative — treating absence as
        # strict block — was v2-B2 (breaks on trace_sink failure).
        return StoreHealth(healthy=False, reason="file_missing")

    # Parse-probe: read + try load
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return StoreHealth(healthy=False, reason="io_error")

    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        # Empty file with ACs → still unhealthy (no evidence, can't tell
        # why). This lets ac_verifier re-run safely.
        return StoreHealth(healthy=False, reason="file_empty")

    parse_failures = 0
    for ln in lines:
        try:
            json.loads(ln)
        except Exception:
            parse_failures += 1

    if parse_failures == len(lines):
        return StoreHealth(healthy=False, reason="parse_error")
    if parse_failures > 0 and parse_failures >= len(lines) // 2:
        return StoreHealth(healthy=False, reason="truncated")

    return StoreHealth(healthy=True, reason="ok")


def load_evidence(task_id: str) -> Dict[str, EvidenceEntry]:
    """Parse evidence.jsonl → {ac_id: latest EvidenceEntry}.

    Last-win on duplicate ac_id (by ts). Missing file → empty dict.
    Corrupt lines skipped with warning. Uses filelock if available.
    """
    p = _evidence_path(task_id)
    if not p.exists():
        return {}

    out: Dict[str, EvidenceEntry] = {}

    def _process(lines: List[str]) -> None:
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                entry = EvidenceEntry(
                    ac_id=str(d["ac_id"]),
                    verify_cmd=str(d.get("verify_cmd", "")),
                    stdout_sha=str(d.get("stdout_sha", "")),
                    exit_code=int(d.get("exit_code", 1)),
                    stderr_excerpt=str(d.get("stderr_excerpt", ""))[:400],
                    duration_ms=int(d.get("duration_ms", 0)),
                    ts=str(d.get("ts", "")),
                )
            except Exception as e:
                logger.warning("load_evidence skip line %d: %s", i, e)
                continue
            prev = out.get(entry.ac_id)
            if prev is None or entry.ts > prev.ts:
                out[entry.ac_id] = entry

    lock_obj = _file_lock(p)
    try:
        if lock_obj is not None:
            with lock_obj:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        else:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except FilelockTimeout:
        logger.warning("load_evidence: lock timeout on %s", p)
        return {}
    except OSError as e:
        logger.warning("load_evidence: io error on %s: %s", p, e)
        return {}

    _process(lines)
    return out


_LIVE_TAG_RE = re.compile(r"\[C:(cli|hook|schema|agent):([^\]]+)\]")


def probe_live_evidence(task_id: str, spec: PlanSpec) -> List[str]:
    """Return list of [C]-failures: declared LIVE artifacts with no
    runtime trace evidence.

    Spec marks LIVE requirements via tags in AC text:
      [C:cli:<name>]    → expect trace event `cli_invoked` with payload.name == <name>
      [C:hook:<name>]   → expect `hook_fired` with name
      [C:schema:<field>] → expect `schema_migration` with field
      [C:agent:<role>]  → expect `agent_spawned_for_task` with role

    AC without [C:...] tag contributes nothing to live check.

    Empty return list = [C] green.
    """
    # Gather required tags
    required: List[Tuple[str, str, str]] = []  # (ac_id, kind, name)
    for ac in spec.acceptance_criteria:
        for kind, name in _LIVE_TAG_RE.findall(ac.text):
            required.append((ac.id, kind, name))

    if not required:
        return []

    # Load trace events scoped to task_id (approximation: trace_sink is
    # global; we filter by payload.task_id if present, else accept any
    # recent event that matches name).
    trace_events = _load_recent_trace_events(task_id)

    failures: List[str] = []
    for ac_id, kind, name in required:
        event_type = {
            "cli": "cli_invoked",
            "hook": "hook_fired",
            "schema": "schema_migration",
            "agent": "agent_spawned_for_task",
        }.get(kind)
        if event_type is None:
            continue
        hit = any(
            ev.get("event") == event_type and (
                ev.get("payload", {}).get("name") == name
                or ev.get("payload", {}).get("role") == name
                or ev.get("payload", {}).get("field") == name
            )
            for ev in trace_events
        )
        if not hit:
            failures.append(f"{ac_id}: {kind}:{name} no trace evidence")
    return failures


def _load_recent_trace_events(task_id: str, max_events: int = 500) -> List[Dict]:
    """Read tail of trace_sink jsonl file. Best-effort; returns [] on
    failure (fail-open caller)."""
    try:
        from src.core.trace_sink import _trace_file
        p = _trace_file()
    except Exception:
        return []
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-max_events:]
    except OSError:
        return []
    events = []
    for ln in lines:
        try:
            ev = json.loads(ln)
            # Event scoping: accept events without task_id too (global
            # events) — gates decide strictness
            events.append(ev)
        except Exception:
            continue
    return events


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv: List[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="plan_spec")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_load = sub.add_parser("load", help="load + print plan summary")
    p_load.add_argument("task_id")
    p_load.add_argument("--version", type=int, default=None)

    p_rev = sub.add_parser("list-revisions", help="list revisions.jsonl")
    p_rev.add_argument("task_id")

    p_ev = sub.add_parser("probe-evidence", help="probe evidence store health")
    p_ev.add_argument("task_id")

    p_cyc = sub.add_parser("cycle-test", help="create → revise → load round-trip")
    p_cyc.add_argument("task_dir")
    p_cyc.add_argument("--zero-acs", action="store_true")

    args = parser.parse_args(argv[1:])

    if args.cmd == "load":
        spec = load_plan(args.task_id, args.version)
        print(json.dumps({
            "task_id": spec.task_id,
            "version": spec.version,
            "line_count": spec.line_count,
            "code_lines_target": spec.code_lines_target,
            "ac_count": len(spec.acceptance_criteria),
            "anchor_count": len(spec.axis_anchors),
            "ac_density_per_100": round(ac_density(spec, spec.code_lines_target), 2) if spec.code_lines_target else None,
            "depth_ratio": round(depth_ratio(spec, spec.code_lines_target), 2) if spec.code_lines_target else None,
        }, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "list-revisions":
        for r in list_revisions(args.task_id):
            print(json.dumps(r, ensure_ascii=False))
        return 0

    if args.cmd == "probe-evidence":
        h = probe_evidence_store(args.task_id)
        print(json.dumps(asdict(h), ensure_ascii=False))
        return 0 if h.healthy else 1

    if args.cmd == "cycle-test":
        # Minimal end-to-end: create dir, write plan_v0 + revisions line, reload
        from src.core.task_dir_layout import task_dir as _td, tasks_root
        # Test-only: allow task_dir arg to be an absolute path OR task_id
        task_id = Path(args.task_dir).name if Path(args.task_dir).is_absolute() else args.task_dir
        d = _td(task_id)
        d.mkdir(parents=True, exist_ok=True)
        ac_block = "" if args.zero_acs else (
            "**AC-TEST-1**:\n"
            "verify_cmd: echo ok\n"
            "signal_hint: cycle_test\n"
        )
        plan_md = (
            "# cycle test plan v0\n\n"
            "- ANCHOR-1: test anchor\n"
            "\n"
            "LOC target: 100\n"
            f"\n{ac_block}\n"
        )
        (d / "plan_v0.md").write_text(plan_md, encoding="utf-8")
        spec = load_plan(task_id, version=0)
        save_revision(task_id, spec, reason="cycle-test initial", reviewer_verdict=None)
        # Second save to assert list_revisions grows
        save_revision(task_id, spec, reason="cycle-test second", reviewer_verdict=None)
        revs = list_revisions(task_id)
        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "ac_count": len(spec.acceptance_criteria),
            "anchor_count": len(spec.axis_anchors),
            "revisions": len(revs),
        }, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
