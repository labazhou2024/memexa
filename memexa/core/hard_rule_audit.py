"""hard_rule_audit -- HARD RULE / gate pairing audit (hardrule_gat 2026-04-30).

Reads memory/feedback_*.md HARD RULE files, validates tier frontmatter schema,
verifies enforces_at symbol grep-able in target file, reports CLAUDE.md §7.1 drift.

CLI:
    python -m memexa.core.hard_rule_audit list [--memory-dir PATH]
    python -m memexa.core.hard_rule_audit verify [--memory-dir PATH]
    python -m memexa.core.hard_rule_audit drift [--claude-md PATH] [--memory-dir PATH]

Per HARD RULE feedback_writer_reader_schema_contract: writer (frontmatter)
matches reader (audit) field set; round-trip identity asserted via test.
"""
from __future__ import annotations


import argparse
import enum
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from memexa.core._path_resolver import memory_dir


_DEFAULT_MEMORY_DIR = memory_dir()
_DEFAULT_CLAUDE_MD = Path(__file__).resolve().parents[3] / "CLAUDE.md"
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


class Tier(str, enum.Enum):
    BLOCKING = "BLOCKING"
    WARN = "WARN"
    HISTORICAL = "HISTORICAL"
    LESSON = "LESSON"


_VALID_TIERS = {t.value for t in Tier}
_FRONTMATTER_RE = re.compile(r"\A---\r?\n([\s\S]*?)\r?\n---\r?\n", re.MULTILINE)
_TIER_RE = re.compile(r"^tier:\s*(\S+)\s*$", re.MULTILINE)
_ENFORCES_AT_RE = re.compile(r"^enforces_at:\s*(\S.*?)\s*$", re.MULTILINE)
_SUPERSEDED_BY_RE = re.compile(r"^superseded_by:\s*(\S.*?)\s*$", re.MULTILINE)
_SINCE_RE = re.compile(r"^since:\s*(\S+)\s*$", re.MULTILINE)
_HARD_RULE_RE = re.compile(r"HARD RULE", re.IGNORECASE)


@dataclass
class FileAudit:
    path: str
    tier: Optional[str] = None
    enforces_at: Optional[str] = None
    superseded_by: Optional[str] = None
    since: Optional[str] = None
    has_frontmatter: bool = False
    has_hard_rule_marker: bool = False
    valid: bool = False
    issues: List[str] = field(default_factory=list)


@dataclass
class AuditReport:
    total_files: int = 0
    by_tier: Dict[str, int] = field(default_factory=dict)
    files: List[FileAudit] = field(default_factory=list)
    drift_count: int = 0
    no_frontmatter_count: int = 0
    invalid_count: int = 0


def _emit_trace(event: str, payload: dict) -> None:
    """Best-effort trace emission. Fail-soft."""
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def parse_frontmatter(path: Path) -> Dict[str, Optional[str]]:
    """Parse YAML frontmatter (CRLF-tolerant; first --- block only).

    Returns dict with keys: tier, enforces_at, superseded_by, since, has_frontmatter.
    Empty dict + has_frontmatter=False if no frontmatter block.
    """
    out: Dict[str, Optional[str]] = {
        "tier": None, "enforces_at": None, "superseded_by": None,
        "since": None, "has_frontmatter": False,
    }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    m = _FRONTMATTER_RE.search(text)
    if not m:
        return out
    out["has_frontmatter"] = True
    body = m.group(1)
    tm = _TIER_RE.search(body)
    if tm:
        out["tier"] = tm.group(1).strip().upper()
    em = _ENFORCES_AT_RE.search(body)
    if em:
        out["enforces_at"] = em.group(1).strip().strip('"\'')
    sm = _SUPERSEDED_BY_RE.search(body)
    if sm:
        out["superseded_by"] = sm.group(1).strip().strip('"\'')
    snm = _SINCE_RE.search(body)
    if snm:
        out["since"] = snm.group(1).strip()
    return out


