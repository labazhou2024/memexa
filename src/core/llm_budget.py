"""
LLM Budget Accountant (v1, 2026-04-20, plan v3.1 T6.5)

Cross-process shared Haiku/LLM token-bucket for
  - ingest_watcher
  - governance_middleware
  - promotion_engine

Goals (plan v3.1 §4 R13, §5 AC-18, §7 T6.5):
  * One authoritative daily USD cap across all modules.
  * Atomic reserve-then-record so concurrent workers can't double-spend.
  * Per-module counters (so we can see who burned the budget).
  * Crash-safe: if a holder is SIGKILL'd mid-reserve, the next acquirer
    can take over after a stale-TTL window (60s, cheap to revisit).
  * Daily reset: a new calendar day silently clears counters, preserves cap.
  * Env override: MEMEXA_HAIKU_DAILY_USD (default 2.0 USD).

Lock library: filelock (per R3 E3 — unified choice across the codebase,
already used by trace_sink.py and soft_signal_classifier.py).

Storage:
  state:    memexa/memexa/data/llm_budget_state.json
  lock:     memexa/memexa/data/llm_budget.lock
  sentinel: memexa/memexa/data/llm_budget.pid (pid of current holder, for
            stale-TTL detection when lock file mtime > _STALE_TTL_SEC)

State shape:
  {
    "day_key":       "YYYY-MM-DD",                 # UTC
    "reserved_usd":  {module_name: float, ...},
    "actual_usd":    {module_name: float, ...},
    "total_spent":   float,                        # reserved + actual-delta
  }

Note on accounting:
  - `reserved_usd[m]` = running reserve credit for module m (positive).
  - `actual_usd[m]`   = running actual-cost ledger for module m.
  - remaining = cap - max(sum(reserved_usd.values()), sum(actual_usd.values()))
      We use the larger of reserved or actual so that an un-reconciled
      reserve still counts against the cap (pessimistic).
  - `record_actual` adds to actual_usd[m] and subtracts the equivalent
    reserve from reserved_usd[m] (floored at 0). Over-spend increases
    actual without touching reserves.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ----- Paths -----
# memexa/memexa/core/llm_budget.py -> memexa/memexa/data/
_DATA_DIR = Path(__file__).parent.parent / "data"
_STATE_FILE = _DATA_DIR / "llm_budget_state.json"
_LOCK_FILE = _DATA_DIR / "llm_budget.lock"
_PID_FILE = _DATA_DIR / "llm_budget.pid"

# ----- Constants -----
_STALE_TTL_SEC = 60.0        # after this, lock holder is presumed dead
_LOCK_TIMEOUT_SEC = 10.0     # filelock acquisition ceiling (per spec)

_ALLOWED_MODULES = {"ingest", "governance", "promotion"}


def _daily_cap_usd() -> float:
    """Env override MEMEXA_HAIKU_DAILY_USD (default 2.0).

    Read each call so tests can monkeypatch env between operations.
    Invalid values silently fall back to 2.0.
    """
    raw = os.environ.get("MEMEXA_HAIKU_DAILY_USD", "2.0")
    try:
        val = float(raw)
        if val < 0:
            return 2.0
        return val
    except (TypeError, ValueError):
        return 2.0


def _today_key() -> str:
    """UTC calendar day as YYYY-MM-DD. Uses timezone-aware datetime."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ----- Lock with stale-TTL recovery -----

def _pid_alive(pid: int) -> bool:
    """Best-effort: is this pid still running?

    Windows has no os.kill(pid, 0); use ctypes OpenProcess. Failures are
    treated as 'not alive' (conservative: prefer lock takeover over
    permanent deadlock).
    """
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(
                    h, ctypes.byref(exit_code)
                )
                STILL_ACTIVE = 259
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError):
        # PermissionError on POSIX means the process exists but we can't
        # signal it — still "alive" for our purposes.
        if sys.platform != "win32" and isinstance(sys.exc_info()[1], PermissionError):
            return True
        return False
    except Exception:
        return False


