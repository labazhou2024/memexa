"""
Message Reader — 统一外部信息感知接口。

为 StrategicPlanner 提供外部上下文，全部 best-effort：
失败返回空 Dict，绝不阻塞心跳。

数据源:
  1. WeChat — memex_feed.json / WeChatDB
  2. QQ — NapCat HTTP API / qq_feed.json
  3. Schedule — schedule_data.json (DDL/事件/待办)
  4. Git — subprocess git status per repo
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MEMEX_ROOT = Path(__file__).parent.parent.parent
_WORKSPACE = _MEMEX_ROOT.parent
_DATA = Path(__file__).parent.parent / "data"
_SCHEDULE_FILE = _WORKSPACE / "schedule_data.json"


# ================================================================
# 1. WeChat
# ================================================================

def read_wechat_recent(hours: int = 24) -> Dict[str, Any]:
    """Read recent WeChat message summaries.

    Priority:
      1. memex_feed.json (if wechat-processor has run)
      2. Return empty
    """
    try:
        feed_file = _DATA / "memex_feed.json"
        if feed_file.exists():
            data = json.loads(feed_file.read_text(encoding="utf-8"))
            # Filter to recent entries
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
            entries = data if isinstance(data, list) else data.get("entries", [])
            recent = [e for e in entries
                      if e.get("timestamp", "9999") >= cutoff or e.get("date", "9999") >= cutoff[:10]]
            action_items = []
            for e in recent:
                for todo in e.get("todos", []):
                    if isinstance(todo, str):
                        action_items.append(todo)
                    elif isinstance(todo, dict):
                        action_items.append(todo.get("text", str(todo)))
            return {
                "source": "wechat",
                "entries": len(recent),
                "action_items": action_items[:10],
                "summary": recent[0].get("summary", "") if recent else "",
            }
    except Exception as e:
        logger.debug("WeChat read failed: %s", e)
    return {"source": "wechat", "entries": 0, "action_items": [], "summary": ""}


# ================================================================
# 2. QQ
# ================================================================

def read_qq_recent(hours: int = 24) -> Dict[str, Any]:
    """Read recent QQ message summaries.

    Priority:
      1. NapCat HTTP API (127.0.0.1:3000) if running
      2. qq_feed.json fallback
      3. Return empty
    """
    # Try NapCat API
    try:
        import urllib.request
        url = "http://127.0.0.1:3000/get_recent_messages"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            messages = data.get("data", []) if isinstance(data, dict) else []
            action_items = []
            for msg in messages[:20]:
                text = msg.get("message", "")
                # Simple action item detection
                if any(kw in text for kw in ["需要", "记得", "别忘", "提醒", "截止", "deadline", "DDL"]):
                    action_items.append(text[:100])
            return {
                "source": "qq_napcat",
                "entries": len(messages),
                "action_items": action_items[:10],
            }
    except Exception:
        pass

    # Fallback: qq_feed.json
    try:
        feed_file = _DATA / "qq_feed.json"
        if feed_file.exists():
            data = json.loads(feed_file.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else data.get("entries", [])
            action_items = []
            for e in entries:
                for todo in e.get("todos", []):
                    action_items.append(todo if isinstance(todo, str) else todo.get("text", ""))
            return {
                "source": "qq_file",
                "entries": len(entries),
                "action_items": action_items[:10],
            }
    except Exception:
        pass

    return {"source": "qq", "entries": 0, "action_items": []}


# ================================================================
# 3. Schedule
# ================================================================

def read_schedule_context() -> Dict[str, Any]:
    """Read schedule context: today's events, upcoming DDLs, todos.

    Returns:
        today_events: list of today's events
        upcoming_ddls: DDLs within 3 days [{text, deadline, days_left}]
        todos: undone todos [{text, priority, deadline}]
        stress_level: 0-10
    """
    result: Dict[str, Any] = {
        "today_events": [],
        "upcoming_ddls": [],
        "todos": [],
        "stress_level": 0,
    }

    if not _SCHEDULE_FILE.exists():
        return result

    try:
        data = json.loads(_SCHEDULE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return result

    today = datetime.now().date()
    today_str = today.isoformat()

    # Today's events
    day_data = data.get(today_str, {})
    events = day_data.get("events", {})
    for slot_id, ev in events.items():
        if isinstance(ev, dict) and ev.get("course"):
            result["today_events"].append({
                "slot": slot_id,
                "title": ev.get("course", ""),
                "note": ev.get("note", ""),
                "start": ev.get("startTime", ""),
                "end": ev.get("endTime", ""),
            })

    # Scan upcoming 7 days for DDLs and todos
    for day_offset in range(7):
        scan_date = today + timedelta(days=day_offset)
        scan_str = scan_date.isoformat()
        day_info = data.get(scan_str, {})

        # Todos with deadlines
        for todo in day_info.get("todos", []):
            if isinstance(todo, dict) and not todo.get("done"):
                ddl_str = todo.get("deadline", "")
                text = todo.get("text", "")
                priority = todo.get("priority", "medium")

                result["todos"].append({
                    "text": text,
                    "priority": priority,
                    "deadline": ddl_str,
                })

                # Track as DDL if deadline within 3 days
                if ddl_str:
                    try:
                        ddl_date = datetime.fromisoformat(ddl_str).date()
                        days_left = (ddl_date - today).days
                        if 0 <= days_left <= 3:
                            result["upcoming_ddls"].append({
                                "text": text,
                                "deadline": ddl_str,
                                "days_left": days_left,
                                "priority": priority,
                            })
                    except (ValueError, TypeError):
                        pass

        # Stress from metadata
        meta = day_info.get("metadata", {})
        if scan_str == today_str and meta.get("stress_level"):
            try:
                result["stress_level"] = int(meta["stress_level"])
            except (ValueError, TypeError):
                pass

    # Also check .memex_oracle_state.json for stress
    try:
        oracle_file = _WORKSPACE / ".memex_oracle_state.json"
        if oracle_file.exists():
            oracle = json.loads(oracle_file.read_text(encoding="utf-8"))
            result["stress_level"] = max(
                result["stress_level"],
                int(oracle.get("stress_level", 0) * 10),
            )
    except Exception:
        pass

    # Dedup DDLs by text
    seen = set()
    unique_ddls = []
    for d in result["upcoming_ddls"]:
        if d["text"] not in seen:
            seen.add(d["text"])
            unique_ddls.append(d)
    result["upcoming_ddls"] = sorted(unique_ddls, key=lambda x: x["days_left"])

    return result


# ================================================================
# 4. Git
# ================================================================

def check_git_status(repo_name: str) -> Dict[str, Any]:
    """Check git repository status.

    Returns:
        branch, last_commit, last_commit_msg, uncommitted_changes,
        days_since_commit
    """
    # Resolve repo path
    repo_paths = {
        "memex": _WORKSPACE / "memex",
        "polymarket-agent": _WORKSPACE / "polymarket-agent",
        "polymarket": _WORKSPACE / "polymarket-agent",
    }
    repo_path = repo_paths.get(repo_name)
    if not repo_path or not repo_path.exists():
        return {"error": f"repo not found: {repo_name}"}

    result: Dict[str, Any] = {
        "repo": repo_name,
        "branch": "unknown",
        "last_commit": "",
        "last_commit_msg": "",
        "uncommitted_changes": 0,
        "days_since_commit": 999,
    }

    try:
        # Branch
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_path), timeout=5,
        )
        result["branch"] = r.stdout.strip() or "unknown"

        # Last commit hash + message
        r = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_path), timeout=5,
        )
        line = r.stdout.strip()
        if line:
            parts = line.split(" ", 1)
            result["last_commit"] = parts[0]
            result["last_commit_msg"] = parts[1] if len(parts) > 1 else ""

        # Days since last commit
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ci"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_path), timeout=5,
        )
        ts = r.stdout.strip()
        if ts:
            try:
                commit_date = datetime.fromisoformat(ts.replace(" +", "+").replace(" -", "-"))
                delta = datetime.now().astimezone() - commit_date
                result["days_since_commit"] = round(delta.total_seconds() / 86400, 1)
            except (ValueError, TypeError):
                pass

        # Uncommitted changes count
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(repo_path), timeout=5,
        )
        changes = [l for l in r.stdout.strip().splitlines() if l.strip()]
        result["uncommitted_changes"] = len(changes)

    except Exception as e:
        result["error"] = str(e)

    return result
