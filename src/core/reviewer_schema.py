"""reviewer_schema.py — Validate reviewer output against plan-declared verify_cmds.

PURE function: no subprocess, no network, no writes.

Contract:
  validate_reviewer_output(
      path: Path,
      plan_verify_cmds: List[str],
      evidence_entries: List[dict],
  ) -> Tuple[bool, List[str]]

  Returns (ok, reasons) where reasons is an empty list on success,
  or a list of string rejection codes on failure.

Reject reason codes:
  empty_ran_verify_commands
  bad_sha256_format
  empty_stdout_tail
  missing_field:<name>
  evidence_verify_cmd_mismatch_plan_declared
  forged_stdout_no_evidence_match
  valid_hash_wrong_content
  corrupted_evidence_jsonl

Schema for each entry in ran_verify_commands[]:
  {
    "cmd": str,
    "exit_code": int,
    "stdout_tail": str  (<= 2000 chars, non-empty),
    "stdout_sha256": str  (64-hex),
    "wall_time_ms": int,
  }
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_reviewer_output(
    path: Path,
    plan_verify_cmds: List[str],
    evidence_entries: List[dict],
) -> Tuple[bool, List[str]]:
    """Validate reviewer output JSON file against plan-declared verify_cmds.

    Args:
        path: Path to the reviewer findings JSON file.
        plan_verify_cmds: List of verify_cmd strings declared in the plan.
        evidence_entries: List of raw dicts from evidence.jsonl.

    Returns:
        (ok, reasons): ok=True if valid; reasons=[] on success.
    """
    # Load the reviewer findings file
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
    except json.JSONDecodeError:
        return (False, ["corrupted_evidence_jsonl"])
    except OSError:
        return (False, ["corrupted_evidence_jsonl"])

    # Extract ran_verify_commands from data
    if isinstance(data, dict):
        ran = data.get("ran_verify_commands", None)
    else:
        # List-shaped findings file: no ran_verify_commands section
        ran = None

    if ran is None or not isinstance(ran, list) or len(ran) == 0:
        return (False, ["empty_ran_verify_commands"])

    reasons: List[str] = []

    for i, entry in enumerate(ran):
        if not isinstance(entry, dict):
            reasons.append(f"missing_field:entry[{i}]_not_dict")
            continue

        # Check required fields exist
        for field_name in ("cmd", "exit_code", "stdout_tail", "stdout_sha256",
                           "wall_time_ms"):
            if field_name not in entry:
                reasons.append(f"missing_field:{field_name}")

        if reasons:
            # Stop checking this entry if fields are missing
            continue

        cmd = entry["cmd"]
        stdout_tail = entry["stdout_tail"]
        stdout_sha256 = entry["stdout_sha256"]

        # Validate stdout_tail is non-empty
        if not isinstance(stdout_tail, str) or not stdout_tail.strip():
            reasons.append("empty_stdout_tail")

        # Validate sha256 format (64 hex chars)
        if not isinstance(stdout_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", stdout_sha256.lower()
        ):
            reasons.append("bad_sha256_format")
            continue  # Skip further sha checks if format is wrong

        # Normalize sha256 to lowercase for comparison
        stdout_sha256_lower = stdout_sha256.lower()

        # Verify sha256 matches stdout_tail content (catches trivially-forged case)
        if isinstance(stdout_tail, str) and stdout_tail.strip():
            computed = hashlib.sha256(stdout_tail.encode("utf-8")).hexdigest()
            if computed != stdout_sha256_lower:
                reasons.append("valid_hash_wrong_content")

        # Check cmd matches plan-declared verify_cmds
        if plan_verify_cmds is not None and len(plan_verify_cmds) > 0:
            if not _verify_cmd_matches_plan(cmd, plan_verify_cmds):
                reasons.append("evidence_verify_cmd_mismatch_plan_declared")

        # Cross-check against evidence_entries: stdout_sha256 must match
        # at least one evidence entry with matching cmd
        if evidence_entries:
            match_found = _cross_check_evidence(cmd, stdout_sha256_lower,
                                                 evidence_entries)
            if not match_found:
                reasons.append("forged_stdout_no_evidence_match")

    if reasons:
        return (False, reasons)
    return (True, [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_cmd(c: str) -> str:
    """Normalize a command string for comparison.

    Steps:
    1. shlex.split to tokenize
    2. Drop empty tokens
    3. Collapse 'python -m X' to 'X'
    4. Apply posixpath.normcase (lowercases on Windows)
    """
    try:
        tokens = [t for t in shlex.split(c) if t]
    except ValueError:
        # shlex parsing error — use raw string
        return os.path.normcase(c.strip().lower())

    if not tokens:
        return ""

    # Collapse 'python -m X ...' to 'X ...'
    if (
        len(tokens) >= 3
        and tokens[0].lower() in ("python", "python3", "python.exe")
        and tokens[1] == "-m"
    ):
        tokens = tokens[2:]

    # Rejoin and normalize
    normalized = " ".join(tokens)
    return os.path.normcase(normalized)


def _verify_cmd_matches_plan(reviewer_cmd: str, plan_cmds: List[str]) -> bool:
    """Return True if reviewer_cmd (after normalization) matches any plan cmd."""
    norm_reviewer = _normalize_cmd(reviewer_cmd)
    for plan_cmd in plan_cmds:
        if norm_reviewer == _normalize_cmd(plan_cmd):
            return True
    return False


def _recompute_prefix_check(stdout_tail: str, claimed_sha256: str) -> Optional[str]:
    """Recompute sha256 of stdout_tail. Return error code if mismatch.

    Returns:
        None if hash matches claimed_sha256 (or claimed is empty/malformed).
        'valid_hash_wrong_content' if stdout_tail hashes to something different
        than claimed_sha256.

    Note: This only catches the trivially-forged case where stdout_tail itself
    is fake; the deeper guard is X5 cross-check vs evidence.jsonl.
    """
    if not claimed_sha256 or not re.fullmatch(r"[0-9a-f]{64}", claimed_sha256.lower()):
        # Can't recompute if format is bad — handled elsewhere
        return None
    computed = hashlib.sha256(stdout_tail.encode("utf-8")).hexdigest()
    if computed != claimed_sha256.lower():
        return "valid_hash_wrong_content"
    return None


def _cross_check_evidence(
    cmd: str,
    stdout_sha256: str,
    evidence_entries: List[dict],
) -> bool:
    """Return True if any evidence entry has matching (cmd, stdout_sha256).

    Matching uses normalized cmd comparison and exact sha256 match.
    """
    norm_reviewer_cmd = _normalize_cmd(cmd)
    for ev in evidence_entries:
        if not isinstance(ev, dict):
            continue
        ev_cmd = ev.get("verify_cmd", ev.get("cmd", ""))
        ev_sha = ev.get("stdout_sha256", "")
        if not ev_sha:
            continue
        # Normalize sha
        ev_sha_lower = ev_sha.lower() if isinstance(ev_sha, str) else ""
        if (
            _normalize_cmd(str(ev_cmd)) == norm_reviewer_cmd
            and ev_sha_lower == stdout_sha256.lower()
        ):
            return True
    return False
