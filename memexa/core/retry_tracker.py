"""
Retry Tracker -- 代码级强制 3 次失败停止规则

不是文档里的"建议"，而是通过文件状态追踪实际修复次数。
当同一个 finding 被修复 3 次仍未解决时，强制阻止继续尝试。

工作方式:
1. fix-agent 开始修复前，调用 record_attempt(finding_id)
2. 修复后检查，调用 record_result(finding_id, resolved=True/False)
3. 下次修复前，调用 can_retry(finding_id) 检查是否已超限
4. 如果超限，返回 False + 原因，fix-agent 必须跳过此 finding

状态文件: memexa/data/retry_state.json
"""

import json
import sys
import time
from pathlib import Path
from typing import Tuple

_DATA_DIR = Path(__file__).parent.parent / "data"
_STATE_FILE = _DATA_DIR / "retry_state.json"
MAX_ATTEMPTS = 3
STALE_HOURS = 4  # 状态超过 4 小时自动清除


def _load() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"findings": {}, "session_start": time.time()}


def _save(state: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _prune_stale(state: dict) -> dict:
    """清除过期的 finding 追踪"""
    now = time.time()
    cutoff = now - STALE_HOURS * 3600
    state["findings"] = {
        k: v for k, v in state["findings"].items()
        if v.get("last_attempt", 0) > cutoff
    }
    return state


def _finding_key(finding_id: str) -> str:
    """规范化 finding ID (去空格，小写，截断)"""
    return finding_id.strip().lower()[:200]


def can_retry(finding_id: str) -> Tuple[bool, str]:
    """
    检查此 finding 是否还允许重试。

    Returns:
        (allowed, reason)
        allowed=True: 可以继续修复
        allowed=False: 已超限，必须跳过
    """
    state = _prune_stale(_load())
    key = _finding_key(finding_id)
    entry = state["findings"].get(key)

    if not entry:
        return True, "first attempt"

    attempts = entry.get("attempts", 0)
    failures = entry.get("failures", 0)

    if failures >= MAX_ATTEMPTS:
        return False, (
            f"ESCALATE: Finding has failed {failures}/{MAX_ATTEMPTS} fix attempts. "
            f"Root cause may require architectural change. "
            f"Do NOT attempt fix #{failures + 1}. Report and move on."
        )

    return True, f"attempt {attempts + 1} (failures so far: {failures})"


def record_attempt(finding_id: str) -> dict:
    """记录一次修复尝试"""
    state = _prune_stale(_load())
    key = _finding_key(finding_id)

    if key not in state["findings"]:
        state["findings"][key] = {
            "attempts": 0,
            "failures": 0,
            "first_seen": time.time(),
            "last_attempt": time.time(),
            "finding_preview": finding_id[:100],
        }

    entry = state["findings"][key]
    entry["attempts"] += 1
    entry["last_attempt"] = time.time()

    _save(state)
    return entry


def record_result(finding_id: str, resolved: bool) -> dict:
    """记录修复结果"""
    state = _prune_stale(_load())
    key = _finding_key(finding_id)

    entry = state["findings"].get(key, {
        "attempts": 1, "failures": 0,
        "first_seen": time.time(), "last_attempt": time.time(),
    })

    if resolved:
        # 已解决，移除追踪
        state["findings"].pop(key, None)
    else:
        entry["failures"] = entry.get("failures", 0) + 1

    _save(state)
    return entry


def reset():
    """清空所有追踪状态"""
    _save({"findings": {}, "session_start": time.time()})


def get_status() -> dict:
    """获取当前追踪状态"""
    state = _prune_stale(_load())
    findings = state.get("findings", {})
    blocked = {k: v for k, v in findings.items() if v.get("failures", 0) >= MAX_ATTEMPTS}
    return {
        "total_tracked": len(findings),
        "blocked": len(blocked),
        "blocked_findings": [
            f"{v['finding_preview']} (failed {v['failures']}x)"
            for v in blocked.values()
        ],
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: retry_tracker.py [check|record|result|status|reset]")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "check":
        finding = sys.argv[2] if len(sys.argv) > 2 else ""
        allowed, reason = can_retry(finding)
        print(reason)
        sys.exit(0 if allowed else 1)

    elif cmd == "record":
        finding = sys.argv[2] if len(sys.argv) > 2 else ""
        entry = record_attempt(finding)
        print(f"Recorded attempt #{entry['attempts']} for: {finding[:80]}")

    elif cmd == "result":
        finding = sys.argv[2] if len(sys.argv) > 2 else ""
        resolved = sys.argv[3].lower() in ("true", "1", "yes") if len(sys.argv) > 3 else False
        entry = record_result(finding, resolved)
        print(f"Result: {'resolved' if resolved else 'failed'}")

    elif cmd == "status":
        status = get_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))

    elif cmd == "reset":
        reset()
        print("Retry tracking reset")

    sys.exit(0)


if __name__ == "__main__":
    main()
