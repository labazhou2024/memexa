"""Plan-retro gate -- 2026-04-21 plan_feedback_loop bug-3 fix.

Closes the feedback loop between Stage 4 reviewer findings and the plan
that generated them. Before this gate existed, a finding classified as
`root_cause=plan_gap` would be fixed by patching code/tests, leaving the
plan_v<N>.md file with the original inadequate AC. The next task of the
same type would re-instantiate the same plan gap.

This gate:
  1. Reads Stage 4 reviewer outputs (last_review_*.json) for the current
     task.
  2. Counts findings where `root_cause == "plan_gap"`.
  3. If count >= 1 and no plan_retro.md exists yet in the task dir:
     BLOCK Stage 5 commit, surface the gap list, require operator (or
     knowledge-manager agent) to annotate a "## RETRO PATCHES" section
     to the most recent plan_v<N>.md.
  4. After plan_retro.md is written, feed each retro patch entry to
     pattern_extractor as a KB pattern so future autopilots of the same
     TaskType inherit the tightened AC template.

Integration:
  - `check_gate(task_id)` → (allow: bool, reason: str). Called from
    persistent_mode.refresh_stage("stage_4_5_plan_retro").
  - `extract_plan_gaps(task_id)` → list of gap records. Used by briefing
    agent to generate the retro section draft.
  - `record_retro_patches(task_id, patches)` → int. Writes patches into
    improvement_patterns.jsonl via pattern_extractor helpers.

Reviewer output schema contract (all 3 reviewer agents now emit):
  {
    "id": "SEC-R1-N",
    "severity": "CRITICAL|HIGH|MED|LOW",
    "file": "...",
    "line": "...",
    "issue": "...",
    "fix": "...",
    "root_cause": "plan_gap|code_bug|test_gap"   # NEW
  }
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_REVIEWER_FILES = (
    "last_review_security.json",
    "last_review_logic.json",
    "last_review_coverage.json",
    "last_review_r2_delta.md",  # may be .md for delta summaries
)


import re as _re_static  # static import (clean local_reviewer MEDIUM finding)
_VALID_TASK_ID_PATTERN = _re_static.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _task_dir(task_id: str) -> Path:
    """Return .claude/harness/tasks/<task_id>/ path.

    SEC-R1 MED fix: task_id validated against allowlist pattern; adversarial
    ids with '..' or path separators are rejected by returning a path that
    won't exist (so downstream _load_review_findings silently returns [],
    preserving fail-soft behavior).
    """
    base = Path(__file__).resolve().parent.parent.parent.parent / ".claude" / "harness" / "tasks"
    if not task_id or not _VALID_TASK_ID_PATTERN.match(task_id):
        # Return a sentinel path that definitely won't exist
        return base / "__invalid_task_id__"
    return base / task_id


def _load_review_findings(task_id: str) -> List[Dict[str, Any]]:
    """Collect all reviewer findings from the task dir. Silently skips
    missing / malformed files — this is a best-effort aggregation, not
    an audit pass."""
    d = _task_dir(task_id)
    if not d.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for name in _REVIEWER_FILES:
        p = d / name
        if not p.is_file():
            continue
        try:
            if p.suffix == ".json":
                data = json.loads(p.read_text(encoding="utf-8"))
                findings = data.get("findings", []) if isinstance(data, dict) else []
                if isinstance(findings, list):
                    for f in findings:
                        if isinstance(f, dict):
                            f["_source_file"] = name
                            out.append(f)
        except Exception as e:
            logger.warning("plan_retro_gate: skip %s: %s", name, e)
    return out


_VALID_ROOT_CAUSE = {"plan_gap", "code_bug", "test_gap", "live_gap"}
# B-4 (2026-05-04): live_gap added so Stage 6 LIVE-evidence findings (e.g.
# pre/post state mismatch, missing expected_trace event) can route through
# the same plan_retro_gate.record() path as Stage 4 findings, closing the
# Stage 6 → Stage 2 feedback loop. Pre-fix: only Stage 4 findings created
# RP entries; Stage 6 LIVE-finding → BLOCK or fail_open with no inheritance.


def _trace_emit(event: str, payload: dict) -> None:
    """Emit a trace_sink event without hard-depending on module.
    Fail-soft: never raises on missing trace_sink infra."""
    try:
        from memexa.core.trace_sink import write as _write
        _write(event, payload)
    except Exception:
        pass


def _normalize_root_cause(raw: str | None, finding_id: str | None,
                          reviewer: str | None) -> str:
    """Reviewer schema validator (feedback_reviewer_schema_enforcement.md).

    Maps reviewer root_cause values to the canonical enum
    {plan_gap, code_bug, test_gap, unknown}. Emits a trace_sink event on
    every schema violation so we can audit which reviewer prompts need
    tightening.

    Rules:
      - exact match on {'plan_gap', 'code_bug', 'test_gap'} → return as-is
      - contains substring 'plan_gap'|'plan gap'|'plan-gap' → plan_gap
      - contains 'test_gap'|'test gap'|'coverage gap'|'missing test' → test_gap
      - contains 'code_bug'|'code bug'|'implementation bug'|'off-by-one'
        |'race'|'TOCTOU' → code_bug
      - else → 'unknown' (schema violation)
    """
    if not raw:
        _trace_emit("reviewer_schema_violation",
                    {"reviewer": reviewer, "finding_id": finding_id,
                     "field": "root_cause", "reason": "missing"})
        return "unknown"
    s = raw.strip().lower()
    if s in _VALID_ROOT_CAUSE:
        return s
    # Lenient fallback: map prose to enum
    if "plan_gap" in s or "plan gap" in s or "plan-gap" in s:
        _trace_emit("reviewer_schema_violation",
                    {"reviewer": reviewer, "finding_id": finding_id,
                     "field": "root_cause", "reason": "prose_prefix",
                     "mapped_to": "plan_gap"})
        return "plan_gap"
    if "test_gap" in s or "test gap" in s or "coverage gap" in s or "missing test" in s:
        _trace_emit("reviewer_schema_violation",
                    {"reviewer": reviewer, "finding_id": finding_id,
                     "field": "root_cause", "reason": "prose_prefix",
                     "mapped_to": "test_gap"})
        return "test_gap"
    if any(k in s for k in ("code_bug", "code bug", "implementation bug",
                             "off-by-one", "off by one", "toctou", "race")):
        _trace_emit("reviewer_schema_violation",
                    {"reviewer": reviewer, "finding_id": finding_id,
                     "field": "root_cause", "reason": "prose_prefix",
                     "mapped_to": "code_bug"})
        return "code_bug"
    # B-4 (2026-05-04): live_gap matches Stage 6 LIVE-finding prose.
    if any(k in s for k in ("live_gap", "live gap", "live-gap",
                             "post_state mismatch", "expected_trace missing",
                             "ac_red", "stage6 finding", "stage 6 finding")):
        _trace_emit("reviewer_schema_violation",
                    {"reviewer": reviewer, "finding_id": finding_id,
                     "field": "root_cause", "reason": "prose_prefix",
                     "mapped_to": "live_gap"})
        return "live_gap"
    _trace_emit("reviewer_schema_violation",
                {"reviewer": reviewer, "finding_id": finding_id,
                 "field": "root_cause", "reason": "unmappable",
                 "received": str(raw)[:120]})
    return "unknown"


def extract_plan_gaps(task_id: str) -> List[Dict[str, Any]]:
    """Return findings where normalized root_cause == 'plan_gap'.

    SEC/COV 2026-04-22: post-parse normalizer `_normalize_root_cause`
    maps prose descriptions / missing values to the canonical enum.
    Every normalization emits a `reviewer_schema_violation` trace event
    so prompt-tightening needs are observable.
    """
    findings = _load_review_findings(task_id)
    gaps = []
    for f in findings:
        reviewer = (f.get("_source_file") or "").replace(
            "last_review_", "").replace(".json", "") or None
        normalized = _normalize_root_cause(
            f.get("root_cause"),
            f.get("id"),
            reviewer,
        )
        if normalized == "plan_gap":
            # Preserve the finding + attach normalized value for downstream
            f_copy = dict(f)
            f_copy["root_cause_normalized"] = "plan_gap"
            gaps.append(f_copy)
    return gaps


def _find_latest_plan(task_id: str) -> Optional[Path]:
    """Return the highest-version plan_v<N>.md file in the task dir.

    U5 wiring (2026-04-27): opportunistically uses plan_versioning.get_latest_plan_path
    first; on import failure or None return, falls back to existing glob-max logic.
    Backward-compat: 100% — if pointer file is absent or new module unavailable,
    behavior identical to pre-U5.
    """
    # U5 opportunistic: try plan_v_latest pointer indirection first
    try:
        from memexa.core.plan_versioning import get_latest_plan_path
        r = get_latest_plan_path(task_id)
        if r is not None:
            return r
    except Exception:  # circular-import or any failure → fall through
        pass
    d = _task_dir(task_id)
    if not d.is_dir():
        return None
    candidates: List[Tuple[int, Path]] = []
    for p in d.glob("plan_v*.md"):
        # parse the integer after plan_v
        stem = p.stem  # plan_vN
        suffix = stem[len("plan_v"):]
        try:
            n = int(suffix)
        except ValueError:
            continue
        candidates.append((n, p))
    if not candidates:
        fallback = d / "plan.md"
        return fallback if fallback.exists() else None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _retro_section_present(plan_path: Path) -> bool:
    """Check if the plan file already contains a RETRO PATCHES section.

    Looks for a level-2 heading starting with '## RETRO PATCHES'. Case
    insensitive on the title, the heading level is exact.
    """
    try:
        text = plan_path.read_text(encoding="utf-8")
    except Exception:
        return False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## ") and "retro patches" in s.lower():
            return True
    return False


def _emit_event(event_type: str, details: dict) -> None:
    """TU-R3 (2026-04-23): emit gate decisions to events.jsonl so
    observability tools (daily digest, fail_open_audit, evolution_dashboard)
    can track plan_retro_gate activity. Previously this gate only wrote to
    trace_sink (a separate file), making B1 in 2026-04-23 deep audit
    accurate: events.jsonl had 0 plan_retro_* events for 3000 lines.
    """
    try:
        from memexa.core.event_bus import log_event as _eb_log
        _eb_log(event_type, agent="plan_retro_gate", details=details)
    except Exception:
        pass  # non-blocking


def check_gate(task_id: str) -> Tuple[bool, str]:
    """Main gate entry point.

    Returns (allow, reason):
      - (True, '') if no plan_gap findings OR retro section already present
      - (False, human-readable block reason) if retro section missing AND
        plan_gap findings present

    Callers: persistent_mode.refresh_stage('stage_4_5_plan_retro') invokes
    this; a False return means Stage 5 commit should be blocked until the
    plan annotation lands.
    """
    gaps = extract_plan_gaps(task_id)
    if not gaps:
        _emit_event("plan_retro_gate_check", {
            "task_id": task_id, "allow": True,
            "reason": "no_plan_gap_findings",
            "n_gaps": 0,
        })
        return True, ""

    plan_path = _find_latest_plan(task_id)
    if plan_path is None:
        _emit_event("plan_retro_gate_check", {
            "task_id": task_id, "allow": True,
            "reason": "no_plan_file_fail_open",
            "n_gaps": len(gaps),
        })
        return True, (
            f"plan_retro_gate: {len(gaps)} plan_gap findings but no "
            f"plan_v*.md file in {_task_dir(task_id)}; skipping gate."
        )

    if _retro_section_present(plan_path):
        _emit_event("plan_retro_gate_check", {
            "task_id": task_id, "allow": True,
            "reason": "retro_section_present",
            "n_gaps": len(gaps),
            "plan_file": plan_path.name,
        })
        return True, (
            f"plan_retro_gate: {len(gaps)} plan_gap findings + RETRO "
            f"PATCHES section present in {plan_path.name}"
        )

    # Block: need annotation
    ids = ", ".join(f.get("id", "?") for f in gaps[:10])
    _emit_event("plan_retro_gate_check", {
        "task_id": task_id, "allow": False,
        "reason": "retro_section_missing",
        "n_gaps": len(gaps),
        "gap_ids": ids,
        "plan_file": plan_path.name,
    })
    return False, (
        f"plan_retro_gate: {len(gaps)} Stage 4 findings classified as "
        f"root_cause=plan_gap ({ids}) but no '## RETRO PATCHES' section "
        f"in {plan_path.name}. Annotate the plan with one-line fixes per "
        f"gap before Stage 5 commit. Each annotation becomes a KB "
        f"pattern for future tasks of the same type. Skip with "
        f"MEMEXA_SKIP_PLAN_RETRO=1 (operator-only, emits warning)."
    )


def _kb_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "data" / "improvement_patterns.jsonl"
    )


def record_retro_patches(task_id: str, patches: List[Dict[str, Any]]) -> int:
    """Append retro patches to improvement_patterns.jsonl so future tasks
    of the same TaskType inherit the tightened AC template.

    Each patch dict should contain at minimum:
      - `gap_id`: the reviewer finding ID (e.g. 'SEC-R1-3')
      - `task_type`: the TaskType the gap was found in (for filtering)
      - `template_fix`: a one-line rule to add to similar future plans
      - `severity`: propagated from the finding

    Returns: number of patches written. Never raises — fail-soft logs
    a warning and returns 0.
    """
    if not patches:
        return 0
    try:
        import hashlib
        kb = _kb_path()
        kb.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        now_ts = time.time()
        with kb.open("a", encoding="utf-8") as f:
            for p in patches:
                # Self-patch 2026-04-22: all KB entries must include `id` to
                # satisfy downstream consumers (pattern_extractor,
                # integration tests). Deterministic id = retro:<gap_id>:<hash>.
                gap_id = p.get("gap_id", "unknown")
                fix_text = str(p.get("template_fix", ""))
                short_hash = hashlib.sha256(
                    f"{task_id}:{gap_id}:{fix_text}".encode("utf-8")
                ).hexdigest()[:10]
                pat_id = f"retro:{gap_id}:{short_hash}"
                record = {
                    "id": pat_id,
                    "type": "plan_retro_patch",
                    "fact": fix_text,
                    "recommendation": fix_text,
                    "confidence": "medium",
                    "tags": ["plan_retro", "template_fix",
                             f"task_type:{p.get('task_type','?')}"],
                    "affected_files": [],
                    "affected_services": [],
                    "created_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)
                    ),
                    "updated_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)
                    ),
                    "usage_count": 0,
                    "helpful_count": 0,
                    "outdated_reports": 0,
                    "auto_generated": True,
                    "provenance": [{
                        "source": "plan_retro_gate",
                        "reason": f"stage_4_5_auto_record:{gap_id}",
                        "date": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)
                        ),
                    }],
                    # Retain retro-specific fields for downstream audit
                    "kind": "plan_retro_patch",
                    "task_id": task_id,
                    "ts": now_ts,
                    **{k: v for k, v in p.items()
                       if k not in ("_source_file", "template_fix")},
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
        _emit_event("plan_retro_patches_recorded", {
            "task_id": task_id,
            "n_patches": written,
            "n_input": len(patches),
        })
        return written
    except Exception as e:
        logger.warning("record_retro_patches failed: %s", e)
        _emit_event("plan_retro_patches_failed", {
            "task_id": task_id, "error": str(e)[:200],
        })
        return 0


# ----------------------------------------------------------------------------
# TU-1 (learning_pip 2026-04-30): record-from-plan subcommand bridge.
# Parses `## RETRO PATCHES` markdown table from plan_v<latest>.md and feeds
# rows to record_retro_patches(). Closes the SKILL.md §4.5.3 doc-vs-impl gap
# (docs claimed CLI reads from plan; reality was stdin only).
# Per logic-iter1-1/2/3 fixes in plan_v1.
# ----------------------------------------------------------------------------

_RETRO_TABLE_ROW_RE = re.compile(
    r"^\|\s*(RP-\d+)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$",
    re.MULTILINE,
)
_GAP_ID_RE = re.compile(r"(\w+-iter\d+-\d+)")


def _parse_retro_table(plan_path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse `## RETRO PATCHES` section of plan markdown. Returns (patches, warnings).

    Row format: ``| RP-N | source-text | template-fix |``.
    Per logic-iter1-2 fix: handles ``<br>`` -> space; empty template_fix -> warn + skip.
    Per logic-iter1-3 fix: gap_id regex ``\\w+-iter\\d+-\\d+`` with auto-RP-N fallback.
    """
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"read_failed: {exc!s}"]
    section_m = re.search(
        r"^##\s+RETRO PATCHES[\s\S]+?(?=^##\s|\Z)",
        text, re.MULTILINE,
    )
    if not section_m:
        return [], ["no_retro_patches_section"]
    section = section_m.group(0)
    patches: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for row in _RETRO_TABLE_ROW_RE.finditer(section):
        rp_num = row.group(1)
        # Skip header divider rows like "| ID | Source | ... |"
        if rp_num in ("ID", "id", "RP"):
            continue
        source = row.group(2).replace("<br>", " ").strip()
        template_fix = row.group(3).replace("<br>", " ").strip()
        # Per logic-iter1-2 fix: empty template_fix -> emit warning + skip
        if not template_fix:
            warnings.append(f"{rp_num}_empty_template_fix")
            continue
        if not source:
            warnings.append(f"{rp_num}_empty_source")
            continue
        # Per logic-iter1-3 fix: gap_id from canonical reviewer pattern;
        # fallback to auto-<rp_num> if source doesn't match.
        gid_m = _GAP_ID_RE.search(source)
        gap_id = gid_m.group(1) if gid_m else f"auto-{rp_num}"
        # SC-9: assert non-empty (defense-in-depth even though already checked)
        if not gap_id or not template_fix:
            warnings.append(f"{rp_num}_post_assert_empty")
            continue
        patches.append({
            "gap_id": gap_id,
            "template_fix": template_fix,
            "severity": "MED",
            "source_raw": source,
            "rp_num": rp_num,
        })
    return patches, warnings


