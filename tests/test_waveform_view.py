"""tests/test_waveform_view.py — 波形视图交互（GUI，Qt 离屏）

验证：波形自定义 ViewBox 的 QGraphicsSceneWheelEvent 滚轮缩放与平移交互无异常。
"""

from __future__ import annotations


from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)


import pytest

pytestmark = pytest.mark.ui


def _case_waveform_wheel_event_zoom_and_pan():
    from PySide6.QtWidgets import QApplication, QGraphicsSceneWheelEvent
    from PySide6.QtCore import Qt, QPointF
    from ui.waveform_view import WaveformView

    QApplication.instance() or QApplication(["test"])
    wf = WaveformView()
    vb = wf._vb

    # 1. Ctrl 组合键缩放
    ev_ctrl = QGraphicsSceneWheelEvent()
    ev_ctrl.setDelta(120)
    ev_ctrl.setPos(QPointF(100, 50))
    ev_ctrl.setModifiers(Qt.ControlModifier)
    vb.wheelEvent(ev_ctrl)

    # 2. Shift 组合键横向平移
    ev_shift = QGraphicsSceneWheelEvent()
    ev_shift.setDelta(-120)
    ev_shift.setPos(QPointF(100, 50))
    ev_shift.setModifiers(Qt.ShiftModifier)
    vb.wheelEvent(ev_shift)

    # 3. Ctrl+Shift 横向缩放
    ev_ctrl_shift = QGraphicsSceneWheelEvent()
    ev_ctrl_shift.setDelta(120)
    ev_ctrl_shift.setPos(QPointF(100, 50))
    ev_ctrl_shift.setModifiers(Qt.ControlModifier | Qt.ShiftModifier)
    vb.wheelEvent(ev_ctrl_shift)

    # 4. Alt 纵向缩放
    ev_alt = QGraphicsSceneWheelEvent()
    ev_alt.setDelta(120)
    ev_alt.setPos(QPointF(100, 50))
    ev_alt.setModifiers(Qt.AltModifier)
    vb.wheelEvent(ev_alt)

    # 5. 普通滚轮：向上滚应查看更高的 Y 区域（中心值增大）。
    vb.setYRange(-1.0, 1.0, padding=0)
    before_center = sum(vb.viewRange()[1]) / 2.0
    ev_plain = QGraphicsSceneWheelEvent()
    ev_plain.setDelta(120)
    ev_plain.setPos(QPointF(100, 50))
    ev_plain.setModifiers(Qt.NoModifier)
    vb.wheelEvent(ev_plain)
    after_center = sum(vb.viewRange()[1]) / 2.0
    assert after_center > before_center

    wf.close()
    print("test_waveform_wheel_event_zoom_and_pan PASSED ✔")


def _case_waveform_public_refresh_api():
    """公共 API（active_sentence_index / refresh_sentence_visuals）

    main_window 不再戳 _blocks/_handles/_labels/_active_idx 私有成员；这里钉死这两个
    公共口的行为：refresh 后色块区间/标签就地更新，active_sentence_index 反映激活句。
    """
    import numpy as np
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    from subs.models import Sentence, SubtitleProject
    from ui.waveform_view import WaveformView

    proj = SubtitleProject(
        source_media_path="m.wav", media_duration=10.0, sample_rate=16000,
        sentences=[
            Sentence(text="第一句测试文本", start_time=1.0, end_time=2.0, words=[]),
            Sentence(text="第二句测试文本", start_time=3.0, end_time=4.0, words=[]),
        ],
    )
    wf = WaveformView()
    try:
        wf.set_audio(np.zeros(16000 * 2, dtype=np.float32), 16000)
        wf.set_project(proj)
        assert wf.active_sentence_index == -1

        # 模拟单句轻量编辑后的原位刷新（时间 + 文本都变了）
        s0 = proj.sentences[0]
        s0.start_time, s0.end_time = 1.5, 2.5
        s0.text = "改动后的第一句"
        wf.refresh_sentence_visuals(0, s0)

        assert tuple(wf._blocks[0].getRegion()) == (1.5, 2.5)
        assert wf._labels[0].toPlainText() == "✓ S1: 改动后的第一句"
        # 未注册的 idx（无图元）调用不应炸
        wf.refresh_sentence_visuals(99, s0)

        wf.set_active_sentence(1, force=True)
        assert wf.active_sentence_index == 1
    finally:
        wf.close()
    print("test_waveform_public_refresh_api PASSED ✔")


