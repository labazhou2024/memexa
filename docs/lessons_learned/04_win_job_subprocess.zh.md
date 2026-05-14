# 4. Win subprocess timeout 需要 Job Object

[English](04_win_job_subprocess.md) · **中文**

> Windows 上 `subprocess.run(cmd, timeout=N)` 杀你 launch 的进程但**不**
> 杀**它**起的进程。如果孙进程阻塞在 socket I/O, 你"被杀"的子进程看起来
> 永远活着。修法是把 launch 包在 Win32 Job Object 里, 让内核负责拆掉
> 整棵树。

## 症状

6 小时 cron 循环本该 <60 分钟完。某次一个 driver 显示
`duration_ms = 15,062,997` — 4 小时 11 分钟 — 才被 Scheduled Task 的
`ExecutionTimeLimit` 终结。Python 层 timeout (`subprocess.run(timeout=2400)`
即 40 分钟) 没触发。

Triage:

- Python 进程是 Task Scheduler 起的 `pythonw.exe`
- 它子是另一个跑 driver entrypoint 的 `python.exe`
- Driver 的孙是 `httpx` client 阻塞在对远程 memory daemon 的 TCP 读。
  daemon 不可达 (host 重启了)
- `proc.wait(timeout=2400)` 从未返 `TimeoutExpired`

## 为啥 Python timeout 不触发

POSIX 上 `subprocess.wait(timeout=...)` 调 `waitpid(2)` + `SIGCHLD` 中断。
Windows 上用 `WaitForSingleObject` 在 child 进程 handle 上。Timeout 时
CPython 只杀 *直接* child。孙 — 用 httpx 的 driver — 有自己 handle, 接着
跑, 占着 parent 的 stdin/stdout pipe, 所以 parent 也看不到 child 的 EOF。

终态: parent "在跑" (技术上是 — 在等已被杀 child 的 pipe 关闭, 但孙还
拿着 pipe 不会关)。孙阻塞在永远不会完成的 TCP 读。3 小时后 Task
Scheduler 用自己机制把整棵树杀了。

## 修法: 包在 Win32 Job Object 里

Job Object 是内核级容器。每个分配给 Job 的进程, 以及那些进程起的每个
进程, 都被追踪。调 `TerminateJobObject` 原子地杀全部。

```python
# memexa/core/win_job_subprocess.py (简化版)
def run_with_job_object(cmd: list[str], timeout: float, **kwargs):
    job = _create_job_object(kill_on_close=True)

    proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_SUSPENDED, **kwargs)
    _assign_pid_to_job(job, proc.pid)
    _resume_thread(proc.pid)

    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_job_object(job)        # ← 杀 child + 所有孙
        raise
    finally:
        _close_handle(job)
```

有意思的点:

1. `CREATE_SUSPENDED` 让 child 暂停, 直到我们把它加进 Job。如果先 resume,
   child 可能在我们把它分进 Job 前就起了孙, 那个孙会逃出去
2. `kill_on_close=True` 意味着关 Job handle 杀 Job 里每个进程 — 对 Python
   解释器自己崩有用
3. 永远在 `finally` 调 `_close_handle(job)`, 跨多个 cron 循环不漏 Job
   handle

## 我们写的测试

```python
# tests/unit/test_win_job_subprocess.py (草图)
def test_grandchild_killed_on_timeout(tmp_path):
    """起 child 起孙 sleep 60s。设 timeout=2。timeout 后孙应该消失。"""
    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "import time; time.sleep(60)\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_with_job_object([sys.executable, str(spawner)], timeout=2.0)

    # 轮询 ~3 秒等孙消失
    deadline = time.time() + 3
    while time.time() < deadline:
        grandchildren = [
            p for p in psutil.process_iter(["cmdline"])
            if any("time.sleep(60)" in (a or "") for a in (p.info["cmdline"] or []))
        ]
        if not grandchildren:
            return
        time.sleep(0.1)
    pytest.fail("孙在 parent 被终止后仍活着")
```

## 这个教训泛化时

Windows 上调 `subprocess.run()` 或 `subprocess.Popen()` 且 child *可能*
起自己的 child 时都需要 Job Object。最常见情况:

- child 自己是 Python 进程 (你的每次 subprocess.Popen 起一个 child;
  那 child 再起就有孙)
- child 是 build 工具 (`bazel`, `cargo`, `cmake`) 扇出 worker
- child 是容器化进程 (`docker run`, `podman run`)

POSIX 上对应是 process group + `os.killpg(...)` on timeout。我们没实现
那条路径; 想加统一 wrapper 欢迎 PR。

## 注意: 老 Windows 上嵌套 Job

Windows 7 和 Windows Server 2008 R2 上一个进程一次只能在一个 Job 里。
如果我们 child 是 VS Code 集成终端起的, VS Code 可能已经把它分进自己
Job, 我们 `_assign_pid_to_job` 调用会失败。Windows 8+ 允许嵌套 Job,
不再是问题。

## 相关

- `memexa/core/win_job_subprocess.py` — 完整实现
- 微软 Job Object 文档:
  [`docs.microsoft.com/en-us/windows/win32/procthread/job-objects`](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- Python issue [bpo-34453](https://bugs.python.org/issue34453) — 请求
  `subprocess.run(timeout=...)` 杀整树 (关闭, API 稳定性原因不可行)
