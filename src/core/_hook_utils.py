"""Shared utilities for Claude Code hook scripts.

Provides:
- read_hook_input(): parse JSON from stdin (Claude Code passes hook data this way)
- emit_decision(): write JSON response to stdout in Claude Code's expected format
- atomic_json_write(): race-safe file write
- safe_load_json(): tolerant JSON load (returns {} on corrupt)
- log_hook_event(): write to events.jsonl with hook context
- get_workspace_root(): robust workspace resolution (CJK path safe)
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def get_workspace_root() -> Path:
    """Resolve workspace root robustly (Windows CJK path safe)."""
    marker = Path(".claude") / "config" / "settings.json"
    # Strategy 1: cwd
    try:
        cwd = Path(os.getcwd())
        if (cwd / marker).exists():
            return cwd
    except OSError:
        pass
    # Strategy 2: __file__ ancestors
    try:
        p = Path(__file__).resolve()
        for _ in range(8):
            p = p.parent
            if (p / marker).exists():
                return p
    except OSError:
        pass
    return Path(os.getcwd())


_WORKSPACE = get_workspace_root()

# [SEC-3] Respect MEMEXA_DATA_DIR / MEMEXA_HARNESS_FILE env vars for test isolation.
# Falls back to workspace-based paths in production.
_env_data = os.environ.get("MEMEXA_DATA_DIR")
if _env_data and Path(_env_data).is_dir():
    _DATA_DIR = Path(_env_data)
else:
    _DATA_DIR = _WORKSPACE / "memexa" / "memexa" / "data"
_EVENTS_FILE = _DATA_DIR / "events.jsonl"

_env_harness = os.environ.get("MEMEXA_HARNESS_FILE")
if _env_harness:
    _HARNESS = Path(_env_harness)
else:
    _HARNESS = _WORKSPACE / ".claude" / "config" / "harness_state.json"


# U6 TU-3: HMAC allowlist for gates that may carry bypass tokens.
# Adding a gate here means: (a) gate is recognised by ceo_approve verifier;
# (b) gate's bypass token is HMAC-signed via MEMEXA_HMAC_KEY env;
# (c) auditor scripts can grep this list to inventory all override channels.
_HMAC_ALLOWLIST = (
    "bench_gate",
    # legacy gates auto-track via session_gate.py existing flow; add here as
    # they sprout explicit bypass-token channels.
)


def _verify_bench_bypass_token(token: str) -> bool:
    """U6 TU-3: HMAC-verify a bench_gate bypass token.

    Token format: hex(hmac_sha256(key, "bench_gate:" + utc_date_yyyy_mm_dd))[:32]
    Key: MEMEXA_HMAC_KEY env var. If unset, bypass is rejected.
    Date binds: token is only valid for the day it was issued (replay-resistant).
    """
    import hmac, hashlib
    from datetime import datetime, timezone
    key = os.environ.get("MEMEXA_HMAC_KEY", "").strip()
    if not key or not token:
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg = f"bench_gate:{today}".encode("utf-8")
    expect = hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expect, token.strip()[:32])


def read_hook_input() -> Dict[str, Any]:
    """Parse hook input JSON from stdin. Returns {} on any failure."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {}


def emit_decision(
    decision: str = "allow",
    reason: str = "",
    additional_context: str = "",
    updated_input: Optional[Dict] = None,
    hook_event_name: str = "",
) -> None:
    """Write JSON response to stdout in Claude Code's expected format.

    For PreToolUse: decision in {allow, deny, ask, defer}
    For SubagentStop/Stop/etc: decision in {block} for blocking, omit for allow
    """
    output: Dict[str, Any] = {}

    if hook_event_name in ("PreToolUse",):
        # PreToolUse uses hookSpecificOutput.permissionDecision
        spec: Dict[str, Any] = {
            "hookEventName": hook_event_name,
            "permissionDecision": decision,
        }
        if reason:
            spec["permissionDecisionReason"] = reason
        if updated_input:
            spec["updatedInput"] = updated_input
        if additional_context:
            spec["additionalContext"] = additional_context
        output["hookSpecificOutput"] = spec
    elif hook_event_name in ("SubagentStop", "Stop", "TaskCompleted", "TaskCreated"):
        # These use top-level decision/reason
        if decision == "block":
            output["decision"] = "block"
            if reason:
                output["reason"] = reason
        if additional_context:
            output["hookSpecificOutput"] = {
                "hookEventName": hook_event_name,
                "additionalContext": additional_context,
            }
    elif hook_event_name in ("SubagentStart", "PostToolUseFailure", "SessionEnd",
                              "PreCompact", "StopFailure"):
        # Observation hooks: only additionalContext supported
        if additional_context:
            output["hookSpecificOutput"] = {
                "hookEventName": hook_event_name,
                "additionalContext": additional_context,
            }
    # else: empty output (silent allow)

    if output:
        try:
            print(json.dumps(output, ensure_ascii=False))
        except Exception:
            pass  # Non-blocking


