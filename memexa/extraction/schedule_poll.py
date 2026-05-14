"""schedule_poll: academic_hub adapter (mac_win_integration U6 v2; replaces v1 own DDL regex).

Thin adapter over `memexa.academic_hub.scan_schedule_ddls()`. Uses proper
`todo['text']` extraction (NOT v1's str(dict) which catches dict field name
'ddl' as false-positive 204 times).

Smoke:
    python -m memexa.memexa.extraction.schedule_poll --smoke
"""
from __future__ import annotations

import io
import sys
from dataclasses import asdict
from typing import Optional

from memexa.extraction.keystone_outbox import write_envelope


class ScheduleDataMissing(FileNotFoundError):
    """schedule_data.json absent. Raised by extract_ddls() if academic_hub returns 0 tasks
    AND the source file at workspace root is missing (per coverage-iter1-2 fix: no longer dead code)."""


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from memexa.core.trace_sink import emit  # type: ignore
        emit(event, payload)
    except Exception:
        pass


def extract_ddls() -> list[dict]:
    """Run academic_hub.scan_schedule_ddls() and return JSON-serializable rows.

    Each row: {title, course, ddl, source, source_detail, status, raw_text, days_left, urgency}

    Raises ScheduleDataMissing if 0 tasks AND schedule_data.json is missing
    (per coverage-iter1-2: ScheduleDataMissing now has live raise site).
    """
    from pathlib import Path
    from memexa.academic_hub import scan_schedule_ddls
    tasks = scan_schedule_ddls()
    if not tasks:
        ws_root = Path(__file__).resolve().parents[3]
        if not (ws_root / "schedule_data.json").exists():
            raise ScheduleDataMissing(f"schedule_data.json absent at {ws_root / 'schedule_data.json'}")
    payload = []
    for t in tasks:
        payload.append({
            "title": t.title,
            "course": t.course,
            "ddl": t.ddl,
            "source": t.source,
            "source_detail": t.source_detail,
            "status": t.status,
            "raw_text": t.raw_text[:200],
            "days_left": t.days_left,
            "urgency": t.urgency,
        })
        _emit_trace("ddl_extracted", {
            "date": t.ddl,
            "course": t.course,
            "urgency": t.urgency,
        })
    _emit_trace("schedule_polled", {"ddl_count": len(payload)})
    return payload


def poll_and_write():
    rows = extract_ddls()
    return write_envelope("schedule", rows, scrubbed_count=0)


def main(argv: list[str]) -> int:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    smoke = "--smoke" in argv
    if smoke:
        rows = extract_ddls()
        print(f"SCHEDULE smoke: ddl_count={len(rows)}")
        urg = {}
        for r in rows:
            urg[r["urgency"]] = urg.get(r["urgency"], 0) + 1
        print(f"  urgency breakdown: {urg}")
        for r in rows[:5]:
            print(f"  [{r['urgency']}] {r['course'] or '(no course)'} | {r['title'][:50]} | ddl={r['ddl']}")
        return 0
    p = poll_and_write()
    print(f"WROTE {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
