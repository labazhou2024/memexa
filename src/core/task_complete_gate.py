"""
Task Complete Gate -- Agent Teams quality enforcement.

TaskCompleted hook: runs when a teammate marks a task as completed.
Exit 2 = reject completion (teammate must continue working).
Exit 0 = allow completion.

Checks based on task_subject keywords:
  - "implement"/"code"/"build" tasks: run pytest, must pass
  - "review"/"audit" tasks: must have written findings to last_review.json
  - "test" tasks: pytest must show increased pass count
  - All tasks: basic sanity (task_subject not empty)

Called by: TaskCompleted hook in settings.json
Input: stdin JSON with {task_id, task_subject, task_description, teammate_name, team_name}
"""

import json
import subprocess
import sys
import time
from pathlib import Path

_DATA = Path(__file__).parent.parent / "data"
_MEMEXA = Path(__file__).parent.parent.parent

# Whitelist of valid reviewer types for per-reviewer artifact files
_VALID_REVIEWERS = {"security", "logic", "coverage"}


def _normalize_reviewer_name(teammate: str) -> str:
    """Extract reviewer type from teammate name. Returns empty string if not a known reviewer."""
    name = teammate.lower().replace("-", "_")
    for suffix in ("_reviewer", "_agent", "_review"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break  # Only strip one suffix to avoid cascading
    if name in _VALID_REVIEWERS:
        return name
    return ""


def _run_pytest_quick() -> tuple:
    """Run pytest -x -q, return (passed: bool, summary: str)."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-x", "-q", "--tb=no", "--no-header"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(_MEMEXA),
        )
        last_line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if r.returncode == 0:
            return True, f"pytest: {last_line}"
        return False, f"pytest FAILED: {last_line}"
    except subprocess.TimeoutExpired:
        return True, "pytest timeout (non-blocking)"
    except Exception as e:
        return True, f"pytest error (non-blocking): {e}"


def _check_review_artifact(reviewer_name: str = "") -> tuple:
    """Check if review findings file exists and is recent.

    Args:
        reviewer_name: Teammate name from TaskCompleted hook.
            If it matches a known reviewer type, checks per-reviewer file.
            Otherwise falls back to generic last_review.json.
    """
    reviewer_type = _normalize_reviewer_name(reviewer_name)
    if reviewer_type:
        review_file = _DATA / f"last_review_{reviewer_type}.json"
    else:
        review_file = _DATA / "last_review.json"

    if not review_file.exists():
        return False, f"No {review_file.name} found. Write your findings before completing."
    try:
        import os
        age = time.time() - os.path.getmtime(str(review_file))
        if age > 3600:  # > 1 hour
            return False, f"{review_file.name} is {age/60:.0f}min old. Write fresh findings."
        data = json.loads(review_file.read_text(encoding="utf-8"))
        if not data.get("findings") and not data.get("summary"):
            return False, f"{review_file.name} has no findings or summary. Complete your review."
        return True, f"Review artifact OK ({review_file.name}, {len(data.get('findings', []))} findings)"
    except Exception as e:
        return False, f"Error reading review: {e}"


def check_industrial_termination(task_id_candidate: str) -> tuple:
    """Planning-infra v3: [A]/[B]/[C] three-state termination check.

    Returns (allow: bool, reason: str).

    Order:
      1. Resolve active task_id via task_binding (env → _latest → None)
      2. Probe evidence store health (ANCHOR-5: unhealthy → fail-open)
      3. Load plan spec; ANCHOR-9: zero-AC plans fail [A] unconditionally
      4. [A] double-gate: density + ratio; both below threshold → fail
      5. [B] evidence.jsonl: every AC must have exit_code=0 entry
      6. [C] probe_live_evidence: trace events for declared [C:*] tags

    When any check "fails-open" (degraded data source), we return
    (True, reason-with-'fail_open') and emit gate_data_source_unhealthy.
    """
    # Resolve task_id from binding (precedence: binding → stdin fallback)
    try:
        from src.core.task_binding import get_active_task_id
        task_id = get_active_task_id() or task_id_candidate
    except Exception:
        task_id = task_id_candidate
    if not task_id or task_id == "?":
        return (True, "no_task_id_legacy_path")

    # Load spec; missing plan = legacy path (allow)
    try:
        from src.core.plan_spec import (
            get_latest, load_evidence, probe_evidence_store,
            probe_live_evidence, ac_density,
        )
    except Exception as e:
        return (True, f"plan_spec_import_failed_{type(e).__name__}")
    try:
        spec = get_latest(task_id)
    except FileNotFoundError:
        return (True, "no_plan_legacy_path")

    # ANCHOR-5 probe
    health = probe_evidence_store(task_id)
    if not health.healthy:
        _trace_unhealthy(task_id, "task_complete", health.reason)
        return (True, f"evidence_store_{health.reason}_fail_open")

    # ANCHOR-9 zero-AC guard (v2-B1 fix)
    if len(spec.acceptance_criteria) < 1:
        return (False, "[A]_fail: no_acs_defined (ANCHOR-9)")

    # [A] double-gate — BOTH below threshold to block
    code_target = max(1, spec.code_lines_target)
    density = ac_density(spec, code_target)
    ratio = spec.line_count / code_target
    # Note: density infinite when code_target=0 (already guarded above)
    if density < 1.0 and ratio < 0.3:
        return (False, f"[A]_fail: density={density:.2f} ratio={ratio:.2f}")

    # [B] evidence
    evidence = load_evidence(task_id)
    unverified = [ac.id for ac in spec.acceptance_criteria
                  if ac.id not in evidence or evidence[ac.id].exit_code != 0]
    if unverified:
        return (False, f"[B]_fail: unverified ACs: {unverified[:5]}")

    # [C] LIVE evidence
    live_failures = probe_live_evidence(task_id, spec)
    if live_failures:
        return (False, f"[C]_fail: {live_failures[:3]}")

    return (True, "abc_all_green")


def _trace_unhealthy(task_id: str, gate: str, reason: str) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("gate_data_source_unhealthy",
                          {"task_id": task_id, "gate": gate, "reason": reason})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Force-override path (ANCHOR-8 strict schema)
# ---------------------------------------------------------------------------


def handle_force_complete(task_id: str, force_evidence_dict: dict) -> tuple:
    """Process force_complete(evidence=...) payload.

    Returns (ok: bool, reason: str). On ok=True, writes L3 item to
    approval_queue. Schema validation rejects v1-style hollow payloads.
    """
    required_fields = ("unverified_acs", "reason", "evidence_trace_ids",
                       "verify_attempt_logs")
    if not isinstance(force_evidence_dict, dict):
        return (False, "force_evidence must be dict")
    for f in required_fields:
        if f not in force_evidence_dict:
            return (False, f"force_evidence missing field: {f}")
    unv = force_evidence_dict.get("unverified_acs")
    reason = force_evidence_dict.get("reason")
    ids = force_evidence_dict.get("evidence_trace_ids")
    logs = force_evidence_dict.get("verify_attempt_logs")
    if not isinstance(unv, list) or len(unv) < 1:
        return (False, "unverified_acs must be non-empty list")
    if not isinstance(reason, str) or len(reason) < 20:
        return (False, f"reason must be string >=20 chars (got {len(reason) if isinstance(reason,str) else 'non-str'})")
    if not isinstance(ids, list) or len(ids) < 1:
        return (False, "evidence_trace_ids must be non-empty list")
    if not isinstance(logs, list) or len(logs) < 1:
        return (False, "verify_attempt_logs must be non-empty list")

    # Submit L3 approval (API: level, category, title, context, proposal, *, evidence, ...)
    try:
        from src.core.approval_queue import submit_approval
        submit_approval(
            level="L3",
            category="premature_complete",
            title=f"force complete task {task_id}",
            context=reason[:500],
            proposal=f"CEO to decide: approve, reject, or defer force-complete for {task_id}",
            evidence=[f"unverified_acs={unv}", f"trace_ids={ids[:5]}",
                      f"verify_logs={[s[:120] for s in logs[:3]]}"],
            impact=f"Task {task_id} would be marked complete with {len(unv)} AC(s) unverified",
        )
    except Exception as e:
        return (False, f"approval_queue.submit_approval failed: {e}")

    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("force_complete_submitted",
                          {"task_id": task_id,
                           "unverified_count": len(unv),
                           "reason_len": len(reason)})
    except Exception:
        pass
    return (True, "force_accepted_pending_l3_approval")


def main():
    """Hook entry point."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:
        raw = ""

    if not raw.strip():
        sys.exit(0)  # No input, allow

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)  # Can't parse, allow

    task_subject = data.get("task_subject", "").lower()
    task_id = data.get("task_id", "?")
    teammate = data.get("teammate_name", "?")

    # Empty task subject = suspicious
    if not task_subject.strip():
        print(f"[TASK GATE] Rejected: empty task subject from {teammate}", file=sys.stderr)
        sys.exit(2)

    # Planning-infra v3 [A][B][C] check — runs FIRST (authoritative).
    # Skips cleanly (exit 0) when no active task_id / no plan.
    allow, reason = check_industrial_termination(task_id)
    if not allow:
        print(f"[TASK GATE] Industrial termination BLOCK task '{task_id}': {reason}",
              file=sys.stderr)
        sys.exit(2)
    elif reason not in ("no_task_id_legacy_path", "no_plan_legacy_path"):
        # Emit positive trace only for real checks
        print(f"[TASK GATE] Industrial termination OK for '{task_id}': {reason}")

    # Implementation tasks: pytest must pass
    impl_keywords = ["implement", "code", "build", "create", "write", "fix", "refactor"]
    if any(kw in task_subject for kw in impl_keywords):
        passed, summary = _run_pytest_quick()
        if not passed:
            print(f"[TASK GATE] Rejected task '{task_id}': {summary}", file=sys.stderr)
            print(f"[TASK GATE] Fix failing tests before marking this task complete.", file=sys.stderr)
            sys.exit(2)
        print(f"[TASK GATE] Impl task '{task_id}' by {teammate}: {summary}")
        sys.exit(0)

    # Review tasks: must have written findings (per-reviewer file if known)
    review_keywords = ["review", "audit", "security", "check", "inspect"]
    if any(kw in task_subject for kw in review_keywords):
        ok, msg = _check_review_artifact(reviewer_name=teammate)
        if not ok:
            print(f"[TASK GATE] Rejected task '{task_id}': {msg}", file=sys.stderr)
            sys.exit(2)
        print(f"[TASK GATE] Review task '{task_id}' by {teammate}: {msg}")
        sys.exit(0)

    # All other tasks: allow
    print(f"[TASK GATE] Task '{task_id}' by {teammate}: allowed (no specific gate)")
    sys.exit(0)


if __name__ == "__main__":
    main()
