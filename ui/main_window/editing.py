"""ui.main_window.editing — Undo 编辑分发、导出与刷新。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from qfluentwidgets import InfoBar, InfoBarPosition

from subs.models import Sentence
from ui.commands import (
    AddSentenceCommand, BoundaryDragCommand,
    ChangeSentenceLanguageCommand,
    DeleteSentencesCommand, EditTextCommand, EditTimeCommand, MergeSentencesCommand,
    SplitSentenceCommand, SplitSentenceByCharCommand, ConfirmSentencesCommand,
    SetSentencesDirtyCommand, ToggleLockSentencesCommand, WordBoundaryDragCommand,
    EditWordTimeCommand, StripTrailingPunctCommand,
)
from ui.export_controller import do_export

logger = logging.getLogger("ui.main_window")


class EditingMixin:
    def _on_export_requested(self, kind: str) -> None:
        if self._project is None or not self._project.sentences:
            QMessageBox.information(self, "提示", "当前项目没有可导出的字幕。")
            return

        # 保存导出面板的最新选项
        self._export_panel.save_options()

        word_style = self._export_panel.word_style.word_highlight_style()
        k_mode = self._export_panel.word_style.k_mode()
        ass_style = self._export_panel.ass_style.current_style()
        template_prefs = self._export_panel.karaoke_template.current_template_prefs()

        saved = do_export(
            project=self._project,
            kind=kind,
            parent=self,
            word_style=word_style,
            ass_style=ass_style,
            k_mode=k_mode,
            template_prefs=template_prefs,
            default_dir=self._last_export_dir or "",
            default_stem=self._last_export_stem or "",
        )
        if saved:
            self._last_export_dir = str(Path(saved).parent)
            saved_stem = Path(saved).stem
            # 成品 ASS 默认加 .applied 防覆盖 k-tag 源，但不要污染下一次其它格式的默认 stem。
            if kind == "karaoke_applied" and saved_stem.endswith(".applied"):
                saved_stem = saved_stem[:-len(".applied")]
            self._last_export_stem = saved_stem
            try:
                from core.app_config import load_preferences, save_preferences
                prefs = load_preferences()
                prefs.export.default_dir = self._last_export_dir
                save_preferences(prefs)
            except Exception:
                logger.debug("[偏好] 保存导出目录记忆失败")
            InfoBar.success(
                title="导出成功",
                content=f"已保存到 {Path(saved).name}",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )


    def _on_text_edited(self, idx: int, new_text: str) -> None:
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return
        sent = self._project.sentences[idx]
        if sent.text == new_text:
            return
        cmd = EditTextCommand(
            self._project, idx, new_text,
            self._make_row_refresher(idx, sent),
        )
        self._undo_stack.push(cmd)


    def _on_time_edited(self, idx: int, new_start: float, new_end: float) -> None:
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return
        sent = self._project.sentences[idx]
        if abs(sent.start_time - new_start) < 1e-4 and abs(sent.end_time - new_end) < 1e-4:
            return
        cmd = EditTimeCommand(
            self._project, idx, new_start, new_end,
            self._make_row_refresher(idx, sent),
        )
        self._undo_stack.push(cmd)


    def _on_language_changed(self, idx: int, lang_short: str) -> None:
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return
        sent = self._project.sentences[idx]
        if sent.language == lang_short:
            return
        cmd = ChangeSentenceLanguageCommand(
            self._project, [idx], lang_short,
            self._make_row_refresher(idx, sent),
        )
        self._undo_stack.push(cmd)


    def _on_set_all_language(self, lang_short: str) -> None:
        if self._project is None or not self._project.sentences:
            return
        # 必须走 QUndoStack（架构红线：UI 不直接改模型）——
        # 复用 ChangeSentenceLanguageCommand（逐句保存旧语言/旧脏标记，精准可撤回）。
        changed_indices = [
            idx for idx, s in enumerate(self._project.sentences)
            if s.language != lang_short
        ]
        if not changed_indices:
            self._sb_mode.setText("模式：所有句子已是该语言")
            return

        def _refresh() -> None:
            for idx in changed_indices:
                self.editor.refresh_row(idx)
            self.editor.refresh_summary()
            if self._project is not None:
                self.waveform.update_blocks_in_place(self._project)

        cmd = ChangeSentenceLanguageCommand(self._project, changed_indices, lang_short, _refresh)
        self._undo_stack.push(cmd)
        self._project.source_language = lang_short
        self._mark_project_modified()  # source_language 不在该 UndoCommand 快照内
        self._sb_mode.setText("模式：已设置全部句子语言")


    def _on_add_sentence(self, row: int, new_sentence: Sentence) -> None:
        if self._project is None:
            return
        cmd = AddSentenceCommand(self._project, row, new_sentence, self._refresh_after_edit)
        self._undo_stack.push(cmd)


    def _on_delete_sentences(self, rows: list) -> None:
        if self._project is None or not rows:
            return
        valid = [r for r in rows if 0 <= r < len(self._project.sentences)]
        if not valid:
            return
        cmd = DeleteSentencesCommand(self._project, valid, self._refresh_after_edit)
        self._undo_stack.push(cmd)


    def _on_split_sentence(self, row: int, cut_time: float) -> None:
        if self._project is None or not (0 <= row < len(self._project.sentences)):
            return
        cmd = SplitSentenceCommand(self._project, row, float(cut_time), self._refresh_after_edit)
        self._undo_stack.push(cmd)


    def _on_split_sentence_at_char(self, row: int, char_index: int, new_text: str) -> None:
        if self._project is None or not (0 <= row < len(self._project.sentences)):
            return
        cmd = SplitSentenceByCharCommand(
            self._project, row, int(char_index), self._refresh_after_edit, new_text=str(new_text),
        )
        self._undo_stack.push(cmd)


    def _on_merge_sentences(self, rows: list) -> None:
        if self._project is None or len(rows) < 2:
            return
        valid = sorted({r for r in rows if 0 <= r < len(self._project.sentences)})
        if len(valid) < 2:
            return
        cmd = MergeSentencesCommand(self._project, valid, self._refresh_after_edit)
        self._undo_stack.push(cmd)


    def _on_boundary_drag_finished(self, idx: int, edge: str, new_s: float) -> None:
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return
        sent = self._project.sentences[idx]
        new_start = float(new_s) if edge == "start" else None
        new_end = float(new_s) if edge == "end" else None
        if new_start is not None and abs(sent.start_time - new_start) < 1e-4:
            return
        if new_end is not None and abs(sent.end_time - new_end) < 1e-4:
            return

        # 近重合 end/start 的命中歧义已由 WaveformView 按拖动方向解析：
        # 向左发出前句 end，向右发出后句 start。这里始终只修改收到的单侧边界，
        # 不再把无缝相邻句合并为不可拉开的“共享边界”。
        cmd = BoundaryDragCommand(
            self._project, idx, new_start, new_end,
            self._make_row_refresher(idx, sent),
        )
        self._undo_stack.push(cmd)
        edge_name = "句首" if edge == "start" else "句尾"
        self._sb_mode.setText(f"模式：已移动 S{idx + 1} {edge_name}")


    def _on_confirm_sentences(self, rows: list) -> None:
        if self._project is None or not rows:
            return
        valid = [r for r in rows if 0 <= r < len(self._project.sentences)]
        if not valid:
            return
        cmd = ConfirmSentencesCommand(
            self._project, valid,
            lambda: [self._refresh_single_row(r) for r in valid],
        )
        self._undo_stack.push(cmd)
        self._sb_mode.setText(f"模式：已确认 {len(valid)} 句（清除待对齐标记）")


    def _on_mark_dirty_sentences(self, rows: list) -> None:
        if self._project is None or not rows:
            return
        valid = [r for r in rows if 0 <= r < len(self._project.sentences)]
        if not valid:
            return
        cmd = SetSentencesDirtyCommand(
            self._project, valid,
            lambda: [self._refresh_single_row(r) for r in valid],
        )
        self._undo_stack.push(cmd)
        self._sb_mode.setText(f"模式：已标脏 {len(valid)} 句（待 AI 重对齐）")


    def _on_toggle_lock_sentences(self, rows: list) -> None:
        if self._project is None or not rows:
            return
        valid = [r for r in rows if 0 <= r < len(self._project.sentences)]
        if not valid:
            return
        cmd = ToggleLockSentencesCommand(
            self._project, valid,
            lambda: [self._refresh_single_row(r) for r in valid],
        )
        self._undo_stack.push(cmd)
        self._sb_mode.setText(f"模式：已切换 {len(valid)} 句锁定保护")


    def strip_trailing_punct(self) -> None:
        """批量删除**全文**句尾标点（字符 + 时间）；锁定句跳过。"""
        if self._project is None or not self._project.sentences:
            return
        valid = list(range(len(self._project.sentences)))

        def _refresh() -> None:
            for row in valid:
                self._refresh_single_row(row)
            self.player.refresh_subtitle_content()

        cmd = StripTrailingPunctCommand(self._project, valid, _refresh)
        self._undo_stack.push(cmd)
        n = getattr(cmd, "_changed", 0)
        if n:
            self._sb_mode.setText(f"模式：已删除 {n} 句的句尾标点")
        else:
            self._sb_mode.setText("模式：没有可删除的句尾标点（或目标句均已锁定）")


    def _on_shortcut_confirm(self) -> None:
        rows = self.editor.selected_rows()
        if rows:
            self._on_confirm_sentences(rows)


    def _on_shortcut_toggle_lock(self) -> None:
        rows = self.editor.selected_rows()
        if rows:
            self._on_toggle_lock_sentences(rows)


    def _on_waveform_word_boundary_dragged(self, sent_idx: int, preview_words: list) -> None:
        if self._project is None or not (0 <= sent_idx < len(self._project.sentences)):
            return
        self.editor.update_word_times(sent_idx, preview_words)


    def _on_waveform_word_boundary_drag_finished(self, sent_idx: int, old_words: list, new_words: list) -> None:
        if self._project is None or not (0 <= sent_idx < len(self._project.sentences)):
            return
        sent = self._project.sentences[sent_idx]
        cmd = WordBoundaryDragCommand(
            self._project, sent_idx, old_words, new_words,
            self._make_row_refresher(sent_idx, sent),
        )
        self._undo_stack.push(cmd)
        self._sb_mode.setText(f"模式：已微调句 S{sent_idx+1} 字边界")


    def _on_word_time_edited(self, sent_idx: int, word_idx: int, new_start: float, new_end: float) -> None:
        if self._project is None or not (0 <= sent_idx < len(self._project.sentences)):
            return
        sent = self._project.sentences[sent_idx]
        cmd = EditWordTimeCommand(
            self._project, sent_idx, word_idx, new_start, new_end,
            self._make_row_refresher(sent_idx, sent),
        )
        self._undo_stack.push(cmd)


    def _refresh_after_edit(self, preferred_sid: int | None = None) -> None:
        """行数/顺序变化时全量刷新；优先按稳定 sid 恢复编辑对象。"""
        if self._project is None:
            return
        active_idx = -1
        if preferred_sid is not None:
            active_idx = next(
                (
                    index for index, sentence in enumerate(self._project.sentences)
                    if sentence.sid == preferred_sid
                ),
                -1,
            )
        if active_idx < 0:
            rows = self.editor.selected_rows()
            active_idx = rows[0] if rows else (
                self.waveform.active_sentence_index
                if self.waveform.active_sentence_index >= 0 else 0
            )
        if self._project.sentences:
            active_idx = max(0, min(active_idx, len(self._project.sentences) - 1))
        else:
            active_idx = -1

        self.editor.set_project(self._project)
        self.waveform.update_blocks_in_place(self._project)

        if 0 <= active_idx < len(self._project.sentences):
            self.editor.select_row(active_idx)
            self.waveform.set_active_sentence(active_idx, force=True)
            if self.editor.is_word_view_active():
                self.editor.show_word_sentence(active_idx)
        self.player.refresh_subtitle_content()


    def _make_row_refresher(self, idx: int, sent: "Sentence") -> Callable[[], None]:
        """生成命令 on_change 回调：目标句若因 sort 挪了行号则退化为全量刷新。

        编辑时间/拖边界类命令 redo 末尾会 project.sort()，
        句子可能换行；此时只刷新原行号会留下整片错位的旧行内容。
        用对象身份（is）判断目标句是否还在原行——没动则保持轻量单行刷新。
        """
        def _cb() -> None:
            if (
                self._project is not None
                and 0 <= idx < len(self._project.sentences)
                and self._project.sentences[idx] is sent
            ):
                self._refresh_single_row(idx)
                self.player.refresh_subtitle_content()
            else:
                # 全量路径内部已经刷新播放器，避免同一次 Undo/Redo 重建两次字幕轨。
                self._refresh_after_edit(preferred_sid=sent.sid)
        return _cb


    def _refresh_single_row(self, idx: int) -> None:
        """单行轻量刷新：只更新该行表格 + 底栏 + 波形该句色块与字级图元。"""
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return
        self.editor.refresh_row(idx)
        self.editor.refresh_summary()

        # 若当前在字级精度 Tab 或当前字级视图展示的就是该句，立刻刷新字级表格
        if self.editor.is_word_view_active() or idx == self.editor.current_word_sentence_index:
            self.editor.show_word_sentence(idx)

        # 波形：原地移动该句的色块+手柄+标签（行数未变的轻量路径）
        self.waveform.refresh_sentence_visuals(idx, self._project.sentences[idx])

        # 若当前波形激活的是该句，强制立刻重建波形字级手柄与切片线（保持当前缩放视野不重置）
        if idx == self.waveform.active_sentence_index or self.editor.is_word_view_active():
            self.waveform.update_word_overlay(idx)


    def _on_dirty_sentence_aligned(self, idx: int) -> None:
        """单句对齐完成后的轻量刷新：刷新该行、波形及当前字幕预览。"""
        self._mark_project_modified()
        self._refresh_single_row(idx)
        self.player.refresh_subtitle_content()


