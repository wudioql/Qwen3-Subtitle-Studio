"""tests/test_toolbar.py — 主窗口工具栏与工作流控制器配置装配（GUI，Qt 离屏）

验证：对齐后端下拉 qwen/mms 切换、AlignConfig 装配正确路由。

注意：
- MainWindow 初始化会恢复工具栏偏好，本文件在 conftest 的隔离配置下运行，
  隔离配置为默认值（align_backend=qwen），故初始 currentData == "qwen"；
- 切换下拉会真实触发偏好持久化（写入隔离目录，不碰用户配置）。
"""

from __future__ import annotations


from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)


import pytest

pytestmark = pytest.mark.ui


def _case_ui_toolbar_and_workflow_controller():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    from ui.main_window import MainWindow
    win = MainWindow()

    # 1. 工具栏对齐后端选择框存在且包含 qwen 与 mms
    assert hasattr(win, "_align_backend")
    assert win._align_backend.count() == 2

    # 2. 切换对齐模式为 mms
    win._align_backend.setCurrentIndex(1)
    assert win._align_backend.currentData() == "mms"

    cfg = win.workflow._get_current_align_config()
    assert cfg.align_backend == "mms"

    # 3. 切换回 qwen
    win._align_backend.setCurrentIndex(0)
    assert win._align_backend.currentData() == "qwen"

    cfg2 = win.workflow._get_current_align_config()
    assert cfg2.align_backend == "qwen"

    # 4. 工具栏「识别语言」只作用于 ASR，不直传对齐配置
    ja_idx = win._global_lang.findData("ja")
    assert ja_idx >= 0
    win._global_lang.setCurrentIndex(ja_idx)
    cfg3 = win.workflow._get_current_align_config()
    assert cfg3.source_language == "auto", (
        f"识别语言不应直传对齐配置，得 {cfg3.source_language!r}"
    )

    # k-tag 下拉即时驱动第五档预览，不必等到实际导出。
    k_combo = win._export_panel.word_style._k_mode
    k_combo.setCurrentIndex(k_combo.findData("k"))
    from core.app_config import load_preferences
    assert load_preferences().export.k_tag_mode == "k"
    assert win.player._stage._k_mode == "k"

    win.close()
    print("test_ui_toolbar_and_workflow_controller PASSED ✔")


# ═════════════ 面板压缩布局契约 ═════════════

def _case_worker_failure_always_restores_ui():
    """失败/取消都必须经 finished 统一恢复全部动作和进度。"""
    from unittest.mock import patch
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    class FakeWorker(QObject):
        progress = Signal(int, int, str)
        failed = Signal(str)
        cancelled = Signal()
        finished_ok = Signal()
        finished = Signal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self._running = False

        def start(self):
            self._running = True

        def isRunning(self):
            return self._running

        def fail(self):
            self.failed.emit("boom")
            self._running = False
            self.finished.emit()

        def requestInterruption(self):
            self.cancelled.emit()
            self._running = False
            self.finished.emit()

    win = MainWindow()
    worker = FakeWorker(win)
    try:
        with patch("ui.workflow_controller.QMessageBox.critical"):
            win.workflow.bind_and_start_worker(worker, mode_label="失败回归")
            worker.progress.emit(0, 0, "模型加载中…")
            assert win._sb_progress.minimum() == win._sb_progress.maximum() == 0
            assert "已用时" in win._sb_mode.text()
            worker.progress.emit(1, 4, "阶段进度")
            assert win._sb_progress.maximum() == 100
            assert win._sb_progress.value() == 25
            assert win.workflow._format_elapsed(65) == "01:05"
            worker.fail()
        assert win.workflow.running_worker is None
        assert not win._sb_progress.isVisible()
        assert win._act_open.isEnabled()
        assert win._act_import_subtitle.isEnabled()
        assert not win._act_cancel_task.isEnabled()
        assert "执行失败" in win._sb_mode.text()

        cancellable = FakeWorker(win)
        win.workflow.bind_and_start_worker(cancellable, mode_label="取消回归")
        assert win.workflow.request_cancel()
        assert win.workflow.running_worker is None
        assert "已取消" in win._sb_mode.text()
        assert win._act_open.isEnabled()
    finally:
        win.close()

