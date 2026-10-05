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


def test_ui_toolbar_and_workflow_controller():
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

def test_worker_failure_always_restores_ui():
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

def test_unsaved_gate_and_media_relink_preserve_subtitles(tmp_path):
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


def test_project_embed_and_apply_triple(tmp_path):
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


def test_style_change_refreshes_subtitle_preview():
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


def test_strip_trailing_punct_entry(tmp_path):
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


def test_sniff_project_file(tmp_path):
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


def test_project_json_drag_and_relink_vocal_path(tmp_path):
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


def test_deleted_undo_stack_during_close_is_ignored():
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


def test_panel_shrink_layout():
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
    print("test_panel_shrink_layout PASSED ✔")


def _widget_in_bar(widget, bar) -> bool:
    """该控件是否真的摆在 ``bar`` 里。

    用 ``QToolBar.widgetForAction(action)`` 逐个比对——这是 Qt 自己维护的
    「action → 工具栏控件」映射，是唯一可靠的归属判据。

    三个看起来可行但**都不行**的替代方案（都实测踩过）：
    - ``widget.parent() is bar``：``addWidget`` 不重设父级，控件是用
      ``ComboBox(self)`` 建的，父级是 MainWindow；
    - ``action.defaultWidget()``：返回的是 action 上另外设的默认控件
      （组合框所在 action 的 defaultWidget 是它前面那个 QLabel）；
    - 控件几何 ``(x, y)``：两行工具栏坐标各自独立，(0,0) 之类的坐标会撞车。
    """
    for act in bar.actions():
        try:
            if bar.widgetForAction(act) is widget:
                return True
        except (AttributeError, RuntimeError):
            break
    return False