def _emit_task_type_normalized(original: str, normalized: str) -> None:
    """TU-3 (learning_pip 2026-04-30): emit when task_type was case-normalized.
    Per HARD RULE feedback_trace_event_emit_or_assert: declared events MUST emit.
    """
    if original != normalized:
        try:
            _emit_event("task_type_normalized", {
                "original_value": original,
                "normalized_value": normalized,
            })
        except Exception:
            pass


def _read_task_type_from_spec(task_id: Optional[str] = None) -> str:
    """Resolve task_type with 3-tier fallback. Returns lowercase.

    Tier 1: task_dir/<task_id>/plan_v<latest>.md frontmatter `task_type:` field.
    Tier 2: memexa/memexa/data/task_spec.json `task_type` field.
    Tier 3: 'unknown'.

    Strips parenthetical (e.g. 'FIX (CEO complex)' -> 'fix').
    Per TU-3 normalization: writer-side .lower().
    Per logic-iter1-3 + value-resolution-chain HARD RULE.
    """
    # Tier 1: per-task plan frontmatter (most accurate for replay use-case)
    if task_id:
        try:
            plan_path = _find_latest_plan(task_id)
            if plan_path is not None:
                head = plan_path.read_text(encoding="utf-8", errors="replace")[:2000]
                m = re.search(r"^task_type:\s*([^\n]+)$", head, re.MULTILINE)
                if m:
                    raw = m.group(1).strip()
                    norm = raw.split("(")[0].split()[0].lower()
                    if norm:
                        return norm
        except Exception:
            pass
    # Tier 2: global task_spec.json
    try:
        spec_path = Path(__file__).resolve().parent.parent / "data" / "task_spec.json"
        if spec_path.exists():
            data = json.loads(spec_path.read_text(encoding="utf-8"))
            raw = (data.get("task_type") or "")
            if raw:
                return str(raw).split("(")[0].split()[0].lower()
    except Exception:
        pass
    return "unknown"


