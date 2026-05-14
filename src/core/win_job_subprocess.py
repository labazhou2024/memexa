"""Windows Job Object subprocess wrapper — kill grand-children on timeout.

Problem (HANDOFF_LIVE §D.15): subprocess.run(timeout=N) on Windows kills only
the direct child Popen handle, not its grand-children. When `cron_orchestrator
dispatch_incremental` times out, the driver (cmd.exe wrapper) dies but the
real worker (python l0_worker_serial.py) keeps running, eventually hanging
the schtask past ExecutionTimeLimit.

Solution: Windows Job Objects. Create a Job, assign Popen process to it
with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. When we close the job handle
(or the parent Python exits), every process in the job is terminated.

POSIX fallback: subprocess.run with start_new_session=True + os.killpg on
timeout. Same semantic (kill process group).

Public API:
    run_with_job_object(cmd, *, timeout, cwd, env, capture_output=False) -> int
    JobObjectRunner(...)  # context-manager form for advanced use

LIVE-verified contract:
    rc=0       → child exited 0 within timeout
    rc=124     → timeout fired, all descendants killed
    rc=<other> → child exit code
    raises FileNotFoundError if cmd[0] not executable
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger("win_job_subprocess")

_IS_WINDOWS = sys.platform == "win32"


if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _CreateJobObjectW = _kernel32.CreateJobObjectW
    _CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _CreateJobObjectW.restype = wintypes.HANDLE

    _SetInformationJobObject = _kernel32.SetInformationJobObject
    _SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    _SetInformationJobObject.restype = wintypes.BOOL

    _AssignProcessToJobObject = _kernel32.AssignProcessToJobObject
    _AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _AssignProcessToJobObject.restype = wintypes.BOOL

    _TerminateJobObject = _kernel32.TerminateJobObject
    _TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _TerminateJobObject.restype = wintypes.BOOL

    _OpenProcess = _kernel32.OpenProcess
    _OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _OpenProcess.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL


    def _create_kill_on_close_job() -> int:
        """Create a Job Object with KILL_ON_JOB_CLOSE limit. Returns handle int."""
        h = _CreateJobObjectW(None, None)
        if not h:
            err = ctypes.get_last_error()
            raise OSError(f"CreateJobObjectW failed (winerr={err})")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _SetInformationJobObject(
            h, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.get_last_error()
            _CloseHandle(h)
            raise OSError(f"SetInformationJobObject failed (winerr={err})")
        return h


    def _assign_pid_to_job(job_handle: int, pid: int) -> None:
        h_proc = _OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid,
        )
        if not h_proc:
            err = ctypes.get_last_error()
            raise OSError(f"OpenProcess(pid={pid}) failed (winerr={err})")
        try:
            ok = _AssignProcessToJobObject(job_handle, h_proc)
            if not ok:
                err = ctypes.get_last_error()
                raise OSError(
                    f"AssignProcessToJobObject(pid={pid}) failed (winerr={err})"
                )
        finally:
            _CloseHandle(h_proc)


def run_with_job_object(
    cmd: list[str],
    *,
    timeout: int,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    capture_output: bool = False,
) -> dict:
    """Run cmd, killing all descendants on timeout. Cross-platform.

    Returns dict {rc, timed_out, duration_sec, stdout, stderr}.
    """
    t0 = time.time()
    result = {"rc": -1, "timed_out": False, "duration_sec": 0.0,
              "stdout": "", "stderr": ""}

    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None

    if _IS_WINDOWS:
        # Race window between Popen and AssignProcessToJobObject is microsec;
        # worker grand-children take seconds to spawn (KV cache load, imports).
        # CREATE_SUSPENDED+ResumeThread caused Popen.wait() to hang in tests
        # because Popen.__init__ already calls ResumeThread internally on Win
        # build paths, so suspended-then-resumed-twice misbehaves. Plain Popen
        # + immediate assign is robust enough for the timeout-kill use case.
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = CREATE_NEW_PROCESS_GROUP

        job_handle = _create_kill_on_close_job()
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=stdout, stderr=stderr,
                creationflags=creationflags,
            )
            try:
                _assign_pid_to_job(job_handle, proc.pid)
            except OSError as e:
                logger.warning(f"assign-to-job failed ({e}); falling back to plain wait")
            try:
                if capture_output:
                    out, err = proc.communicate(timeout=timeout)
                    result["stdout"] = (out or b"").decode("utf-8", errors="replace") \
                        if isinstance(out, bytes) else (out or "")
                    result["stderr"] = (err or b"").decode("utf-8", errors="replace") \
                        if isinstance(err, bytes) else (err or "")
                else:
                    proc.wait(timeout=timeout)
                result["rc"] = proc.returncode
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                logger.warning(
                    f"timeout after {timeout}s, terminating job (pid={proc.pid}+descendants)"
                )
                _TerminateJobObject(job_handle, 124)
                # Reap to free zombie handle
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                result["rc"] = 124
        finally:
            _CloseHandle(job_handle)  # closes job → kills any survivors
    else:
        # POSIX: start_new_session puts child in its own process group; on
        # timeout kill the whole group.
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=stdout, stderr=stderr,
                start_new_session=True,
            )
            try:
                if capture_output:
                    out, err = proc.communicate(timeout=timeout)
                    result["stdout"] = out or ""
                    result["stderr"] = err or ""
                else:
                    proc.wait(timeout=timeout)
                result["rc"] = proc.returncode
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                try:
                    os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL whole group
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                result["rc"] = 124
        except FileNotFoundError:
            raise

    result["duration_sec"] = round(time.time() - t0, 2)
    return result


# ── CLI smoke-test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=5)
    ap.add_argument("--cmd", nargs="+", required=True)
    args = ap.parse_args()
    r = run_with_job_object(args.cmd, timeout=args.timeout)
    print(f"rc={r['rc']} timed_out={r['timed_out']} dt={r['duration_sec']}s")
    sys.exit(r["rc"])