def find_hard_rule_files(memory_dir: Path) -> List[Path]:
    """Glob feedback_*.md and filter to those containing 'HARD RULE' marker."""
    if not memory_dir.is_dir():
        return []
    out: List[Path] = []
    for p in sorted(memory_dir.glob("feedback_*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _HARD_RULE_RE.search(text):
            out.append(p)
    return out


def _enforces_at_split(value: str) -> Tuple[str, str]:
    """Split 'path:symbol' into (path, symbol). Last colon is separator (Win path safe)."""
    # Reverse-find since Windows paths have C:/... style colons we should ignore
    # Use forward-slash normalization first
    norm = value.replace("\\", "/")
    # Find LAST colon that's NOT a drive letter colon
    # Heuristic: skip first colon if it's at position 1 (drive letter)
    last_colon = norm.rfind(":")
    if last_colon < 0:
        return (norm, "")
    if last_colon == 1 and len(norm) > 2 and norm[0].isalpha():
        # Just a drive letter; no symbol
        return (norm, "")
    return (norm[:last_colon], norm[last_colon + 1:])


def _verify_enforces_at(value: str) -> Tuple[bool, str]:
    """Verify enforces_at points to existing file + symbol grep-able in it.

    Returns (ok, reason).
    """
    if not value:
        return (False, "enforces_at_empty")
    rel_path, symbol = _enforces_at_split(value)
    if not rel_path or not symbol:
        return (False, f"enforces_at_malformed: {value}")
    # Resolve relative to workspace root
    target = _WORKSPACE_ROOT / rel_path
    if not target.is_file():
        # Also try without leading "memexa/"
        if rel_path.startswith("memexa/"):
            target2 = _WORKSPACE_ROOT / rel_path[len("memexa/"):]
            if target2.is_file():
                target = target2
            else:
                return (False, f"enforces_at_path_missing: {rel_path}")
        else:
            return (False, f"enforces_at_path_missing: {rel_path}")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return (False, f"enforces_at_read_failed: {e}")
    # grep symbol with broader pattern (function/class/constant/Rule N reference)
    sym_escaped = re.escape(symbol)
    pat = re.compile(
        rf"(?:def\s+{sym_escaped}\b|class\s+{sym_escaped}\b|"
        rf"^\s*{sym_escaped}\s*=|\b{sym_escaped}\b)",
        re.MULTILINE,
    )
    if not pat.search(content):
        return (False, f"enforces_at_symbol_not_found: {symbol} in {rel_path}")
    return (True, "")


def _verify_superseded_by(value: str, memory_dir: Path) -> Tuple[bool, str]:
    """Verify superseded_by file exists in memory dir."""
    if not value:
        return (False, "superseded_by_empty")
    target = memory_dir / value
    if not target.is_file():
        return (False, f"superseded_by_path_missing: {value}")
    return (True, "")


def audit_file(path: Path, memory_dir: Optional[Path] = None) -> FileAudit:
    """Audit single file. Validates frontmatter schema per tier."""
    if memory_dir is None:
        memory_dir = path.parent
    fa = FileAudit(path=str(path).replace("\\", "/"))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        fa.has_hard_rule_marker = bool(_HARD_RULE_RE.search(text))
    except OSError as e:
        fa.issues.append(f"read_failed: {e}")
        return fa
    fm = parse_frontmatter(path)
    fa.has_frontmatter = fm["has_frontmatter"]
    fa.tier = fm["tier"]
    fa.enforces_at = fm["enforces_at"]
    fa.superseded_by = fm["superseded_by"]
    fa.since = fm["since"]
    if not fa.has_frontmatter:
        fa.issues.append("no_frontmatter")
        _emit_trace("hard_rule_no_frontmatter", {"file": fa.path})
        return fa
    if fa.tier is None:
        fa.issues.append("tier_field_missing")
        return fa
    if fa.tier not in _VALID_TIERS:
        fa.issues.append(f"tier_invalid: {fa.tier}")
        return fa
    # Per-tier validation
    if fa.tier == Tier.BLOCKING.value:
        if not fa.enforces_at:
            fa.issues.append("blocking_missing_enforces_at")
        else:
            ok, reason = _verify_enforces_at(fa.enforces_at)
            if not ok:
                fa.issues.append(reason)
    elif fa.tier == Tier.HISTORICAL.value:
        if not fa.superseded_by:
            fa.issues.append("historical_missing_superseded_by")
        else:
            ok, reason = _verify_superseded_by(fa.superseded_by, memory_dir)
            if not ok:
                fa.issues.append(reason)
    # WARN / LESSON: no extra requirements
    fa.valid = not fa.issues
    return fa


def audit_all(memory_dir: Path) -> AuditReport:
    """Audit all HARD RULE files in memory_dir."""
    report = AuditReport()
    files = find_hard_rule_files(memory_dir)
    report.total_files = len(files)
    for p in files:
        fa = audit_file(p, memory_dir=memory_dir)
        report.files.append(fa)
        if fa.tier:
            report.by_tier[fa.tier] = report.by_tier.get(fa.tier, 0) + 1
        if "no_frontmatter" in fa.issues:
            report.no_frontmatter_count += 1
        if not fa.valid:
            report.invalid_count += 1
            for issue in fa.issues:
                _emit_trace("hard_rule_drift_detected", {
                    "file": fa.path, "reason": issue,
                })
    _emit_trace("hard_rule_audit_done", {
        "total": report.total_files,
        "by_tier": dict(report.by_tier),
        "drift_count": report.invalid_count,
    })
    return report


_CLAUDE_MD_FEEDBACK_RE = re.compile(
    r"^\s*-\s+(feedback_[a-z0-9_]+\.md)\b",
    re.MULTILINE,
)


def parse_claude_md_tier0(claude_md_path: Path) -> Set[str]:
    """Parse §7.1 Tier-0 list of feedback_*.md filenames from CLAUDE.md."""
    if not claude_md_path.is_file():
        return set()
    try:
        text = claude_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    # Look for section anchored by "HARD RULE 文件" marker or "Tier-0"
    return set(_CLAUDE_MD_FEEDBACK_RE.findall(text))


def compute_drift(
    claude_md_set: Set[str], audit_blocking_set: Set[str]
) -> Dict[str, List[str]]:
    """Symmetric diff. Returns dict with keys missing_in_claude / extra_in_claude."""
    return {
        "missing_in_claude": sorted(audit_blocking_set - claude_md_set),
        "extra_in_claude": sorted(claude_md_set - audit_blocking_set),
        "in_both": sorted(audit_blocking_set & claude_md_set),
    }


def cli_list(memory_dir: Path) -> int:
    report = audit_all(memory_dir)
    summary = {
        "total": report.total_files,
        "by_tier": dict(report.by_tier),
        "no_frontmatter": report.no_frontmatter_count,
        "invalid": report.invalid_count,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cli_verify(memory_dir: Path) -> int:
    report = audit_all(memory_dir)
    if report.invalid_count == 0 and report.no_frontmatter_count == 0:
        print(json.dumps({
            "verdict": "PASS",
            "total": report.total_files,
            "by_tier": dict(report.by_tier),
        }, ensure_ascii=False))
        return 0
    issues = [
        {"file": fa.path, "issues": fa.issues}
        for fa in report.files if fa.issues
    ]
    print(json.dumps({
        "verdict": "FAIL",
        "total": report.total_files,
        "invalid_count": report.invalid_count,
        "no_frontmatter_count": report.no_frontmatter_count,
        "first_5": issues[:5],
    }, ensure_ascii=False, indent=2))
    return 1


def cli_drift(memory_dir: Path, claude_md_path: Path) -> int:
    report = audit_all(memory_dir)
    blocking_set = {
        Path(fa.path).name for fa in report.files
        if fa.tier == Tier.BLOCKING.value
    }
    claude_set = parse_claude_md_tier0(claude_md_path)
    diff = compute_drift(claude_set, blocking_set)
    _emit_trace("hard_rule_drift_report", {
        "claude_count": len(claude_set),
        "blocking_count": len(blocking_set),
        "missing_in_claude": len(diff["missing_in_claude"]),
        "extra_in_claude": len(diff["extra_in_claude"]),
    })
    print(json.dumps({
        "claude_md_tier0_count": len(claude_set),
        "audit_blocking_count": len(blocking_set),
        "drift": diff,
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m memexa.core.hard_rule_audit",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="List HARD RULE files by tier")
    p_list.add_argument("--memory-dir", default=str(_DEFAULT_MEMORY_DIR))
    p_verify = sub.add_parser("verify", help="Verify all HARD RULE frontmatter + enforces_at")
    p_verify.add_argument("--memory-dir", default=str(_DEFAULT_MEMORY_DIR))
    p_drift = sub.add_parser("drift", help="Report CLAUDE.md §7.1 vs audit BLOCKING set")
    p_drift.add_argument("--memory-dir", default=str(_DEFAULT_MEMORY_DIR))
    p_drift.add_argument("--claude-md", default=str(_DEFAULT_CLAUDE_MD))
    args = parser.parse_args(argv)
    memory_dir = Path(args.memory_dir).resolve()
    if args.cmd == "list":
        return cli_list(memory_dir)
    if args.cmd == "verify":
        return cli_verify(memory_dir)
    if args.cmd == "drift":
        claude_md = Path(args.claude_md).resolve()
        return cli_drift(memory_dir, claude_md)
    return 2


if __name__ == "__main__":
    sys.exit(main())
