"""ui.sentence_level_view.view — SentenceLevelView 主组件。"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QHBoxLayout, QSizePolicy,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, ComboBox, FluentIcon as FIF,
    PrimaryPushButton, PushButton, RoundMenu, Action, TableWidget,
)

from subs.models import Sentence, SubtitleProject
from ui.languages import SENTENCE_LANGUAGES, code_to_name, name_to_code
from ui.time_utils import format_time, parse_time

from .delegate import _SentenceTableDelegate
from .visuals import _apply_sentence_visual_state

_COLUMNS: List[tuple[str, str]] = [
    ("idx",   "#"),
    ("start", "起始"),
    ("end",   "结束"),
    ("dur",   "时长"),
    ("text",  "文本"),
    ("words", "字词数"),
    ("lang",  "语言"),
]

_LANG_CODE_TO_NAME = {code: name for code, name in SENTENCE_LANGUAGES}
_LANG_NAME_TO_CODE = {name: code for code, name in SENTENCE_LANGUAGES}

class SentenceLevelView(QWidget):
    """句级字幕视图：表格 + 插入/删除/拆分/合并/确认/锁定工具条。"""

    # ── 对 MainWindow 的信号（与原 SubsEditor 保持一致） ──
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
    confirm_sentences_requested = Signal(list)       # rows
    mark_dirty_sentences_requested = Signal(list)    # rows
    toggle_lock_sentences_requested = Signal(list)   # rows
    strip_trailing_punct_requested = Signal()        # 批量删除句尾标点（全文）
    project_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sentence_level_view")
        self._project: Optional[SubtitleProject] = None
        self._playhead_s: float = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        # ── Fluent 工具条 ──
        tb = QHBoxLayout()
        tb.setSpacing(7)
        self._btn_add = PushButton(self)
        self._btn_add.setText("插入句")
        self._btn_add.setIcon(FIF.ADD)
        self._btn_del = PushButton(self)
        self._btn_del.setText("删除句")
        self._btn_del.setIcon(FIF.DELETE)
        self._btn_split = PushButton(self)
        self._btn_split.setText("在光标处拆分")
        self._btn_split.setIcon(FIF.CUT)
        self._btn_merge = PushButton(self)
        self._btn_merge.setText("合并选中")
        self._btn_merge.setIcon(FIF.CONNECT)
        self._btn_confirm = PushButton(self)
        self._btn_confirm.setText("确认编辑")
        self._btn_confirm.setIcon(FIF.ACCEPT)
        self._btn_confirm.setToolTip("将选中句的标脏状态清除为干净句（快捷键 Ctrl+K），固定编辑效果，重对齐时将跳过此句")
        self._btn_lock = PushButton(self)
        self._btn_lock.setText("锁定保护")
        self._btn_lock.setIcon(FIF.PIN)
        self._btn_lock.setToolTip("锁定/解锁选中句（快捷键 Ctrl+L），锁定后任何自动对齐（包括全文重对齐）均绝对不覆写")

        self._btn_split.setToolTip(
            "双击文本列进入编辑 → 把光标放到某字后面 → 按 Ctrl+Enter（或点此按钮）即在光标处拆分；"
            "普通 Enter 仍是确认内容。"
        )
        _buttons = (self._btn_add, self._btn_del, self._btn_split, self._btn_merge)
        for b in _buttons:
            b.setEnabled(False)
            b.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            tb.addWidget(b)
        tb.addStretch(1)
        # 确认编辑 / 锁定保护靠右（其余四个操作按钮靠左）
        self._btn_confirm.setEnabled(False)
        self._btn_lock.setEnabled(False)
        self._btn_confirm.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self._btn_lock.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        tb.addWidget(self._btn_confirm)
        tb.addWidget(self._btn_lock)
        # 最小宽度自动计算：六按钮完整文字宽 + 间距（防止面板被压窄时按钮文字
        # 被裁成「标处拆」类残缺显示；数值随文案/字体/DPI 变化自适应，不写死）
        _all_btns = (*_buttons, self._btn_confirm, self._btn_lock)
        _need = sum(b.sizeHint().width() for b in _all_btns) + tb.spacing() * (len(_all_btns) - 1) + 16
        self.setMinimumWidth(_need)
        self._lbl_summary = CaptionLabel("尚未打开项目", self)
        self._lbl_summary.setMinimumWidth(0)
        self._lbl_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tb.addWidget(self._lbl_summary)
        root.addLayout(tb)

        # 语言列的批量操作放回句级页面：选一次、点一次即可应用到全部句子。
        lang_bar = QHBoxLayout()
        lang_bar.setSpacing(8)
        lang_bar.addWidget(BodyLabel("全部句子语言", self))
        self._all_lang = ComboBox(self)
        for code, name in SENTENCE_LANGUAGES:
            if code:
                self._all_lang.addItem(name, userData=code)
        self._all_lang.setMinimumWidth(128)
        self._all_lang.setToolTip("Forced Aligner 需要每句有明确语言；这里可一键覆盖全部句子")
        lang_bar.addWidget(self._all_lang)
        self._btn_apply_lang = PrimaryPushButton(self)
        self._btn_apply_lang.setText("应用到全部")
        self._btn_apply_lang.setIcon(FIF.LANGUAGE)
        self._btn_apply_lang.setEnabled(False)
        self._btn_apply_lang.clicked.connect(self._on_apply_all_language)
        lang_bar.addWidget(self._btn_apply_lang)
        lang_bar.addStretch(1)
        # 批量删除句尾标点（全文）靠右放置；本行其余内容（语言标签/下拉/应用按钮）保持靠左
        self._btn_strip_punct = PushButton(self)
        self._btn_strip_punct.setText("删除全部句尾标点")
        self._btn_strip_punct.setIcon(FIF.ERASE_TOOL)
        self._btn_strip_punct.setEnabled(False)
        self._btn_strip_punct.setToolTip(
            "删除**全文所有句**末尾的标点（如 。！？…）及其占用的时间；句中标点不动，锁定句跳过，可 Ctrl+Z 撤销。"
        )
        self._btn_strip_punct.clicked.connect(lambda: self.strip_trailing_punct_requested.emit())
        lang_bar.addWidget(self._btn_strip_punct)
        root.addLayout(lang_bar)

        # ── Fluent 表格 ──
        self._table = TableWidget(self)
        self._table.setRowCount(0)
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([c[1] for c in _COLUMNS])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setBorderVisible(True)
        self._table.setBorderRadius(8)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.ElideRight)
        # 单击选中整行；双击或按编辑键激活编辑
        self._table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        vh = self._table.verticalHeader()
        vh.setVisible(False)
        vh.setMinimumSectionSize(40)
        vh.setDefaultSectionSize(42)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.Fixed)
        self._table.setColumnWidth(6, 100)
        self._table.itemSelectionChanged.connect(self._on_sel)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)

        # 右键上下文菜单
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)

        # 统一全表项目委托：共享单套 selectedRows / pressedRow / hoverRow，彻底消除列间状态割裂
        self._delegate = _SentenceTableDelegate(self._table)
        self._delegate.enter_split_requested.connect(self._on_text_enter_split)
        self._table.setItemDelegate(self._delegate)

        root.addWidget(self._table, 1)

        # ── 按钮接槽 ──
        self._btn_add.clicked.connect(self._on_add_clicked)
        self._btn_del.clicked.connect(self._on_del_clicked)
        self._btn_split.clicked.connect(self._on_split_clicked)
        self._btn_merge.clicked.connect(self._on_merge_clicked)
        self._btn_confirm.clicked.connect(self._on_confirm_clicked)
        self._btn_lock.clicked.connect(self._on_lock_clicked)

    # ── 对外 API（SubsEditor 转发用） ──────────────────────
    @property
    def project(self) -> Optional[SubtitleProject]:
        return self._project

    def set_project(self, project: SubtitleProject) -> None:
        self._project = project
        self._populate_all_rows()
        has_project = project is not None
        for b in (self._btn_add, self._btn_del, self._btn_split, self._btn_merge, self._btn_confirm, self._btn_lock):
            b.setEnabled(has_project)
        self._btn_apply_lang.setEnabled(bool(project and project.sentences))
        self._btn_strip_punct.setEnabled(bool(project and project.sentences))
        if project is not None:
            n = len(project.sentences)
            dur = project.media_duration or 0.0
            total_words = sum(s.word_count() for s in project.sentences)
            summary = f"共 {n} 句 · 媒体 {dur:.1f}s · 字词 {total_words}"
            self._lbl_summary.setText(summary)
            self._lbl_summary.setToolTip(summary)
            preferred = project.source_language
            if not preferred and project.sentences:
                preferred = project.sentences[0].language
            i = self._all_lang.findData(preferred)
            if i >= 0:
                self._all_lang.setCurrentIndex(i)

    def select_row(self, idx: int) -> None:
        if 0 <= idx < self._table.rowCount():
            self._table.selectRow(idx)
            self._table.scrollToItem(self._table.item(idx, 0),
                                    hint=QAbstractItemView.PositionAtCenter)

    def highlight_row(self, idx: int) -> None:
        """播放高亮：轻量把表格选中行同步到当前播放句。

        与 ``select_row``（用户点行语义）解耦：
        - ``blockSignals`` 下 ``selectRow``，不触发 ``row_selected`` → 不移动播放头、
          不重建波形字级图元（否则播放中每跨一句就会把播放头闪回句首并重建图元）；
        - 已高亮同一行时直接返回（播放中同句内每 tick 免空转）；
        - 仅当该行不可见时才滚动居中，同句内不反复滚动。
        """
        if not (0 <= idx < self._table.rowCount()):
            return
        sm = self._table.selectionModel()
        if sm is not None:
            current_rows = {i.row() for i in sm.selectedRows()}
            if current_rows == {idx}:
                return
        self._table.blockSignals(True)
        try:
            self._table.selectRow(idx)
        finally:
            self._table.blockSignals(False)
        item = self._table.item(idx, 0)
        if item is None:
            return
        center = self._table.visualItemRect(item).center()
        if not self._table.viewport().rect().contains(center):
            self._table.scrollToItem(item, hint=QAbstractItemView.PositionAtCenter)

    def refresh_row(self, idx: int) -> None:
        """轻量刷新单行（不重建全表）。用于拖边界/改时间后只更新那一行。"""
        if self._project is None or idx < 0 or idx >= len(self._project.sentences):
            return
        self._table.blockSignals(True)
        try:
            self._fill_row(idx, self._project.sentences[idx])
        finally:
            self._table.blockSignals(False)

    def refresh_summary(self) -> None:
        """轻量刷新底部统计栏。"""
        if self._project is None:
            return
        n = len(self._project.sentences)
        dur = self._project.media_duration or 0.0
        total_words = sum(s.word_count() for s in self._project.sentences)
        n_dirty = self._project.dirty_count()
        n_locked = len(self._project.locked_indices())
        lock_info = f" · 待对齐 {n_dirty}" if n_dirty else ""
        lock_info += f" · 锁定 {n_locked}" if n_locked else ""
        self._lbl_summary.setText(f"共 {n} 句 · 媒体 {dur:.1f}s · 字词 {total_words}{lock_info}")

    def selected_rows(self) -> list[int]:
        if self._table.selectionModel() is None:
            return []
        return sorted({i.row() for i in self._table.selectionModel().selectedRows()})

    def set_playhead(self, seconds: float) -> None:
        self._playhead_s = max(0.0, float(seconds))

    def mark_dirty_visual(self, idx: int) -> None:
        if self._project is not None and 0 <= idx < self._table.rowCount() and idx < len(self._project.sentences):
            s = self._project.sentences[idx]
            _apply_sentence_visual_state(self._table, idx, s.is_dirty, s.is_locked)

    def clear_dirty_visual(self, idx: int) -> None:
        if self._project is not None and 0 <= idx < self._table.rowCount() and idx < len(self._project.sentences):
            s = self._project.sentences[idx]
            _apply_sentence_visual_state(self._table, idx, s.is_dirty, s.is_locked)

    # ── 工具按钮槽 ──────────────────────────────────────
    def _current_row(self) -> int:
        rows = self.selected_rows()
        return rows[0] if rows else 0

    def _on_confirm_clicked(self) -> None:
        rows = self.selected_rows()
        if rows:
            self.confirm_sentences_requested.emit(rows)

    def _on_lock_clicked(self) -> None:
        rows = self.selected_rows()
        if rows:
            self.toggle_lock_sentences_requested.emit(rows)

    def _show_context_menu(self, pos) -> None:
        if self._project is None or not self._project.sentences:
            return
        rows = self.selected_rows()
        if not rows:
            item = self._table.itemAt(pos)
            if item is not None:
                row = item.row()
                self.select_row(row)
                rows = [row]
            else:
                return

        menu = RoundMenu(parent=self)

        act_confirm = Action(FIF.ACCEPT, f"确认编辑 (清除待对齐标记, {len(rows)} 句)")
        act_dirty = Action(FIF.HIGHTLIGHT, f"标记为待重对齐 (标脏, {len(rows)} 句)")

        all_locked = all(
            self._project.sentences[r].is_locked
            for r in rows if r < len(self._project.sentences)
        )
        lock_text = f"解除锁定 ({len(rows)} 句)" if all_locked else f"锁定保护 ({len(rows)} 句)"
        act_lock = Action(FIF.PIN, lock_text)

        act_confirm.triggered.connect(lambda: self.confirm_sentences_requested.emit(rows))
        act_dirty.triggered.connect(lambda: self.mark_dirty_sentences_requested.emit(rows))
        act_lock.triggered.connect(lambda: self.toggle_lock_sentences_requested.emit(rows))

        menu.addAction(act_confirm)
        menu.addAction(act_dirty)
        menu.addAction(act_lock)
        menu.addSeparator()

        act_add = Action(FIF.ADD, "插入句")
        act_add.triggered.connect(self._on_add_clicked)
        menu.addAction(act_add)

        act_del = Action(FIF.DELETE, f"删除选中句 ({len(rows)} 句)")
        act_del.triggered.connect(self._on_del_clicked)
        menu.addAction(act_del)

        if len(rows) >= 2:
            act_merge = Action(FIF.CONNECT, f"合并选中 ({len(rows)} 句)")
            act_merge.triggered.connect(self._on_merge_clicked)
            menu.addAction(act_merge)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_apply_all_language(self) -> None:
        code = self._all_lang.currentData() or ""
        if code and self._project is not None and self._project.sentences:
            self.set_all_language_requested.emit(code)

    def _on_add_clicked(self) -> None:
        if self._project is None or not self._project.sentences:
            return
        row = self._current_row()
        s = self._project.sentences[row]
        new_sentence = Sentence(
            text="",
            start_time=s.end_time,
            end_time=s.end_time + 3.0,
            language=s.language,
            words=[],
            is_dirty=True,
        )
        self.add_sentence_requested.emit(row + 1, new_sentence)

    def _on_del_clicked(self) -> None:
        if self._project is None:
            return
        rows = sorted({i.row() for i in self._table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self.delete_sentences_requested.emit(rows)

    def _on_split_clicked(self) -> None:
        if self._project is None or not self._project.sentences:
            return
        row = self._current_row()
        info = self._delegate.split_info_for(row)
        if info is not None:
            char_idx, text = info
            cur = self._table.item(row, 4).text() if self._table.item(row, 4) else ""
            if text == cur and 0 < char_idx < len(cur):
                self.split_sentence_at_char_requested.emit(row, int(char_idx), str(text))
                return
        self.split_sentence_requested.emit(row, self._playhead_s)

    def _on_text_enter_split(self, row: int, char_index: int, new_text: str) -> None:
        if self._project is None or not (0 <= row < len(self._project.sentences)):
            return
        self.split_sentence_at_char_requested.emit(row, int(char_index), str(new_text))

    def _on_merge_clicked(self) -> None:
        if self._project is None:
            return
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        if len(rows) < 2:
            return
        self.merge_sentences_requested.emit(rows)

    # ── 内部 ───────────────────────────────────────────
    def _populate_all_rows(self) -> None:
        self._table.blockSignals(True)
        try:
            if self._project is None:
                self._table.setRowCount(0)
                return
            self._table.setRowCount(len(self._project.sentences))
            for i, s in enumerate(self._project.sentences):
                self._fill_row(i, s)
        finally:
            self._table.blockSignals(False)

    def _fill_row(self, row: int, s: Sentence) -> None:
        if s.timed:
            start_str = format_time(s.start_time)
            end_str = format_time(s.end_time)
            dur_str = format_time(s.duration)
        else:
            start_str = end_str = dur_str = ""
        vals = [
            (str(row + 1),
             Qt.ItemIsSelectable | Qt.ItemIsEnabled),
            (start_str,
             Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable),
            (end_str,
             Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable),
            (dur_str,
             Qt.ItemIsSelectable | Qt.ItemIsEnabled),
            (s.text,
             Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable),
            (str(s.word_count()),
             Qt.ItemIsSelectable | Qt.ItemIsEnabled),
            (code_to_name(s.language),
             Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable),
        ]
        for col, (text, flags) in enumerate(vals):
            it = QTableWidgetItem(text)
            it.setFlags(flags)
            if col in (0, 5, 6):
                it.setTextAlignment(Qt.AlignCenter)
            elif col in (1, 2, 3):
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(row, col, it)
        _apply_sentence_visual_state(self._table, row, s.is_dirty, s.is_locked)

    def _on_sel(self) -> None:
        rows = self.selected_rows()
        if rows:
            self.row_selected.emit(rows[0])

    def _on_cell_clicked(self, row: int, col: int) -> None:
        if col == 0:
            if self._project is not None and 0 <= row < len(self._project.sentences):
                self.row_number_clicked.emit(row)
        elif col == 6:
            # 单击语言列单元格，立即激活并弹出语言选择下拉菜单
            if self._project is not None and 0 <= row < len(self._project.sentences):
                item = self._table.item(row, 6)
                if item is not None:
                    self._table.editItem(item)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._project is None:
            return
        row, col = item.row(), item.column()
        if col == 4:
            new_text = item.text()
            if 0 <= row < len(self._project.sentences):
                self.text_changed.emit(row, new_text)
        elif col in (1, 2):
            if 0 <= row < len(self._project.sentences):
                s = self._project.sentences[row]
                if col == 1:
                    new_start = parse_time(item.text(), s.start_time)
                    new_end = s.end_time
                else:
                    new_start = s.start_time
                    new_end = parse_time(item.text(), s.end_time)
                self.time_changed.emit(row, new_start, new_end)
        elif col == 6:
            if 0 <= row < len(self._project.sentences):
                name = item.text() or ""
                code = name_to_code(name)
                self.language_changed.emit(row, code)


__all__ = ["SentenceLevelView"]
