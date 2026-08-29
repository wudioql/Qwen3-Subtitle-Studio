"""播放器应用内沉浸模式：隐藏其它 UI，不重挂 mpv 原生窗口。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Qt


class PlayerFocusMixin:
    """让现有 PlayerPanel 原地填满 QMainWindow 客户区并可精确还原。"""

    def _setup_player_focus_mode(self) -> None:
        self._player_focus_active = False
        self._player_focus_snapshot = None
        self.player.focus_mode_requested.connect(self._toggle_player_focus_mode)
        self.player.set_focus_mode(False)

    def _toggle_player_focus_mode(self) -> None:
        self._set_player_focus_mode(not self._player_focus_active)

    def _set_player_focus_mode(self, active: bool) -> None:
        active = bool(active)
        if active == self._player_focus_active:
            return
        if active:
            self._enter_player_focus_mode()
        else:
            self._leave_player_focus_mode()

    def _enter_player_focus_mode(self) -> None:
        if self._player_focus_active:
            return
        widgets = (
            self.editor,
            self.waveform,
            self._export_panel,
            self.menuBar(),
            self._main_toolbar,
            self.statusBar(),
        )
        self._player_focus_snapshot = {
            "main_sizes": self._main_h_split.sizes(),
            "left_sizes": self._left_v_split.sizes(),
            "top_sizes": self._top_split.sizes(),
            "hidden": {widget: widget.isHidden() for widget in widgets},
        }
        for widget in widgets:
            widget.hide()
        self._player_focus_active = True
        self.player.set_focus_mode(True)
        self.player.setFocus(Qt.FocusReason.OtherFocusReason)

    def _leave_player_focus_mode(self) -> None:
        if not self._player_focus_active:
            return
        snapshot = self._player_focus_snapshot or {}
        hidden = snapshot.get("hidden", {})
        for widget, was_hidden in hidden.items():
            widget.setVisible(not was_hidden)
        self._player_focus_active = False
        self._player_focus_snapshot = None
        self.player.set_focus_mode(False)

        # 控件重新 show 后 Qt 会先做一轮布局；下一事件循环再恢复 splitter 尺寸，
        # 避免显示过程把用户原来的比例覆盖为 sizeHint 默认值。
        def restore_sizes() -> None:
            for splitter, key in (
                (self._main_h_split, "main_sizes"),
                (self._left_v_split, "left_sizes"),
                (self._top_split, "top_sizes"),
            ):
                sizes = snapshot.get(key)
                if sizes:
                    splitter.setSizes(sizes)

        QTimer.singleShot(0, restore_sizes)

    def event(self, event) -> bool:
        # 沉浸模式中先截获 Esc 的 ShortcutOverride，避免菜单里的“取消任务”动作
        # 抢先消费按键；退出沉浸后，既有 Esc 取消任务快捷键照常生效。
        if (
            getattr(self, "_player_focus_active", False)
            and event.type() == QEvent.Type.ShortcutOverride
            and event.key() == Qt.Key.Key_Escape
        ):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        if getattr(self, "_player_focus_active", False) and event.key() == Qt.Key.Key_Escape:
            self._leave_player_focus_mode()
            event.accept()
            return
        super().keyPressEvent(event)


__all__ = ["PlayerFocusMixin"]
