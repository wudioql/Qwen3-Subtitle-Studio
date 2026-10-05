"""ui.player — 播放预览组件包。

对外（与单文件时代相同的公开名，2026-10-05 由 ``ui/`` 根目录收包）::

    from ui.player import PlayerPanel          # 原 ui.player_panel.PlayerPanel
    from ui.player import _VideoSubtitleStage  # 原 ui.player_panel._VideoSubtitleStage
    from ui.player import MpvWorker            # 原 ui.mpv_worker.MpvWorker

内部件（子域单一职责，``panel`` 是兼容 façade 且须 <500 行）：

* ``panel.py``            — ``PlayerPanel``：布局与对外 API，混入三个 mixin
* ``stage.py``            — ``_VideoSubtitleStage``：QVideoSink 与字幕同画布绘制
* ``subtitle_preview.py`` — ``SubtitlePreviewMixin``：复用 ``render_export`` 生成预览字幕
* ``qt_runtime.py``       — ``QtPlaybackRuntimeMixin``：Qt 软解/首卷/暂停静音
* ``focus_surface.py``    — 画面点击与媒体门禁（``FocusClickHost`` / ``PlayerFocusSurfaceMixin``）
* ``subtitle_overlay.py`` — 字幕叠层纯函数与绘制
* ``mpv_backend.py``      — **唯一** python-mpv 接入点
* ``mpv_worker.py``       — mpv daemon worker（原生调用只在此发起，GUI 线程非阻塞入队）
* ``qt_media.py``         — 可选 QtMultimedia 导入的唯一真源

留在 ``ui/`` 根的邻居（不属于本簇）：``karaoke_coordinates``、``subtitle_render_policy``。
"""
from __future__ import annotations

from .mpv_worker import MpvWorker
from .panel import PlayerPanel, _VideoSubtitleStage
from .subtitle_overlay import PREVIEW_MODES, compute_overlay_segments, paint_subtitle_overlay

__all__ = [
    "PlayerPanel",
    "_VideoSubtitleStage",
    "MpvWorker",
    "PREVIEW_MODES",
    "compute_overlay_segments",
    "paint_subtitle_overlay",
]
