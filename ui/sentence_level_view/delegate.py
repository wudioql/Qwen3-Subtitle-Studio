"""ui.sentence_level_view.delegate — 句级表格委托（文本拆分 / 语言下拉）。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QEvent, Signal, QTimer
from PySide6.QtWidgets import (
    QAbstractItemDelegate, QStyleOptionViewItem, QWidget,
)
from qfluentwidgets import ComboBox, LineEdit, TableItemDelegate, TableWidget, isDarkTheme

from ui.languages import SENTENCE_LANGUAGES

_LANG_NAME_TO_CODE = {name: code for code, name in SENTENCE_LANGUAGES}
_LANG_CODE_TO_NAME = {code: name for code, name in SENTENCE_LANGUAGES}

class _SentenceTableDelegate(TableItemDelegate):
    """句级表格全列统一项目委托：全列共享统一的选中、按下、悬浮状态，保证整行 7 列无缝同步切换。"""

    enter_split_requested = Signal(int, int, str)  # row, char_index, text

    def __init__(self, parent: TableWidget):
        super().__init__(parent)
        self._active_text_editor = None
        self._active_text_row: int = -1
        self._last_split: dict[int, tuple[int, str]] = {}
        self._editing_index: Optional[tuple[int, int]] = None

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index):
        col = index.column()
        if col == 4:  # 文本列
            editor = LineEdit(parent)
            editor.setMinimumHeight(36)
            editor.setTextMargins(8, 0, 8, 0)
            editor.setClearButtonEnabled(True)
            editor.installEventFilter(self)
            editor.cursorPositionChanged.connect(lambda *_a: self._capture_text(index, editor))
            editor.textChanged.connect(lambda *_a: self._capture_text(index, editor))
            self._active_text_editor = editor
            self._active_text_row = index.row()
            self._capture_text(index, editor)
            return editor
        elif col == 6:  # 语言列
            self._editing_index = (index.row(), index.column())
            editor = ComboBox(parent)
            editor.setMinimumHeight(32)
            bg = "#202022" if isDarkTheme() else "#FFFFFF"
            text_c = "#F5F5F7" if isDarkTheme() else "#1C1C1E"
            border_c = "#38383A" if isDarkTheme() else "rgba(0,0,0,0.15)"
            editor.setStyleSheet(
                f"ComboBox {{ background-color: {bg}; color: {text_c}; border-radius: 6px; border: 1px solid {border_c}; }}"
            )
            for code, name in SENTENCE_LANGUAGES:
                editor.addItem(name, userData=code)
            cur = index.data(Qt.DisplayRole) or ""
            cur_code = _LANG_NAME_TO_CODE.get(cur, cur)
            i = editor.findData(cur_code)
            if i >= 0:
                editor.setCurrentIndex(i)
            editor.currentIndexChanged.connect(lambda _idx: self._commit_and_close(editor))
            def _open_menu():
                if hasattr(editor, "_showComboMenu"):
                    editor._showComboMenu()
                elif hasattr(editor, "showPopup"):
                    editor.showPopup()
            QTimer.singleShot(0, _open_menu)
            return editor
        else:
            return super().createEditor(parent, option, index)

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index):
        col = index.column()
        if col == 4:
            editor.setGeometry(option.rect.adjusted(2, 2, -2, -2))
        elif col == 6:
            editor.setGeometry(option.rect.adjusted(3, 3, -3, -3))
        else:
            super().updateEditorGeometry(editor, option, index)

    def setEditorData(self, editor, index):
        col = index.column()
        if col == 4:
            editor.setText(index.data(Qt.DisplayRole) or "")
        elif col == 6:
            cur = index.data(Qt.DisplayRole) or ""
            cur_code = _LANG_NAME_TO_CODE.get(cur, cur)
            i = editor.findData(cur_code)
            if i >= 0:
                editor.setCurrentIndex(i)
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        col = index.column()
        if col == 4:
            model.setData(index, editor.text(), Qt.EditRole)
        elif col == 6:
            code = editor.currentData() or ""
            model.setData(index, _LANG_CODE_TO_NAME.get(code, ""), Qt.EditRole)
        else:
            super().setModelData(editor, model, index)

    def destroyEditor(self, editor, index):
        if index.column() == 4:
            self._active_text_editor = None
            self._active_text_row = -1
        elif index.column() == 6:
            self._editing_index = None
        super().destroyEditor(editor, index)

    def paint(self, painter, option, index):
        # 若当前正在编辑语言列单元格，清空 option.text 避免底层绘制文本与 ComboBox 重影
        opt = QStyleOptionViewItem(option)
        if self._editing_index == (index.row(), index.column()):
            opt.text = ""
        super().paint(painter, opt, index)

    def _commit_and_close(self, editor):
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def eventFilter(self, obj, event):
        if obj is self._active_text_editor and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ControlModifier:
                    self._on_split(obj)
                    return True
        return super().eventFilter(obj, event)

    def _capture_text(self, index, editor):
        row = index.row()
        self._last_split[row] = (editor.cursorPosition(), editor.text())
        self._active_text_editor = editor
        self._active_text_row = row

    def split_info_for(self, row: int):
        return self._last_split.get(row)

    def _on_split(self, editor):
        row = self._active_text_row
        info = self._last_split.get(row)
        if info is None:
            return
        char_index, text = info
        self.enter_split_requested.emit(row, char_index, text)
        self.closeEditor.emit(editor, QAbstractItemDelegate.NoHint)


__all__ = ["_SentenceTableDelegate"]
