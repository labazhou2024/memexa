"""
Evolution Metrics -- Tracks and measures evolution effectiveness.

Dimensions:
  - Agent Quality: avg_judge_score, score_trend
  - Pattern Health: active_patterns, retrieval_rate, avg_confidence
  - Prompt Evolution: attempts, deploy_rate, avg_improvement
  - System Health: test_pass_rate, event_error_rate
  - Efficiency: avg review rounds, pipeline duration

Health Score:
  health = 0.3*test + 0.2*error + 0.2*pattern + 0.15*evo + 0.15*efficiency
  <0.5 = DEGRADED, <0.3 = CRITICAL

Storage: data/evolution_metrics.json (rolling 90 days)
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

from memexa.core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_METRICS_FILE = _DATA_DIR / "evolution_metrics.json"
ROLLING_DAYS = 90


@dataclass
class MetricsSnapshot:
    """Single point-in-time metrics snapshot."""
    timestamp: str
    test_pass_rate: float      # 0-1
    event_error_rate: float    # 0-1 (lower is better)
    pattern_count: int
    avg_confidence: float      # 0-1
    retrieval_count: int       # total pattern retrievals
    evolution_attempts: int
    evolution_deploys: int
    deploy_rate: float         # 0-1
    avg_judge_score: float     # 1-5
    health_score: float        # 0-1 composite
    health_status: str         # HEALTHY/DEGRADED/CRITICAL

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MetricsSnapshot":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class EvolutionMetrics:
    """Tracks evolution effectiveness with rolling history."""

    def __init__(self, metrics_file: Path = None):
        self._file = metrics_file or _METRICS_FILE
        self._history: List[MetricsSnapshot] = []
        self._load()

    def _load(self):
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self._history = [MetricsSnapshot.from_dict(s) for s in data.get("snapshots", [])]
            except Exception as e:
                logger.warning("Failed to load metrics: %s", e)

    def _save(self):
        # Rolling window: keep only last 90 days
        cutoff = (datetime.utcnow() - timedelta(days=ROLLING_DAYS)).isoformat()
        self._history = [s for s in self._history if s.timestamp >= cutoff]

        data = {
            "version": "1.0",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "snapshots": [s.to_dict() for s in self._history],
        }
        atomic_write_json(self._file, data)

    def collect_snapshot(self) -> MetricsSnapshot:
        """Collect current metrics from all sources and compute health score."""

        # 1. Test pass rate
        test_pass_rate = self._get_test_pass_rate()

        # 2. Event error rate
        event_error_rate = self._get_event_error_rate()

        # 3. Pattern health
        pattern_count, avg_confidence, retrieval_count = self._get_pattern_stats()

        # 4. Evolution stats
        evo_attempts, evo_deploys, deploy_rate = self._get_evolution_stats()

        # 5. Average judge score from events
        avg_judge_score = self._get_avg_judge_score()

        # Compute health score
        test_score = test_pass_rate
        error_score = max(0.0, 1.0 - event_error_rate * 5)  # 20% error = 0 score
        pattern_score = min(1.0, pattern_count / 20) * avg_confidence if pattern_count > 0 else 0.0
        evo_score = deploy_rate
        efficiency_score = min(1.0, avg_judge_score / 5.0)

        health = (0.3 * test_score + 0.2 * error_score + 0.2 * pattern_score +
                  0.15 * evo_score + 0.15 * efficiency_score)

        if health >= 0.5:
            status = "HEALTHY"
        elif health >= 0.3:
            status = "DEGRADED"
        else:
            status = "CRITICAL"

        snapshot = MetricsSnapshot(
            timestamp=datetime.utcnow().isoformat() + "Z",
            test_pass_rate=round(test_pass_rate, 3),
            event_error_rate=round(event_error_rate, 4),
            pattern_count=pattern_count,
            avg_confidence=round(avg_confidence, 3),
            retrieval_count=retrieval_count,
            evolution_attempts=evo_attempts,
            evolution_deploys=evo_deploys,
            deploy_rate=round(deploy_rate, 3),
            avg_judge_score=round(avg_judge_score, 2),
            health_score=round(health, 3),
            health_status=status,
        )

        self._history.append(snapshot)
        self._save()
        logger.info("Metrics snapshot: health=%.3f (%s)", health, status)
        return snapshot

    def _get_test_pass_rate(self) -> float:
        memexa_dir = self._file.parent.parent.parent  # data/ -> memexa/ -> memexa/
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no", "--no-header"],
                capture_output=True, text=True, cwd=str(memexa_dir), timeout=120,
            )
            output = result.stdout.strip()
            passed = failed = 0
            for part in output.split("\n")[-1].split(","):
                p = part.strip()
                if "passed" in p:
                    passed = int(p.split()[0])
                elif "failed" in p:
                    failed = int(p.split()[0])
            total = passed + failed
            return passed / max(total, 1)
        except Exception:
            return 0.0

    def _get_event_error_rate(self) -> float:
        try:
            from .event_bus import read_events
            events = read_events(last_n=200)
            if not events:
                return 0.0
            errors = sum(1 for e in events
                        if "fail" in e.get("type", "").lower() or "error" in e.get("type", "").lower())
            return errors / len(events)
        except Exception:
            return 0.0

    def _get_pattern_stats(self) -> Tuple[int, float, int]:
        try:
            from .semantic_memory import get_semantic_memory
            sm = get_semantic_memory()
            stats = sm.stats
            retrieval_count = sum(p.times_retrieved for p in sm._patterns.values())
            return stats["pattern_count"], stats["avg_confidence"], retrieval_count
        except Exception:
            return 0, 0.0, 0

    def _get_evolution_stats(self) -> Tuple[int, int, float]:
        try:
            from .prompt_evolver import get_prompt_evolver
            pe = get_prompt_evolver()
            stats = pe.stats
            attempts = stats["total_evolutions"]
            rate = stats["deployment_rate"]
            deploys = int(attempts * rate)
            return attempts, deploys, rate
        except Exception:
            return 0, 0, 0.0

    def _get_avg_judge_score(self) -> float:
        try:
            from .event_bus import read_events
            events = read_events(last_n=200)
            scores = []
            for e in events:
                details = e.get("details", {})
                score = details.get("score")
                if isinstance(score, (int, float)) and 1 <= score <= 5:
                    scores.append(score)
            return sum(scores) / max(len(scores), 1) if scores else 3.0
        except Exception:
            return 3.0

    def get_trend(self, window: int = 7) -> str:
        """Analyze health score trend over last N snapshots."""
        if len(self._history) < 2:
            return "insufficient_data"

        recent = self._history[-window:]
        if len(recent) < 2:
            return "insufficient_data"

        first_half = recent[:len(recent)//2]
        second_half = recent[len(recent)//2:]

        avg_first = sum(s.health_score for s in first_half) / len(first_half)
        avg_second = sum(s.health_score for s in second_half) / len(second_half)

        diff = avg_second - avg_first
        if diff > 0.05:
            return "improving"
        elif diff < -0.05:
            return "declining"
        return "stable"

    @property
    def latest(self) -> Optional[MetricsSnapshot]:
        return self._history[-1] if self._history else None

    @property
    def summary(self) -> Dict[str, Any]:
        latest = self.latest
        if not latest:
            return {"status": "no_data"}
        return {
            "health": latest.health_score,
            "status": latest.health_status,
            "trend": self.get_trend(),
            "snapshots_count": len(self._history),
            "test_pass_rate": latest.test_pass_rate,
            "pattern_count": latest.pattern_count,
        }


# Singleton
_instance: Optional[EvolutionMetrics] = None

def get_evolution_metrics() -> EvolutionMetrics:
    global _instance
    if _instance is None:
        _instance = EvolutionMetrics()
    return _instance
