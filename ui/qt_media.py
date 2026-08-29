"""QtMultimedia 可选依赖适配层。

统一播放器相关模块的防御性导入，并在导入 QtMultimedia 前再次声明软解策略。
目标环境安装完整版 PySide6；缺失 Addons 时播放器仍可构造并显示字幕画布。
"""

from __future__ import annotations

import os

# main.py 应更早设置；这里作为直接导入播放器模块时的保底。
os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "")
os.environ.setdefault("QT_FFMPEG_HW_DECODING", "0")

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

    HAS_QT_MULTIMEDIA = True
except Exception:  # noqa: BLE001
    HAS_QT_MULTIMEDIA = False
    QMediaPlayer = None  # type: ignore[assignment,misc]
    QAudioOutput = None  # type: ignore[assignment,misc]
    QVideoSink = None  # type: ignore[assignment,misc]


__all__ = [
    "HAS_QT_MULTIMEDIA",
    "QAudioOutput",
    "QMediaPlayer",
    "QVideoSink",
]
