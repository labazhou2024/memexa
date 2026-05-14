"""
Structured schemas for agent-to-agent data transfer.

Instead of free text, agents produce and consume typed dictionaries
matching these schemas. JSON serialization guaranteed.

Used by: review_gate.py, fix-agent, session_gate.py, briefing agent
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import List, Literal, Optional


@dataclass
class ReviewFinding:
    """Single finding from a code review."""
    severity: Literal["critical", "high", "medium", "low"]
    file: str
    line: Optional[int] = None
    category: str = ""  # security, logic, performance, style, test
    issue: str = ""
    suggested_fix: str = ""
    confidence: float = 0.8  # 0-1, how sure the reviewer is


@dataclass
class ReviewResult:
    """Complete output from review_gate or code-reviewer agent."""
    verdict: Literal["APPROVED", "CHANGES_REQUIRED"]
    score: int  # 0-100
    findings: List[ReviewFinding] = field(default_factory=list)
    summary: str = ""
    timestamp: str = ""
    reviewer: str = "review_gate"  # which agent produced this

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, data: dict) -> "ReviewResult":
        findings = [ReviewFinding(**f) for f in data.get("findings", [])]
        return cls(
            verdict=data.get("verdict", "CHANGES_REQUIRED"),
            score=data.get("score", 0),
            findings=findings,
            summary=data.get("summary", ""),
            timestamp=data.get("timestamp", ""),
            reviewer=data.get("reviewer", "unknown"),
        )


@dataclass
class FixResult:
    """Output from fix-agent after attempting to fix a finding."""
    finding_id: str  # severity:file:line or hash
    status: Literal["fixed", "partial", "skipped", "escalated"]
    files_changed: List[str] = field(default_factory=list)
    tests_added: List[str] = field(default_factory=list)
    verification: str = ""  # how was the fix verified
    attempt: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


@dataclass
class GateDecision:
    """Output from any gate (commit-gate, release-gate, etc.)."""
    gate_name: str  # commit-gate, push-gate, plan-gate, exit-gate
    passed: bool
    blockers: List[str] = field(default_factory=list)  # reasons for blocking
    warnings: List[str] = field(default_factory=list)  # non-blocking issues
    metrics: dict = field(default_factory=dict)  # tests_passed, security_score, etc.
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


# JSON schema string for claude -p --json-schema (future Agent SDK use)
REVIEW_RESULT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVED", "CHANGES_REQUIRED"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "category": {"type": "string"},
                    "issue": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
                "required": ["severity", "file", "issue"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "score", "summary"],
})