def _case_unsaved_gate_and_media_relink_preserve_subtitles(tmp_path):
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from core.audio_io import AudioInfo
    from subs.models import Sentence, SubtitleProject, WordTimestamp
    from ui.main_window import MainWindow

    win = MainWindow()
    media = tmp_path / "replacement.wav"
    media.touch()
    sentence = Sentence(
        "保留字幕", 1.0, 2.0,
        words=[WordTimestamp("保留字幕", 1.0, 2.0)],
        language="zh", is_dirty=True, is_locked=True,
    )
    project = SubtitleProject(sentences=[sentence], source_language="zh")
    win._apply_project(project)
    win._reset_project_file_state(modified=False)
    assert not win.has_unsaved_changes

    win._mark_project_modified()
    box, save_button, discard_button, cancel_button = win._create_unsaved_dialog("测试替换")
    assert [save_button.text(), discard_button.text(), cancel_button.text()] == [
        "保存工程", "不保存", "取消",
    ]
    box.close()
    with patch.object(win, "_ask_unsaved_changes", return_value="cancel"):
        assert not win._maybe_save_changes("测试替换")
    with patch.object(win, "_ask_unsaved_changes", return_value="discard"):
        assert win._maybe_save_changes("测试替换")

    before = project.to_dict()["sentences"]
    # 重新关联现在是异步编排：同步守卫照旧，重活移出主线程。
    # 1) 编排层：守卫通过后以正确参数启动媒体准备 Worker。
    with patch.object(win.project_ctrl, "_start_media_prep") as start_prep:
        assert win.project_ctrl.relink_media_file(media)
    start_prep.assert_called_once()
    assert start_prep.call_args.kwargs["do_vocals"] is False
    assert start_prep.call_args.kwargs["done_label"] == "媒体已重新关联，字幕数据保持不变"

    # 2) 提交层：Worker 回传结果后的同步提交逻辑（字幕保留、路径更新、标脏）。
    info = AudioInfo(media, 16000, 1, 5.0, 80000)
    with patch.object(win.player, "load"), \
         patch.object(win.project_ctrl, "load_waveform_audio"):
        win.project_ctrl._finish_relink_prep(media, None, info, False)
    assert project.to_dict()["sentences"] == before
    assert project.source_media_path == str(media)
    assert project.audio_path == str(media)
    assert win.has_unsaved_changes

    win._reset_project_file_state(modified=False)
    win.close()