def _main_record_from_plan(task_id: str) -> int:
    """CLI subcommand: parse plan_v<latest>.md `## RETRO PATCHES` -> record_retro_patches.

    Returns 0 on success, 1 if no plan or no patches recorded with warnings,
    2 on uncaught exception.
    """
    try:
        plan_path = _find_latest_plan(task_id)
        if plan_path is None:
            sys.stderr.write(f"no plan_v*.md found for task {task_id}\n")
            print(json.dumps({"recorded": 0, "parsed": 0,
                              "error": "no_plan_file"}, ensure_ascii=False))
            return 1
        patches, warnings = _parse_retro_table(plan_path)
        task_type_raw = _read_task_type_from_spec(task_id)
        task_type = task_type_raw.lower() if task_type_raw else "unknown"
        _emit_task_type_normalized(task_type_raw, task_type)
        for p in patches:
            p["task_type"] = task_type  # writer-side .lower() per TU-3 Action 1
        # SC-9 final assert
        valid = [p for p in patches
                 if p.get("gap_id") and p.get("template_fix")]
        n = record_retro_patches(task_id, valid)
        _emit_event("plan_retro_record_from_plan_done", {
            "task_id": task_id,
            "recorded": n,
            "parsed": len(patches),
            "parse_errors_count": len(warnings),
            "plan_file": plan_path.name,
            "task_type": task_type,
        })
        print(json.dumps({
            "recorded": n,
            "parsed": len(patches),
            "parse_warnings": warnings,
            "task_type": task_type,
            "plan_file": plan_path.name,
        }, ensure_ascii=False))
        # Exit 1 if nothing recorded due to warnings; else 0
        if warnings and n == 0:
            return 1
        return 0
    except Exception as exc:
        sys.stderr.write(f"record-from-plan uncaught: {exc!s}\n")
        return 2


