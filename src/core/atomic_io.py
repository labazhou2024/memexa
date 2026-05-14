"""
Atomic IO - 原子文件写入工具
防止崩溃时产生半截数据，替代所有 open().write() JSON 状态文件操作。

Features:
- 原子写入 JSON 和文本文件（write-to-tmp + os.replace）
- 写入后 flush + fsync 确保数据落盘
- 异常时自动清理临时文件
- 可选旧文件备份（.bak）
- 安全读取 JSON（解析失败返回 default）
"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# Type alias for path arguments
PathLike = Union[str, Path]


def atomic_write_json(
    path: PathLike,
    data: Any,
    indent: int = 2,
    backup: bool = False,
    encoding: str = "utf-8",
) -> None:
    """
    原子写入 JSON 文件。

    流程：
    1. （可选）备份旧文件为 {path}.bak
    2. 在同一目录创建临时文件，写入序列化 JSON
    3. flush + fsync 确保数据落盘
    4. os.replace 原子替换目标文件

    Args:
        path:     目标文件路径
        data:     可 JSON 序列化的数据对象
        indent:   JSON 缩进空格数，默认 2
        backup:   True 时写入前将旧文件备份为 {path}.bak
        encoding: 文件编码，默认 utf-8

    Raises:
        TypeError:          data 无法序列化为 JSON
        OSError:            文件系统操作失败
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # --- optional backup ---
    if backup and target.exists():
        bak_path = target.with_suffix(target.suffix + ".bak")
        try:
            shutil.copy2(target, bak_path)
            logger.debug(f"Backed up {target} -> {bak_path}")
        except OSError as exc:
            # Backup failure is non-fatal; log and continue
            logger.warning(f"Failed to create backup for {target}: {exc}")

    # Serialize before touching the filesystem so we fail fast on bad data
    content = json.dumps(data, ensure_ascii=False, indent=indent)
    encoded = content.encode(encoding)

    _atomic_write_bytes(target, encoded)
    logger.debug(f"atomic_write_json: wrote {len(encoded)} bytes to {target}")


def atomic_write_text(
    path: PathLike,
    content: str,
    encoding: str = "utf-8",
) -> None:
    """
    原子写入文本文件。

    Args:
        path:     目标文件路径
        content:  文本内容
        encoding: 文件编码，默认 utf-8

    Raises:
        OSError: 文件系统操作失败
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    encoded = content.encode(encoding)
    _atomic_write_bytes(target, encoded)
    logger.debug(f"atomic_write_text: wrote {len(encoded)} bytes to {target}")


def safe_read_json(
    path: PathLike,
    default: Any = None,
    encoding: str = "utf-8",
) -> Any:
    """
    安全读取 JSON 文件，任何错误均返回 default 而不抛出异常。

    Args:
        path:     目标文件路径
        default:  读取或解析失败时的返回值，默认 None
        encoding: 文件编码，默认 utf-8

    Returns:
        解析后的 Python 对象，或 default
    """
    target = Path(path)
    try:
        text = target.read_text(encoding=encoding)
        return json.loads(text)
    except FileNotFoundError:
        logger.debug(f"safe_read_json: file not found: {target}")
        return default
    except json.JSONDecodeError as exc:
        logger.warning(f"safe_read_json: JSON parse error in {target}: {exc}")
        return default
    except OSError as exc:
        logger.warning(f"safe_read_json: OS error reading {target}: {exc}")
        return default


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """
    Write *data* to *target* atomically using a sibling temp file.

    Uses NamedTemporaryFile in the same directory as *target* so that
    os.replace() stays on the same filesystem (required for atomicity on
    NTFS and POSIX).  Calls flush() + os.fsync() before the rename to
    guarantee durability even on crash.

    Cleans up the temp file on any exception.
    """
    dir_path = target.parent
    tmp_fd = None
    tmp_path = None

    try:
        # delete=False so we can close the file before calling os.replace
        # (Windows requires the file to be closed before renaming)
        tmp_fd = tempfile.NamedTemporaryFile(
            mode="wb",
            dir=dir_path,
            suffix=".tmp",
            delete=False,
        )
        tmp_path = Path(tmp_fd.name)

        tmp_fd.write(data)
        tmp_fd.flush()
        os.fsync(tmp_fd.fileno())  # force kernel buffer -> disk
        tmp_fd.close()
        tmp_fd = None  # mark as closed so the except block skips close()

        # Atomic rename - on NTFS (Windows 10+) and POSIX this is a single
        # syscall that either succeeds fully or leaves the original intact.
        # Retry on Windows PermissionError (OneDrive sync, antivirus locks).
        import time as _time
        for _attempt in range(3):
            try:
                os.replace(tmp_path, target)
                tmp_path = None  # rename succeeded; nothing to clean up
                break
            except PermissionError:
                if _attempt < 2:
                    _time.sleep(0.1 * (_attempt + 1))
                else:
                    raise  # Give up after 3 attempts

    except Exception:
        # Best-effort cleanup of the temp file
        if tmp_fd is not None:
            try:
                tmp_fd.close()
            except OSError:
                pass
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
