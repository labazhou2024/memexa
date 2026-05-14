# macOS 部署

[English](macos.md) · **中文**

> macOS 13+ 加 Homebrew。推荐 Apple Silicon (audio pipeline 用 MLX 加速)。

## 1. 工具链

```bash
brew install python@3.11 git docker
brew install --cask docker  # 没装 Docker Desktop 就装
```

## 2. Clone + 安装

```bash
git clone https://github.com/labazhou2024/memex.git memex
cd memex
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## 3. 配置

```bash
cp .env.example .env
$EDITOR .env

mkdir -p ~/.memex
cp config/aliases.example.yaml  ~/.memex/aliases.yaml
cp config/identity.example.yaml ~/.memex/identity.yaml
$EDITOR ~/.memex/aliases.yaml
$EDITOR ~/.memex/identity.yaml
```

## 4. 起后端

```bash
docker compose -f docker-compose.example.yml up -d
curl -sf http://127.0.0.1:8888/healthz
```

## 5. 注册 launchd 任务

`scripts/macos/install_launchd.sh` 为每个长跑 driver 和 dashboard 安一个
plist。

```bash
bash scripts/macos/install_launchd.sh
```

安装的:

| Label                                 | 调度       | 任务                                              |
|---------------------------------------|-----------|--------------------------------------------------|
| `org.memex.cron6h`                    | 6 小时    | `python -m src.cron.cron_orchestrator run-incremental --all` |
| `org.memex.audio_recorder_watch`      | 2 分钟    | 从 `data/audio/inbox/` 拉新文件, 跑 ASR              |
| `org.memex.dashboard`                 | KeepAlive | `python -m src.dashboard.sys_monitor.server`       |

验证:

```bash
launchctl list | grep org.memex
```

## 6. MLX whisper 离线模式

Audio pipeline 引用 `mlx-whisper`。默认首次跑会去 Hugging Face Hub 查; 网
慢时会 stall。安装脚本下完模型后设 `HF_HUB_OFFLINE=1`。

如果 audio job 首次运行卡 >5 分钟, 手动下:

```bash
huggingface-cli download mlx-community/whisper-large-v3-turbo
export HF_HUB_OFFLINE=1
```

## 7. 卸载

```bash
bash scripts/macos/uninstall_launchd.sh
docker compose -f docker-compose.example.yml down
```
