"""ui.main_window.navigation — 面板互连、播放头与项目应用。"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut

from subs.models import SubtitleProject

logger = logging.getLogger("ui.main_window")


class NavigationMixin:
    def _wire_panel_interconnects(self) -> None:
        self.editor.row_selected.connect(self._on_sentence_row_selected)
        self.editor.row_number_clicked.connect(self._jump_to_sentence_start)
        self.editor.language_changed.connect(self._on_language_changed)
        self.editor.set_all_language_requested.connect(self._on_set_all_language)
        self.player.position_changed.connect(self._on_playback_moved)
        self.waveform.playhead_seeked.connect(self._on_waveform_playhead_seek)
        self.waveform.click_seeked.connect(self._on_waveform_click_seek)
        self.editor.text_changed.connect(self._on_text_edited)
        self.editor.time_changed.connect(self._on_time_edited)
        self.editor.word_time_edited.connect(self._on_word_time_edited)
        self.editor.add_sentence_requested.connect(self._on_add_sentence)
        self.editor.delete_sentences_requested.connect(self._on_delete_sentences)
        self.editor.split_sentence_requested.connect(self._on_split_sentence)
        self.editor.split_sentence_at_char_requested.connect(self._on_split_sentence_at_char)
        self.editor.merge_sentences_requested.connect(self._on_merge_sentences)
        self.editor.confirm_sentences_requested.connect(self._on_confirm_sentences)
        self.editor.mark_dirty_sentences_requested.connect(self._on_mark_dirty_sentences)
        self.editor.toggle_lock_sentences_requested.connect(self._on_toggle_lock_sentences)
        self.editor.strip_trailing_punct_requested.connect(self.strip_trailing_punct)
        # 脏句重对齐入口仅工具栏/菜单 Ctrl+R（_act_align_dirty → mode=dirty）；
        # 旧 realign_dirty_requested 信号无 emit 且曾误接到全文重对齐，已删除。
        self.editor.realign_sentence_requested.connect(self._on_realign_single_sentence)
        self.waveform.boundary_drag_finished.connect(self._on_boundary_drag_finished)
        self.waveform.word_boundary_dragged.connect(self._on_waveform_word_boundary_dragged)
        self.waveform.word_boundary_drag_finished.connect(self._on_waveform_word_boundary_drag_finished)
        self.waveform.view_range_changed.connect(self._on_waveform_view_range_changed)
        self.editor.page_changed.connect(self._on_editor_page_changed)


    def _setup_space_shortcut(self) -> None:
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        self._space_shortcut.setContext(Qt.ApplicationShortcut)
        self._space_shortcut.activated.connect(self._on_space_play_pause)


    def _on_space_play_pause(self) -> None:
        if self._is_editing_text():
            return
        self.player.toggle_play_pause()


    def _is_editing_text(self) -> bool:
        focus = self.focusWidget()
        from PySide6.QtWidgets import QLineEdit, QTextEdit, QPlainTextEdit
        return isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit))


    def _on_waveform_view_range_changed(self, lo: float, hi: float) -> None:
        """当处于句级字幕模式时，实时记忆用户调整的波形视野范围。"""
        if not self.editor.is_word_view_active():
            self._sentence_mode_x_range = (lo, hi)


    def _on_editor_page_changed(self, route: str) -> None:
        if self._project is None or not self._project.sentences:
            return
        if route == "word":
            if self._sentence_mode_x_range is None:
                self._sentence_mode_x_range = self.waveform.get_view_range()
            rows = self.editor.selected_rows()
            target = rows[0] if rows else 0
            self.waveform.set_active_sentence(target, force=True)
            self.waveform.focus_on_sentence(target, target_ratio=0.80)
        elif route == "sentence":
            if self._sentence_mode_x_range is not None:
                self.waveform.set_view_range(*self._sentence_mode_x_range)


    def _on_sentence_row_selected(self, idx: int) -> None:
        self._move_playhead_by_row(idx)
        if self.editor.is_word_view_active():
            # 仅在切换到不同句子时自动 80% 聚焦；同一句内的微调不重置用户的缩放视野
            is_different = (self.waveform.active_sentence_index != idx)
            self.waveform.set_active_sentence(idx, force=True)
            if is_different:
                self.waveform.focus_on_sentence(idx, target_ratio=0.80)
        else:
            self.waveform.set_active_sentence(idx, force=True)


    def _move_playhead_by_row(self, idx: int) -> None:
        if self._project is None:
            return
        if 0 <= idx < len(self._project.sentences):
            s = self._project.sentences[idx]
            if s.start_time is not None:
                self.waveform.set_playhead(float(s.start_time))


    def _on_waveform_click_seek(self, t: float) -> None:
        # 波形点击：播放头停到点击处，字级视图/波形字级图元跟随该句。
        self.player.seek(float(t))
        self.waveform.set_playhead(float(t))
        self._follow_at(float(t))

    def _on_waveform_playhead_seek(self, t: float) -> None:
        # 拖播放头：同波形点击。
        self.player.seek(float(t))
        self.waveform.set_playhead(float(t))
        self._follow_at(float(t))

    def _follow_at(self, t: float) -> None:
        """让句级选中 + 波形字级图元 + 字级视图跟随时间 t 所在句（幂等，可重复调用）。"""
        if not (self._project and self._project.sentences):
            return
        idx = self._project.find_sentence_at(float(t))
        if idx is not None:
            self._follow_sentence(idx)

    def _follow_sentence(self, idx: int) -> None:
        """光标所在句 = 句级高亮句 + 波形字级图元句 + 字级视图句。"""
        self.editor.highlight_row(idx)                      # 句级高亮 + 字级视图跟随
        self.waveform.set_active_sentence(idx, force=True)  # 波形字级边界手柄跟随


    def _jump_to_sentence_start(self, idx: int) -> None:
        if self._project is None:
            return
        if 0 <= idx < len(self._project.sentences):
            s = self._project.sentences[idx]
            if s.start_time is not None:
                t = float(s.start_time)
                self.player.seek(t)
                self.player.play()
                self.waveform.set_playhead(t)
                # 目标句不在当前视野时，平移视窗让光标落在视野左侧 20%
                self.waveform.follow_playhead(t)


    def _on_playback_moved(self, t: float) -> None:
        self.waveform.set_playhead(t)
        # 播放中光标移出视野 → 平移视窗使光标落回视野左侧 20%（句级/字级一致）
        self.waveform.follow_playhead(t)
        self.editor.set_playhead(t)   # 更新句级「在光标处拆分」用播放头 + 字级页内当前字高亮
        self._follow_at(t)


    def _apply_project(self, project: SubtitleProject) -> None:
        if not project.source_media_path and self._project and self._project.source_media_path:
            project.source_media_path = self._project.source_media_path
        self._project = project
        self._sentence_mode_x_range = None
        media_path = (project.source_media_path or project.audio_path or "").strip()
        has_media = bool(media_path)
        if project.source_media_path:
            self._last_export_dir = str(Path(project.source_media_path).parent)
            self._last_export_stem = Path(project.source_media_path).stem
            self._sb_path.setText(f"媒体：{Path(project.source_media_path).name}")
        elif project.audio_path:
            self._sb_path.setText(f"音频：{Path(project.audio_path).name}")
        elif project.sentences:
            self._sb_path.setText("工程：仅字幕（未关联媒体）")
        self.editor.set_project(project)
        self.waveform.set_project(project)
        self.player.set_project(project)   # 播放面板字幕预览的数据源
        self.workflow.set_actions_project_state(
            has_media=has_media,
            has_sentences=bool(project.sentences),
        )

        # 保持/恢复当前选中句的激活态与字级图元展示（解决全文重对齐后字级图元消失的问题）
        if project.sentences:
            rows = self.editor.selected_rows()
            target = rows[0] if rows else 0
            self.editor.select_row(target)
            self.waveform.set_active_sentence(target, force=True)
            if self.editor.is_word_view_active():
                self.waveform.focus_on_sentence(target, target_ratio=0.80)


    def dragEnterEvent(self, event) -> None:
        self.project_ctrl.handle_drag_enter(event)


    def dropEvent(self, event) -> None:
        self.project_ctrl.handle_drop(event)


