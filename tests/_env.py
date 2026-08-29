"""tests/_env — 测试环境三件套的**唯一实现**。

供：
- ``tests/conftest.py``（pytest 会话，强制偏好隔离目录）
- ``tests/_bootstrap.py``（``python tests/xxx.py`` 直跑，幂等、不覆盖已有 env）

三件套：
1. ``sys.path`` 注入项目根（``from core import …`` / ``from subs import …``）
2. Qt 离屏：``QT_QPA_PLATFORM=offscreen``（可用外部环境覆盖；本模块只用 setdefault）
3. 偏好隔离：``QSS_CONFIG_DIR`` → 临时目录，避免改写项目根 ``.config/preferences.json``

**禁止**在本模块顶层 import 任何项目包（``core.app_config`` 在 import 时读取
``QSS_CONFIG_DIR``，必须先设 env）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# tests/ 的上一级 = 项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def apply_test_env(
    *,
    force_config_dir: bool = False,
    config_prefix: str = "qss_test_config_",
    temp_prefix: str = "qss_test_temp_",
) -> Path:
    """应用测试环境隔离；返回 PROJECT_ROOT。

    隔离项（防止测试改写用户真实运行态）：
    - ``QSS_CONFIG_DIR`` → 临时目录（偏好读写隔离）；
    - ``QSS_TEMP_DIR``  → 临时目录（core.constants.TEMP_DIR 隔离，
      app.log / ffmpeg 探针 / 提取缓存等不落项目根 .temp）；
    - ``QT_QPA_PLATFORM=offscreen``（可用外部环境覆盖）。

    Args:
        force_config_dir: True = 总是新建临时目录并写入 ``QSS_CONFIG_DIR``
            （pytest 会话用，保证整次运行隔离）。
            False = 仅当环境变量尚未设置时创建（直跑用，且不覆盖 pytest 已设值）。
        config_prefix: ``tempfile.mkdtemp`` 前缀，便于区分会话/直跑残留目录。
        temp_prefix: 同 config_prefix，用于 ``QSS_TEMP_DIR``。
    """
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    # 空字符串视为未设置（setdefault 不会覆盖 ""）
    if not (os.environ.get("QT_QPA_PLATFORM") or "").strip():
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # 单元/UI 离屏测试不启动真实 native mpv worker；Windows 真机 mpv 验收由
    # e2e/严格探针负责，需显式移除该变量。
    if not (os.environ.get("QSS_DISABLE_MPV") or "").strip():
        os.environ["QSS_DISABLE_MPV"] = "1"

    if force_config_dir or not (os.environ.get("QSS_CONFIG_DIR") or "").strip():
        os.environ["QSS_CONFIG_DIR"] = tempfile.mkdtemp(prefix=config_prefix)

    # TEMP_DIR 隔离（import core.constants 之前必须设好；conftest/_bootstrap 均早于项目 import）
    if force_config_dir or not (os.environ.get("QSS_TEMP_DIR") or "").strip():
        os.environ["QSS_TEMP_DIR"] = tempfile.mkdtemp(prefix=temp_prefix)

    return PROJECT_ROOT


__all__ = ["PROJECT_ROOT", "apply_test_env"]
