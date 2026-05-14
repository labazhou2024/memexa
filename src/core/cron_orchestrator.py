"""cron_orchestrator.py — pure dispatcher for all 11 incremental drivers (R-5).
   (count synced 2026-05-13: manifest now has wechat/qq/email/cc/browser/audio/
   diary/lab/research/structured/traces — sums to 11)

Reads cron_manifest.yaml, dispatches `python -m <driver_module> --incremental ...`
for each registered driver. Emits cron_orchestrator_dispatch trace events.

CLI:
  python -m src.core.cron_orchestrator list-drivers
  python -m src.core.cron_orchestrator run-incremental --driver diary
  python -m src.core.cron_orchestrator run-incremental --all
  python -m src.core.cron_orchestrator run-incremental --all --dry-run
  python -m src.core.cron_orchestrator validate-manifest
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _emit(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def _load_manifest():
    from src.core.cron_manifest import load
    return load()


def _current_host() -> str:
    """Return 'win' or 'mac' based on running platform."""
    sys_name = platform.system().lower()
    if sys_name == "windows":
        return "win"
    elif sys_name == "darwin":
        return "mac"
    else:
        # Linux treated as mac for CI/test purposes
        return "mac"


def dispatch_incremental(
    driver_id: str,
    dry_run: bool = False,
    extra_args: Optional[List[str]] = None,
    timeout: int = 2400,
) -> int:
    """Dispatch a single driver by id via --incremental subprocess.

    Args:
        driver_id: id string from manifest (e.g. "diary")
        dry_run: if True, pass --dry-run to driver as well
        extra_args: additional CLI args to append
        timeout: subprocess timeout in seconds (default 20 min)

    Returns:
        Exit code (0 = success, non-zero = failure)

    Raises:
        ValueError: if driver_id not found in manifest (BLOCK with explicit error)
    """
    manifest = _load_manifest()
    entry = manifest.get_driver_by_id(driver_id)
    if entry is None:
        known = [d.id for d in manifest.drivers]
        raise ValueError(
            f"Driver {driver_id!r} absent from cron_manifest.yaml — "
            f"BLOCKED. Known drivers: {known}"
        )

    cmd = [sys.executable, "-m", entry.driver_module] + list(entry.incremental_args)
    if dry_run:
        cmd.append("--dry-run")
    if extra_args:
        cmd.extend(extra_args)

    env = {**os.environ, "PYTHONPATH": str(_REPO), "PYTHONIOENCODING": "utf-8"}

    t0 = time.time()
    print(
        f"[cron_orchestrator] dispatch {driver_id!r} host={entry.host} "
        f"cmd={' '.join(cmd[:4])}... dry_run={dry_run}",
        flush=True,
    )

    if dry_run and "--dry-run" in cmd:
        # If driver already has --dry-run we just run it
        pass

    # 2026-05-11 v3 (Phase 2.4): use Win Job Object to kill grand-children on
    # timeout. subprocess.run(timeout=N) on Win only kills the direct Popen
    # handle — worker grand-children (l0_worker_serial.py) survive and hang the
    # schtask past ExecutionTimeLimit (HANDOFF §D.15). On POSIX, falls back to
    # start_new_session + killpg with identical semantics.
    try:
        from src.core.win_job_subprocess import run_with_job_object
        r = run_with_job_object(
            cmd, timeout=timeout, cwd=str(_REPO), env=env,
            capture_output=False,
        )
        rc = r["rc"]
        if r["timed_out"]:
            print(
                f"[cron_orchestrator] TIMEOUT driver={driver_id} after "
                f"{r['duration_sec']:.1f}s — job + descendants killed",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[cron_orchestrator] ERROR driver={driver_id}: {exc}", file=sys.stderr)
        rc = 1

    duration_ms = int((time.time() - t0) * 1000)
    _emit("cron_orchestrator_dispatch", {
        "driver_id": driver_id,
        "driver_module": entry.driver_module,
        "host": entry.host,
        "exit_code": rc,
        "duration_ms": duration_ms,
        "dry_run": dry_run,
    })
    print(
        f"[cron_orchestrator] {driver_id!r} exit={rc} duration={duration_ms}ms",
        flush=True,
    )
    return rc


def run_all_incremental(
    host_filter: Optional[str] = None,
    dry_run: bool = False,
    extra_args: Optional[List[str]] = None,
) -> dict:
    """Dispatch all drivers matching host_filter.

    Args:
        host_filter: 'win' | 'mac' | None (defaults to current host)
        dry_run: passed through to each driver
        extra_args: additional args for each driver

    Returns:
        dict mapping driver_id -> exit_code
    """
    host = host_filter or _current_host()
    manifest = _load_manifest()
    drivers = manifest.get_drivers_by_host(host)

    if not drivers:
        print(f"[cron_orchestrator] no drivers found for host={host!r}")
        return {}

    results: dict = {}
    for entry in drivers:
        # 2026-05-12: skip drivers that own their schtask (e.g. audio →
        # AudioIngest6h). Prevents double-run + GraphMaintenance6h 3h
        # ExecutionTimeLimit blowout from long ASR pipelines.
        if getattr(entry, "skip_in_orchestrator", False):
            print(
                f"[cron_orchestrator] SKIP {entry.id} "
                f"(skip_in_orchestrator=true — owned by dedicated schtask)",
                flush=True,
            )
            results[entry.id] = 0  # treat as ok (delegated)
            continue
        try:
            rc = dispatch_incremental(
                driver_id=entry.id,
                dry_run=dry_run,
                extra_args=extra_args,
            )
        except ValueError as exc:
            print(f"[cron_orchestrator] BLOCK {entry.id}: {exc}", file=sys.stderr)
            rc = 2
        results[entry.id] = rc

    n_ok = sum(1 for v in results.values() if v == 0)
    n_fail = len(results) - n_ok
    _emit("cron_orchestrator_run_all", {
        "host": host,
        "n_drivers": len(drivers),
        "n_ok": n_ok,
        "n_fail": n_fail,
        "dry_run": dry_run,
        "results": results,
    })
    print(
        f"[cron_orchestrator] run_all host={host} "
        f"n={len(drivers)} ok={n_ok} fail={n_fail}",
        flush=True,
    )
    return results


def list_drivers() -> None:
    """Print all driver entries from manifest (currently 11, was 8 pre-2026-05)."""
    manifest = _load_manifest()
    print(f"cron_manifest.yaml version={manifest.version}  "
          f"drivers={len(manifest.drivers)}")
    print("-" * 60)
    for d in manifest.drivers:
        print(
            f"  {d.id:<12} host={d.host:<4}  "
            f"schedule={d.schedule!r:<20}  "
            f"module={d.driver_module}"
        )
    print("-" * 60)
    print(f"  {len(manifest.schtasks)} schtask entries: "
          f"{[s.id for s in manifest.schtasks]}")


def validate_manifest() -> int:
    """Validate manifest and print summary. Returns 0 on success, 1 on failure."""
    try:
        manifest = _load_manifest()
        print(f"OK: version={manifest.version}, drivers={len(manifest.drivers)}, "
              f"schtasks={len(manifest.schtasks)}")
        # 2026-05-10: incremental_args is now optional (some drivers — wechat/qq/email v5
        # — manage their own cursor and don't need --incremental). Schema enforces 0+ items.
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


def _cli() -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.core.cron_orchestrator",
        description="memexa cron orchestrator — dispatch incremental drivers",
    )
    sub = p.add_subparsers(dest="cmd")

    # list-drivers
    sub.add_parser("list-drivers", help="List all 8 registered drivers")

    # validate-manifest
    sub.add_parser("validate-manifest", help="Validate cron_manifest.yaml")

    # run-incremental
    run_p = sub.add_parser("run-incremental", help="Run drivers incrementally")
    grp = run_p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--driver", metavar="ID", help="Single driver id")
    grp.add_argument("--all", action="store_true", help="All drivers for current host")
    run_p.add_argument("--host", default=None, help="Host filter: win|mac (default: current)")
    run_p.add_argument("--dry-run", action="store_true", help="Pass --dry-run to drivers")

    args = p.parse_args()
    if args.cmd == "list-drivers":
        list_drivers()
        return 0
    elif args.cmd == "validate-manifest":
        return validate_manifest()
    elif args.cmd == "run-incremental":
        if args.all:
            results = run_all_incremental(
                host_filter=args.host,
                dry_run=args.dry_run,
            )
            failed = [k for k, v in results.items() if v != 0]
            if failed:
                print(f"FAILED drivers: {failed}", file=sys.stderr)
                return 1
            return 0
        else:
            try:
                rc = dispatch_incremental(
                    driver_id=args.driver,
                    dry_run=args.dry_run,
                )
                return rc
            except ValueError as exc:
                print(f"BLOCK: {exc}", file=sys.stderr)
                return 2
    else:
        p.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(_cli())
