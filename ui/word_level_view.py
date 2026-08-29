"""ui.word_level_view — 字级精度页面

历史问题：
    旧 SubsEditor 里"字词级"是一个空占位 Tab，字级精度显示未实现；
    逐字效果（高亮样式/k-tag 模式）散落在偏好设置里。

本页面实现：
    - 选中某句后，表格展示该句的字/词级时间戳（字、起始、结束、时长、语言）
    - 时间戳可编辑（编辑后回写 sentence.words 并标脏，待重对齐）
    - 顶部显示该句文本与字数统计
    - 提供"对选中句重对齐"按钮（字级失效时手动刷新）
    - 无选中句 / 无字级时显示明确的空状态提示
    - 标点字（is_punct）以斜体灰色标注，与"不参与逐字动效"的语义一致

这是只读+轻编辑视图；复杂的拆分/合并仍在句级表格完成。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, FluentIcon as FIF, PrimaryPushButton, TableWidget,
)

from subs.models import SubtitleProject, WordTimestamp
from .languages import code_to_name
from .time_utils import format_time as _fmt_time, parse_time as _parse_time


_COLUMNS = ["#", "字/词", "起始", "结束", "时长", "语言"]
_PUNCT_COLOR = QColor("#888888")


class WordLevelView(QWidget):
    """字级精度视图：展示/编辑当前选中句的字级时间戳。"""

    # 请求对某句重对齐（idx）→ MainWindow 接 AlignWorker sentences 模式
    realign_sentence_requested = Signal(int)
    # 字级被编辑 → 通知外部刷新（标脏等）
    word_edited = Signal(int)
    # 字级单元格编辑信号 → sent_idx, word_idx, new_start, new_end
    word_time_edited = Signal(int, int, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("word_level_view")
        self._project: Optional[SubtitleProject] = None
        self._current_idx: int = -1
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # 顶部信息条
        info_bar = QHBoxLayout()
        self._lbl_sentence = BodyLabel("请先在「句级字幕」中选择一句", self)
        self._lbl_sentence.setWordWrap(True)
        info_bar.addWidget(self._lbl_sentence, 1)
        self._btn_realign = PrimaryPushButton(self)
        self._btn_realign.setText("重对齐此句")
        self._btn_realign.setIcon(FIF.HIGHTLIGHT)
        self._btn_realign.setToolTip("对当前选中句重新运行 Forced Aligner，刷新字级时间戳")
        self._btn_realign.setEnabled(False)
        self._btn_realign.clicked.connect(self._on_realign)
        info_bar.addWidget(self._btn_realign)
        root.addLayout(info_bar)

        # 字级 Fluent 表格
        self._table = TableWidget(self)
        self._table.setRowCount(0)
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setBorderVisible(True)
        self._table.setBorderRadius(8)
        self._table.setWordWrap(False)
        vh = self._table.verticalHeader()
        vh.setVisible(False)
        vh.setMinimumSectionSize(36)
        vh.setDefaultSectionSize(38)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (2, 3, 4, 5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self._table, 1)

        # 底部状态
        self._lbl_status = CaptionLabel("", self)
        self._lbl_status.setWordWrap(True)
        root.addWidget(self._lbl_status)

    # ── 对外 API ──────────────────────────────────────
    def set_project(self, project: Optional[SubtitleProject]) -> None:
        self._project = project
        if self._current_idx >= 0 and project is not None and \
                self._current_idx < len(project.sentences):
            self.show_sentence(self._current_idx, force=True)
        else:
            self._clear("请先在「句级字幕」中选择一句")

    def show_sentence(self, idx: int, *, force: bool = False) -> None:
        """展示第 idx 句的字级表格。

        播放推进时每 tick 都会被调用；同一句重复展示直接返回（不重建表格），
        跨句才真正刷新。``force=True`` 用于「同一句内容已变」的强制重建
        （切项目 / 字级编辑后刷新）。
        """
        if not force and self._current_idx == idx and self._table.rowCount() > 0:
            return
        self._current_idx = idx
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            self._clear("请先在「句级字幕」中选择一句")
            return
        s = self._project.sentences[idx]
        self._lbl_sentence.setText(f"S{idx+1}：{s.text or '（空句）'}")
        self._btn_realign.setEnabled(True)

        if not s.words:
            self._clear(
                "此句暂无字级时间戳。点击「重对齐此句」生成，或在句级页面编辑后用「全文重对齐」。"
            )
            return

        self._loading = True
        try:
            self._table.setRowCount(len(s.words))
            for r, w in enumerate(s.words):
                self._fill_row(r, w)
        finally:
            self._loading = False

        n_real = sum(1 for w in s.words if not w.is_punct)
        n_punct = len(s.words) - n_real
        dur = s.duration
        # 字级内容一致性守门——漂移在句级/波形完全不可见，在这里显形
        from core.text_utils import words_content_match as _wcm
        if not _wcm(s):
            self._lbl_status.setStyleSheet("color:#d4380d; font-weight:bold;")
            self._lbl_status.setText(
                "⚠ 字级内容与句文本已不一致（文本修改后未重对齐，或分配漂移）。"
                "逐字导出/手柄拖拽读的仍是下方旧词序列——请先「重对齐此句」。\n"
                f"共 {len(s.words)} 个片段（{n_real} 字/词，{n_punct} 标点）· 句时长 {dur:.3f}s"
            )
        else:
            self._lbl_status.setStyleSheet("")
            self._lbl_status.setText(
                f"共 {len(s.words)} 个片段（{n_real} 字/词，{n_punct} 标点）· 句时长 {dur:.3f}s"
            )

    @property
    def current_sentence_index(self) -> int:
        """当前展示的句子索引（未展示任何句为 -1），供外部判断是否已在显示该句。"""
        return self._current_idx

    def refresh_current(self) -> None:
        """外部修改后刷新当前句（保持选中）。"""
        if self._current_idx >= 0:
            self.show_sentence(self._current_idx, force=True)

    def update_word_times(self, sent_idx: int, words: List[WordTimestamp]) -> None:
        """波形拖动字手柄时的轻量实时刷新（不重建表格）。"""
        if sent_idx != self._current_idx or self._loading or self._table.rowCount() != len(words):
            return
        self._table.blockSignals(True)
        try:
            for r, w in enumerate(words):
                dur = max(0.0, w.end_time - w.start_time)
                it_s = self._table.item(r, 2)
                it_e = self._table.item(r, 3)
                it_d = self._table.item(r, 4)
                if it_s:
                    it_s.setText(_fmt_time(w.start_time))
                if it_e:
                    it_e.setText(_fmt_time(w.end_time))
                if it_d:
                    it_d.setText(f"{dur:.3f}s")
        finally:
            self._table.blockSignals(False)

    def set_playhead(self, seconds: float) -> None:
        """播放位置变化时高亮当前正在播放的字/词所在行。

        仅在当前页展示的句子有字级时间戳时生效。
        """
        if self._project is None or self._current_idx < 0:
            return
        if self._current_idx >= len(self._project.sentences):
            return
        s = self._project.sentences[self._current_idx]
        if not s.words:
            return

        # 找当前时间所在的 word 行
        target_row = -1
        for r, w in enumerate(s.words):
            if w.start_time <= seconds <= w.end_time:
                target_row = r
                break
        # 如果没落在任何 word 内，找最近的
        if target_row < 0:
            best_dist = float("inf")
            for r, w in enumerate(s.words):
                mid = (w.start_time + w.end_time) / 2.0
                d = abs(seconds - mid)
                if d < best_dist:
                    best_dist = d
                    target_row = r

        # 高亮该行（选中 + 仅在不可见时滚动，避免播放中每跨一字滚动抖动）
        if 0 <= target_row < self._table.rowCount():
            sm = self._table.selectionModel()
            if sm is None:
                return
            current_rows = {i.row() for i in sm.selectedRows()}
            if current_rows == {target_row}:
                return
            # blockSignals：播放高亮不应触发选择信号/重绘风暴
            self._table.blockSignals(True)
            try:
                self._table.selectRow(target_row)
            finally:
                self._table.blockSignals(False)
            item = self._table.item(target_row, 0)
            if item is not None:
                center = self._table.visualItemRect(item).center()
                if not self._table.viewport().rect().contains(center):
                    self._table.scrollToItem(
                        item, hint=QAbstractItemView.ScrollHint.PositionAtCenter,
                    )

    # ── 内部 ──────────────────────────────────────────
    def _clear(self, msg: str) -> None:
        self._loading = True
        self._table.setRowCount(0)
        self._loading = False
        self._lbl_status.setStyleSheet("")
        self._lbl_status.setText(msg)
        self._btn_realign.setEnabled(
            self._project is not None and 0 <= self._current_idx < len(self._project.sentences)
        )

    def _fill_row(self, r: int, w: WordTimestamp) -> None:
        dur = max(0.0, w.end_time - w.start_time)
        vals = [
            (str(r + 1), False),
            (w.text, False),
            (_fmt_time(w.start_time), True),
            (_fmt_time(w.end_time), True),
            (f"{dur:.3f}s", False),
            (code_to_name(w.language) or "—", False),
        ]
        for c, (text, editable) in enumerate(vals):
            it = QTableWidgetItem(text)
            flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
            if editable:
                flags |= Qt.ItemIsEditable
            it.setFlags(flags)
            if c in (0, 2, 3, 4):
                it.setTextAlignment(Qt.AlignCenter if c == 0 else (Qt.AlignRight | Qt.AlignVCenter))
            if w.is_punct:
                it.setForeground(QBrush(_PUNCT_COLOR))
                f = it.font()
                f.setItalic(True)
                it.setFont(f)
            self._table.setItem(r, c, it)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or self._project is None:
            return
        row, col = item.row(), item.column()
        if self._current_idx < 0 or self._current_idx >= len(self._project.sentences):
            return
        s = self._project.sentences[self._current_idx]
        if row >= len(s.words):
            return
        w = s.words[row]
        new_start = w.start_time
        new_end = w.end_time
        if col == 2:  # 起始
            parsed = _parse_time(item.text(), w.start_time)
            if parsed is not None and abs(parsed - w.start_time) > 1e-4:
                new_start = parsed
            else:
                item.setText(_fmt_time(w.start_time))
        elif col == 3:  # 结束
            parsed = _parse_time(item.text(), w.end_time)
            if parsed is not None and abs(parsed - w.end_time) > 1e-4:
                new_end = parsed
            else:
                item.setText(_fmt_time(w.end_time))
        if new_start != w.start_time or new_end != w.end_time:
            # 视图只发信号不写模型（架构红线）——若先写模型再发命令，EditWordTimeCommand
            # 的「旧值快照」捞到已被本视图改过的数据，Ctrl+Z 撤销字级时间无效。
            # 只发信号，写入/标脏/fix_times 全部由 EditWordTimeCommand 完成，
            # 表格刷新走命令 on_change 回调链（保证显示的是规范化后的时间格式）。
            self.word_time_edited.emit(self._current_idx, row, new_start, new_end)

    def _on_realign(self) -> None:
        if self._current_idx >= 0:
            self.realign_sentence_requested.emit(self._current_idx)