def atomic_json_write(path: Path, data: Any) -> bool:
    """Race-safe JSON write (tempfile + rename). Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}.",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            Path(tmp_path).replace(path)
            return True
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            return False
    except Exception:
        return False


def safe_load_json(path: Path, default: Optional[Dict] = None) -> Dict:
    """Load JSON tolerantly. Returns default ({}) on any failure."""
    if default is None:
        default = {}
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def event_ts(ev: dict) -> str:
    """P2 (2026-04-23): single reader-side utility for event timestamp.

    After R1 unified writer to 6-field schema (ts), pre-R1 entries in
    events.jsonl still have `timestamp` field. This helper returns `ts`
    if present, falls back to `timestamp` for legacy entries. Centralizes
    what was 4 scattered defensive `ev.get("ts") or ev.get("timestamp")`
    reads. Once legacy entries age out of analysis windows (>7d retention),
    this function becomes a no-op alias for `ev.get("ts", "")`.
    """
    return ev.get("ts") or ev.get("timestamp") or ""


def log_hook_event(
    event_type: str,
    hook_name: str,
    details: Optional[Dict] = None,
) -> None:
    """Append hook event to events.jsonl. Non-blocking on failure.

    TU-R1 (2026-04-23): this is now a shim that delegates to
    event_bus.log_event for unified 6-field schema
    ({ts, type, category, agent, session, details}). Fixes B3 in deep audit
    where hook-originated entries used `timestamp` + reader queries on
    `ts`/`payload`/`event` all returned None/`?`.

    Fallback write path kept for bootstrap scenarios where event_bus
    import is unavailable; fallback now also uses 6-field schema.
    """
    try:
        # Delegate to event_bus for unified schema + rotation + locking
        from src.core.event_bus import log_event as _eb_log
        _eb_log(event_type, agent=f"hook:{hook_name}", details=details or {})
        return
    except Exception:
        pass  # fall through to direct-write fallback
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Fallback: unified 6-field schema (NOT the legacy 4-field one)
        from datetime import timezone as _tz
        entry = {
            "ts": datetime.now(tz=_tz.utc).isoformat(),
            "type": event_type,
            "category": "",
            "agent": f"hook:{hook_name}",
            "session": "",
            "details": details or {},
        }
        with open(_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Non-blocking


# ================================================================
# Gate decision logging [V-4] — observability with dedup
# ================================================================

# In-process dedup cache: {(gate, rule, target_basename): last_emit_ts}
_gate_allow_dedup: Dict[tuple, float] = {}
_GATE_DEDUP_WINDOW_SEC = 60


def _should_emit_gate(gate: str, rule: str, decision: str, target: str) -> bool:
    """Decide whether to emit this gate event given rate-limit policy.

    Policy (default: dedup_60s):
      - All 'deny' and 'ask' events: always emit (high signal)
      - 'allow' events: dedup by (gate, rule, target_basename) within 60s
    """
    if decision in ("deny", "ask", "block"):
        return True
    # Try loading config policy; default to dedup_60s
    try:
        from memexa.config_loader import load_config
        cfg = load_config()
        policy = cfg.get("feature_flags", {}).get("gate_event_sampling", "dedup_60s")
    except Exception:
        policy = "dedup_60s"
    if policy == "all":
        return True
    if policy == "deny_only":
        return False  # allow events suppressed
    # Default: dedup_60s
    import time as _time
    basename = os.path.basename(target or "")[:60]
    key = (gate, rule, basename)
    now = _time.time()
    last = _gate_allow_dedup.get(key, 0.0)
    if now - last < _GATE_DEDUP_WINDOW_SEC:
        return False
    _gate_allow_dedup[key] = now
    # Housekeeping: prune stale entries to avoid unbounded memory growth
    if len(_gate_allow_dedup) > 500:
        cutoff = now - _GATE_DEDUP_WINDOW_SEC * 2
        for k in [k for k, v in _gate_allow_dedup.items() if v < cutoff]:
            _gate_allow_dedup.pop(k, None)
    return True


def log_gate_decision(
    gate: str,
    rule: str,
    decision: str,
    target: str = "",
    reason: str = "",
    extra: Optional[Dict] = None,
) -> None:
    """Emit a gate decision event to events.jsonl with rate-limiting.

    Args:
        gate: gate module name (e.g. 'pretool_gate', 'session_gate')
        rule: specific rule fired (e.g. 'root_dir', 'ast_syntax')
        decision: 'allow' | 'deny' | 'ask' | 'block'
        target: file path or command being decided on
        reason: human-readable reason string
        extra: additional structured fields
    """
    try:
        if not _should_emit_gate(gate, rule, decision, target):
            return
        details = {
            "gate": gate,
            "rule": rule,
            "decision": decision,
            "target": target[:200] if target else "",
            "reason": reason[:300] if reason else "",
        }
        if extra:
            details.update(extra)
        log_hook_event("gate_decision", f"gate:{gate}", details=details)
    except Exception:
        pass  # Non-blocking


def is_autopilot_active() -> bool:
    """Check if autopilot mode is currently active."""
    state_file = _DATA_DIR / "persistent_mode_state.json"
    state = safe_load_json(state_file)
    return state.get("active", False) and state.get("mode") == "autopilot"


def get_workspace_paths() -> Dict[str, Path]:
    """Return commonly-used workspace paths."""
    return {
        "workspace": _WORKSPACE,
        "data": _DATA_DIR,
        "events": _EVENTS_FILE,
        "harness": _HARNESS,
    }
