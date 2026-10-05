"""PlayerPanel 画面点击、媒体门禁与沉浸模式提示子域。"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget
from qfluentwidgets import FluentIcon as FIF, PrimaryPushButton, PushButton


class FocusClickHost(QWidget):
    """mpv 原生宿主的父窗口点击回退；主要输入由 mpv MOUSE_BTN0 提供。"""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._click_press_pos = None

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


class PlayerFocusSurfaceMixin:
    """汇合 Qt stage clicked 与 mpv binding，并维护紧凑底部控制条。"""

    def _build_transport_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addStretch(1)
        self._btn_play = PrimaryPushButton(self)
        self._btn_play.setText("播放")
        self._btn_play.setIcon(FIF.PLAY)
        self._btn_pause = PushButton(self)
        self._btn_pause.setText("暂停")
        self._btn_pause.setIcon(FIF.PAUSE)
        self._btn_stop = PushButton(self)
        self._btn_stop.setText("停止")
        self._btn_stop.setIcon(FIF.CANCEL)
        self._btn_play.clicked.connect(self.play)
        self._btn_pause.clicked.connect(self.pause)
        self._btn_stop.clicked.connect(self.stop)
        for button in (self._btn_play, self._btn_pause, self._btn_stop):
            button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            bar.addWidget(button)
        bar.addStretch(1)
        return bar

    def _install_focus_click_target(self, widget: QWidget) -> None:
        self._focus_click_targets.add(widget)
        widget.clicked.connect(self._request_focus_mode_toggle)

    def _request_focus_mode_toggle(self) -> None:
        if not self.has_playable_media():
            return
        now = time.monotonic()
        if now - self._last_focus_toggle_request < 0.20:
            return
        self._last_focus_toggle_request = now
        self.focus_mode_requested.emit()

    def has_playable_media(self) -> bool:
        return self._media_path is not None and self._media_path.is_file()

    def set_focus_mode(self, active: bool) -> None:
        self._focus_mode = bool(active)
        self._update_focus_click_hint()

    def _update_focus_click_hint(self) -> None:
        playable = self.has_playable_media()
        if playable:
            text = (
                "单击还原完整界面（Esc 也可）"
                if self._focus_mode
                else "单击进入沉浸预览"
            )
            cursor = Qt.CursorShape.PointingHandCursor
        else:
            text = "导入媒体后可单击进入沉浸预览"
            cursor = Qt.CursorShape.ArrowCursor
        for widget in self._focus_click_targets:
            widget.setToolTip(text)
            widget.setCursor(cursor)


__all__ = ["FocusClickHost", "PlayerFocusSurfaceMixin"]