def _maybe_recover_stale_lock() -> bool:
    """If lock-file mtime > _STALE_TTL_SEC and pid sentinel is dead,
    forcibly remove the lock file so the next FileLock() can acquire.

    Returns True if recovery was performed (caller may want to log).
    Safe to call without holding any lock — filesystem mtime check is
    the only authority here.
    """
    if not _LOCK_FILE.exists():
        return False
    try:
        mtime = _LOCK_FILE.stat().st_mtime
        age = time.time() - mtime
        if age <= _STALE_TTL_SEC:
            return False
        # Age exceeded; check sentinel
        holder_pid = -1
        if _PID_FILE.exists():
            try:
                holder_pid = int(_PID_FILE.read_text(encoding="utf-8").strip() or "-1")
            except (ValueError, OSError):
                holder_pid = -1
        if _pid_alive(holder_pid):
            return False
        # Holder is dead or unknown — reclaim
        try:
            _LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            _PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning(
            "llm_budget: reclaimed stale lock (age=%.1fs, dead_pid=%s)",
            age, holder_pid,
        )
        # Best-effort trace event (never block on logging)
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event(
                "hook_outcome",
                {
                    "hook": "llm_budget.stale_lock_recovery",
                    "age_sec": round(age, 1),
                    "dead_pid": holder_pid,
                },
            )
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("llm_budget: stale-lock check failed: %s", e)
        return False


def _acquire():
    """Return a filelock context manager. Runs stale-TTL recovery first.

    Any failure to import filelock raises — this module is a hard
    dependency per R3 E3.
    """
    from filelock import FileLock  # required dep
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _maybe_recover_stale_lock()
    return FileLock(str(_LOCK_FILE), timeout=_LOCK_TIMEOUT_SEC)


def _write_sentinel() -> None:
    try:
        _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _clear_sentinel() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ----- State I/O -----

def _blank_state() -> Dict:
    return {
        "day_key": _today_key(),
        "reserved_usd": {},
        "actual_usd": {},
    }


def _load_state() -> Dict:
    """Read current state; treat missing/corrupt as fresh blank."""
    if not _STATE_FILE.exists():
        return _blank_state()
    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return _blank_state()
        data.setdefault("day_key", _today_key())
        data.setdefault("reserved_usd", {})
        data.setdefault("actual_usd", {})
        # Coerce to floats defensively
        data["reserved_usd"] = {
            k: float(v) for k, v in data["reserved_usd"].items()
            if isinstance(v, (int, float))
        }
        data["actual_usd"] = {
            k: float(v) for k, v in data["actual_usd"].items()
            if isinstance(v, (int, float))
        }
        return data
    except (json.JSONDecodeError, OSError, ValueError):
        logger.warning("llm_budget: corrupt state, starting fresh")
        return _blank_state()


def _save_state(state: Dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, _STATE_FILE)


def _maybe_daily_reset(state: Dict) -> Dict:
    """If day_key has rolled over, clear counters but keep shape."""
    today = _today_key()
    if state.get("day_key") != today:
        state["day_key"] = today
        state["reserved_usd"] = {}
        state["actual_usd"] = {}
    return state


def _pessimistic_spent(state: Dict) -> float:
    """max(sum reserved, sum actual) — what the cap should test against."""
    r = sum(state.get("reserved_usd", {}).values())
    a = sum(state.get("actual_usd", {}).values())
    return max(r, a)


# ----- Public API -----