def _case_waveform_confirmed_visual_and_preview_word_drag():
    """确认后视觉变化；内部字界拖动只改预览副本，句级手柄承担外边界。"""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from subs.models import Sentence, SubtitleProject, WordTimestamp
    from ui.waveform_view import WaveformView

    sentence = Sentence(
        text="你好", start_time=0.4, end_time=1.2, is_dirty=True,
        words=[
            WordTimestamp("你", 0.4, 0.8),
            WordTimestamp("好", 0.8, 1.2),
        ],
    )
    project = SubtitleProject(sentences=[sentence], media_duration=2.0)
    view = WaveformView()
    try:
        view.set_project(project)
        view.set_active_sentence(0, force=True)
        dirty_color = view._blocks[0].brush.color().name(QColor.HexArgb)
        assert view._labels[0].toPlainText().startswith("●")

        sentence.is_dirty = False
        view.refresh_sentence_visuals(0, sentence)
        confirmed_color = view._blocks[0].brush.color().name(QColor.HexArgb)
        assert confirmed_color != dirty_color
        assert view._labels[0].toPlainText().startswith("✓")

        # 外侧边界由两个句级手柄承担；两字只需一个内部字界手柄。
        assert len(view._handles[0]) == 2
        assert len(view._word_handles) == 1
        previews = []
        finished = []
        view.word_boundary_dragged.connect(lambda _idx, words: previews.append(words))
        view.word_boundary_drag_finished.connect(
            lambda _idx, old, new: finished.append((old, new))
        )

        view._on_word_handle_dragged(0, 1.0)
        assert sentence.words[0].end_time == 0.8  # 拖动中不写模型
        assert sentence.words[1].start_time == 0.8
        assert previews[-1][0].end_time == 1.0
        assert previews[-1][1].start_time == 1.0

        view._on_word_handle_drag_finished(0, 1.0)
        assert finished
        old_words, new_words = finished[-1]
        assert old_words[0].end_time == 0.8
        assert new_words[0].end_time == 1.0
        assert old_words[0].start_time == new_words[0].start_time == sentence.start_time
        assert old_words[-1].end_time == new_words[-1].end_time == sentence.end_time
    finally:
        view.close()

def _case_coincident_sentence_boundaries_split_by_drag_direction():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from subs.models import Sentence, SubtitleProject, WordTimestamp
    from ui.main_window import MainWindow

    previous = Sentence(
        "前句", 0.0, 1.0,
        words=[WordTimestamp("前句", 0.0, 1.0)],
    )
    following = Sentence(
        "后句", 1.0, 2.0,
        words=[WordTimestamp("后句", 1.0, 2.0)],
    )
    project = SubtitleProject(sentences=[previous, following], media_duration=2.0)
    win = MainWindow()
    try:
        win._apply_project(project)
        prev_end_handle = win.waveform._handles[0][1]
        next_start_handle = win.waveform._handles[1][0]

        # 即使实际命中后句 start，向左拖也只预览/提交前句 end。
        next_start_handle.moving = True
        next_start_handle.setValue(0.8)
        assert prev_end_handle.value() == 0.8
        assert next_start_handle.value() == 1.0
        assert previous.end_time == following.start_time == 1.0  # 拖动中不写模型
        next_start_handle.moving = False
        win.waveform._on_sentence_handle_drag_finished(next_start_handle, 1, "start")
        assert previous.end_time == 0.8
        assert following.start_time == 1.0
        assert previous.words[-1].end_time == 0.8
        assert following.words[0].start_time == 1.0
        assert previous.is_dirty and not following.is_dirty
        win._undo_stack.undo()
        assert previous.end_time == following.start_time == 1.0
        assert previous.words[-1].end_time == following.words[0].start_time == 1.0
        assert not previous.is_dirty and not following.is_dirty

        # 即使实际命中前句 end，向右拖也只预览/提交后句 start。
        prev_end_handle.moving = True
        prev_end_handle.setValue(1.2)
        assert prev_end_handle.value() == 1.0
        assert next_start_handle.value() == 1.2
        assert previous.end_time == following.start_time == 1.0
        prev_end_handle.moving = False
        win.waveform._on_sentence_handle_drag_finished(prev_end_handle, 0, "end")
        assert previous.end_time == 1.0
        assert following.start_time == 1.2
        assert previous.words[-1].end_time == 1.0
        assert following.words[0].start_time == 1.2
        assert not previous.is_dirty and following.is_dirty
        win._undo_stack.undo()
        assert previous.end_time == following.start_time == 1.0
        assert previous.words[-1].end_time == following.words[0].start_time == 1.0
    finally:
        win._reset_project_file_state(modified=False)
        win.close()


def _case_time_reorder_keeps_edited_sentence_selected_by_sid():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from subs.models import Sentence, SubtitleProject, WordTimestamp
    from ui.main_window import MainWindow

    first = Sentence(
        "第一句", 1.0, 2.0,
        words=[WordTimestamp("第一句", 1.0, 2.0)],
    )
    second = Sentence("第二句", 3.0, 4.0)
    project = SubtitleProject(sentences=[first, second], media_duration=6.0)
    win = MainWindow()
    try:
        win._apply_project(project)
        win.editor.select_row(0)
        edited_sid = first.sid
        win._on_time_edited(0, 4.5, 5.5)  # 排序后第一句移动到 index=1
        assert win.waveform.active_sentence_index == 1
        assert project.sentences[1].sid == edited_sid
        assert project.sentences[1].words[0].start_time == 4.5
        assert project.sentences[1].words[-1].end_time == 5.5
        assert win.editor.selected_rows() == [1]
        win._undo_stack.undo()
        assert project.sentences[0].sid == edited_sid
        assert project.sentences[0].words[0].start_time == 1.0
        assert project.sentences[0].words[-1].end_time == 2.0
    finally:
        win._reset_project_file_state(modified=False)
        win.close()


