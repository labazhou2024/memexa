"""HMAC-protected daily Sonnet quarantine counter — TU-7 (P1).

Backfill writes Sonnet output to a daily-capped quarantine bucket when the
primary local extractors (Mac Qwen3-MLX + DeepSeek) both fail or schema-fail
on narrative content. To prevent runaway cost / abuse, this counter:

  - Atomically increments a per-day count under HMAC-SHA256 (key from
    memex/config.yaml secret-key, falls back to env MEMEX_HMAC_KEY for tests)
  - Verifies HMAC on every read; tampered file → block (no fail-open)
  - Writes a flag file `data/sonnet_quarantine_blocked.flag` when count >= cap
    OR HMAC verify fails. Pretool_gate Rule 17 checks this flag.

Schema (data/sonnet_quarantine_counter.json):
  {
    "date": "YYYY-MM-DD",
    "count": int,
    "hmac": "<sha256-hex>",     # HMAC over canonical JSON of date+count
    "last_updated_at": "<ISO-8601>"
  }

API:
  - get_daily_count() -> int
  - increment(usd_cost: float = 0.0) -> tuple[int, bool]
        returns (new_count, blocked); blocked=True iff cap exceeded post-increment
  - verify_hmac() -> bool
  - is_blocked() -> bool
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.core.atomic_io import atomic_write_json


_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_COUNTER_PATH = _DATA / "sonnet_quarantine_counter.json"
_BLOCKED_FLAG = _DATA / "sonnet_quarantine_blocked.flag"
_CONFIG_PATH = _REPO / "config.yaml"

DEFAULT_CAP = 50  # max sonnet quarantine extracts per UTC day


def emit(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def _today_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_secret_key() -> bytes:
    """Resolve HMAC secret. Tier-1 env (test/CI), Tier-2 config.yaml.

    Tier-2 yaml read kept minimal (no PyYAML dep): grep first
    `secret_key: <value>` line. Production config.yaml is owner-readable
    only per project security posture.
    """
    env_key = os.environ.get("MEMEX_HMAC_KEY")
    if env_key:
        return env_key.encode("utf-8")
    if _CONFIG_PATH.exists():
        try:
            for line in _CONFIG_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("hmac_secret:") or line.startswith("secret_key:"):
                    val = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val.encode("utf-8")
        except OSError:
            pass
    # Last-resort dev key (deterministic per-host); production MUST set env or yaml
    return f"dev-key-{os.environ.get('USERNAME', 'unknown')}-DO-NOT-USE-IN-PROD".encode("utf-8")


def _compute_hmac(date: str, count: int, key: Optional[bytes] = None) -> str:
    """Canonical HMAC: sha256(key, "<date>|<count>")."""
    if key is None:
        key = _load_secret_key()
    payload = f"{date}|{count}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _read_counter() -> dict:
    """Read counter file. Returns fresh dict if missing/corrupt/wrong-day."""
    today = _today_utc_date()
    if not _COUNTER_PATH.exists():
        return {"date": today, "count": 0,
                "hmac": _compute_hmac(today, 0),
                "last_updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        d = json.loads(_COUNTER_PATH.read_text(encoding="utf-8"))
        if d.get("date") != today:
            # Day rolled over — reset
            return {"date": today, "count": 0,
                    "hmac": _compute_hmac(today, 0),
                    "last_updated_at": datetime.now(timezone.utc).isoformat()}
        return d
    except Exception:
        # Corrupt: do NOT silently reset. Caller's verify_hmac will fail.
        return {"date": today, "count": -1,
                "hmac": "CORRUPT", "last_updated_at": ""}


def _write_counter_atomic(count: int) -> dict:
    """Compute new HMAC, atomic-write counter file. Returns the new dict."""
    today = _today_utc_date()
    new = {
        "date": today,
        "count": int(count),
        "hmac": _compute_hmac(today, int(count)),
        "last_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_COUNTER_PATH, new)
    except OSError:
        pass
    return new


def verify_hmac() -> bool:
    """Verify HMAC of on-disk counter. Empty/missing file returns True (vacuous)."""
    if not _COUNTER_PATH.exists():
        return True
    try:
        d = json.loads(_COUNTER_PATH.read_text(encoding="utf-8"))
        date = d.get("date", "")
        count = int(d.get("count", -1))
        recorded = d.get("hmac", "")
        if count < 0 or not date or not recorded:
            return False
        expected = _compute_hmac(date, count)
        return hmac.compare_digest(expected, recorded)
    except Exception:
        return False


def get_daily_count() -> int:
    """Return today's count; -1 if HMAC verify fails."""
    if not verify_hmac():
        return -1
    d = _read_counter()
    return int(d.get("count", 0))


