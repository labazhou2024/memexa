# macOS deployment

**English** · [中文](macos.zh.md)

> macOS 13+ with Homebrew. Apple Silicon recommended (MLX acceleration
> in the audio pipeline).

## 1. Tooling

```bash
brew install python@3.11 git docker
brew install --cask docker  # if you don't already have Docker Desktop
```

## 2. Clone + install

```bash
git clone https://github.com/labazhou2024/memex.git memex
cd memex
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## 3. Configure

```bash
cp .env.example .env
$EDITOR .env

mkdir -p ~/.memex
cp config/aliases.example.yaml  ~/.memex/aliases.yaml
cp config/identity.example.yaml ~/.memex/identity.yaml
$EDITOR ~/.memex/aliases.yaml
$EDITOR ~/.memex/identity.yaml
```

## 4. Start the backend

```bash
docker compose -f docker-compose.example.yml up -d
curl -sf http://127.0.0.1:8888/healthz
```

## 5. Register launchd jobs

`scripts/macos/install_launchd.sh` installs one plist per long-running
driver and the dashboard.

```bash
bash scripts/macos/install_launchd.sh
```

Installed:

| Label                                 | Schedule  | Job                                              |
|---------------------------------------|-----------|--------------------------------------------------|
| `org.memex.cron6h`                  | 6 h       | `python -m src.cron.cron_orchestrator run-incremental --all` |
| `org.memex.audio_recorder_watch`    | 2 min     | Pulls new files from `data/audio/inbox/`, runs ASR |
| `org.memex.dashboard`               | KeepAlive | `python -m src.dashboard.sys_monitor.server`       |

Verify:

```bash
launchctl list | grep org.memex
```

## 6. MLX whisper offline mode

The audio pipeline imports `mlx-whisper`. By default Hugging Face Hub is
consulted on first run; this stalls if your network is slow. The
installer sets `HF_HUB_OFFLINE=1` once the model is downloaded.

If you see the audio job hang for >5 min during the very first run,
manually download the model:

```bash
huggingface-cli download mlx-community/whisper-large-v3-turbo
export HF_HUB_OFFLINE=1
```

## 7. Uninstall

```bash
bash scripts/macos/uninstall_launchd.sh
docker compose -f docker-compose.example.yml down
```
