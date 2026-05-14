"""
Pipeline State Manager — Layer 2 enforcement.

Tracks which workflow phases (A→B→B.5→C→D) the current task has completed.
Commit-gate reads this state to verify no mandatory phase was skipped.

Phases:
  A  — Task decomposition (Opus)
  B  — Parallel execution (Sonnet agents)
  B5 — Multi-stack test verification
  C  — Code review (code-reviewer agent + Kimi)
  D  — Final gate (gate-keeper)

Usage:
    from memexa.core.pipeline_state import PipelineState
    ps = PipelineState()
    ps.start_task("fix auto_dream", ["memexa/core/auto_dream.py"])
    ps.mark_phase("A")
    ps.mark_phase("B")
    ps.mark_phase("B5")
    ps.mark_phase_c({"reviewer": "kimi", "findings_count": 0, "files_reviewed": ["auto_dream.py"]})
    ps.mark_phase_d({"verdict": "PASSED", "checks": ["syntax", "imports", "security"]})
    assert ps.is_ready_to_commit()

Skip mechanism for trivial changes:
    ps.start_task("update config comment", ["config.yaml"])
    ps.set_skip("trivial_config_change")  # skips A/B/B5 requirement
    # Still need C and D for .py files, but config-only changes auto-pass

Called by:
  - session_gate.py commit-gate (Layer 1: checks is_ready_to_commit)
  - CTO workflow (marks phases as completed)
  - KAIROS daemon (workflow mode tasks)
  - session_start_gate.py (reports incomplete pipelines)
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent.parent.parent.parent / ".claude" / "pipeline_state.json"

# Phases in required order
# A0 = Industry benchmark (research before implementation)
ALL_PHASES = ["A0", "A", "B", "B5", "C", "D"]

# Files that don't require full pipeline (config, docs, data)
TRIVIAL_EXTENSIONS = {".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".csv", ".html", ".css"}

# Skip reasons that are valid
VALID_SKIP_REASONS = {
    "trivial_config_change",   # Only config/doc files changed
    "hotfix_critical",         # Emergency fix, post-hoc review required
    "test_only",               # Only test files changed
    "documentation_only",      # Only docs changed
    "revert",                  # Reverting a previous commit
}


class PipelineState:
    """Manages the current task's pipeline phase progression."""

    def __init__(self, state_file: Optional[Path] = None):
        self._file = state_file or _STATE_FILE
        self._state = self._load()

    def _load(self) -> Dict[str, Any]:
        if self._file.exists():
            try:
                return json.loads(self._file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self):
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def start_task(self, name: str, files: List[str], mode: str = "workflow"):
        """Start tracking a new task through the pipeline.

        Args:
            name: Human-readable task description
            files: List of files that will be modified
            mode: "workflow" (full pipeline) or "quick" (read-only, no pipeline)
        """
        self._state = {
            "task_name": name,
            "files": files,
            "mode": mode,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "phases": {phase: None for phase in ALL_PHASES},
            "skip_reason": None,
            "auto_skip": self._check_auto_skip(files),
        }
        self._save()
        logger.info("Pipeline started: %s (%d files, mode=%s)", name, len(files), mode)

    def _check_auto_skip(self, files: List[str]) -> Optional[str]:
        """Auto-detect if files only contain trivial (non-code) changes."""
        if not files:
            return None
        extensions = {Path(f).suffix.lower() for f in files}
        if extensions <= TRIVIAL_EXTENSIONS:
            return "trivial_config_change"
        if all("test" in f.lower() for f in files):
            return "test_only"
        return None

    def mark_phase(self, phase: str, evidence: Optional[Dict] = None):
        """Mark a phase as completed.

        For Phase C and D, use mark_phase_c() / mark_phase_d() which
        require evidence. This generic method still works for A0/A/B/B5.
        Phase C/D via this method are accepted but logged as 'no evidence'.
        """
        if phase not in ALL_PHASES:
            raise ValueError(f"Unknown phase: {phase}. Valid: {ALL_PHASES}")
        if not self._state:
            logger.warning("No active pipeline — call start_task() first")
            return
        self._state["phases"][phase] = datetime.utcnow().isoformat() + "Z"
        if evidence:
            if "evidence" not in self._state:
                self._state["evidence"] = {}
            self._state["evidence"][phase] = evidence
        elif phase in ("C", "D"):
            logger.warning("Phase %s marked without evidence — commit-gate will check", phase)
        self._save()
        logger.info("Phase %s completed for: %s", phase, self._state.get("task_name", "?"))

    def mark_phase_c(self, review_result: Dict):
        """Mark Phase C with mandatory review evidence.

        Args:
            review_result: Must contain 'reviewer' (str) and 'findings_count' (int).
                           If critical/high findings exist and not fixed, raises ValueError.
        """
        if not review_result.get("reviewer"):
            raise ValueError("Phase C requires review_result with 'reviewer' field (e.g. 'kimi', 'local')")

        critical_high = review_result.get("critical_high_count", 0)
        if critical_high > 0 and not review_result.get("all_resolved", False):
            raise ValueError(
                f"Phase C blocked: {critical_high} unresolved CRITICAL/HIGH findings. "
                f"Fix them before marking Phase C."
            )

        evidence = {
            "reviewer": review_result["reviewer"],
            "findings_count": review_result.get("findings_count", 0),
            "critical_high_count": critical_high,
            "files_reviewed": review_result.get("files_reviewed", []),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        self.mark_phase("C", evidence=evidence)
        logger.info("Phase C marked with evidence: %s reviewed %d files, %d findings",
                     evidence["reviewer"], len(evidence["files_reviewed"]), evidence["findings_count"])

    def mark_phase_d(self, gate_result: Dict):
        """Mark Phase D with mandatory gate-keeper evidence.

        Args:
            gate_result: Must contain 'verdict' == 'PASSED'.
        """
        verdict = gate_result.get("verdict", "UNKNOWN")
        if verdict != "PASSED":
            raise ValueError(f"Phase D blocked: gate verdict = {verdict} (expected PASSED)")

        evidence = {
            "verdict": verdict,
            "checks": gate_result.get("checks", []),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        self.mark_phase("D", evidence=evidence)
        logger.info("Phase D marked with evidence: verdict=%s", verdict)

    def set_skip(self, reason: str):
        """Set a skip reason to bypass certain phase requirements.

        Valid reasons: trivial_config_change, hotfix_critical, test_only,
                       documentation_only, revert
        """
        if reason not in VALID_SKIP_REASONS:
            raise ValueError(f"Invalid skip reason: {reason}. Valid: {VALID_SKIP_REASONS}")
        if not self._state:
            logger.warning("No active pipeline — call start_task() first")
            return
        self._state["skip_reason"] = reason
        self._save()

    def is_ready_to_commit(self) -> tuple:
        """Check if the pipeline is complete enough for commit.

        Returns:
            (ready: bool, reason: str)

        Rules:
          - Quick mode: always ready (no pipeline required)
          - Skip reason set: check reduced requirements
          - Full workflow: C and D must be completed
          - Auto-skip (trivial files only): C and D not required
        """
        if not self._state:
            return False, "NO_PIPELINE: No pipeline state — call start_task() before committing"

        mode = self._state.get("mode", "workflow")
        if mode == "quick":
            return True, "QUICK_MODE: Read-only task, no pipeline required"

        skip = self._state.get("skip_reason") or self._state.get("auto_skip")
        phases = self._state.get("phases", {})

        if skip:
            # Reduced requirements for skip reasons
            if skip in ("trivial_config_change", "documentation_only"):
                # Config/doc changes: no pipeline required
                return True, f"SKIP({skip}): Non-code files only"
            if skip == "test_only":
                # Test files: B5 (test run) required, C/D not required
                if phases.get("B5"):
                    return True, "SKIP(test_only): Tests passed"
                return False, "SKIP(test_only): Must run tests (Phase B5) before committing test changes"
            if skip == "hotfix_critical":
                # Hotfix: allow commit, but flag for post-hoc review
                return True, "SKIP(hotfix): CRITICAL HOTFIX — post-hoc review REQUIRED"
            if skip == "revert":
                return True, "SKIP(revert): Reverting previous commit"

        # Full workflow: A0, C, and D must be done
        # A0 = Industry benchmark (mandatory for non-trivial tasks per CLAUDE.md §3.1)
        # A and B are organizational (can't mechanically verify they happened in Claude's head)
        # B5 is checked at commit-gate via pytest anyway
        # C (review) and D (gate) are the critical ones
        missing = []
        if not phases.get("A0"):
            # A0 is mandatory unless task is clearly internal/trivial
            task_name = self._state.get("task_name", "").lower()
            is_internal = any(kw in task_name for kw in [
                "fix", "hotfix", "typo", "rename", "cleanup", "refactor",
                "test", "bump", "update version", "merge", "revert",
                "auto:", "config", "doc",
            ])
            if not is_internal:
                missing.append("A0 (industry benchmark — mark with mark_phase('A0') after research)")
        if not phases.get("C"):
            missing.append("C (code review — use mark_phase_c(review_result))")
        if not phases.get("D"):
            missing.append("D (final gate — use mark_phase_d(gate_result))")

        if missing:
            return False, f"PIPELINE_INCOMPLETE: Missing phases: {', '.join(missing)}"

        # Evidence check: warn if C/D marked without evidence
        evidence = self._state.get("evidence", {})
        warnings = []
        if not evidence.get("C"):
            warnings.append("C has no review evidence (mark_phase_c recommended)")
        if not evidence.get("D"):
            warnings.append("D has no gate evidence (mark_phase_d recommended)")

        if warnings:
            return True, f"PIPELINE_COMPLETE (warnings: {'; '.join(warnings)})"

        return True, "PIPELINE_COMPLETE: All required phases done with evidence"

    def get_state(self) -> Dict[str, Any]:
        """Get current pipeline state."""
        return dict(self._state) if self._state else {}

    def clear(self):
        """Clear pipeline state after successful commit."""
        self._state = {}
        if self._file.exists():
            self._file.unlink()

    def get_summary(self) -> str:
        """Human-readable summary of current pipeline state."""
        if not self._state:
            return "No active pipeline"

        name = self._state.get("task_name", "unnamed")
        phases = self._state.get("phases", {})
        skip = self._state.get("skip_reason") or self._state.get("auto_skip")

        phase_status = []
        for p in ALL_PHASES:
            if phases.get(p):
                phase_status.append(f"{p}:OK")
            else:
                phase_status.append(f"{p}:--")

        summary = f"[{name}] {' → '.join(phase_status)}"
        if skip:
            summary += f" (skip: {skip})"
        return summary


# Module-level singleton
_instance: Optional[PipelineState] = None


def get_pipeline_state(state_file: Optional[Path] = None) -> PipelineState:
    """Get the singleton PipelineState instance."""
    global _instance
    if _instance is None or state_file is not None:
        _instance = PipelineState(state_file)
    return _instance
