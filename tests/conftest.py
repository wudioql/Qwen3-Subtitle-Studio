"""tests/conftest.py — pytest 会话级基础设施

在收集/导入任何测试模块之前生效。实现见 ``tests/_env.py``（唯一真源）；
直跑三件套见 ``tests/_bootstrap.py``（同一实现，force_config_dir=False）。

注意：不要在本模块顶层 import 项目包 —— 环境变量必须先于
``core.app_config`` 的 import 生效。
"""

from __future__ import annotations

from _env import PROJECT_ROOT, apply_test_env

# pytest：强制隔离偏好目录（整次会话一个临时目录）
apply_test_env(force_config_dir=True, config_prefix="qss_test_config_")

__all__ = ["PROJECT_ROOT"]
