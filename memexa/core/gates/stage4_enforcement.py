"""stage4_enforcement — pure predicate for Stage 4 review enforcement (TU-5).

Extracted as a testable predicate per plan_v3 B4 so session_gate can ask
"should I block this commit because Stage 4 review wasn't run?" without
the caller having to know how to spawn reviewer agents.

Block when ALL of:
  - autopilot flag is active
  - complexity == 'complex'
  - no `review_findings/*.json` files exist in task directory

Otherwise allow. Pure function — no subprocess, no network, no
side-effects (except reading task dir).

Uniform `check(task_id) -> (allow: bool, reason: str)` surface for
gate_runner compatibility; internal `should_block_stage4` is the
testable predicate.

Extension (TU-2, AC-B2): if plan_verify_cmds and evidence_entries are
provided, each findings file is also validated via validate_reviewer_output.
Backward-compat: when plan_verify_cmds=None, behaves as v0 (count-based only).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def should_block_stage4(
    task_id: str,
    autopilot_active: bool,
    complexity: str,
    findings_dir: Optional[Path] = None,
    plan_verify_cmds: Optional[List[str]] = None,
    evidence_entries: Optional[List[Dict]] = None,
) -> Tuple[bool, str]:
    """Pure predicate. Return (block, reason).

    block=True  → commit should be BLOCKED (no findings found or schema fails)
    block=False → commit allowed (either not applicable or findings present)

    Test shape:
      should_block_stage4("t", autopilot_active=True,
                          complexity="complex", findings_dir=<empty dir>)
      → (True, "...")
      # add a stub findings.json →
      → (False, "")

    Extension (AC-B2):
      When plan_verify_cmds and evidence_entries are both provided, each
      findings file is also validated via validate_reviewer_output.
      Backward-compat: when plan_verify_cmds is None, only count-based
      check runs (existing behaviour unchanged).
    """
    if not autopilot_active:
        return (False, "autopilot not active — Stage 4 optional")
    if complexity != "complex":
        return (False, f"complexity={complexity} — Stage 4 optional")
    if findings_dir is None:
        return (False, "findings_dir not provided")
    if not findings_dir.is_dir():
        return (
            True,
            f"Stage 4 enforcement BLOCK: review_findings dir missing "
            f"({findings_dir}); spawn security/logic/coverage reviewers first",
        )

    findings_files = list(findings_dir.glob("*.json"))
    if not findings_files:
        return (
            True,
            f"Stage 4 enforcement BLOCK: no review_findings/*.json in "
            f"{findings_dir}; spawn reviewer agents first",
        )

    # Additionally: each findings file must contain a non-empty list OR
    # a dict with non-empty `findings` key (LOG-1 fix: dict {"reviewer":
    # "..."} alone is NOT valid review, it's just metadata).
    real = 0
    for f in findings_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            # allow-silent: malformed findings file; skip but don't count
            continue
        if isinstance(data, list) and len(data) > 0:
            real += 1
        elif isinstance(data, dict):
            inner = data.get("findings")
            if isinstance(inner, list) and len(inner) > 0:
                real += 1
            # else: dict without real findings key → don't count as real
    if real == 0:
        return (
            True,
            f"Stage 4 enforcement BLOCK: {len(findings_files)} findings "
            f"files found but all empty (vacuous-pass shape); spawn real reviewers",
        )

    # --- AC-B2 extension: schema validation via validate_reviewer_output ---
    # Only active when plan_verify_cmds AND evidence_entries are both provided.
    if plan_verify_cmds is not None and evidence_entries is not None:
        try:
            from memexa.core.reviewer_schema import validate_reviewer_output
        except ImportError:
            # reviewer_schema not available — fail-open to preserve backward-compat
            pass
        else:
            schema_failures: List[str] = []
            for findings_file in findings_files:
                try:
                    ok, reasons = validate_reviewer_output(
                        findings_file, plan_verify_cmds, evidence_entries
                    )
                except Exception as exc:
                    # Catch-all: treat any unexpected error as a schema failure.
                    schema_failures.append(
                        f"{findings_file.name}: unexpected_error:{type(exc).__name__}"
                    )
                    continue
                if not ok:
                    for reason in reasons:
                        schema_failures.append(f"{findings_file.name}: {reason}")

            if schema_failures:
                reasons_str = "; ".join(schema_failures[:5])
                return (
                    True,
                    f"Stage 4 enforcement BLOCK: reviewer schema validation failed "
                    f"({len(schema_failures)} issue(s)): {reasons_str}",
                )

    return (False, f"Stage 4 OK: {real}/{len(findings_files)} findings files valid")


def _resolve_findings_dir(task_id: str) -> Optional[Path]:
    try:
        from memexa.core import task_dir_layout
        td = task_dir_layout.task_dir(task_id)
    except Exception:
        return None
    if not td or not td.exists():
        return None
    return td / "review_findings"


def check(task_id: str) -> Tuple[bool, str]:
    """Uniform gate entry — reads autopilot flag + complexity + dir state."""
    try:
        from memexa.core._autopilot_flag import autopilot_active
        auto = autopilot_active()
    except Exception:
        auto = False

    complexity = "simple"
    # allow-silent: fail-soft observability path
    try:
        # Read data/task_spec.json if present
        spec = Path(__file__).resolve().parent.parent.parent.parent.parent / "memexa" / "memexa" / "data" / "task_spec.json"
        if spec.exists():
            d = json.loads(spec.read_text(encoding="utf-8"))
            complexity = str(d.get("complexity", "simple"))
    # allow-silent: observability fail-soft
    except Exception:
        pass

    findings_dir = _resolve_findings_dir(task_id)
    block, reason = should_block_stage4(
        task_id, autopilot_active=auto,
        complexity=complexity, findings_dir=findings_dir,
    )
    # Uniform contract: return allow (the inverse of block)
    return (not block, reason)


if __name__ == "__main__":
    import sys
    tid = sys.argv[1] if len(sys.argv) > 1 else ""
    ok, msg = check(tid)
    print(json.dumps({"allow": ok, "reason": msg}, ensure_ascii=False))
    sys.exit(0 if ok else 1)
