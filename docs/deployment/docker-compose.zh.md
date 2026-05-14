# Linux + Docker 部署

[English](docker-compose.md) · **中文**

> Linux 是社区支持 (maintainer 主要在 Win + macOS 开发)。大部分东西能用;
> cron 用 systemd timer 或 `docker compose` 周期任务代替。

## 1. 工具链

```bash
# Ubuntu / Debian
sudo apt-get install -y python3.11 python3.11-venv git docker.io docker-compose-plugin

# Arch
sudo pacman -S python git docker docker-compose

# 把自己加 docker 组免 sudo
sudo usermod -aG docker $USER
newgrp docker
```

## 2. Clone + 安装

```bash
git clone https://github.com/labazhou2024/memexa.git memexa
cd memexa
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## 3. 配置

```bash
cp .env.example .env
$EDITOR .env

mkdir -p ~/.memexa
cp config/aliases.example.yaml  ~/.memexa/aliases.yaml
cp config/identity.example.yaml ~/.memexa/identity.yaml
$EDITOR ~/.memexa/aliases.yaml
$EDITOR ~/.memexa/identity.yaml
```

## 4. 全套起起来

```bash
docker compose -f docker-compose.example.yml up -d
```

会起:

- `hindsight-api` 在 `:8888`
- `postgres` (含 pgvector extension) 在 `:5433`
- `bge-m3-sidecar` 在 `:18082`
- `memexa-dashboard` 在 `:8765`

## 5. 排 cron — systemd timer 配方

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
ExecStart=/home/%I/memexa/.venv/bin/python -m memexa.cron.cron_orchestrator run-incremental --all
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

启用:

```bash
sudo systemctl enable --now memexa-cron@$USER.timer
systemctl list-timers | grep memexa
```

## 6. Audio pipeline 备注

Audio driver 依赖 `mlx-whisper`, 只有 Apple Silicon 优化构建。Linux 上换
`openai-whisper` 或 `faster-whisper`:

```bash
pip install faster-whisper
export MEMEXA_ASR_BACKEND=faster-whisper
```

Driver 通过环境变量自动检测。

## 7. 卸载

```bash
sudo systemctl disable --now memexa-cron@$USER.timer
sudo rm /etc/systemd/system/memexa-cron.{service,timer}
docker compose -f docker-compose.example.yml down -v
```

## 8. 为啥 Linux 是社区档

原代码库在 Win + macOS (笔记本 + Mac Studio 部署) 长出来的。Linux 路径
存在但 maintainer 日常不跑。加真 cron 覆盖 / `systemd-user` unit 模板 /
`nix flake` 的 PR 非常欢迎。