def _case_project_embed_and_apply_triple(tmp_path):
    """保存工程嵌入三件套；打开工程应用三件套到偏好并刷新面板。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    win = MainWindow()
    # 工程（空句集 + 三件套 None）
    from subs.models import Sentence, SubtitleProject
    proj = SubtitleProject(sentences=[Sentence("测试", 0.0, 1.0, language="zh")])
    win._apply_project(proj)

    # 1) 嵌入：伪造导出面板当前值 → _embed_export_settings 写入 proj 三字段
    fake_style = SimpleNamespace(to_dict=lambda: {"font_name": "X"})
    fake_tpl = SimpleNamespace(to_dict=lambda: {"templates": []})
    fake_ws = SimpleNamespace(
        bold=True, italic=False, underline=True, strike=False,
        ass_extra="", ass_highlight_color="#FFD54F",
    )
    panel = SimpleNamespace(
        word_style=SimpleNamespace(
            word_highlight_style=lambda: fake_ws, k_mode=lambda: "kf"),
        ass_style=SimpleNamespace(current_style=lambda: fake_style),
        karaoke_template=SimpleNamespace(current_template_prefs=lambda: fake_tpl),
    )
    win._export_panel = panel
    win.project_ctrl._embed_export_settings(proj)
    assert proj.ass_style_data == {"font_name": "X"}
    assert proj.karaoke_template_data == {"templates": []}
    assert proj.export_settings["k_tag_mode"] == "kf"
    assert proj.export_settings["word_style"]["bold"] is True

    # 2) 应用：打开工程后 _apply_project_export_settings 覆盖偏好 + 刷新面板
    prefs = SimpleNamespace(
        export=SimpleNamespace(k_tag_mode="kf"),
        style=SimpleNamespace(
            bold=False, italic=False, underline=False, strike=False,
            ass_extra_tags="", ass_highlight_color="#FFFFFF",
        ),
        ass_style=SimpleNamespace(apply=MagicMock()),
        karaoke_template=SimpleNamespace(apply=MagicMock()),
    )
    win._export_panel = SimpleNamespace(apply_prefs_from=MagicMock())
    win.player = SimpleNamespace(subtitle_overlay=SimpleNamespace(refresh_styles=MagicMock()))
    with patch("core.app_config.load_preferences", return_value=prefs), \
         patch("core.app_config.save_preferences") as save_p:
        win.project_ctrl._apply_project_export_settings(proj)
    assert prefs.style.bold is True                      # word_style 应用到偏好
    assert prefs.style.underline is True
    assert prefs.ass_style.apply.called                    # ASS 样式应用
    assert prefs.karaoke_template.apply.called             # 模板应用
    assert save_p.called
    assert win._export_panel.apply_prefs_from.called       # 面板刷新
    assert win.player.subtitle_overlay.refresh_styles.called  # 字幕预览刷新

    # 3) 旧工程（无三件套）→ 不覆盖偏好
    old_proj = SubtitleProject(sentences=[Sentence("旧", 0.0, 1.0)])
    with patch("core.app_config.load_preferences") as lp, \
         patch("core.app_config.save_preferences") as sp:
        win.project_ctrl._apply_project_export_settings(old_proj)
    lp.assert_not_called()
    sp.assert_not_called()

    win.close()


def _case_style_change_refreshes_subtitle_preview():
    """样式变更 → 立即写偏好 + 刷新播放器字幕预览（六档预览实时反映）。

    覆盖：逐字高亮（checkbox/颜色）、ASS 文字样式（弹窗保存）。
    此前只有卡拉OK模板保存会刷新，逐字高亮/ASS 样式变更不联动预览。
    """
    from unittest.mock import MagicMock, patch
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    win = MainWindow()
    card = win._export_panel.word_style

    # 1) 逐字高亮 checkbox 变更 → 写偏好 + 刷新预览
    with patch.object(win.player, "refresh_subtitle_styles") as refresh:
        card._cb_bold.setChecked(True)
    assert refresh.called
    from core.app_config import load_preferences
    assert load_preferences().style.bold is True

    # 2) 高亮颜色变更 → 写偏好 + 刷新预览
    # （ColorPickerButton.setColor 为编程式设置、不 emit；此处改颜色后直接走同一条槽，
    #   等价于用户弹窗选色后 colorChanged 触发的处理）
    card._highlight_color.setColor(QColor("#123456"))
    with patch.object(win.player, "refresh_subtitle_styles") as refresh:
        card._on_word_style_changed()
    assert refresh.called
    assert load_preferences().style.ass_highlight_color == "#123456"

    # 3) ASS 样式弹窗保存 → 写偏好 + 刷新预览
    from subs.ass_style import AssStylePrefs
    fake_style = AssStylePrefs()
    fake_dialog = MagicMock()
    fake_dialog.return_value.exec.return_value = 1   # QDialog.Accepted
    fake_dialog.return_value.current_style = fake_style
    ass_card = win._export_panel.ass_style
    with patch("ui.export_panel.AssStyleDialog", fake_dialog), \
         patch.object(win.player, "refresh_subtitle_styles") as refresh:
        ass_card._open_dialog()
    assert refresh.called
    win.close()


def _case_strip_trailing_punct_entry(tmp_path):
    """句级字幕「删除句尾标点」按钮：信号驱动全文批量，锁定句跳过，可撤销，不标脏。"""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from core.text_utils import merge_punct_into_words
    from subs.models import Sentence, SubtitleProject, WordTimestamp
    from ui.main_window import MainWindow

    def w(chars, start):
        return [WordTimestamp(text=c, start_time=start + i * 0.2, end_time=start + (i + 1) * 0.2)
                for i, c in enumerate(chars)]

    win = MainWindow()
    proj = SubtitleProject(
        sentences=[
            Sentence(text="第一句。", start_time=0.0, end_time=0.6, language="zh",
                     words=merge_punct_into_words("第一句。", w("第一句", 0.0))),
            Sentence(text="第二句！", start_time=1.0, end_time=1.6, language="zh",
                     words=merge_punct_into_words("第二句！", w("第二句", 1.0))),
        ],
        source_language="zh",
    )
    for s in proj.sentences:
        s.fix_times_from_words()
    win._apply_project(proj)

    view = win.editor._sentence_view
    assert hasattr(view, "_btn_strip_punct")
    assert view._btn_strip_punct.isEnabled()   # 有句即启用
    assert view._btn_strip_punct.icon() is not None  # 有图标

    # 点击按钮 → 发信号 → MainWindow 全文批量删除
    view._btn_strip_punct.click()
    assert all(not s.text.endswith(("。", "！")) for s in win._project.sentences)
    assert all(s.is_dirty is False for s in win._project.sentences)  # 不标脏
    assert "删除" in win._sb_mode.text()

    win._undo_stack.undo()                     # 可撤销
    assert any(s.text.endswith(("。", "！")) for s in win._project.sentences)
    win.close()


def _case_sniff_project_file(tmp_path):
    """拖放普通 .json 不得被当作工程（嗅探 schema_version）。"""
    import json
    from ui.project_controller import sniff_project_file

    # 合法工程
    proj = tmp_path / "ok.qss.json"
    proj.write_text(json.dumps({"schema_version": 1, "sentences": []}), encoding="utf-8")
    assert sniff_project_file(proj) is True

    # 普通 JSON（无 schema_version）
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    assert sniff_project_file(other) is False

    # 非 JSON
    txt = tmp_path / "note.json"
    txt.write_text("not json", encoding="utf-8")
    assert sniff_project_file(txt) is False

    # 不存在
    assert sniff_project_file(tmp_path / "missing.json") is False


def _case_project_json_drag_and_relink_vocal_path(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from core.audio_io import AudioInfo
    from subs.models import Sentence, SubtitleProject
    from ui.main_window import MainWindow

    project_file = tmp_path / "demo.qss.json"
    project_file.write_text('{"schema_version": 1, "sentences": []}', encoding="utf-8")

    class FakeUrl:
        def isLocalFile(self):
            return True

        def toLocalFile(self):
            return str(project_file)

    class FakeMime:
        def hasUrls(self):
            return True

        def urls(self):
            return [FakeUrl()]

    class FakeEvent:
        def __init__(self):
            self.accepted = False

        def mimeData(self):
            return FakeMime()

        def acceptProposedAction(self):
            self.accepted = True

        def ignore(self):
            self.accepted = False

    win = MainWindow()
    enter_event = FakeEvent()
    win.project_ctrl.handle_drag_enter(enter_event)
    assert enter_event.accepted
    drop_event = FakeEvent()
    with patch.object(win.project_ctrl, "open_project_file") as opened:
        win.project_ctrl.handle_drop(drop_event)
    opened.assert_called_once_with(project_file)
    assert drop_event.accepted

    # 默认启用 + 模型可用 + 用户确认时，决策层必须判定「执行人声分离」。
    media = tmp_path / "replacement.mp4"
    media.touch()
    vocal = tmp_path / "replacement.vocals.wav"
    vocal.touch()
    separator = MagicMock()
    separator.is_available.return_value = True
    prefs = SimpleNamespace(asr=SimpleNamespace(extract_vocals=True))
    with patch("core.app_config.load_preferences", return_value=prefs), \
         patch("core.vocal_separator.get_vocal_separator", return_value=separator), \
         patch.object(win.project_ctrl, "_ask_vocal_extraction", return_value=True):
        assert win.project_ctrl._should_extract_vocals(media) is True

    project = SubtitleProject(sentences=[Sentence("保留", 0.0, 1.0)])
    win._apply_project(project)
    win._reset_project_file_state(modified=False)
    info = AudioInfo(media, 16000, 1, 5.0, 80000)
    with patch.object(win.player, "load") as player_load, \
         patch.object(win.project_ctrl, "load_waveform_audio"):
        win.project_ctrl._finish_relink_prep(media, vocal, info, True)
    assert project.audio_path == str(vocal)
    player_load.assert_called_once_with(media)  # 视频仍播原画面，人声件只供推理

    win._reset_project_file_state(modified=False)
    win.close()


def _case_deleted_undo_stack_during_close_is_ignored():
    """Windows/PySide 析构回归：QUndoStack 已删后不得从 cleanChanged 回调再访问它。"""
    import shiboken6
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from subs.models import Sentence, SubtitleProject
    from ui.main_window import MainWindow

    win = MainWindow()
    win._apply_project(SubtitleProject(sentences=[Sentence("测试", 0.0, 1.0)]))
    win._project_modified = False
    shiboken6.delete(win._undo_stack)
    assert not win.has_unsaved_changes
    win._on_undo_clean_changed(True)  # 不抛 RuntimeError
    win.close()


def _case_panel_shrink_layout_pack():
    """压窄防残缺 2 合 1（用户实测回归）：
    1. 句级工具条按钮：视图最小宽度自动计算（六按钮完整文字宽之和），
       压到最小宽度时任何按钮不得被裁（曾裁成「标处拆」式残缺）；
    2. 导出侧栏说明标签：wordWrap 标签保持按宽算高（Preferred+minWidth=1），
       窄侧栏下文字加高完整显示，不再定高截断产生上下空洞。"""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from qfluentwidgets import CaptionLabel
    from ui.sentence_level_view import SentenceLevelView

    v = SentenceLevelView()
    try:
        btns = (v._btn_add, v._btn_del, v._btn_split, v._btn_merge, v._btn_confirm, v._btn_lock)
        need = sum(b.sizeHint().width() for b in btns)
        assert v.minimumWidth() >= need, "最小宽度必须容纳六按钮完整文字"
        v.resize(v.minimumWidth(), 400)
        v.show()
        for b in btns:
            assert b.width() >= b.sizeHint().width(), f"按钮「{b.text()}」被裁"
    finally:
        v.close()

    # 整面板多宽度扫描：卡片与 wrap 标签在任何宽度下都不得被定高截断
    # （根因曾有两层：标签 Ignored 丢按宽算高；卡片 Maximum 把宽敞时的
    #   sizeHint 当上限拒绝增高——两层都补齐后此契约才成立）
    from ui.export_panel import ExportPanel
    for W in (300, 260, 222):
        panel = ExportPanel(on_export=lambda k: None)
        try:
            panel.resize(W, 900)
            panel.show()
            word_labels = [button.text() for button in panel._grp_word._buttons]
            assert len(word_labels) == 7 and "应用模板后 ASS" in word_labels
            for card in (panel._grp_sentence, panel._grp_word, panel.word_style,
                         panel.ass_style, panel.karaoke_template):
                need = card.layout().heightForWidth(card.width())
                assert card.height() >= need - 1, \
                    f"W={W} 卡片被截断: 实际 {card.height()} < 需要 {need}"
            for lbl in panel.word_style.findChildren(CaptionLabel):
                if not lbl.wordWrap():
                    continue
                need_h = lbl.heightForWidth(max(1, lbl.geometry().width()))
                assert lbl.geometry().height() >= need_h - 1, \
                    f"W={W} 说明标签被截断: {lbl.text()[:18]}…"
        finally:
            panel.close()
    print("test_panel_shrink_layout_pack PASSED ✔")


def test_toolbar_ui_assembly_pack():
    """test_toolbar_ui_assembly_pack：合并 4 个场景（断言逐条保留，见各 _case_*）。"""
    _case_ui_toolbar_and_workflow_controller()
    _case_worker_failure_always_restores_ui()
    _case_deleted_undo_stack_during_close_is_ignored()
    _case_panel_shrink_layout_pack()


def test_toolbar_project_flow_pack(tmp_path):
    """test_toolbar_project_flow_pack：合并 4 个场景（断言逐条保留，见各 _case_*）。"""
    _case_unsaved_gate_and_media_relink_preserve_subtitles(tmp_path=tmp_path)
    _case_project_embed_and_apply_triple(tmp_path=tmp_path)
    _case_sniff_project_file(tmp_path=tmp_path)
    _case_project_json_drag_and_relink_vocal_path(tmp_path=tmp_path)


def test_toolbar_style_punct_pack(tmp_path):
    """test_toolbar_style_punct_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_style_change_refreshes_subtitle_preview()
    _case_strip_trailing_punct_entry(tmp_path=tmp_path)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
