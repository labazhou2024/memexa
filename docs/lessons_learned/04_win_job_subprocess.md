# 4. Win subprocess timeout requires Job Object

**English** · [中文](04_win_job_subprocess.zh.md)

> `subprocess.run(cmd, timeout=N)` on Windows kills the process you
> launched but not the processes *it* launched. If the grandchild is
> blocked on socket I/O, your "killed" subprocess looks alive forever.
> Fix is to wrap the launch in a Win32 Job Object so the kernel is
> responsible for tearing down the whole tree.

## Symptom

A 6-hour cron cycle was supposed to finish in <60 minutes. One
particular run showed `duration_ms = 15,062,997` — four hours, eleven
minutes — for a single driver, before the Scheduled Task's
`ExecutionTimeLimit` finally killed it. The Python-level timeout
(`subprocess.run(timeout=2400)`, i.e. 40 minutes) had not fired.

Triage:

- The Python process was `pythonw.exe` started by Task Scheduler.
- Its child was another `python.exe` running the driver entrypoint.
- The driver's grandchild was an `httpx` client blocked on a TCP read
  to the remote memory daemon. The daemon was unreachable (host had
  rebooted).
- `proc.wait(timeout=2400)` never returned `TimeoutExpired`.

## Why the Python timeout did not fire

`subprocess.wait(timeout=...)` on POSIX calls `waitpid(2)` with
`SIGCHLD` interruption. On Windows, it uses `WaitForSingleObject` on
the child process handle. When that times out, CPython only kills the
*direct* child. The grandchild — the httpx-using driver — has its own
handle and keeps running, holding open the parent's stdin/stdout pipes,
which means the parent never sees EOF from its child either.

End state: the parent is "running" (technically — it is waiting for the
already-killed child's pipes to close, which they will not because the
grandchild still has them). The grandchild is blocked on a TCP read
that will never complete. Three hours later Task Scheduler comes along
and kills the whole thing via its own mechanism.

## Fix: wrap in a Win32 Job Object

A Job Object is a kernel-level container. Every process assigned to
the Job, and every process those processes spawn, is tracked. Calling
`TerminateJobObject` kills them all atomically.

```python
# src/core/win_job_subprocess.py (simplified)
def run_with_job_object(cmd: list[str], timeout: float, **kwargs):
    job = _create_job_object(kill_on_close=True)

    proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_SUSPENDED, **kwargs)
    _assign_pid_to_job(job, proc.pid)
    _resume_thread(proc.pid)

    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_job_object(job)        # ← kills child + all grandchildren
        raise
    finally:
        _close_handle(job)
```

The interesting bits:

1. `CREATE_SUSPENDED` keeps the child paused until we add it to the
   Job. If we resumed first, the child could spawn a grandchild before
   we assigned it to the Job, and that grandchild would escape.
2. `kill_on_close=True` means closing the Job handle terminates every
   process in it — useful if the Python interpreter itself crashes.
3. Always call `_close_handle(job)` in `finally` so we do not leak Job
   handles across many cron cycles.

## Tests we wrote

```python
# tests/unit/test_win_job_subprocess.py (sketch)
def test_grandchild_killed_on_timeout(tmp_path):
    """Spawn child that spawns grandchild that sleeps 60s.
    Set timeout=2. After timeout, grandchild must be gone."""
    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "import time; time.sleep(60)\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_with_job_object([sys.executable, str(spawner)], timeout=2.0)

    # poll for ~3 seconds for the grandchild to disappear
    deadline = time.time() + 3
    while time.time() < deadline:
        grandchildren = [
            p for p in psutil.process_iter(["cmdline"])
            if any("time.sleep(60)" in (a or "") for a in (p.info["cmdline"] or []))
        ]
        if not grandchildren:
            return
        time.sleep(0.1)
    pytest.fail("grandchild survived parent termination")
```

## When the lesson generalises

Any time you call `subprocess.run()` or `subprocess.Popen()` on
Windows and the child *might* spawn its own children, you need a Job
Object. This is most commonly the case when:

- The child is itself a Python process (every Python subprocess.Popen
  creates a child of yours; if that child spawns again, you have
  grandchildren).
- The child is a build tool (`bazel`, `cargo`, `cmake`) that fans
  out workers.
- The child is a containerised process (`docker run`, `podman run`).

On POSIX, the equivalent is a process group + `os.killpg(...)` on
timeout. We did not implement that path; if someone wants to add a
unified wrapper, PRs welcome.

## Caveat: nested Job Objects on older Windows

On Windows 7 and Windows Server 2008 R2, a process can only be in one
Job at a time. If our child is launched by Visual Studio Code's
integrated terminal, VS Code may have already assigned it to its own
Job, and our `_assign_pid_to_job` call will fail. Windows 8+ allows
nested Jobs and this is no longer a concern.

## See also

- `src/core/win_job_subprocess.py` — full implementation.
- Microsoft docs on Job Objects:
  [`docs.microsoft.com/en-us/windows/win32/procthread/job-objects`](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- Python issue [bpo-34453](https://bugs.python.org/issue34453) — request
  for `subprocess.run(timeout=...)` to kill the whole tree (closed,
  fix not feasible due to API stability).
