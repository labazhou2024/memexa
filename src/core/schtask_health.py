"""schtask health monitor — daily digest + SessionStart cheatsheet.

Polls all `memex\\...` schtasks via `schtasks /Query /XML`, tracks LastResult,
emits cheatsheet for SessionStart hook injection.

Output:
    data/schtask_health.json  — current state (refreshed every call)
    data/schtask_health_history.jsonl — append-only history (every poll)

CLI:
    python -m src.core.schtask_health           # refresh + print cheatsheet
    python -m src.core.schtask_health --json    # raw JSON only
    python -m src.core.schtask_health --digest  # daily digest (markdown)
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_STATE = _DATA / "schtask_health.json"
_HISTORY = _DATA / "schtask_health_history.jsonl"

# Known memex schtasks. Map TaskName -> {expected_max_age_min, criticality}
_KNOWN = {
    "GraphMaintenance6h":    {"max_age_min": 380, "critical": True},   # 6h+1h grace
    "BackfillPipeline":      {"max_age_min": 1500, "critical": False}, # daily 03:00 + grace
    "OutboxDrainCron":       {"max_age_min": 50, "critical": True},
    "HindsightWatchdog":     {"max_age_min": 25, "critical": True},
    "HindsightMemPressure":  {"max_age_min": 80, "critical": False},
    "CalendarDaemon":        {"max_age_min": 380, "critical": False},
    "Phase1Monitor":         {"max_age_min": 25, "critical": False},
    "PhaseBMonitor":         {"max_age_min": 20, "critical": False},
    "AudioIngest6h":         {"max_age_min": 380, "critical": False},  # 6h cycle
}


def _parse_query_xml() -> list[dict]:
    """Query all schtasks via CSV, filter \\memex\\* in-process.

    `schtasks /Query /TN "\\memex\\"` fails with "system cannot find file";
    safer to fetch all + Python-filter.
    """
    cmd = ["cmd", "/c", "schtasks /Query /FO CSV /V"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return []
    except Exception:
        return []
    # schtasks /Query /FO CSV /V header (positional, locale-stable):
    #   col 0: HostName / 主机名
    #   col 1: TaskName / 任务名
    #   col 2: Next Run Time / 下次运行时间
    #   col 3: Status / 模式 (zh_CN locale)
    #   col 4: Logon Mode / 登录状态
    #   col 5: Last Run Time / 上次运行时间
    #   col 6: Last Result / 上次结果
    # Using indices because CSV header may be GBK-encoded Chinese.
    import csv, io
    reader = csv.reader(io.StringIO(r.stdout))
    rows = list(reader)
    if len(rows) < 2:
        return []
    out = []
    for row in rows[1:]:
        if len(row) < 7:
            continue
        name = (row[1] or "").strip().strip('"')
        if not name.startswith("\\memex\\"):
            continue
        out.append({
            "task_name": name.replace("\\memex\\", ""),
            "next_run": (row[2] or "").strip(),
            "status": (row[3] or "").strip(),
            "last_run_time": (row[5] or "").strip(),
            "last_result": (row[6] or "").strip(),
        })
    return out


def _parse_last_run_age(last_run_str: str) -> int | None:
    """Parse 'MM/DD/YYYY HH:MM:SS AM' or 'YYYY/M/D HH:MM:SS' → age in minutes."""
    if not last_run_str or last_run_str in ("N/A", ""):
        return None
    s = last_run_str.strip()
    fmts = [
        "%m/%d/%Y %I:%M:%S %p",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    ]
    # Sometimes Win schtasks emits "2026/5/12 9:36:09" — try fuzzier parse
    # by splitting on space then individual parts
    for fmt in fmts:
        try:
            dt = datetime.datetime.strptime(s, fmt)
            age_min = (datetime.datetime.now() - dt).total_seconds() / 60
            return int(age_min)
        except ValueError:
            continue
    # Try regex extract
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})", s)
    if m:
        try:
            dt = datetime.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
            )
            return int((datetime.datetime.now() - dt).total_seconds() / 60)
        except Exception:
            pass
    return None


def collect_health() -> dict:
    rows = _parse_query_xml()
    state = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "schtasks": [],
        "critical_alerts": [],
        "summary": {"total": 0, "healthy": 0, "fail": 0, "stale": 0, "unknown": 0},
    }
    for row in rows:
        name = row["task_name"]
        known = _KNOWN.get(name, {"max_age_min": 1440, "critical": False})
        age = _parse_last_run_age(row.get("last_run_time", ""))
        lr = row.get("last_result", "")
        # Normalize result code
        try:
            lr_int = int(lr) if lr.lstrip("-").isdigit() else None
        except Exception:
            lr_int = None
        if lr_int == 0:
            status = "ok"
        elif lr_int is None:
            status = "unknown"
        else:
            status = f"fail_rc={lr_int}"
        stale = (age is not None and age > known["max_age_min"])
        if stale:
            status = f"stale_age={age}m"
        entry = {
            "name": name,
            "status": status,
            "last_result": lr_int,
            "last_run_age_min": age,
            "next_run": row.get("next_run"),
            "critical": known["critical"],
        }
        state["schtasks"].append(entry)
        state["summary"]["total"] += 1
        if status == "ok":
            state["summary"]["healthy"] += 1
        elif status.startswith("fail"):
            state["summary"]["fail"] += 1
            if known["critical"]:
                state["critical_alerts"].append(
                    f"{name}: {status} (last={row.get('last_run_time','?')})"
                )
        elif status.startswith("stale"):
            state["summary"]["stale"] += 1
            if known["critical"]:
                state["critical_alerts"].append(
                    f"{name}: not run for {age}min (max={known['max_age_min']}m)"
                )
        else:
            state["summary"]["unknown"] += 1

    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    with _HISTORY.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(state, ensure_ascii=False) + "\n")
    return state


def cheatsheet(state: dict) -> str:
    """Compact one-screen cheatsheet for SessionStart hook injection.

    Uses ASCII markers (OK/FAIL/WARN) instead of unicode for Win GBK console
    compatibility. Callers that need unicode can re-render from raw state.
    """
    lines = ["== schtask health =="]
    s = state["summary"]
    lines.append(
        f"{s['healthy']}/{s['total']} healthy | fail={s['fail']} stale={s['stale']} "
        f"unknown={s['unknown']}"
    )
    for st in state["schtasks"]:
        if st["status"] == "ok":
            marker = "[OK]  "
        elif st["critical"]:
            marker = "[FAIL]"
        else:
            marker = "[WARN]"
        lines.append(
            f"  {marker} {st['name']:22s} {st['status']:18s} "
            f"age={st['last_run_age_min']}m"
        )
    if state["critical_alerts"]:
        lines.append("== CRITICAL ALERTS ==")
        for a in state["critical_alerts"]:
            lines.append(f"  ! {a}")
    return "\n".join(lines)


def digest_md(state: dict) -> str:
    """Daily morning digest in markdown for briefing."""
    lines = ["## schtask 健康度", ""]
    lines.append(f"updated: {state['ts']}")
    s = state["summary"]
    lines.append(f"healthy={s['healthy']} / fail={s['fail']} / stale={s['stale']}")
    lines.append("")
    lines.append("| task | status | age | next |")
    lines.append("|---|---|---|---|")
    for st in state["schtasks"]:
        lines.append(
            f"| `{st['name']}` | {st['status']} | "
            f"{st['last_run_age_min']}m | {st.get('next_run', '?')} |"
        )
    if state["critical_alerts"]:
        lines.append("")
        lines.append("### 🚨 CRITICAL")
        for a in state["critical_alerts"]:
            lines.append(f"- {a}")
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("--digest", action="store_true", help="markdown digest")
    args = ap.parse_args()

    state = collect_health()
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    elif args.digest:
        print(digest_md(state))
    else:
        print(cheatsheet(state))


if __name__ == "__main__":
    main()