def check_and_reserve(
    module: str,
    estimated_cost_usd: float,
) -> Tuple[bool, str]:
    """Atomically check remaining budget and reserve for `module`.

    Returns:
        (True, "ok")        -- reservation recorded; caller may call LLM
        (False, <reason>)   -- do NOT call LLM. reason in:
            "budget_exhausted"   -- cap reached
            "invalid_module"     -- module not in whitelist
            "invalid_cost"       -- cost <= 0 or NaN
            "lock_timeout"       -- couldn't acquire lock within timeout
            "io_error"           -- state read/write failed

    Thread-safe / cross-process-safe via filelock with stale-TTL recovery.
    """
    if module not in _ALLOWED_MODULES:
        return (False, "invalid_module")
    try:
        cost = float(estimated_cost_usd)
    except (TypeError, ValueError):
        return (False, "invalid_cost")
    if cost <= 0 or cost != cost:  # NaN check
        return (False, "invalid_cost")

    try:
        lock = _acquire()
    except ImportError:
        return (False, "io_error")

    try:
        with lock:
            _write_sentinel()
            try:
                state = _load_state()
                state = _maybe_daily_reset(state)
                cap = _daily_cap_usd()
                spent = _pessimistic_spent(state)
                # H3 fix (2026-04-20): FP-strict cap enforcement. Under
                # concurrent many-small-reserves, sum() float error can
                # leave the final slot marginally over-cap. Subtract a
                # 1e-9 epsilon so the invariant `total_reserved <= cap`
                # holds exactly, even in the worst FP-accumulation case.
                if spent + cost > cap - 1e-9:
                    return (False, "budget_exhausted")
                state["reserved_usd"][module] = (
                    state["reserved_usd"].get(module, 0.0) + cost
                )
                try:
                    _save_state(state)
                except OSError as e:
                    logger.warning("llm_budget: save failed: %s", e)
                    return (False, "io_error")
                return (True, "ok")
            finally:
                _clear_sentinel()
    except Exception as e:
        # filelock.Timeout and anything else bubble here
        name = type(e).__name__
        if name == "Timeout":
            return (False, "lock_timeout")
        logger.warning("llm_budget: unexpected error: %s: %s", name, e)
        return (False, "io_error")


def record_actual(module: str, actual_cost_usd: float) -> None:
    """Reconcile actual LLM cost against a prior reservation.

    - Adds actual to actual_usd[module].
    - Subtracts the equivalent from reserved_usd[module] (floored at 0).
      If actual > reserved, reserved becomes 0 (we can't "credit back"
      what was never there; the actual ledger takes over as source of truth).
    - Silently no-op on invalid inputs or errors (caller shouldn't crash
      after a successful LLM call).
    """
    if module not in _ALLOWED_MODULES:
        return
    try:
        actual = float(actual_cost_usd)
    except (TypeError, ValueError):
        return
    if actual < 0 or actual != actual:  # NaN
        return

    try:
        lock = _acquire()
    except ImportError:
        return

    try:
        with lock:
            _write_sentinel()
            try:
                state = _load_state()
                state = _maybe_daily_reset(state)
                state["actual_usd"][module] = (
                    state["actual_usd"].get(module, 0.0) + actual
                )
                prior_reserved = state["reserved_usd"].get(module, 0.0)
                state["reserved_usd"][module] = max(0.0, prior_reserved - actual)
                _save_state(state)
            finally:
                _clear_sentinel()
    except Exception as e:
        logger.warning("llm_budget: record_actual failed: %s", e)


def get_remaining_usd() -> float:
    """Remaining budget in USD. Never raises; returns 0.0 on I/O error.

    [LOG-R2-002 2026-04-20] Apply the same cap - 1e-9 epsilon used in
    check_and_reserve so remaining never reports more than what the
    reserve path will actually admit.
    """
    try:
        # Read-only; no lock needed for a best-effort snapshot
        state = _load_state()
        state = _maybe_daily_reset(state)
        cap = _daily_cap_usd()
        return max(0.0, (cap - 1e-9) - _pessimistic_spent(state))
    except Exception:
        return 0.0


def get_module_counters() -> dict:
    """Snapshot of per-module counters. Read-only, never raises."""
    try:
        state = _load_state()
        state = _maybe_daily_reset(state)
        out = {}
        modules = set(state.get("reserved_usd", {}).keys()) | set(
            state.get("actual_usd", {}).keys()
        )
        for m in modules:
            out[m] = {
                "reserved_usd": state["reserved_usd"].get(m, 0.0),
                "actual_usd": state["actual_usd"].get(m, 0.0),
            }
        return out
    except Exception:
        return {}


