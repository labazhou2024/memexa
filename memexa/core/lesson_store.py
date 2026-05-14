"""
LessonStore — structured lessons learned from KAIROS task executions.

Stores heuristic-derived lessons from project outcomes and retrieves the
most relevant ones for future task prompts, providing zero-cost contextual
guidance based on past successes and failures.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class StructuredLesson:
    lesson_id: str              # "les_TIMESTAMP_SEQ"
    trigger: str                # When this lesson applies
    action: str                 # What to do differently
    evidence: List[str]         # Project IDs that contributed
    confidence: float           # 0.0-1.0, starts at 0.5
    times_applied: int          # How many times injected into a prompt
    times_helped: int           # How many times task succeeded after applying
    category: str               # "error_recovery","performance","architecture","testing","general"
    created_at: str             # ISO timestamp
    last_applied_at: str        # ISO timestamp
    source_error: str = ""      # Original error that triggered this lesson


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class LessonStore:
    MAX_LESSONS = 100
    DECAY_THRESHOLD = 0.15
    MIN_APPLICATIONS = 3

    def __init__(self, data_file: Path = None):
        self._lock = threading.Lock()
        self._data_file = data_file or (_DATA_DIR / "lessons.json")
        self._lessons: List[StructuredLesson] = []
        self._seq = 0
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load lessons from disk; silently start fresh on any error."""
        try:
            text = self._data_file.read_text(encoding="utf-8")
            raw = json.loads(text)
            self._lessons = [StructuredLesson(**r) for r in raw.get("lessons", [])]
            self._seq = raw.get("seq", len(self._lessons))
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("LessonStore: failed to load %s: %s", self._data_file, exc)

    def _save(self) -> None:
        """Atomically persist lessons to disk (must be called under self._lock).

        Retries on Windows file locking errors (WinError 32/5) caused by
        OneDrive sync, antivirus, or concurrent KAIROS workers.
        """
        import os
        import time as _time

        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seq": self._seq,
            "lessons": [asdict(l) for l in self._lessons],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        tmp = self._data_file.with_suffix(f".json.{os.getpid()}.tmp")

        for attempt in range(3):
            try:
                tmp.write_bytes(content)
                os.replace(tmp, self._data_file)
                return
            except PermissionError:
                # WinError 32 (file in use) or WinError 5 (access denied)
                if attempt < 2:
                    _time.sleep(0.1 * (attempt + 1))
                else:
                    logger.warning("LessonStore: save failed after 3 retries")
            except Exception as exc:
                logger.error("LessonStore: save failed: %s", exc)
                break

        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _make_id(self) -> str:
        ts = int(time.time())
        self._seq += 1
        return f"les_{ts}_{self._seq:04d}"

    def _keywords(self, text: str) -> set:
        """Extract lowercase word tokens (length >= 3) from text."""
        import re
        return {w for w in re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())}

    def _find_similar(self, trigger: str, action: str) -> Optional[StructuredLesson]:
        """Find an existing lesson with high keyword overlap on trigger+action."""
        query = self._keywords(trigger) | self._keywords(action)
        if not query:
            return None
        best: Optional[StructuredLesson] = None
        best_ratio = 0.0
        for les in self._lessons:
            existing = self._keywords(les.trigger) | self._keywords(les.action)
            union = query | existing
            if not union:
                continue
            ratio = len(query & existing) / len(union)
            if ratio > best_ratio:
                best_ratio = ratio
                best = les
        # Threshold: >60% Jaccard similarity counts as duplicate
        return best if best_ratio >= 0.6 else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_lesson(self, trigger: str, action: str, evidence: List[str],
                   category: str = "general", confidence: float = 0.5,
                   source_error: str = "") -> str:
        """Add a new lesson. Dedup: merge into similar existing lesson if found."""
        with self._lock:
            similar = self._find_similar(trigger, action)
            if similar is not None:
                # Merge: add new evidence, boost confidence slightly
                for ev in evidence:
                    if ev not in similar.evidence:
                        similar.evidence.append(ev)
                similar.confidence = min(1.0, similar.confidence + 0.05)
                self._save()
                logger.debug("LessonStore: merged into %s", similar.lesson_id)
                return similar.lesson_id

            lesson_id = self._make_id()
            now = self._now_iso()
            lesson = StructuredLesson(
                lesson_id=lesson_id,
                trigger=trigger,
                action=action,
                evidence=list(evidence),
                confidence=confidence,
                times_applied=0,
                times_helped=0,
                category=category,
                created_at=now,
                last_applied_at=now,
                source_error=source_error,
            )
            self._lessons.append(lesson)

            # Enforce MAX_LESSONS: remove the lowest-confidence lesson
            if len(self._lessons) > self.MAX_LESSONS:
                self._lessons.sort(key=lambda l: l.confidence)
                self._lessons.pop(0)

            self._save()
            logger.debug("LessonStore: added %s", lesson_id)
            return lesson_id

    def retrieve_lessons(self, task_context: str, category: str = None,
                         top_k: int = 3) -> List[StructuredLesson]:
        """Retrieve most relevant lessons for a task context.

        Score = keyword_similarity * confidence * recency_boost.
        Updates times_applied and last_applied_at for returned lessons.
        """
        with self._lock:
            query = self._keywords(task_context)
            if not query:
                return []

            now_ts = time.time()
            scored: List[tuple] = []

            for les in self._lessons:
                if category and les.category != category:
                    continue
                candidate = (
                    self._keywords(les.trigger)
                    | self._keywords(les.action)
                    | self._keywords(les.source_error)
                )
                union = query | candidate
                if not union:
                    continue
                keyword_sim = len(query & candidate) / len(union)
                if keyword_sim == 0.0:
                    continue

                # Recency boost: lessons applied in last 24h get 1.1x
                try:
                    last_ts = datetime.fromisoformat(les.last_applied_at).timestamp()
                except ValueError:
                    last_ts = 0.0
                recency_boost = 1.1 if (now_ts - last_ts) < 86400 else 1.0

                score = keyword_sim * les.confidence * recency_boost
                scored.append((score, les))

            scored.sort(key=lambda x: x[0], reverse=True)
            top = [les for _, les in scored[:top_k]]

            # Update application stats
            now_iso = self._now_iso()
            for les in top:
                les.times_applied += 1
                les.last_applied_at = now_iso

            if top:
                self._save()

            return top

    def record_outcome(self, lesson_id: str, task_succeeded: bool) -> None:
        """Record whether a task succeeded after this lesson was applied."""
        with self._lock:
            for les in self._lessons:
                if les.lesson_id == lesson_id:
                    if task_succeeded:
                        les.times_helped += 1
                        les.confidence = min(1.0, les.confidence + 0.05)
                    else:
                        les.confidence = max(0.0, les.confidence - 0.03)
                    self._save()
                    return
            logger.warning("LessonStore: lesson %s not found for record_outcome", lesson_id)

    def generate_lesson_from_failure(self, project: dict, error_msg: str,
                                      error_category: str) -> Optional[str]:
        """Auto-generate a lesson from a failed project using heuristic rules."""
        error_lower = error_msg.lower()
        title = project.get("title", "")
        num_turns = project.get("num_turns", 0) or 0
        cost_usd = project.get("cost_usd", 0.0) or 0.0
        budget = project.get("max_budget_usd", 1.0) or 1.0
        proj_id = project.get("id", "unknown")

        trigger: Optional[str] = None
        action: Optional[str] = None
        category = "error_recovery"

        if "timeout" in error_lower:
            trigger = "task execution results in timeout error"
            action = "set longer timeout or split the task into smaller sub-tasks"
        elif "permission" in error_lower:
            trigger = "task fails with permission error"
            action = "check permission mode parameters before execution"
        elif "bypass" in error_lower:
            trigger = "task uses incorrect permission bypass flag"
            action = "use 'bypassPermissions' not 'bypass' for --permission-mode"
        elif "import" in error_lower or "module" in error_lower:
            trigger = "task fails with import or module not found error"
            action = "verify dependencies are installed before execution"
            category = "testing"
        elif num_turns > 30:
            trigger = "task requires more than 30 turns to complete"
            action = "task is too complex — decompose into smaller independent units"
            category = "architecture"
        elif budget > 0 and cost_usd > 0.8 * budget:
            trigger = "task consumes more than 80% of its budget"
            action = "consider lower-cost model or reduce scope for expensive tasks"
            category = "performance"

        if trigger is None or action is None:
            return None

        evidence = [f"{proj_id} failed: {error_msg[:120]}"]
        return self.add_lesson(
            trigger=trigger,
            action=action,
            evidence=evidence,
            category=category,
            confidence=0.5,
            source_error=error_msg[:256],
        )

    def generate_lesson_from_success(self, project: dict, result: dict) -> Optional[str]:
        """Auto-generate a lesson from a successful project.

        Only generates for projects with interesting efficiency or fix patterns.
        """
        proj_id = project.get("id", "unknown")
        title = project.get("title", "")
        cost_usd = result.get("cost_usd", 0.0) or 0.0
        budget = project.get("max_budget_usd", 1.0) or 1.0
        num_turns = result.get("num_turns", 0) or 0

        trigger: Optional[str] = None
        action: Optional[str] = None
        category = "general"

        if budget > 0 and cost_usd < 0.3 * budget and num_turns < 10:
            trigger = "task is small-scoped with clear single objective"
            action = "keep tasks focused with single objectives for low cost and fast completion"
            category = "performance"
        elif "fix" in title.lower() and result.get("success"):
            # Capture fix pattern from title
            trigger = f"performing fix task similar to: {title[:80]}"
            action = "follow the same approach that succeeded for this fix pattern"
            category = "error_recovery"
        else:
            return None

        evidence = [f"{proj_id} succeeded: cost=${cost_usd:.3f}, turns={num_turns}"]
        return self.add_lesson(
            trigger=trigger,
            action=action,
            evidence=evidence,
            category=category,
            confidence=0.5,
        )

    def decay_and_prune(self) -> int:
        """Decay all lessons by 0.98; prune ineffective ones.

        Prunes lessons below DECAY_THRESHOLD that have been applied
        >= MIN_APPLICATIONS times with a help_rate < 0.3.
        Returns number of pruned lessons.
        """
        with self._lock:
            pruned = 0
            surviving: List[StructuredLesson] = []
            for les in self._lessons:
                les.confidence = round(les.confidence * 0.98, 6)
                help_rate = (
                    les.times_helped / les.times_applied
                    if les.times_applied >= self.MIN_APPLICATIONS
                    else 1.0  # not enough data — keep it
                )
                should_prune = (
                    les.confidence < self.DECAY_THRESHOLD
                    and les.times_applied >= self.MIN_APPLICATIONS
                    and help_rate < 0.3
                )
                if should_prune:
                    pruned += 1
                    logger.debug("LessonStore: pruned %s (conf=%.3f, help_rate=%.2f)",
                                 les.lesson_id, les.confidence, help_rate)
                else:
                    surviving.append(les)

            self._lessons = surviving
            if pruned:
                self._save()
            return pruned

    def format_lessons_for_prompt(self, lessons: List[StructuredLesson]) -> str:
        """Format lessons as a prompt injection block."""
        if not lessons:
            return ""
        lines = ["=== LESSONS FROM PAST EXPERIENCE ==="]
        for i, les in enumerate(lessons, start=1):
            evidence_summary = "; ".join(les.evidence[:3])
            if len(les.evidence) > 3:
                evidence_summary += f" (+{len(les.evidence) - 3} more)"
            lines.append(
                f"{i}. [confidence: {les.confidence:.1f}] "
                f"When {les.trigger}: {les.action}"
            )
            lines.append(f"   Evidence: {evidence_summary}")
        lines.append("===================================")
        return "\n".join(lines)

    def get_stats(self) -> dict:
        """Return stats about the lesson store."""
        with self._lock:
            if not self._lessons:
                return {
                    "total": 0,
                    "avg_confidence": 0.0,
                    "categories": {},
                    "most_effective": [],
                }

            avg_conf = sum(l.confidence for l in self._lessons) / len(self._lessons)

            categories: dict = {}
            for les in self._lessons:
                categories[les.category] = categories.get(les.category, 0) + 1

            # Most effective: highest help_rate among those with >= MIN_APPLICATIONS
            qualified = [
                l for l in self._lessons if l.times_applied >= self.MIN_APPLICATIONS
            ]
            qualified.sort(
                key=lambda l: l.times_helped / l.times_applied if l.times_applied else 0,
                reverse=True,
            )
            most_effective = [
                {
                    "lesson_id": l.lesson_id,
                    "trigger": l.trigger[:60],
                    "help_rate": round(l.times_helped / l.times_applied, 2),
                    "confidence": l.confidence,
                }
                for l in qualified[:5]
            ]

            return {
                "total": len(self._lessons),
                "avg_confidence": round(avg_conf, 3),
                "categories": categories,
                "most_effective": most_effective,
            }


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_instance: Optional[LessonStore] = None


def get_lesson_store() -> LessonStore:
    global _instance
    if _instance is None:
        _instance = LessonStore()
    return _instance
