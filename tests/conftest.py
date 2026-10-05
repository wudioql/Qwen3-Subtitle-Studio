"""tests/conftest.py — pytest 会话级基础设施

在收集/导入任何测试模块之前生效。实现见 ``tests/_env.py``（唯一真源）；
直跑三件套见 ``tests/_bootstrap.py``（同一实现，force_config_dir=False）。

注意：不要在本模块顶层 import 项目包 —— 环境变量必须先于
``core.app_config`` 的 import 生效。
"""

from __future__ import annotations

import pytest

from _env import PROJECT_ROOT, apply_test_env

# pytest：强制隔离偏好目录（整次会话一个临时目录）
apply_test_env(force_config_dir=True, config_prefix="qss_test_config_")


@pytest.fixture(autouse=True)
def _restore_qt_theme_after_each_test():
    """每个测试后还原**进程级** Qt 主题状态。

    起因（2026-10-04 实测）：``test_splitter_theme`` 的
    ``test_first_theme_toggle_does_not_change_layout`` 会连切 3 次主题并
    停在深色，而 ``_on_toggle_theme`` 改的是**全局**状态——于是全量套件里
    后续任何**依赖渲染结果**的测试都会在深色主题下运行。
    当时的表现是 test_toolbar 的墨迹像素判据量到 0，被误判成「文字被截断」，
    单跑却绿——典型的**顺序依赖**假失败。

    这里加 autouse fixture 做兜底：即使将来再有人忘记还原，也不会污染后续
    测试。

    ⚠️ 性能：``apply_theme`` 要重新编译并安装整套 QSS，若每条测试都无条件
    调用，全量套件会从 ~112s 涨到 ~292s。所以先做一次**廉价判断**——
    当前不是深色主题就直接返回（绝大多数测试本来就在浅色下跑）。
    """
    yield
    try:
        from qfluentwidgets import isDarkTheme

        if not isDarkTheme():                     # 本来就是浅色，无需动作
            return
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        from ui.themes import apply_theme

        apply_theme(app, False)
    except Exception:                              # noqa: BLE001 - 兜底不得影响测试
        pass


@pytest.fixture(autouse=True)
def _isolate_preferences_after_each_test():
    """每个测试后清空**偏好**的进程级状态（内存缓存 + 落盘文件）。

    起因（2026-10-05 实测）：``core.app_config.load_preferences`` 有模块级
    ``_PREFS_CACHE``，而任何 UI 交互式改动（如把 k-tag 下拉切成 ``k``）都会走
    ``save_preferences``，**同时**刷新缓存与磁盘上的 preferences.json。
    于是「前一个测试改了偏好 → 后一个测试读到它」构成顺序依赖：

    - ``test_toolbar::test_ui_toolbar_and_workflow_controller`` 把
      ``export.k_tag_mode`` 写成 ``"k"``；
    - 之后跑的 ``test_playback_responsiveness::test_mpv_subtitle_generation``
      断言 ``{\\kf``，实际拿到 ``{\\k50}``。

    全量顺序下 playback 恰好排在 toolbar **之前**才没暴露；换成任意子集/乱序
    就会失败。同理 ``player_preview_mode`` 等字段也会这样泄漏。

    这里删掉落盘文件 + 清缓存，使每个测试都从**默认偏好**起步（等价于单独跑）。
    ``invalidate_preferences_cache()`` 无参调用会清掉所有按路径分键的条目，因此
    用 ``tmp_path`` 自建偏好文件的测试也一并覆盖。
    """
    yield
    try:
        from core import app_config

        app_config.invalidate_preferences_cache()
        app_config.DEFAULT_PREFERENCES_PATH.unlink(missing_ok=True)
    except Exception:                              # noqa: BLE001 - 兜底不得影响测试
        pass


__all__ = ["PROJECT_ROOT"]
