"""TU-5 (plan_v1, 2026-04-25) — Mode-A → Mode-B reviewer fallback.

When sub-agent reviewers fail (auth_fail / stall), the autopilot skill
must fall back to main-session inline review with framework-test
attestation. Without this, Stage 4 silently degrades on sub-agent
provider 401 events (LIVE: 4 consecutive 401s in 24b759b's Stage 4).

Public surface:
  detect_failure(output, elapsed_sec) -> "auth_fail" | "stall" | None
  attest_main_session(...) -> dict (writes evidence + emits trace)

Heuristics:
  auth_fail = regex match on '401', 'authentication_error', 'invalid_api_key'
              AND output length < 2KB (real reviews exceed this).
  stall     = output length < 50 bytes AND elapsed > 120s.

False-positive guards:
  - 'returned 401 items' in a long body returns None (length check).
  - empty output with elapsed < 120s returns None (still working).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

_AUTH_FAIL_RE = re.compile(
    r'\b(?:401|authentication_error|invalid_api_key)\b',
    re.IGNORECASE,
)
_MAX_LEGIT_RESPONSE = 2048
_STALL_THRESHOLD_SEC = 120.0
_STALL_BYTES = 50


def detect_failure(output: str, elapsed_sec: float = 0.0) -> Optional[str]:
    """Classify a sub-agent's response.

    Returns:
        "auth_fail" — short response containing auth-error tokens.
        "stall"     — empty/very-short response after long elapsed time.
        None        — looks legitimate or still pending.
    """
    if output is None:
        output = ""
    n = len(output)

    # Real review responses are large; never auth_fail
    if n >= _MAX_LEGIT_RESPONSE:
        return None

    if n > 0 and _AUTH_FAIL_RE.search(output):
        return "auth_fail"

    if n < _STALL_BYTES and elapsed_sec > _STALL_THRESHOLD_SEC:
        return "stall"

    return None


def attest_main_session(
    reviewer_role: str,
    ac_id: str,
    cmd: str,
    stdout: str,
    exit_code: int,
    task_id: str,
    fallback_reason: str,
    evidence_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record main-session attestation entry + emit trace event.

    Returns the appended evidence record (for caller verification).

    Raises:
        ValueError: invalid reviewer_role or fallback_reason.
        OSError: evidence write failed.
    """
    if reviewer_role not in {"security", "logic", "coverage", "verifier", "consolidated"}:
        raise ValueError(f"unknown reviewer_role: {reviewer_role!r}")
    if fallback_reason not in {"auth_fail", "stall", "manual", "agent_type_unavailable"}:
        raise ValueError(f"unknown fallback_reason: {fallback_reason!r}")

    record = {
        "ac_id": ac_id,
        "cmd": cmd,
        "exit_code": int(exit_code),
        "stdout_sha256": _sha256(stdout),
        "task_id": task_id,
        "reviewer_role": reviewer_role,
        "verified_by": "main_session_fallback",
        "fallback_reason": fallback_reason,
        "ts": time.time(),
    }

    if evidence_path is None:
        # Default: workspace .claude/harness/tasks/<tid>/evidence.jsonl
        from src.core.task_dir_layout import task_dir
        evidence_path = task_dir(task_id) / "evidence.jsonl"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with evidence_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # Emit trace
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("reviewer_fallback_triggered", {
            "reviewer_role": reviewer_role,
            "ac_id": ac_id,
            "task_id": task_id,
            "fallback_reason": fallback_reason,
            "exit_code": int(exit_code),
        })
    except Exception:  # pragma: no cover
        pass  # trace fail-soft; evidence file is authoritative

    return record


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def load_role_prompt(role_name: str) -> str:
    """Read .claude/agents/<role_name>.md (frontmatter + body) for role injection.

    Used by build_general_purpose_dispatch_prompt() when CC's Agent tool does
    not load custom subagent_types (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
    not honored in the running CC binary). Returns empty string on miss.
    """
    try:
        from src.core.task_dir_layout import _workspace_root
        agent_md = _workspace_root() / ".claude" / "agents" / f"{role_name}.md"
        if not agent_md.exists():
            return ""
        return agent_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def build_general_purpose_dispatch_prompt(
    role_name: str,
    task_prompt: str,
    output_path: Optional[str] = None,
    word_budget: int = 500,
) -> str:
    """Wrap a role-specific task as a general-purpose Agent prompt.

    Usage (when Agent tool reports "Agent type X not found"):
        prompt = build_general_purpose_dispatch_prompt(
            "security-reviewer",
            "review TU-3 of plan_v0 for OWASP issues",
            output_path=".claude/harness/tasks/<tid>/review_findings/security_iter1.json",
        )
        Agent(subagent_type="general-purpose", prompt=prompt)

    The wrapped prompt prepends the role's .claude/agents/<role>.md so the
    general-purpose subagent inherits role identity, mandatory process, and
    output format. Anti-stall headers are added per skill §1.1.
    """
    role_md = load_role_prompt(role_name)
    role_section = (
        f"# ROLE INJECTION: {role_name}\n\n"
        "You are operating as the role specified below. Adopt its identity,\n"
        "mandatory process, and output format exactly. The Agent runtime\n"
        f"could not load `.claude/agents/{role_name}.md` directly, so its\n"
        "content is inlined here:\n\n"
        "---ROLE-DEFINITION-START---\n"
        f"{role_md if role_md else f'(no .claude/agents/{role_name}.md found; act as a generic {role_name} expert)'}\n"
        "---ROLE-DEFINITION-END---\n\n"
    )
    output_clause = (
        f"Deliver via SINGLE Write tool call to `{output_path}`.\n"
        if output_path else ""
    )
    anti_stall = (
        "## HARD CONSTRAINTS\n"
        "- NO Python wrappers / NO Bash heredoc.\n"
        f"{output_clause}"
        f"- ≤{word_budget} words.\n"
        "- Inline final message MUST contain a verdict line:\n"
        f"  \"{role_name.upper()} WRITTEN: <verdict>\" | \"INSUFFICIENT_DATA: <reason>\".\n"
        "- Time budget <300s. If your output is <50 bytes after 60s of work,\n"
        "  bail with INSUFFICIENT_DATA.\n\n"
    )
    return f"{role_section}{anti_stall}## TASK\n\n{task_prompt}\n"


__all__ = [
    "detect_failure",
    "attest_main_session",
    "load_role_prompt",
    "build_general_purpose_dispatch_prompt",
]
