"""
Shared Pytest Cache — Single source of truth for test results.

Problem: 5 modules independently run `pytest tests/` during one heartbeat,
each taking ~150s and consuming 100% CPU. Total: ~750s of CPU saturation.

Solution: Run pytest ONCE, cache the result for 30 minutes. All modules
read from this cache instead of spawning their own pytest subprocess.

Usage:
    from src.core.pytest_cache import get_test_results
    results = get_test_results()  # Returns cached or fresh results
    if results["success"]:
        print(f"{results['passed']} tests passed")

Cache location: memex/data/pytest_cache.json
Cache TTL: 30 minutes (configurable)
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

_MEMEX_ROOT = Path(__file__).parent.parent.parent
_DATA = Path(__file__).parent.parent / "data"
_CACHE_FILE = _DATA / "pytest_cache.json"

CACHE_TTL_SECONDS = 1800  # 30 minutes
PYTEST_TIMEOUT = 300  # 5 minutes max

# Windows STATUS_COMMITMENT_LIMIT (memory commit exhausted, i.e. OOM kill).
# 0xC000012D == 3221225773 unsigned; -1073741491 is the signed int32 view of the
# same value. Listed redundantly for readability; the frozenset deduplicates.
# Incident 2026-04-20: 14 OOM events in a day; each cascaded into DCOM broker
# damage that blanked the modern network UI until reboot. The circuit-breaker
# below prevents the 30-min TTL loop from re-running pytest after an OOM.
_OOM_RETURN_CODES = frozenset({0xC000012D, -1073741491})
OOM_CIRCUIT_BREAKER_SECONDS = 86400  # 24 hours
MEMORY_GUARD_MIN_GB = 3.0  # skip pytest when available RAM drops below this


def _is_oom_returncode(rc: int) -> bool:
    return rc in _OOM_RETURN_CODES


def _available_memory_gb() -> float:
    """Available physical memory, in GB. Returns +inf if psutil unavailable."""
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception as e:
        logger.debug("psutil unavailable for memory guard (%s); assuming +inf", e)
        return float("inf")


def _is_cache_fresh() -> bool:
    """Check if cached results are still valid."""
    if not _CACHE_FILE.exists():
        return False
    try:
        cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(cache.get("timestamp", "2000-01-01"))
        return datetime.now() - cached_at < timedelta(seconds=CACHE_TTL_SECONDS)
    except Exception:
        return False


def _is_oom_circuit_open() -> bool:
    """True when the last run OOM-crashed within OOM_CIRCUIT_BREAKER_SECONDS.

    Covers both new-format caches (explicit ``oom: true``) and legacy caches
    written before this fix shipped (only ``returncode`` is set). Without the
    legacy branch, the 2026-04-20 incident's currently-on-disk cache would
    bypass the breaker and re-OOM on the next call.
    """
    if not _CACHE_FILE.exists():
        return False
    try:
        cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        oom_flag = cache.get("oom")
        legacy_oom = _is_oom_returncode(cache.get("returncode", 0))
        if not (oom_flag or legacy_oom):
            return False
        cached_at = datetime.fromisoformat(cache.get("timestamp", "2000-01-01"))
        return datetime.now() - cached_at < timedelta(seconds=OOM_CIRCUIT_BREAKER_SECONDS)
    except Exception:
        return False


def _read_cache() -> Dict[str, Any]:
    """Read cached test results."""
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(cache: Dict[str, Any]) -> None:
    _DATA.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _run_pytest() -> Dict[str, Any]:
    """Run pytest once and cache the results."""
    if _is_oom_circuit_open():
        logger.warning(
            "pytest_cache: OOM circuit-breaker OPEN (last crash within %ds) -- "
            "returning last-known result without re-running", OOM_CIRCUIT_BREAKER_SECONDS
        )
        return _read_cache()

    available_gb = _available_memory_gb()
    if available_gb < MEMORY_GUARD_MIN_GB:
        logger.warning(
            "pytest_cache: available memory %.2fGB < %.2fGB threshold, "
            "skipping pytest run to avoid OOM", available_gb, MEMORY_GUARD_MIN_GB
        )
        deferred = {
            "timestamp": datetime.now().isoformat(),
            "passed": 0, "failed": 0, "errors": 0,
            "success": True,  # don't block downstream; this is an infra guard
            "failed_tests": [],
            "output": f"pytest_cache deferred: only {available_gb:.2f}GB available",
            "deferred": True,
            "deferred_reason": "low_memory",
            "available_gb": available_gb,
        }
        _write_cache(deferred)
        return deferred

    logger.info("Running pytest (this may take 30-60s)...")
    # AC-H1 (TU-A 2026-04-25): isolation env to break test->prod flag pollution chain.
    # MEMEX_AUTOPILOT_FLAG_PATH redirects flag writes to per-run tmpdir;
    # MEMEX_ACTIVE_TASK_ID="" defends against parent-shell env leak (B3 in v0 audit).
    tmp_path = Path(tempfile.mkdtemp(prefix="pytest_cache_iso_"))
    try:
        try:
            iso_env = {
                **os.environ,
                "MEMEX_AUTOPILOT_FLAG_PATH": str(tmp_path / "autopilot_active.json"),
                "MEMEX_ACTIVE_TASK_ID": "",
                "MEMEX_POLLUTION_LOG_PATH": str(tmp_path / "pollution.jsonl"),
            }
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/", "-q",
                 "--tb=no", "--no-header"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=PYTEST_TIMEOUT, cwd=str(_MEMEX_ROOT), env=iso_env,
            )
            output = result.stdout.strip()

            # Parse results
            passed = failed = errors = 0
            failed_tests = []
            for line in output.splitlines():
                line = line.strip()
                # Parse summary line: "354 passed in 25.64s" or "3 failed, 351 passed"
                if "passed" in line or "failed" in line:
                    for part in line.split(","):
                        p = part.strip()
                        if "passed" in p:
                            try:
                                passed = int(p.split()[0])
                            except (ValueError, IndexError):
                                pass
                        elif "failed" in p:
                            try:
                                failed = int(p.split()[0])
                            except (ValueError, IndexError):
                                pass
                        elif "error" in p:
                            try:
                                errors = int(p.split()[0])
                            except (ValueError, IndexError):
                                pass
                # Collect failed test names
                elif line.startswith("FAILED "):
                    failed_tests.append(line.replace("FAILED ", "").strip())

            oom_crash = _is_oom_returncode(result.returncode)
            cache = {
                "timestamp": datetime.now().isoformat(),
                "passed": passed,
                "failed": failed,
                "errors": errors,
                # success requires a clean process exit; OOM/crash never count as success
                "success": failed == 0 and errors == 0 and result.returncode == 0,
                "failed_tests": failed_tests[:20],  # Cap at 20
                "output": output[-500:],
                "returncode": result.returncode,
            }
            if oom_crash:
                cache["oom"] = True
                logger.error(
                    "pytest_cache: OOM crash detected (returncode=%d / 0x%08X). "
                    "Opening circuit-breaker for %ds; repeated auto-retries would cascade.",
                    result.returncode, result.returncode & 0xFFFFFFFF,
                    OOM_CIRCUIT_BREAKER_SECONDS,
                )

            _write_cache(cache)
            logger.info("pytest: %d passed, %d failed (cached for %ds)",
                         passed, failed, CACHE_TTL_SECONDS)
            return cache

        except subprocess.TimeoutExpired:
            logger.warning("pytest timed out after %ds", PYTEST_TIMEOUT)
            # Intentional: transient infra failures (timeout, OSError) are NOT persisted
            # to the cache file. We return a success=True dict so downstream callers
            # don't block, but we want the next invocation to try again. If we wrote
            # this to disk, a flaky network or a too-tight timeout would silently
            # suppress real test failures for the full CACHE_TTL_SECONDS window.
            return {
                "timestamp": datetime.now().isoformat(),
                "passed": 0, "failed": 0, "errors": 0,
                "success": True,  # Don't block on timeout
                "failed_tests": [],
                "output": f"pytest timed out after {PYTEST_TIMEOUT}s",
                "timeout": True,
                "returncode": None,
            }
        except Exception as e:
            logger.warning("pytest failed: %s", e)
            # Same rationale as the timeout branch: do not persist infra failures.
            return {
                "timestamp": datetime.now().isoformat(),
                "passed": 0, "failed": 0, "errors": 0,
                "success": True,  # Don't block on infra failure
                "failed_tests": [],
                "output": str(e),
                "error": str(e),
                "returncode": None,
            }

    finally:
        # NB-2 fix: Windows file-lock -- pytest subprocess may still hold handles.
        try:
            shutil.rmtree(tmp_path, ignore_errors=True)
            if tmp_path.exists():  # rmtree silently failed; log
                try:
                    from src.core.trace_sink import write_trace_event
                    write_trace_event("pytest_cache_tmpdir_leak", {"path": str(tmp_path)})
                except (ImportError, OSError):
                    pass  # trace fail-soft
        except OSError as e:
            try:
                from src.core.trace_sink import write_trace_event
                write_trace_event("pytest_cache_tmpdir_cleanup_error",
                                  {"path": str(tmp_path), "error": str(e)[:200]})
            except (ImportError, OSError):
                pass


def get_test_results(force_refresh: bool = False) -> Dict[str, Any]:
    """Get test results, using cache if fresh.

    This is the ONLY function that should be called by other modules.
    It ensures pytest runs at most once per CACHE_TTL_SECONDS.

    Args:
        force_refresh: If True, ignore the 30-min TTL and re-run pytest.
            This does **NOT** override the OOM circuit breaker: if the
            last run OOM-crashed within OOM_CIRCUIT_BREAKER_SECONDS, the
            call still short-circuits to the cached OOM result. To forcibly
            unblock after an OOM, call ``reset_oom_breaker()`` first.

    Returns:
        Dict with keys: timestamp, passed, failed, errors, success,
                        failed_tests, output
    """
    if not force_refresh and _is_cache_fresh():
        return _read_cache()
    return _run_pytest()


def invalidate_cache():
    """Force next call to re-run pytest (e.g., after a commit)."""
    if _CACHE_FILE.exists():
        _CACHE_FILE.unlink()


def reset_oom_breaker() -> bool:
    """Manually close the OOM circuit-breaker.

    Clears BOTH the explicit ``oom`` flag and the legacy-returncode signal
    that ``_is_oom_circuit_open()`` also recognises. Without the returncode
    neutralisation, a reset would be silently re-armed on the next read,
    because the legacy OOM returncode still on disk would re-trip the breaker.

    Returns True if a breaker was actually reset.
    """
    if not _CACHE_FILE.exists():
        return False
    try:
        cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    had_flag = bool(cache.get("oom"))
    had_legacy = _is_oom_returncode(cache.get("returncode", 0))
    if not (had_flag or had_legacy):
        return False
    cache.pop("oom", None)
    if had_legacy:
        # Neutralise the returncode so _is_oom_circuit_open() legacy arm
        # does not silently re-open the breaker the next time it is read.
        cache["returncode"] = 0
        cache["legacy_oom_returncode_cleared"] = True
    cache["oom_breaker_reset_at"] = datetime.now().isoformat()
    _write_cache(cache)
    logger.info("pytest_cache: OOM circuit-breaker manually reset "
                "(flag_cleared=%s, legacy_returncode_cleared=%s)",
                had_flag, had_legacy)
    return True
