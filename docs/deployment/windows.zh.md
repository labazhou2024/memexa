# Windows 部署

[English](windows.md) · **中文**

> Win 10 / Win 11 + PowerShell 5.1+ (PowerShell 7 也行)。

## 1. 工具链

```powershell
# Python 3.10+
winget install --id Python.Python.3.11

# Git
winget install --id Git.Git

# Docker Desktop (跑 Hindsight 后端用; 远程 PostgreSQL 可跳)
winget install --id Docker.DockerDesktop
```

## 2. Clone + 安装

```powershell
git clone https://github.com/labazhou2024/memex.git memex
cd memex
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 3. 配置

```powershell
Copy-Item .env.example .env
notepad .env

New-Item -ItemType Directory -Force $HOME\.memex | Out-Null
Copy-Item config\aliases.example.yaml  $HOME\.memex\aliases.yaml
Copy-Item config\identity.example.yaml $HOME\.memex\identity.yaml
notepad $HOME\.memex\aliases.yaml
notepad $HOME\.memex\identity.yaml
```

## 4. 起后端

```powershell
docker compose -f docker-compose.example.yml up -d
```

## 5. 注册 6 小时 Scheduled Task

`scripts/windows/register_cron_tasks.ps1` 为每个 source 注册一个任务。每个
任务跑 `cron_silent.py` (它设 `CREATE_NO_WINDOW` 避免弹窗闪烁) 包住 driver。

```powershell
.\scripts\windows\register_cron_tasks.ps1 -InstallAll
```

`\Memgraph\` task path 下创建的任务:

| Task                   | 调度            | Driver                                                |
|------------------------|-----------------|--------------------------------------------------------|
| `Memgraph_Cron6h`      | 6 h `00:30`     | `python -m src.cron.cron_orchestrator run-incremental --all` |
| `Memgraph_AudioIngest` | 6 h `00:45`     | `python -m src.drivers.backfill_v5_audio_driver`        |
| `Memgraph_Dashboard`   | 登录时          | `python -m src.dashboard.sys_monitor.server`            |

验证:

```powershell
Get-ScheduledTask -TaskPath '\Memgraph\' | Format-Table TaskName,State,LastRunTime,LastTaskResult
```

## 6. 子进程 timeout 陷阱

Windows 上 `subprocess.run(timeout=...)` 超时**不**杀孙进程。
`src/core/win_job_subprocess.py` 用 Win32 Job Object 包每个 cron-invoked
子进程, 孙进程跟父一起死。Driver 自动用这个 wrapper; 如果你自己从 Memgraph
内部 launch 子进程, 优先用 `from src.core.win_job_subprocess import run_with_job_object`。

见 [lessons_learned/04_win_job_subprocess.md](../lessons_learned/04_win_job_subprocess.md)。

## 7. 卸载

```powershell
.\scripts\windows\register_cron_tasks.ps1 -UninstallAll
docker compose -f docker-compose.example.yml down
```