def is_blocked(cap: int = DEFAULT_CAP) -> bool:
    """True iff blocked-flag file present OR count >= cap OR HMAC tampered."""
    if _BLOCKED_FLAG.exists():
        return True
    if not verify_hmac():
        return True
    if get_daily_count() >= cap:
        return True
    return False


def _write_blocked_flag(reason: str, count: int) -> None:
    """Write the blocked-flag file consumed by pretool_gate Rule 17."""
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_BLOCKED_FLAG, {
            "reason": reason,
            "count": count,
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        })
    except OSError:
        pass


def clear_blocked_flag() -> bool:
    """CEO-only operation; called from clear-quarantine CLI. Returns success."""
    try:
        if _BLOCKED_FLAG.exists():
            _BLOCKED_FLAG.unlink()
            try:
                emit("sonnet_quarantine_unblocked", {"by": "ceo"})
            except Exception:
                pass
            return True
    except OSError:
        pass
    return False


def increment(usd_cost: float = 0.0,
               cap: int = DEFAULT_CAP) -> tuple[int, bool]:
    """Atomically bump today's counter by 1.

    Returns (new_count, blocked). blocked=True ↔ count > cap post-increment.
    Side-effect: writes blocked-flag file if cap crossed.
    """
    # Read current → verify → bump
    if not verify_hmac():
        _write_blocked_flag(reason="hmac_verify_fail", count=-1)
        try:
            emit("hmac_verify_fail", {"file": str(_COUNTER_PATH)})
        except Exception:
            pass
        return -1, True

    d = _read_counter()
    new_count = int(d.get("count", 0)) + 1
    _write_counter_atomic(new_count)

    blocked = new_count > cap
    if blocked:
        _write_blocked_flag(reason="cap_exceeded", count=new_count)
        try:
            emit("sonnet_quarantine_blocked", {
                "count": new_count, "cap": cap, "usd_cost": usd_cost,
            })
        except Exception:
            pass
    else:
        try:
            emit("hmac_verify_pass", {"count": new_count, "cap": cap,
                                        "usd_cost": usd_cost})
        except Exception:
            pass
    return new_count, blocked


def status_dict() -> dict:
    """Return CLI-friendly status dict. Used by tools and verify scripts."""
    return {
        "date": _today_utc_date(),
        "count": get_daily_count(),
        "cap": DEFAULT_CAP,
        "is_blocked": is_blocked(),
        "blocked_flag_exists": _BLOCKED_FLAG.exists(),
        "hmac_verified": verify_hmac(),
    }


def main() -> int:
    """CLI: `python -m src.core.sonnet_quarantine_counter [status|increment|clear]`."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["status", "increment", "clear", "verify"])
    p.add_argument("--cost", type=float, default=0.0)
    args = p.parse_args()

    if args.cmd == "status":
        print(json.dumps(status_dict(), ensure_ascii=False))
        return 0
    if args.cmd == "increment":
        count, blocked = increment(usd_cost=args.cost)
        print(json.dumps({"count": count, "blocked": blocked}, ensure_ascii=False))
        return 1 if blocked else 0
    if args.cmd == "clear":
        ok = clear_blocked_flag()
        print(json.dumps({"cleared": ok}, ensure_ascii=False))
        return 0 if ok else 1
    if args.cmd == "verify":
        ok = verify_hmac()
        print(json.dumps({"hmac_verified": ok}, ensure_ascii=False))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