def test_toolbar_is_two_rows_with_vocal_button():
    """反回归：工具栏必须是**两行**，且「人声提取」夹在导入字幕与识别之间。

    起因（用户实测）：单行放不下——7 个动作按钮 + 3 组「标签+下拉」+ 主题/设置，
    QToolBar **不会自动换行**，窗口稍窄就把右侧控件直接裁掉。
    分行原则：第 1 行动作按钮，第 2 行从「识别语言」开始的全部设置类下拉。

    这里同时钉住三条容易回归的契约：
    1. 两行是**两个独立 QToolBar**（不是把控件塞进同一个）；
    2. 人声提取独立成组，位置在「导入字幕」右、「识别生成字幕」左；
    3. 三个下拉的宽度被显式夹住——不夹的话 qfluentwidgets 的 ComboBox 会按
       最长项文字算 sizeHint，三个加起来近 1000px，行内控件照样被裁。
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        row1, row2 = win._main_toolbar, win._main_toolbar_settings
        assert row1 is not row2, "工具栏必须拆成两个 QToolBar"
        assert row1.objectName() != row2.objectName(), "两行需用不同 objectName 便于 QSS 区分"
        win.show()
        QApplication.instance().processEvents()

        # 第 1 行：全部动作按钮（含人声提取）
        for btn, _act, _icon in win._toolbar_action_items:
            assert _widget_in_bar(btn, row1), f"动作按钮「{btn.text()}」应在第 1 行"
            assert not _widget_in_bar(btn, row2), f"「{btn.text()}」不应在第 2 行"

        # 第 2 行：三个设置类下拉 + 主题/设置
        from ui.main_window.chrome import ChromeMixin
        for combo in (win._global_lang, win._asr_backend, win._align_backend):
            assert _widget_in_bar(combo, row2), "设置类下拉应在第 2 行"
            assert not _widget_in_bar(combo, row1), "设置类下拉不应在第 1 行"
            # 宽度必须容得下「最长一项文字 + 控件装饰」，而不是某个拍脑袋的定值。
            # 原断言是 ``maximumWidth() <= 160``，那是在「宁可截断文字也要压窄」
            # 的旧前提下写的；用户真机反馈「文字显示不全」后已推翻。
            # 装饰宽度 46px 来自实测：文字墨迹起点恒为 12px、文字区右边界恒为
            # ``width-34``（详见 chrome.py::_fit_combo 的标定说明）。
            fm = combo.fontMetrics()
            need = max(fm.horizontalAdvance(combo.itemText(i))
                       for i in range(combo.count()))
            assert combo.minimumWidth() >= need + ChromeMixin._COMBO_CHROME, (
                f"下拉宽度容不下最长项文字：min={combo.minimumWidth()} "
                f"< 文字 {need} + 装饰 {ChromeMixin._COMBO_CHROME}")
            assert combo.minimumWidth() == combo.maximumWidth(), \
                "定宽下拉的 min/max 应一致（否则 QToolBar 仍可能取到窄值）"
        for btn in (win._btn_theme, win._btn_settings):
            assert _widget_in_bar(btn, row2), "主题/设置按钮应在第 2 行"

        # 人声提取位置：导入字幕 < 人声提取 < 识别生成字幕
        texts = [t[0].text() for t in win._toolbar_action_items]
        assert "人声提取" in texts, texts
        assert texts.index("导入字幕") < texts.index("人声提取") < texts.index("识别生成字幕"), texts

        # 启用态随媒体有无联动
        assert win._act_extract_vocals.isEnabled() is False
        win.workflow.set_actions_project_state(has_media=True, has_sentences=False)
        assert win._act_extract_vocals.isEnabled() is True
    finally:
        win.close()


def test_toolbar_rows_not_clipped_at_narrow_width():
    """两行工具栏在常见宽度下都不得裁控件。

    判据用 ``min(sizeHint, maximumWidth)``：组合框的 sizeHint 是**不加约束**时
    按最长项文字算出来的自然宽度（实测「☁️ 云端 · SiliconFlow」达 268px），
    而 ``_fit_combo`` 刻意把它夹到 120~150px。用裸 sizeHint 断言会把
    「按设计夹窄」误判成「被裁」——真正要防的是**行宽不够导致的截断**。
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    def act_widget(act):
        for getter in ("widget", "defaultWidget"):
            g = getattr(act, getter, None)
            w = g() if callable(g) else None
            if w is not None:
                return w
        return None

    def eff_need(w):
        mx = w.maximumWidth()
        hint = w.sizeHint().width()
        return hint if mx <= 0 else min(hint, mx)

    win = MainWindow()
    try:
        for W in (1920, 1366, 1024):
            win.resize(W, 800)
            win.show()
            QApplication.instance().processEvents()
            for name, bar in (("row1", win._main_toolbar),
                              ("row2", win._main_toolbar_settings)):
                need_total = 0
                for act in bar.actions():
                    w = act_widget(act)
                    if w is None:
                        need_total += 12          # separator
                        continue
                    if not w.isVisible():
                        continue
                    need = eff_need(w)
                    need_total += need
                    assert w.width() + 1 >= need, (
                        f"W={W} {name} 控件被裁: {w.text()} {w.width()} < {need}")
                assert need_total <= bar.width() + 4, (
                    f"W={W} {name} 整行超宽: 需要 {need_total} > 可用 {bar.width()}")
            win.hide()
    finally:
        win.close()


