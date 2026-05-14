# Linux + Docker deployment

**English** · [中文](docker-compose.zh.md)

> Linux is community-supported (the maintainer develops on Win + macOS).
> Most things work; cron is replaced by systemd timers or
> `docker compose` recurring jobs.

## 1. Tooling

```bash
# Ubuntu / Debian
sudo apt-get install -y python3.11 python3.11-venv git docker.io docker-compose-plugin

# Arch
sudo pacman -S python git docker docker-compose

# Add yourself to the docker group so you can run without sudo
sudo usermod -aG docker $USER
newgrp docker
```

## 2. Clone + install

```bash
git clone https://github.com/labazhou2024/memexa.git memexa
cd memexa
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## 3. Configure

```bash
cp .env.example .env
$EDITOR .env

mkdir -p ~/.memexa
cp config/aliases.example.yaml  ~/.memexa/aliases.yaml
cp config/identity.example.yaml ~/.memexa/identity.yaml
$EDITOR ~/.memexa/aliases.yaml
$EDITOR ~/.memexa/identity.yaml
```

## 4. Bring everything up

```bash
docker compose -f docker-compose.example.yml up -d
```

This brings up:

- `hindsight-api` on `:8888`
- `postgres` (with pgvector extension) on `:5433`
- `bge-m3-sidecar` on `:18082`
- `memexa-dashboard` on `:8765`

## 5. Schedule the cron — systemd timer recipe

```ini
# /etc/systemd/system/memexa-cron.service
[Unit]
Description=Memgraph 6-hour incremental cron
After=network.target

[Service]
Type=oneshot
User=%I
WorkingDirectory=/home/%I/memexa
EnvironmentFile=/home/%I/memexa/.env
ExecStart=/home/%I/memexa/.venv/bin/python -m src.cron.cron_orchestrator run-incremental --all
```

```ini
# /etc/systemd/system/memexa-cron.timer
[Unit]
Description=Run Memgraph cron every 6 hours

[Timer]
OnCalendar=00/6:30
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl enable --now memexa-cron@$USER.timer
systemctl list-timers | grep memexa
```

## 6. Audio pipeline note

The audio driver depends on `mlx-whisper`, which only ships
Apple-Silicon-optimised builds. On Linux, swap in `openai-whisper` or
`faster-whisper`:

```bash
pip install faster-whisper
export MEMEXA_ASR_BACKEND=faster-whisper
```

The driver auto-detects via the environment variable.

## 7. Uninstall

```bash
sudo systemctl disable --now memexa-cron@$USER.timer
sudo rm /etc/systemd/system/memexa-cron.{service,timer}
docker compose -f docker-compose.example.yml down -v
```

## 8. Why Linux is community-tier

The original codebase grew on Win + macOS for laptop + Mac Studio
deployment. Linux paths exist but are not exercised in the maintainer's
daily workflow. PRs that add real cron coverage, a `systemd-user` unit
template, or a `nix flake` are very welcome.
