"""
Oracle Agent - Behavioral Prediction
Reads real data from schedule_data.json and mood history.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# context_bus removed in CC-Native migration; mood history features disabled

logger = logging.getLogger(__name__)

_WORKSPACE_SCHEDULE = Path(__file__).parents[4] / "schedule_data.json"
_MEMEX_SCHEDULE = Path(__file__).parents[3] / "schedule_data.json"


@dataclass
class OracleBriefing:
    level: str
    message: Optional[str]
    alerts: List[str]
    stress_level: float = 0.0
    ddl_density: float = 0.0
    schedule_density: float = 0.0
    upcoming_ddls: List[Dict] = field(default_factory=list)
    today_events: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "level": self.level, "message": self.message, "alerts": self.alerts,
            "stress_level": self.stress_level, "ddl_density": self.ddl_density,
            "schedule_density": self.schedule_density,
            "upcoming_ddls": self.upcoming_ddls, "today_events": self.today_events,
        }


class Oracle:
    """Predictive agent reading real schedule + mood data."""

    def __init__(self):
        self._schedule_cache = None
        self._cache_time = None
        self._cache_ttl = timedelta(minutes=10)

    def _load_schedule(self) -> Dict:
        now = datetime.now()
        if self._schedule_cache is not None and self._cache_time:
            if now - self._cache_time < self._cache_ttl:
                return self._schedule_cache
        for path in (_WORKSPACE_SCHEDULE, _MEMEX_SCHEDULE):
            if path.exists():
                try:
                    self._schedule_cache = json.loads(path.read_text(encoding="utf-8"))
                    self._cache_time = now
                    return self._schedule_cache
                except Exception:
                    pass
        self._schedule_cache = {}
        self._cache_time = now
        return {}

    def _week_key(self, dt: datetime) -> str:
        monday = dt - timedelta(days=dt.weekday())
        return monday.strftime("%Y-%m-%d")

    def _get_today_events(self) -> List[Dict]:
        schedule = self._load_schedule()
        if not schedule:
            return []
        now = datetime.now()
        wk = self._week_key(now)
        dow = now.weekday()
        events = schedule.get(wk, {}).get("events", {})
        if not isinstance(events, dict):
            return []
        result = []
        for key, ev in events.items():
            if not isinstance(ev, dict):
                continue
            parts = key.split("_")
            if len(parts) != 2:
                continue
            try:
                day = int(parts[0])
            except ValueError:
                continue
            if day == dow:
                result.append({
                    "course": ev.get("course", ""),
                    "startTime": ev.get("startTime", ""),
                    "endTime": ev.get("endTime", ""),
                    "note": ev.get("note", ""),
                })
        result.sort(key=lambda x: x.get("startTime", ""))
        return result

    async def _calc_ddl_density(self) -> float:
        schedule = self._load_schedule()
        if not schedule:
            return 0.0
        now = datetime.now()
        count = 0.0
        for offset in range(2):
            wk = self._week_key(now + timedelta(weeks=offset))
            todos = schedule.get(wk, {}).get("todos", [])
            if not isinstance(todos, list):
                continue
            for todo in todos:
                if isinstance(todo, dict) and not todo.get("done", False):
                    count += 1.0 if todo.get("ddl") else 0.5
        return count / 14.0

    async def _get_upcoming_ddls(self, days: int = 7) -> List[Dict]:
        schedule = self._load_schedule()
        if not schedule:
            return []
        now = datetime.now()
        ddls = []
        for offset in range(2):
            wk = self._week_key(now + timedelta(weeks=offset))
            todos = schedule.get(wk, {}).get("todos", [])
            if not isinstance(todos, list):
                continue
            for todo in todos:
                if isinstance(todo, dict) and not todo.get("done", False):
                    ddls.append({
                        "text": todo.get("text", "")[:100],
                        "ddl": todo.get("ddl"),
                        "week": wk,
                    })
        return ddls

    def _schedule_density(self) -> float:
        schedule = self._load_schedule()
        wk = self._week_key(datetime.now())
        events = schedule.get(wk, {}).get("events", {})
        return len(events) / 5.0 if isinstance(events, dict) else 0.0

    async def predict(self) -> OracleBriefing:
        # context_bus removed; mood history unavailable
        history = []
        stress = 0.0
        ddl_d = await self._calc_ddl_density()
        ddls = await self._get_upcoming_ddls()
        today = self._get_today_events()
        sched_d = self._schedule_density()

        alerts = []
        if self._consecutive_stress(history, 3, 7):
            alerts.append("HIGH_STRESS_PATTERN")
        if ddl_d > 1.5:
            alerts.append("DDL_CLUSTER_WARNING")
        if sched_d > 4.0:
            alerts.append("SCHEDULE_OVERLOAD")
        if len(today) >= 6:
            alerts.append("BUSY_DAY")

        level = "CRITICAL" if len(alerts) >= 3 else ("ALERT" if alerts else "NORMAL")
        msg = self._build_msg(alerts, ddls, stress, today, sched_d)

        return OracleBriefing(
            level=level,
            message=msg if (alerts or today) else None,
            alerts=alerts,
            stress_level=stress,
            ddl_density=ddl_d,
            schedule_density=sched_d,
            upcoming_ddls=ddls,
            today_events=today,
        )

    @staticmethod
    def _calc_current_stress(history):
        if not history:
            return 0.0
        recent = history[-7:]
        levels = [h.get("stress_level", 0) for h in recent if h.get("stress_level")]
        return sum(levels) / len(levels) if levels else 0.0

    @staticmethod
    def _consecutive_stress(history, n=3, threshold=7):
        seq = 0
        for e in history:
            s = e.get("stress_level")
            if s and s >= threshold:
                seq += 1
                if seq >= n:
                    return True
            else:
                seq = 0
        return False

    @staticmethod
    def _build_msg(alerts, ddls, stress, today, sched_d):
        lines = []
        if today:
            lines.append("today (%d events):" % len(today))
            for ev in today:
                t = "%s-%s" % (ev["startTime"], ev["endTime"]) if ev.get("startTime") else "all-day"
                lines.append("  %s  %s" % (t, ev.get("course", "?")))
            lines.append("")
        if alerts:
            lines.append("signals:")
            for a in alerts:
                if a == "HIGH_STRESS_PATTERN":
                    lines.append("  - consecutive high stress (3+ days)")
                elif a == "DDL_CLUSTER_WARNING":
                    lines.append("  - DDL cluster (%d pending)" % len(ddls))
                elif a == "SCHEDULE_OVERLOAD":
                    lines.append("  - schedule overload (%.1f/day)" % sched_d)
                elif a == "BUSY_DAY":
                    lines.append("  - busy day (%d events)" % len(today))
            lines.append("")
        if ddls:
            lines.append("pending:")
            for d in ddls[:5]:
                ds = " [DDL: %s]" % d["ddl"] if d.get("ddl") else ""
                lines.append("  - %s%s" % (d["text"][:60], ds))
        return "\n".join(lines)
