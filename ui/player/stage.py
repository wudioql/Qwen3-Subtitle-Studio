"""QVideoSink 视频帧与 QPainter 字幕的同画布舞台。"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from .qt_media import HAS_QT_MULTIMEDIA, QVideoSink
from .subtitle_overlay import (
    PREVIEW_MODES,
    compute_overlay_segments,
    paint_subtitle_overlay,
)

logger = logging.getLogger(__name__)

class _VideoSubtitleStage(QWidget):
    """单画布：视频帧（QVideoSink）+ 字幕同绘，并直接发出可靠点击信号。"""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._click_press_pos = None
        self.setObjectName("player_video_subtitle_stage")
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._is_video = False
        self._frame: Optional[QImage] = None
        self._project = None
        self._time = 0.0
        self._mode = "sentence"
        self._ass_style = None
        self._word_style = None
        self._karaoke_template = None
        self._k_mode = "kf"

        # 播放响应缓存：
        #   - 缩放后的视频帧（仅帧变化/尺寸变化时重缩放，字幕重绘不再重缩放全帧）
        #   - 字幕渲染 pixmap（签名不变直接 blit，省每帧重建 QFont/QPainterPath/描边）
        self._scaled_frame: Optional[QImage] = None
        self._scaled_key: tuple = ()
        self._subtitle_pix: Optional[QPixmap] = None
        self._subtitle_sig: tuple = ()
        self._style_version = 0

        # 静默首帧预卷：hold 期间只留首帧、不刷画面（避免导入媒体后“闪播一下又停”）
        self._prime_hold = False
        self._prime_frame: Optional[QImage] = None

        self._sink = None
        if HAS_QT_MULTIMEDIA and QVideoSink is not None:
            self._sink = QVideoSink(self)
            self._sink.videoFrameChanged.connect(self._on_video_frame)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        press_pos = self._click_press_pos
        self._click_press_pos = None
        is_click = (
            event.button() == Qt.MouseButton.LeftButton
            and press_pos is not None
            and (event.position().toPoint() - press_pos).manhattanLength() <= 6
        )
        super().mouseReleaseEvent(event)
        if is_click:
            self.clicked.emit()

    def video_sink(self):
        return self._sink

    def set_is_video(self, video: bool) -> None:
        self._is_video = bool(video)
        if not self._is_video:
            self._frame = None
            self._scaled_frame = None
            self._scaled_key = ()
        self.update()

    def set_project(self, project) -> None:
        self._project = project
        self.refresh_styles()
        self.update()

    def refresh_content(self) -> None:
        """同一 project 对象被原地编辑后，废弃字幕像素缓存并立即重绘。"""
        self._subtitle_sig = ()
        self._subtitle_pix = None
        self.update()

    def set_time(self, t: float) -> None:
        self._time = float(t)
        self.update()

    def set_mode(self, mode: str) -> None:
        if mode in PREVIEW_MODES:
            self._mode = mode
            self._style_version += 1
            self.update()

    def mode(self) -> str:
        return self._mode

    # 供测试/调试读取（与旧 SubtitleOverlay 私有字段兼容）
    @property
    def project(self):
        return self._project

    def refresh_styles(self) -> None:
        try:
            from core.app_config import load_preferences
            prefs = load_preferences()
            self._ass_style = prefs.ass_style.to_style()
            self._word_style = prefs.style
            active = prefs.karaoke_template.to_prefs().effective().templates
            self._karaoke_template = active[0] if active else None
            self._k_mode = prefs.export.k_tag_mode or "kf"
        except Exception:  # noqa: BLE001
            self._ass_style = None
            self._word_style = None
            self._karaoke_template = None
            self._k_mode = "kf"
        self._style_version += 1
        self.update()

    def clear_frame(self) -> None:
        self._frame = None
        self._scaled_frame = None
        self._scaled_key = ()
        self.update()

    def release_prime_frame(self) -> None:
        """结束静默预卷：把滞留的首帧作为静帧显示（无则保持现状）。"""
        self._prime_hold = False
        if self._prime_frame is not None:
            self._frame = self._prime_frame
            self._prime_frame = None
            self._scaled_frame = None
            self._scaled_key = ()
            self.update()

    # duck-type 兼容旧代码访问 subtitle_overlay._time / ._project
    @property
    def _time_prop(self):
        return self._time

    def _on_video_frame(self, frame) -> None:
        try:
            if frame is None or not frame.isValid():
                return
            img = frame.toImage()
            if img is None or img.isNull():
                return
            if self._prime_hold:
                # 静默预卷：只留首帧，不刷新画面（导入后不再“播放一下又停”）
                if self._prime_frame is None:
                    self._prime_frame = img
                return
            # toImage() 已返回独立 QImage，无需再 copy()（省每帧一次全帧拷贝）
            self._frame = img
            self._scaled_frame = None   # 新帧 → 缩放缓存失效
            self._scaled_key = ()
            self.update()
        except Exception as e:  # noqa: BLE001
            logger.debug("[播放器] videoFrame→Image 失败: %s", e)

    def _scaled_frame_or_none(self) -> Optional[QImage]:
        """返回缩放后的视频帧（按当前控件尺寸）。仅帧/尺寸变化时重缩放并缓存。"""
        key = (
            id(self._frame) if self._frame is not None else None,
            self.width(),
            self.height(),
        )
        if self._scaled_key == key and self._scaled_frame is not None:
            return self._scaled_frame
        self._scaled_key = key
        self._scaled_frame = None
        if self._frame is None or self._frame.isNull():
            return None
        scaled = self._frame.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scaled_frame = scaled
        return scaled

    def _overlay_signature(self) -> tuple:
        """字幕渲染签名：分段内容/状态（卡拉OK进度量化到 0.1）+ 模式 + 尺寸 + 样式版本。"""
        segs = compute_overlay_segments(self._project, self._time, self._mode)
        key = tuple(
            (s.text, s.state, round(float(s.progress), 1))
            for s in segs
        )
        return (
            self._mode,
            self._k_mode,
            key,
            self.width(),
            self.height(),
            self._style_version,
        )

    def _render_subtitle_pixmap(self) -> Optional[QPixmap]:
        w, h = self.width(), self.height()
        if w <= 1 or h <= 1:
            return None
        pix = QPixmap(w, h)
        pix.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pix)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        paint_subtitle_overlay(
            qp, w, h, self._project, self._time, self._mode,
            self._ass_style, self._word_style,
            self._karaoke_template, self._k_mode,
        )
        qp.end()
        return pix

    def paintEvent(self, event) -> None:  # noqa: ARG002
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # 背景
        p.fillRect(self.rect(), QColor("#0B1020" if self._is_video else "#111827"))

        if self._is_video:
            scaled = self._scaled_frame_or_none()
            if scaled is not None:
                # 预缩放帧直接 blit（无每帧缩放/拷贝）
                x = (self.width() - scaled.width()) // 2
                y = (self.height() - scaled.height()) // 2
                p.drawImage(x, y, scaled)
            else:
                # 加载后尚未拿到首帧时的占位（点播放或预卷成功后会换成画面）
                p.setPen(QColor("#64748B"))
                p.drawText(
                    self.rect(),
                    int(Qt.AlignmentFlag.AlignCenter),
                    "视频已加载\n"
                    "点击「播放」显示画面（或等待首帧预览）\n"
                    "HEVC 若刷 d3d11 日志可忽略——已走软解",
                )
        else:
            p.setPen(QColor("#64748B"))
            p.drawText(
                self.rect(),
                int(Qt.AlignmentFlag.AlignCenter),
                "音频媒体 · 无画面\n字幕预览显示于此",
            )

        # 字幕：签名不变直接 blit 缓存 pixmap（省每帧重建 QFont/QFontMetrics/QPainterPath/描边/填充）
        sig = self._overlay_signature()
        if self._subtitle_sig != sig:
            self._subtitle_pix = self._render_subtitle_pixmap()
            self._subtitle_sig = sig
        if self._subtitle_pix is not None:
            p.drawPixmap(0, 0, self._subtitle_pix)
        p.end()


__all__ = ["_VideoSubtitleStage"]
