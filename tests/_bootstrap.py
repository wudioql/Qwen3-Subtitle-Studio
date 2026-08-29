"""tests/_bootstrap — ``python tests/xxx.py`` 直跑三件套入口。

实现见 ``tests/_env.py``（与 conftest 共用）。用法（须在其它项目 import 之前）：

    from _bootstrap import PROJECT_ROOT  # noqa: F401

pytest 收集时 conftest 已 ``force_config_dir=True`` 设好 ``QSS_CONFIG_DIR``；
本模块 ``force_config_dir=False``，不会覆盖会话目录。
"""

from __future__ import annotations

from _env import PROJECT_ROOT, apply_test_env

apply_test_env(force_config_dir=False, config_prefix="qss_script_config_")

__all__ = ["PROJECT_ROOT"]
