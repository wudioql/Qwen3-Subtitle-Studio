"""字幕编辑器薄容器：Fluent Pivot + 句级 / 字级两个对称视图。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import Pivot

from subs.models import SubtitleProject
from .sentence_level_view import SentenceLevelView
from .word_level_view import WordLevelView


class SubsEditor(QWidget):
    """字幕编辑器：句级/字级双页面容器。"""

    row_selected = Signal(int)
    text_changed = Signal(int, str)
    time_changed = Signal(int, float, float)
    language_changed = Signal(int, str)
    row_number_clicked = Signal(int)
    set_all_language_requested = Signal(str)
    add_sentence_requested = Signal(int, object)
    delete_sentences_requested = Signal(list)
    split_sentence_requested = Signal(int, float)
    split_sentence_at_char_requested = Signal(int, int, str)
    merge_sentences_requested = Signal(list)
    confirm_sentences_requested = Signal(list)
    mark_dirty_sentences_requested = Signal(list)
    toggle_lock_sentences_requested = Signal(list)
    realign_sentence_requested = Signal(int)
    word_time_edited = Signal(int, int, float, float)  # sent_idx, word_idx, start, end
    strip_trailing_punct_requested = Signal()          # 批量删除句尾标点（全文）
    page_changed = Signal(str)                         # "sentence" | "word"
    project_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("subs_editor")
        self._project: Optional[SubtitleProject] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 5)
        root.setSpacing(5)

        self._sentence_view = SentenceLevelView(self)
        self._words_view = WordLevelView(self)
        self._sentence_view.setObjectName("sentencePage")
        self._words_view.setObjectName("wordPage")

        self._pivot = Pivot(self)
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._sentence_view)
        self._stack.addWidget(self._words_view)
        self._pivot.addItem(
            routeKey="sentence",
            text="句级字幕",
            onClick=lambda _checked=False: self._stack.setCurrentWidget(self._sentence_view),
        )
        self._pivot.addItem(
            routeKey="word",
            text="字级精度",
            onClick=lambda _checked=False: self._stack.setCurrentWidget(self._words_view),
        )
        self._pivot.setCurrentItem("sentence")
        self._stack.currentChanged.connect(self._on_page_changed)
        root.addWidget(self._pivot)
        root.addWidget(self._stack, 1)

        sv = self._sentence_view
        sv.row_selected.connect(self.row_selected)
        sv.text_changed.connect(self.text_changed)
        sv.time_changed.connect(self.time_changed)
        sv.language_changed.connect(self.language_changed)
        sv.row_number_clicked.connect(self.row_number_clicked)
        sv.set_all_language_requested.connect(self.set_all_language_requested)
        sv.add_sentence_requested.connect(self.add_sentence_requested)
        sv.delete_sentences_requested.connect(self.delete_sentences_requested)
        sv.split_sentence_requested.connect(self.split_sentence_requested)
        sv.split_sentence_at_char_requested.connect(self.split_sentence_at_char_requested)
        sv.merge_sentences_requested.connect(self.merge_sentences_requested)
        sv.confirm_sentences_requested.connect(self.confirm_sentences_requested)
        sv.mark_dirty_sentences_requested.connect(self.mark_dirty_sentences_requested)
        sv.toggle_lock_sentences_requested.connect(self.toggle_lock_sentences_requested)
        sv.strip_trailing_punct_requested.connect(self.strip_trailing_punct_requested)
        sv.row_selected.connect(self._on_sentence_row_selected)
        sv.time_changed.connect(lambda *_: self._words_view.refresh_current())

        self._words_view.word_edited.connect(self._on_word_edited)
        self._words_view.word_time_edited.connect(self.word_time_edited)
        self._words_view.realign_sentence_requested.connect(self.realign_sentence_requested)

    @property
    def project(self) -> Optional[SubtitleProject]:
        return self._project

    def set_project(self, project: SubtitleProject) -> None:
        self._project = project
        self._sentence_view.set_project(project)
        self._words_view.set_project(project)

    def select_row(self, idx: int) -> None:
        self._sentence_view.select_row(idx)
        if self._stack.currentWidget() is self._words_view:
            self._words_view.show_sentence(idx)

    def highlight_row(self, idx: int) -> None:
        """播放高亮：轻量同步句级选中行，不触发 row_selected 副作用。

        - 句级视图走 ``highlight_row``（blockSignals 下 selectRow，条件滚动）；
        - 字级 Tab 激活时同步展示当前播放句（与原 select_row 一致），
          但不移动播放头、不 force 重建波形字级图元。
        """
        self._sentence_view.highlight_row(idx)
        if self._stack.currentWidget() is self._words_view:
            self._words_view.show_sentence(idx)

    def selected_rows(self) -> list[int]:
        return self._sentence_view.selected_rows()

    def set_playhead(self, seconds: float) -> None:
        self._sentence_view.set_playhead(seconds)
        # 仅当字级页可见时才同步字级高亮——隐藏页每 tick 扫描/选中是纯浪费
        if self._stack.currentWidget() is self._words_view:
            self._words_view.set_playhead(seconds)

    def mark_dirty_visual(self, idx: int) -> None:
        self._sentence_view.mark_dirty_visual(idx)

    def clear_dirty_visual(self, idx: int) -> None:
        self._sentence_view.clear_dirty_visual(idx)

    def refresh_row(self, idx: int) -> None:
        """轻量刷新单行（不重建全表）。"""
        self._sentence_view.refresh_row(idx)

    def update_word_times(self, sent_idx: int, words: list) -> None:
        """轻量刷新字级表格时间（不重建全表）。"""
        self._words_view.update_word_times(sent_idx, words)

    def refresh_summary(self) -> None:
        """轻量刷新底部统计栏。"""
        self._sentence_view.refresh_summary()

    def is_word_view_active(self) -> bool:
        """当前是否处于字级精度 Tab。"""
        return self._stack.currentWidget() is self._words_view

    def show_word_sentence(self, idx: int) -> None:
        """让字级视图展示指定句子（不切换 Tab，供外部轻量刷新用）。"""
        self._words_view.show_sentence(idx)

    @property
    def current_word_sentence_index(self) -> int:
        """字级视图当前展示的句子索引（未展示为 -1），供「是否已在显示该句」判断。"""
        return self._words_view.current_sentence_index

    def _on_page_changed(self, idx: int) -> None:
        key = "word" if self._stack.widget(idx) is self._words_view else "sentence"
        self._pivot.setCurrentItem(key)
        if key == "word":
            rows = self._sentence_view.selected_rows()
            target = rows[0] if rows else 0
            if self._project is not None and 0 <= target < len(self._project.sentences):
                self._words_view.set_project(self._project)
                self._words_view.show_sentence(target)
        self.page_changed.emit(key)

    def _on_sentence_row_selected(self, idx: int) -> None:
        if self._stack.currentWidget() is self._words_view:
            self._words_view.show_sentence(idx)

    def _on_word_edited(self, idx: int) -> None:
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return
        s = self._project.sentences[idx]
        self.time_changed.emit(idx, s.start_time, s.end_time)


__all__ = ["SubsEditor"]