def test_toolbar_really_occupies_two_visual_rows():
    """两个 QToolBar 必须真的落在**不同的 y** 上——即视觉上确实是两行。

    为什么这条不可省（2026-10-04 真机复测）：``QMainWindow.addToolBar`` 是
    **顺序追加**语义，连续调用两次会把两个工具栏**并排放进同一行**
    （实测 ``tb.y == tb2.y == 0``），而不是注释里以为的「垂直堆叠」。
    必须显式调用 ``addToolBarBreak()`` 才会换行（实测 ``y=0`` / ``y=20``）。

    前一条 :func:`test_toolbar_is_two_rows_with_vocal_button` 只用
    ``widgetForAction`` 验证**控件归属**，那种判据在两行被合并成一行时
    依然全部成立——所以它一直绿着，而真机看到的是 1 行。
    判据必须是**几何**（y 坐标不同）。
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win.resize(1600, 800)
        win.show()
        QApplication.instance().processEvents()
        y1 = win._main_toolbar.geometry().y()
        y2 = win._main_toolbar_settings.geometry().y()
        assert y1 != y2, (
            f"两个工具栏的 y 相同（{y1}）——它们被合并成同一行了，"
            "需要 addToolBarBreak() 才能换行"
        )
        assert abs(y2 - y1) >= 1, f"两行间距异常：y1={y1}, y2={y2}"
    finally:
        win.close()


def test_toolbar_combo_renders_every_item_without_truncation():
    """下拉的**每一项**都必须完整渲染，不被静默裁掉末字。

    判据用 ``grab()`` 取**真实渲染像素**并量墨迹宽度，而不是算公式——
    前两版测试都因为「公式与实际绘制不符」而和实现一起错、一直绿着。

    实测标定（headless，``grab()`` 逐列扫暗像素）：

    - 文字**左对齐**，墨迹起点恒为 12px，**不随控件宽度变化**；
    - 文字区右边界恒为 ``width-34``，而箭头画在 ``width-22``
      →箭头与文字天然留12px 间隙，**箭头压不到文字**；
    - 所以差几像素不会「被箭头遮挡」，而是 Qt **静默裁掉末字**：
      「自动检测」advance=56、完整需 102px，而旧实现只给 96px
      → 实测墨迹只有 52px（= advance - 4），用户看到的就是「显示不全」。

    旧公式 ``width - 11 - 22`` 算成63 >= 56 一路放行，压根抓不到。
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    def _ink_width(combo):
        """墨迹列宽。**必须与主题无关**：浅色主题文字是深色、深色主题是亮色，
        只找暗像素会在深色主题下量到 0 而误报「被截断」。

        实现：以控件**背景色**（取四角像素的中位亮度）为基准，
        统计与背景显著不同的列。
        """
        img = combo.grab().toImage()
        corners = [img.pixelColor(x, y).lightness()
                   for x, y in ((0, 0), (img.width() - 1, 0),
                                (0, img.height() - 1),
                                (img.width() - 1, img.height() - 1))]
        bg = sorted(corners)[len(corners) // 2]
        cols = [x
                for x in range(img.width())
                for y in range(img.height())
                if abs(img.pixelColor(x, y).lightness() - bg) > 60]
        return (max(cols) - min(cols) + 1) if cols else 0

    win = MainWindow()
    try:
        win.resize(1920, 800)
        win.show()
        QApplication.instance().processEvents()
        for combo in (win._global_lang, win._asr_backend, win._align_backend):
            fm = combo.fontMetrics()
            orig = combo.currentIndex()
            for i in range(combo.count()):
                combo.setCurrentIndex(i)
                QApplication.instance().processEvents()
                text = combo.itemText(i)
                # 墨迹宽 < advance 说明末字被裁（末字右侧的 字形边距 约 4px，
                # 所以完整时应满足 ink >= advance - 4）。
                ink = _ink_width(combo)
                assert ink >= fm.horizontalAdvance(text) - 4, (
                    f"{text!r} 被截断: 实际墨迹宽 {ink} < 需要 "
                    f"{fm.horizontalAdvance(text)}（控件宽 {combo.width()}）")
            combo.setCurrentIndex(orig)
    finally:
        win.close()


def test_toolbar_second_row_is_right_aligned():
    """第 2 行整体靠右：第一个可视控件的x 明显偏右，不是贴着左边缘。

    用户指定「第2 行希望全都靠右」。原来弹性 spacer 放在中间，只有末尾的
    「主题/设置」靠右，前面的下拉仍左对齐——现在 spacer 移到最前面。
    判据用几何（首个控件 x），不靠「属于哪个 toolbar」推断。
    """
    from PySide6.QtWidgets import QApplication, QSizePolicy
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        W = 1600
        win.resize(W, 800)
        win.show()
        QApplication.instance().processEvents()
        bar = win._main_toolbar_settings
        # 必须用 bar.widgetForAction：QWidgetAction 自身没有 .widget() 方法
        # （它只是 action，控件由 QToolBar 侧维护）。
        vis = []
        for a in bar.actions():
            w = bar.widgetForAction(a)
            if w is None or not w.isVisible():
                continue
            # 排除纯弹性占位（spacer 本身 isVisible 为真，且就是最左边那个）
            if w.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding:
                continue
            vis.append(w)
        assert vis, "第 2 行没有可见控件"
        first_x = vis[0].mapTo(bar, vis[0].rect().topLeft()).x()
        # spacer 是 Expanding 的，占掉左侧全部空白；故首个真实控件应贴近右端
        assert first_x > W * 0.25, (
            f"第 2 行首个控件 x={first_x}（窗口宽 {W}），看起来仍靠左")
    finally:
        win.close()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
