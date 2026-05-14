"""P4 (2026-04-23): scoped env-var override module.

Closes deep-audit S2 partially: of 64 MEMEXA_* env reads, the 10 most
DANGEROUS ones (operator SKIP / ALLOW gates + autorun toggles) are now
read through this single module rather than scattered `os.environ.get`
call sites. This adds an audit trail (`get_override` logs when a gate
skip fires) and a single point for future HMAC hardening.

Why scoped to 10: full migration of 64 sites is ~500 LoC with regression
risk. The other 54 are benign (path overrides / test mocks / cache sizes)
that pretool_gate Rule 13 allowlist already covers.

API:
  from src.core.overrides import get_override
  if get_override("MEMEXA_ALLOW_PRODUCTION_TMP"):
      ...  # operator bypass active

All access goes through `get_override` which:
  1. Reads os.environ.get(name, default)
  2. If value indicates "override active" (truthy string), emits
     `override_used` event so audits know when a gate was skipped
  3. Returns the canonical boolean/string

CLI:
  python -m src.core.overrides list        # show migrated vars + status
  python -m src.core.overrides check       # verify no stale references
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 10 highest-risk env vars migrated in this scope.
# Each entry: {name: (default, type, description)}
# type is one of: "bool" (truthy / "1"/"true"/"yes"), "int", "str"
MIGRATED: dict = {
    "MEMEXA_ALLOW_PRODUCTION_TMP": (
        "0", "bool",
        "skip tmp-path guard in graph_memory.write_fact (legacy tests)",
    ),
    "MEMEXA_ALLOW_COLD_START_OVER": (
        "0", "bool",
        "skip pretool_gate Rule 12 cold_start_meter size block",
    ),
    "MEMEXA_SKIP_PLAN_RETRO": (
        "0", "bool",
        "skip plan_retro_gate block (operator, when plan file corrupt)",
    ),
    "MEMEXA_SKIP_SAMPLE_GATE": (
        "0", "bool",
        "skip prompt_evolver sample validation (evolution debugging)",
    ),
    "MEMEXA_L6_AUTORUN": (
        "0", "bool",
        "auto-deploy evolved prompts without CEO approval",
    ),
    "MEMEXA_L6_EVOLUTION": (
        "0", "bool",
        "master switch for self-evolution prompt engine",
    ),
    "MEMEXA_SESSION_START_VERBOSE": (
        "0", "bool",
        "print full KAIROS narrative on SessionStart (4583B -> 920B compact)",
    ),
    "MEMEXA_GRAPHITI_ENABLED": (
        "shadow", "str",
        "graphiti backend: '0' / 'shadow' / '1' (enabled)",
    ),
    "MEMEXA_GRAPH_BACKEND": (
        "neo4j", "str",
        "graph driver: 'neo4j' / 'none' / 'blocked'",
    ),
    "MEMEXA_MAX_REINFORCEMENTS": (
        "12", "int",
        "persistent_mode reinforcement budget (1-200)",
    ),
    # 2026-04-24 plan_v4 TU-5 (AC-A4 part-2, S-B1): OWASP LLM01 out-of-band override.
    # Primary channel is ~/.claude_gates_override file (written by gates_override CLI).
    # Env var is secondary/backward-compat only when file not present.
    # Registered here so get_override emits audit event on every read.
    "MEMEXA_GATES_OVERRIDE": (
        "", "str",
        "OWASP LLM01 out-of-band override channel for plan_retro gate skip",
    ),
}

# In-memory dedup for audit log (avoid flood on tight loop)
_last_logged: dict = {}
_LOG_DEDUP_SEC = 60


def _coerce(raw: str, typ: str):
    if typ == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if typ == "int":
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 0
    return raw or ""


def _emit_override_used(name: str, value, default: str) -> None:
    """Emit an event when a non-default override value is read."""
    try:
        import time
        now = time.time()
        last = _last_logged.get(name, 0)
        if now - last < _LOG_DEDUP_SEC:
            return
        _last_logged[name] = now
        from src.core.event_bus import log_event
        log_event(
            "override_used",
            agent="src.core.overrides",
            details={
                "name": name,
                "value": str(value)[:64],
                "default": default,
            },
        )
    except Exception:
        pass  # non-blocking


def get_override(name: str, default_override: Optional[str] = None):
    """Canonical entry point for the 10 migrated env vars.

    Args:
      name: the env var name (must be in MIGRATED)
      default_override: explicit default override (rare; test only)
    Returns:
      coerced value (bool/int/str per MIGRATED[name][1])
    """
    if name not in MIGRATED:
        # Unregistered name: fall through to raw env (keeps backwards compat
        # with unmigrated 54 vars). Discourage via pretool_gate Rule 11.
        return os.environ.get(name, "")
    default, typ, _desc = MIGRATED[name]
    effective_default = default_override if default_override is not None else default
    raw = os.environ.get(name, effective_default)
    value = _coerce(raw, typ)
    # Audit: non-default value means override active
    if raw != effective_default and raw != default:
        _emit_override_used(name, value, effective_default)
    return value


def is_migrated(name: str) -> bool:
    return name in MIGRATED


def list_migrated() -> dict:
    """Return current status of all migrated overrides."""
    out = {}
    for name, (default, typ, desc) in MIGRATED.items():
        out[name] = {
            "default": default,
            "type": typ,
            "description": desc,
            "current": os.environ.get(name, default),
            "coerced": get_override(name),
        }
    return out


def check() -> int:
    """CI: check that this module + env_allowlist stay consistent.

    Returns 0 if all MIGRATED names are in env_allowlist.json.
    """
    allow_path = Path(__file__).resolve().parent.parent / "data" / "env_allowlist.json"
    try:
        allow_data = json.loads(allow_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: cannot read env_allowlist.json: {e}", file=sys.stderr)
        return 1
    allow_names = set(allow_data.get("names", []))
    missing = sorted(set(MIGRATED) - allow_names)
    if missing:
        print("FAIL: MIGRATED names missing from env_allowlist.json:")
        for n in missing:
            print(f"  - {n}")
        return 1
    print(f"OK: all {len(MIGRATED)} migrated overrides in allowlist.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="overrides")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list")
    sub.add_parser("check")
    args = p.parse_args(argv)
    if args.cmd == "check":
        return check()
    if args.cmd == "list":
        print(json.dumps(list_migrated(), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(list_migrated(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
