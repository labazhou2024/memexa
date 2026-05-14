# Windows deployment

**English** · [中文](windows.zh.md)

> Win 10 / Win 11 with PowerShell 5.1+ (PowerShell 7 also works).

## 1. Tooling

```powershell
# Python 3.10+
winget install --id Python.Python.3.11

# Git
winget install --id Git.Git

# Docker Desktop (for Hindsight backend; skip if you have a remote PostgreSQL)
winget install --id Docker.DockerDesktop
```

## 2. Clone + install

```powershell
git clone https://github.com/labazhou2024/memexa.git memexa
cd memexa
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 3. Configure

```powershell
Copy-Item .env.example .env
notepad .env

New-Item -ItemType Directory -Force $HOME\.memexa | Out-Null
Copy-Item config\aliases.example.yaml  $HOME\.memexa\aliases.yaml
Copy-Item config\identity.example.yaml $HOME\.memexa\identity.yaml
notepad $HOME\.memexa\aliases.yaml
notepad $HOME\.memexa\identity.yaml
```

## 4. Start the backend

```powershell
docker compose -f docker-compose.example.yml up -d
```

## 5. Register six-hour scheduled tasks

`scripts/windows/register_cron_tasks.ps1` registers one task per source.
Each task runs `cron_silent.py` (which sets `CREATE_NO_WINDOW` to avoid
popup flicker) wrapping the driver.

```powershell
.\scripts\windows\register_cron_tasks.ps1 -InstallAll
```

Tasks created under `\Memgraph\` task path:

| Task                  | Schedule        | Driver                                                |
|-----------------------|-----------------|--------------------------------------------------------|
| `Memgraph_Cron6h`      | 6 h `00:30`     | `python -m memexa.cron.cron_orchestrator run-incremental --all` |
| `Memgraph_AudioIngest` | 6 h `00:45`     | `python -m memexa.drivers.backfill_v5_audio_driver`        |
| `Memgraph_Dashboard`   | At logon        | `python -m memexa.dashboard.sys_monitor.server`            |

Verify with:

```powershell
Get-ScheduledTask -TaskPath '\Memgraph\' | Format-Table TaskName,State,LastRunTime,LastTaskResult
```

## 6. Subprocess timeout gotcha

`subprocess.run(timeout=...)` on Windows does **not** kill grandchild
processes when the timeout fires. `memexa/core/win_job_subprocess.py`
wraps every cron-invoked subprocess in a Win32 Job Object so the
grandchild is killed alongside the parent. Drivers use this wrapper
automatically; if you launch your own subprocess from within Memgraph,
prefer `from memexa.core.win_job_subprocess import run_with_job_object`.

See [lessons_learned/04_win_job_subprocess.md](../lessons_learned/04_win_job_subprocess.md).

## 7. Uninstall

```powershell
.\scripts\windows\register_cron_tasks.ps1 -UninstallAll
docker compose -f docker-compose.example.yml down
```
