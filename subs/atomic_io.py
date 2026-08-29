"""subs.atomic_io — 统一原子写盘工具。

工程保存（subs.models.save_json）与偏好保存（core.app_config.save_preferences）
均已实现「同目录临时文件 + fsync + os.replace」；导出写盘此前直接 ``write_text``，
磁盘满/中途崩溃可能留下半截字幕。本模块把同一套语义抽成公共函数，供导出
等所有"写整份文本文件"的路径复用。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union


def atomic_write_text(
    path: Union[str, Path],
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
) -> Path:
    """把 text 原子地写入 path（同目录临时文件 + flush + fsync + os.replace）。

    - 成功返回目标 Path；
    - 写盘/替换失败抛原始异常（磁盘满、权限、路径错误等），由调用方提示；
    - 失败时清理临时文件，不破坏已有目标文件内容。
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


__all__ = ["atomic_write_text"]