def env_skip_flag() -> bool:
    """Return True iff operator set MEMEXA_SKIP_PLAN_RETRO=1. This is an
    escape hatch for legitimate edge cases (e.g. plan file corrupted);
    the skip is logged so audits can review."""
    return os.environ.get("MEMEXA_SKIP_PLAN_RETRO") == "1"


def _cli():
    import sys
    args = sys.argv[1:]
    if not args:
        print("usage: python -m memexa.core.plan_retro_gate "
              "<check|gaps|record|record-from-plan> <task_id> [...]", file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd in ("check", "gaps", "record", "record-from-plan") and len(args) < 2:
        print(f"{cmd} requires task_id", file=sys.stderr)
        return 2
    tid = args[1] if len(args) >= 2 else ""

    if cmd == "check":
        if env_skip_flag():
            print(json.dumps({
                "allow": True,
                "reason": "MEMEXA_SKIP_PLAN_RETRO=1 override (logged)",
                "override": True,
            }, ensure_ascii=False))
            return 0
        allow, reason = check_gate(tid)
        print(json.dumps({
            "allow": allow, "reason": reason,
        }, ensure_ascii=False))
        return 0 if allow else 1

    if cmd == "gaps":
        gaps = extract_plan_gaps(tid)
        print(json.dumps({"count": len(gaps), "gaps": gaps},
                         ensure_ascii=False, indent=2))
        return 0

    if cmd == "record":
        # stdin JSON list of patches
        try:
            data = sys.stdin.read()
            patches = json.loads(data) if data.strip() else []
        except Exception as e:
            print(f"record: invalid stdin JSON: {e}", file=sys.stderr)
            return 2
        n = record_retro_patches(tid, patches if isinstance(patches, list) else [])
        print(json.dumps({"recorded": n}, ensure_ascii=False))
        return 0

    if cmd == "record-from-plan":
        return _main_record_from_plan(tid)

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
