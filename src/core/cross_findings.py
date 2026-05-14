"""Cross-findings injection helper for iterative cross-review.

Enforces the "max 2000 chars/reviewer, top 5 by severity" rule from SKILL.md
Stage 4. Previously this rule was prose-only and routinely violated.

Used by autopilot orchestration between rounds of iterative review.
"""

import json
from pathlib import Path
from typing import Optional

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def build_cross_prompt(
    reviewer_json_path: Path,
    max_chars: int = 2000,
    top_n: int = 5,
) -> str:
    """Build a bounded cross-findings string from a reviewer's JSON output.

    Reads the reviewer's last_review_*.json, extracts the top_n findings by
    severity, and returns a JSON string truncated to max_chars.

    Used to feed previous-round findings to next-round reviewers without
    blowing up context.

    Args:
        reviewer_json_path: Path to last_review_*.json
        max_chars: Maximum characters in returned string (hard cap)
        top_n: Maximum number of findings to include

    Returns:
        Truncated JSON string with top_n severity-sorted findings.
        Empty string if file is missing/corrupt.
    """
    if not reviewer_json_path.exists():
        return ""

    # Size pre-check: don't even open files >512KB (signals abuse)
    try:
        if reviewer_json_path.stat().st_size > 512 * 1024:
            return f'[OOM_GUARD] {reviewer_json_path.name} too large; skipped.'
    except OSError:
        return ""

    try:
        data = json.loads(reviewer_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""

    findings = data.get("findings", []) or data.get("issues", [])
    if not isinstance(findings, list):
        return ""

    # Sort by severity (CRITICAL first), then truncate to top_n
    sorted_findings = sorted(
        findings,
        key=lambda f: _SEVERITY_ORDER.get(
            (f.get("severity") or "LOW").upper(), 3
        ),
    )[:top_n]

    # Project to severity + issue only (drop verbose fields)
    compact = [
        {
            "severity": f.get("severity", "LOW"),
            "issue": (f.get("issue") or f.get("text") or "")[:300],
        }
        for f in sorted_findings
    ]

    text = json.dumps(compact, ensure_ascii=False)
    return text[:max_chars]


def build_all_cross_prompts(
    data_dir: Path,
    exclude_reviewer: Optional[str] = None,
    max_chars_per: int = 2000,
    top_n: int = 5,
) -> dict:
    """Build cross-findings for all reviewers except `exclude_reviewer`.

    Returns dict mapping reviewer name -> truncated findings string.
    """
    result = {}
    for review_file in data_dir.glob("last_review_*.json"):
        reviewer = review_file.stem.replace("last_review_", "")
        if exclude_reviewer and reviewer == exclude_reviewer:
            continue
        prompt = build_cross_prompt(review_file, max_chars=max_chars_per, top_n=top_n)
        if prompt:
            result[reviewer] = prompt
    return result