# ═════════════ 播放光标视野跟随（句级/字级通用 follow_playhead） ═════════════

def _make_view(duration=100.0):
    from ui.waveform_view import WaveformView
    view = WaveformView()
    view.resize(900, 240)
    view._duration_s = duration
    return view


def _case_follow_playhead_jumps_to_left_20pct():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    view = _make_view()
    try:
        view.plot.setXRange(0.0, 10.0, padding=0)
        view.follow_playhead(15.0)                       # 越出右缘
        lo, hi = view.get_view_range()
        span = hi - lo
        assert abs(span - 10.0) < 1e-6, "视野跨度（用户缩放）不得改变"
        assert abs((15.0 - lo) / span - 0.20) < 0.01, "光标应落在视野左侧 20%"
    finally:
        view.close()
    print("test_follow_playhead_jumps_to_left_20pct PASSED ✔")


def _case_follow_playhead_noop_when_visible_and_clamps_at_edges():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    view = _make_view(duration=100.0)
    try:
        # 光标在视野内 → 不动
        view.plot.setXRange(20.0, 30.0, padding=0)
        view.follow_playhead(25.0)
        lo, hi = view.get_view_range()
        assert abs(lo - 20.0) < 1e-6 and abs(hi - 30.0) < 1e-6

        # 越出左缘（回跳早句）→ 同样收回
        view.follow_playhead(5.0)
        lo, hi = view.get_view_range()
        assert lo <= 5.0 <= hi

        # 尾部：不越媒体末端
        view.plot.setXRange(0.0, 10.0, padding=0)
        view.follow_playhead(99.5)
        lo, hi = view.get_view_range()
        assert hi <= 100.0 + 1e-6
        assert lo <= 99.5 <= hi

        # 头部：不出负区
        view.plot.setXRange(50.0, 60.0, padding=0)
        view.follow_playhead(0.5)
        lo, hi = view.get_view_range()
        assert lo >= 0.0 and lo <= 0.5 <= hi
    finally:
        view.close()
    print("test_follow_playhead_noop_when_visible_and_clamps_at_edges PASSED ✔")


def _case_sentence_mode_playback_and_row_click_follow():
    """主窗两条链路：播放推进 / 行号点击，句级模式下光标出视野必须跟随。"""
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    from subs.models import Sentence, SubtitleProject
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        proj = SubtitleProject(
            media_duration=100.0,
            sentences=[
                Sentence(text="早", start_time=1.0, end_time=3.0),
                Sentence(text="晚", start_time=60.0, end_time=63.0),
            ],
        )
        win._apply_project(proj)
        win.waveform._duration_s = 100.0
        assert not win.editor.is_word_view_active(), "默认应为句级页面"

        # 链路 1：播放推进越出视野
        win.waveform.plot.setXRange(0.0, 10.0, padding=0)
        win._on_playback_moved(30.0)
        lo, hi = win.waveform.get_view_range()
        assert lo <= 30.0 <= hi, "句级模式播放光标出视野必须跟随"
        assert abs((hi - lo) - 10.0) < 1e-6

        # 链路 2：点行号跳到远句（player.seek/play 打桩，离屏无媒体）
        win.waveform.plot.setXRange(0.0, 10.0, padding=0)
        with patch.object(win.player, "seek"), patch.object(win.player, "play"):
            win._jump_to_sentence_start(1)               # start=60.0
        lo, hi = win.waveform.get_view_range()
        assert lo <= 60.0 <= hi, "行号点击跳远句必须把视野带过去"
    finally:
        win.close()
    print("test_sentence_mode_playback_and_row_click_follow PASSED ✔")


def test_follow_playhead_pack():
    """光标跟随 3 合 1：出视野跳左 20% / 视野内不动+头尾钳制 / 播放与行号两链路。"""
    _case_follow_playhead_jumps_to_left_20pct()
    _case_follow_playhead_noop_when_visible_and_clamps_at_edges()
    _case_sentence_mode_playback_and_row_click_follow()


def test_waveform_interaction_pack():
    """test_waveform_interaction_pack：合并 5 个场景（断言逐条保留，见各 _case_*）。"""
    _case_waveform_wheel_event_zoom_and_pan()
    _case_waveform_public_refresh_api()
    _case_waveform_confirmed_visual_and_preview_word_drag()
    _case_coincident_sentence_boundaries_split_by_drag_direction()
    _case_time_reorder_keeps_edited_sentence_selected_by_sid()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