def reset_if_new_day() -> None:
    """Force a daily-reset check. Useful for long-running daemons that
    might hold in-memory state across midnight without hitting
    check_and_reserve.
    """
    try:
        lock = _acquire()
    except ImportError:
        return
    try:
        with lock:
            _write_sentinel()
            try:
                state = _load_state()
                before = state.get("day_key")
                state = _maybe_daily_reset(state)
                if state.get("day_key") != before:
                    _save_state(state)
            finally:
                _clear_sentinel()
    except Exception as e:
        logger.warning("llm_budget: reset_if_new_day failed: %s", e)


def refund(module: str, amount: float) -> bool:
    """Refund a previously-reserved amount back to the budget.

    P0-2 (2026-04-21): closes the reservation-leak loophole where
    check_and_reserve succeeded but the subsequent LLM call raised
    (FileNotFoundError, TimeoutExpired, parse_fail etc.) — record_actual
    was never called so reserved_usd monotonically climbed until UTC
    midnight or manual reset_reserves(). Callers should invoke refund()
    from the except branch after any exception between reserve and
    successful record_actual.

    - Subtracts amount from reserved_usd[module], floored at 0.
    - Silently no-op on invalid inputs (mirrors record_actual style —
      caller in exception-handler should never crash).
    - filelock-protected, same lock/sentinel as reserve/record_actual.

    Returns True if refund applied, False otherwise.
    """
    if module not in _ALLOWED_MODULES:
        return False
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    if amt <= 0 or amt != amt:  # negative or NaN
        return False
    try:
        lock = _acquire()
    except ImportError:
        return False
    try:
        with lock:
            _write_sentinel()
            try:
                state = _load_state()
                state = _maybe_daily_reset(state)
                reserved = state.setdefault("reserved_usd", {})
                current = reserved.get(module, 0.0)
                new_val = max(0.0, current - amt)
                reserved[module] = new_val
                try:
                    _save_state(state)
                except OSError as e:
                    logger.warning("llm_budget: refund save failed: %s", e)
                    return False
                return True
            finally:
                _clear_sentinel()
    except Exception as e:
        logger.warning("llm_budget: refund failed: %s: %s",
                       type(e).__name__, e)
        return False


def reset_reserves(module: Optional[str] = None) -> Dict:
    """Clear phantom `reserved_usd` counters left behind by failed subprocess
    calls (e.g., FileNotFoundError before record_actual).

    Holds the same filelock as check_and_reserve / record_actual so no race
    with live reservations. Does NOT touch actual_usd (those are real spend).
    Does NOT advance day_key.

    Args:
      module: if given, only reset that module's reserves. Else reset all 3.

    Returns: updated state dict.

    Usage: `python -m src.core.llm_budget reset-reserves [module]`
    """
    try:
        lock = _acquire()
    except ImportError:
        logger.warning("llm_budget: reset_reserves: filelock unavailable")
        return _blank_state()
    try:
        with lock:
            _write_sentinel()
            try:
                state = _load_state()
                state = _maybe_daily_reset(state)
                reserved = state.setdefault("reserved_usd", {})
                targets = [module] if module else sorted(_ALLOWED_MODULES)
                for m in targets:
                    if m not in _ALLOWED_MODULES:
                        continue
                    reserved[m] = 0.0
                _save_state(state)
                return state
            finally:
                _clear_sentinel()
    except Exception as e:
        # filelock.Timeout or I/O errors: don't crash CLI.
        logger.warning("llm_budget: reset_reserves failed: %s", e)
        return _blank_state()


def _cli(argv):
    """arch_v2 §F/§G playbook CLI: `python -m src.core.llm_budget status`."""
    import json as _json
    import sys as _sys
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "status":
        out = {
            "remaining_usd": round(get_remaining_usd(), 4),
            "daily_cap_usd": _daily_cap_usd(),
            "per_module": get_module_counters(),
        }
        print(_json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if cmd == "reset-reserves":
        module = argv[2] if len(argv) > 2 else None
        state = reset_reserves(module)
        print(_json.dumps({"reset": True, "module": module or "all",
                           "state": state}, ensure_ascii=False, indent=2))
        return 0
    print("usage: python -m src.core.llm_budget [status|reset-reserves [module]]", file=_sys.stderr)
    return 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli(_sys.argv))
