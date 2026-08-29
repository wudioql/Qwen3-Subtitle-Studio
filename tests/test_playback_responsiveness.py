"""tests/test_playback_responsiveness.py — 播放响应优化钉样（GUI，Qt 离屏）。

覆盖：
- 播放高亮（highlight_row）与用户选中（select_row）解耦：播放高亮不触发 row_selected
  （不把播放头闪回句首、不重建字级图元）；_on_playback_moved 走 highlight_row；
- 波形点击/拖播放头统一 _follow_at：轻量选中 + set_active_sentence(force=True)；
- 播放推进跨句：字级视图与波形字级图元跟随切换；
- 视频帧绘制直接 drawImage 到目标矩形（无中间 scaled QImage），render 不抛异常；
- 字幕 QPixmap 缓存（同字复用）与缩放帧缓存（新帧失效）；
- 静默首帧预卷（hold 期间不刷画面、只留首帧，结束后作静帧显示）；
- mpv 预览字幕按导出语义真生成（句级 SRT / 逐字 ASS / k-tag ASS）+ 档位映射表完整；
- 句级页激活时字级视图 set_playhead 不被调用。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套)

import pytest

from subs.models import Sentence, SubtitleProject

pytestmark = pytest.mark.ui


def _win_with_two_sentences():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    win = MainWindow()
    proj = SubtitleProject(
        sentences=[
            Sentence(text="第一句", start_time=0.0, end_time=1.0),
            Sentence(text="第二句", start_time=1.0, end_time=2.0),
        ],
        source_language="zh",
    )
    win._apply_project(proj)
    return win


def _case_playback_highlight_decoupled_and_moved():
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.sentence_level_view import SentenceLevelView

    # highlight_row 不触发 row_selected（播放高亮 ≠ 用户选中）
    view = SentenceLevelView()
    proj = SubtitleProject(sentences=[
        Sentence(text="第一句", start_time=0.0, end_time=1.0),
        Sentence(text="第二句", start_time=1.0, end_time=2.0),
    ])
    view.set_project(proj)
    emitted = []
    view.row_selected.connect(emitted.append)
    view.highlight_row(1)
    assert emitted == [], "播放高亮不得触发 row_selected（避免播放头闪回句首/重建图元）"
    assert view.selected_rows() == [1], "播放高亮应同步表格选中行到当前播放句"
    view.select_row(0)
    assert emitted == [0]
    view.close()

    # _on_playback_moved 走 highlight_row 而非 select_row
    win = _win_with_two_sentences()
    with patch.object(win.editor, "highlight_row") as hl, \
         patch.object(win.editor, "select_row") as sel, \
         patch.object(win.waveform, "follow_playhead"):
        win._on_playback_moved(1.5)
    hl.assert_called_once_with(1)
    sel.assert_not_called()
    win.close()


def _case_user_seek_moved_selects_row_like_table_click():
    """波形点击/拖播放头：统一走 _follow_at（highlight_row + set_active_sentence(force=True)）。"""
    from unittest.mock import patch

    win = _win_with_two_sentences()
    with patch.object(win.player, "seek") as seek, \
         patch.object(win.editor, "highlight_row") as hl, \
         patch.object(win.editor, "select_row") as sel, \
         patch.object(win.waveform, "set_active_sentence") as set_active, \
         patch.object(win.waveform, "set_playhead"):
        win._on_waveform_click_seek(1.5)
    seek.assert_called_once_with(1.5)
    hl.assert_called_once_with(1)
    set_active.assert_called_once_with(1, force=True)
    sel.assert_not_called()   # 不触发 row_selected（播放头不被拉回句首）

    hl.reset_mock()
    with patch.object(win.player, "seek"), \
         patch.object(win.editor, "highlight_row") as hl, \
         patch.object(win.waveform, "set_active_sentence"), \
         patch.object(win.waveform, "set_playhead"):
        win._on_waveform_playhead_seek(0.5)
    hl.assert_called_once_with(0)

    # 越界/无项目时不炸
    win._follow_at(999.0)
    win.close()


def _case_playback_follows_sentence_and_word_view():
    """播放推进跨句：字级视图与波形字级图元都跟随光标所在句切换。"""
    from unittest.mock import patch

    from subs.models import WordTimestamp

    win = _win_with_two_sentences()
    win._project.sentences[0].words = [
        WordTimestamp("第", 0.0, 0.25), WordTimestamp("一", 0.25, 0.5),
    ]
    win._project.sentences[1].words = [
        WordTimestamp("第", 1.0, 1.25), WordTimestamp("二", 1.25, 1.5),
    ]
    # 切到字级 Tab
    win.editor._stack.setCurrentWidget(win.editor._words_view)

    with patch.object(win.waveform, "set_active_sentence") as set_active, \
         patch.object(win.waveform, "follow_playhead"):
        win._on_playback_moved(0.5)
    set_active.assert_called_with(0, force=True)
    assert win.editor.current_word_sentence_index == 0

    with patch.object(win.waveform, "set_active_sentence") as set_active, \
         patch.object(win.waveform, "follow_playhead"):
        win._on_playback_moved(1.5)
    set_active.assert_called_with(1, force=True)   # 播放推进跨句：波形字级图元跟随
    assert win.editor.current_word_sentence_index == 1   # 字级视图跟随切换
    win.close()


def _case_video_stage_renders_frame_directly():
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.player_panel import _VideoSubtitleStage

    stage = _VideoSubtitleStage()
    stage.resize(320, 180)
    stage.set_is_video(True)
    frame = QImage(640, 360, QImage.Format.Format_RGB32)
    frame.fill(0x204060)
    stage._frame = frame

    out = QImage(320, 180, QImage.Format.Format_RGB32)
    stage.render(out)   # 走 paintEvent（drawImage 目标矩形路径），不抛异常即守护
    assert not out.isNull()
    stage.close()


def _case_player_panel_caches():
    """字幕 QPixmap 同字复用 + 缩放帧缓存新帧失效。"""
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.player_panel import _VideoSubtitleStage
    from subs.models import WordTimestamp

    proj = SubtitleProject(sentences=[
        Sentence(text="你好世界", start_time=1.0, end_time=3.0, words=[
            WordTimestamp(text="你", start_time=1.0, end_time=1.5),
            WordTimestamp(text="好", start_time=1.5, end_time=2.0),
            WordTimestamp(text="世", start_time=2.0, end_time=2.5),
            WordTimestamp(text="界", start_time=2.5, end_time=3.0),
        ]),
    ])
    stage = _VideoSubtitleStage()
    stage.resize(320, 180)
    stage.set_project(proj)
    stage.set_mode("word")
    out = QImage(320, 180, QImage.Format.Format_RGB32)

    # 字幕 QPixmap 缓存：同字不同时间 → 签名一致 → 复用同一 pixmap
    stage.set_time(1.2)
    stage.render(out)
    pix1 = stage._subtitle_pix
    sig1 = stage._subtitle_sig
    assert pix1 is not None
    stage.set_time(1.3)
    stage.render(out)
    assert stage._subtitle_sig == sig1
    assert stage._subtitle_pix is pix1
    # 跨字边界 → 签名变化 → 重新渲染
    stage.set_time(2.2)
    stage.render(out)
    assert stage._subtitle_sig != sig1
    assert stage._subtitle_pix is not pix1

    # 缩放帧缓存：同帧命中；新帧对象 → 重新缩放
    stage.set_is_video(True)
    frame = QImage(640, 360, QImage.Format.Format_RGB32)
    frame.fill(0x204060)
    stage._frame = frame
    s1 = stage._scaled_frame_or_none()
    s2 = stage._scaled_frame_or_none()
    assert s1 is not None and s1 is s2          # 缓存命中
    stage._frame = QImage(640, 360, QImage.Format.Format_RGB32)   # 新帧（新对象）
    s3 = stage._scaled_frame_or_none()
    assert s3 is not s1                          # 帧变化 → 重新缩放
    stage.close()


def _case_prime_preview_hold_release():
    """静默首帧预卷：预卷期间不刷画面、只留首帧；结束后作为静帧显示（修复「导入后闪播一下」）。"""
    from PySide6.QtGui import QImage
    from PySide6.QtMultimedia import QVideoFrame
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.player_panel import _VideoSubtitleStage

    stage = _VideoSubtitleStage()
    stage.resize(320, 180)

    def make_frame(color):
        img = QImage(64, 36, QImage.Format.Format_RGB32)
        img.fill(color)
        return QVideoFrame(img)

    # 预卷 hold：喂两帧 → 画面不更新，仅滞留首帧
    stage._prime_hold = True
    stage._prime_frame = None
    stage._on_video_frame(make_frame(0x112233))
    stage._on_video_frame(make_frame(0x445566))
    assert stage._frame is None, "预卷期间不应刷新画面"
    assert stage._prime_frame is not None, "应滞留首帧"
    first = stage._prime_frame.pixelColor(0, 0)

    # 结束预卷：释放滞留首帧作为静帧
    stage.release_prime_frame()
    assert stage._frame is not None and stage._prime_frame is None
    assert stage._frame.pixelColor(0, 0) == first, "释放的应是滞留的首帧"
    # 幂等
    stage.release_prime_frame()
    assert stage._frame is not None
    stage.close()


def _case_mpv_backend_renders_audio_subtitles_and_falls_back_without_waiting(tmp_path):
    """mpv ready 后视频/音频均接管；纯音频也在 force-window 上用 libass 预览。"""
    from pathlib import Path

    from PySide6.QtWidgets import QApplication, QWidget

    QApplication.instance() or QApplication(["test"])
    from ui.player_panel import PlayerPanel

    class FakeMpv:
        failed = False
        dll_path = Path("C:/fake/libmpv-2.dll")

        def __init__(self):
            self.loaded = []
            self.pause_calls = 0
            self.shutdown_calls = 0

        def load(self, path):
            self.loaded.append(Path(path))
            return True

        def pause(self):
            self.pause_calls += 1
            return True

        def position(self):
            return 1.25

        def shutdown(self):
            self.shutdown_calls += 1

    video = tmp_path / "clip.mp4"
    audio = tmp_path / "voice.wav"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")

    panel = PlayerPanel()
    fake = FakeMpv()
    panel._mpv = fake
    panel._mpv_ready = True
    panel._mpv_host = QWidget(panel)

    panel.load(video)
    assert panel._active_backend == "mpv"
    assert fake.loaded == [video]

    panel.load(audio)
    assert panel._active_backend == "mpv", "纯音频也要用 force-window + libass 预览字幕"
    assert fake.loaded == [video, audio]
    assert fake.pause_calls >= 1
    panel._update_backend_label()
    assert panel._backend_label.text() == "libmpv · 音频"

    # 音频 mpv 命令若超时，仍须无等待地回退 QMediaPlayer。
    panel._on_mpv_failed("watchdog timeout")
    assert panel._active_backend == "qt"
    assert panel._mpv_ready is False
    assert "watchdog timeout" in panel._backend_label.toolTip()
    panel.shutdown_mpv()
    assert fake.shutdown_calls == 1
    panel.close()


def _case_mpv_subtitle_generation():
    """mpv 预览字幕按导出语义真生成（句级 SRT / 逐字 ASS / k-tag ASS），映射表完整。

    沙箱无 python-mpv → mpv 后端不激活，但字幕生成函数是纯逻辑可直测。
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.player_panel import PlayerPanel
    from ui.subtitle_overlay import PREVIEW_MODES
    from subs.models import WordTimestamp

    panel = PlayerPanel()
    proj = SubtitleProject(
        source_media_path="clip.mp4",
        sentences=[
            Sentence(text="你好世界", start_time=0.0, end_time=1.0, language="zh",
                     words=[
                         WordTimestamp(text="你", start_time=0.0, end_time=0.5, language="zh"),
                         WordTimestamp(text="好", start_time=0.5, end_time=1.0, language="zh"),
                     ]),
        ],
    )
    panel.set_project(proj)

    # 档位 → 导出 kind 映射覆盖除 karaoke_template 外的五档（该档单独走应用器）
    assert set(PlayerPanel._MPV_PREVIEW_KIND) == set(PREVIEW_MODES) - {"karaoke_template"}

    # 句级 SRT
    srt = panel._render_preview_subtitle("srt_sentence")
    assert "你好世界" in srt and "-->" in srt

    # 逐字 ASS（split 策略）
    ass_split = panel._render_preview_subtitle("ass_split")
    assert "[Script Info]" in ass_split and "Dialogue:" in ass_split

    # k-tag ASS（libass 真渲染 kf）
    karaoke = panel._render_preview_subtitle("karaoke")
    assert "[Script Info]" in karaoke and "{\\kf" in karaoke

    # 所选模板档：原 k-tag 保留为 Comment/effect=karaoke，视觉只渲染 fx Dialogue。
    applied = panel._render_karaoke_template_applied()
    assert "Dialogue:" in applied and ",fx," in applied
    assert ",karaoke,{\\kf" in applied
    assert not any(
        r"{\kf" in line for line in applied.splitlines() if line.startswith("Dialogue:")
    )
    assert "\\fscx" in applied

    # 默认居中坐标：kara[0] 在行左边界，真实 syllable 向右；连续相同文本可重置。
    from subs.ass_style import AssStylePrefs
    coord = panel._make_coord_provider(AssStylePrefs(
        font_name="Arial", font_size=60, alignment=2,
        play_res_x=1920, play_res_y=1080, margin_v=60,
    ))
    blank = coord(0, "", "AB")
    first = coord(1, "A", "AB")
    assert blank[0] < first[0] < blank[2] == 960
    assert coord(0, "", "AB") == blank  # index 回绕代表下一句，cursor 必须重置

    panel.close()


def _case_player_focus_mode_and_compact_controls(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QSizePolicy

    app = QApplication.instance() or QApplication(["test"])
    win = _win_with_two_sentences()
    win.show()
    app.processEvents()
    QTest.mouseClick(win.player._stage, Qt.MouseButton.LeftButton)
    assert win._player_focus_active is False  # 没有媒体时单击不得误进沉浸模式

    media = tmp_path / "focus.mp4"
    media.write_bytes(b"video")
    win.player._media_path = media
    win.player._update_focus_click_hint()

    # 顶部为右对齐紧凑预览组；画面居中；最后一行为两侧 stretch 的控制条。
    root = win.player._root_layout
    assert root.itemAt(0).layout() is win.player._preview_bar
    assert root.itemAt(root.count() - 1).layout() is win.player._transport_bar
    preview = win.player._preview_bar
    transport = win.player._transport_bar
    assert preview.itemAt(0).spacerItem() is not None
    assert win.player._mode_combo.sizePolicy().horizontalPolicy() == QSizePolicy.Fixed
    assert 230 <= win.player._mode_combo.width() <= 310
    assert transport.itemAt(0).spacerItem() is not None
    assert transport.itemAt(1).widget() is win.player._btn_play
    assert transport.itemAt(2).widget() is win.player._btn_pause
    assert transport.itemAt(3).widget() is win.player._btn_stop
    assert transport.itemAt(4).spacerItem() is not None

    parent_before = win.player.parentWidget()
    QTest.mouseClick(win.player._stage, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert win._player_focus_active is True
    assert win.player.parentWidget() is parent_before  # 禁止重挂播放器/mpv HWND
    for widget in (
        win.editor,
        win.waveform,
        win._export_panel,
        win.menuBar(),
        win._main_toolbar,
        win.statusBar(),
    ):
        assert widget.isHidden()
    assert not win.player.isHidden()
    assert win.player._btn_play.isEnabled()
    assert win.player._btn_pause.isEnabled()
    assert win.player._btn_stop.isEnabled()

    # 再点画面恢复；200ms 去重窗只抑制同一 native 点击的重复来源。
    QTest.qWait(220)
    QTest.mouseClick(win.player._stage, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert win._player_focus_active is False
    assert all(
        not widget.isHidden()
        for widget in (
            win.editor,
            win.waveform,
            win._export_panel,
            win.menuBar(),
            win._main_toolbar,
            win.statusBar(),
        )
    )
    QTest.qWait(220)
    QTest.mouseClick(win.player._stage, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert win._player_focus_active is True
    QTest.keyClick(win, Qt.Key.Key_Escape)
    app.processEvents()
    assert win._player_focus_active is False

    win.close()


def _case_qt_pause_mutes_before_waiting_for_media_backend():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication(["test"])
    from ui.player_panel import PlayerPanel

    calls = []

    class FakeAudio:
        def __init__(self):
            self.muted = False

        def isMuted(self):
            return self.muted

        def setMuted(self, value):
            self.muted = bool(value)
            calls.append(("mute", self.muted))

    class FakePlayer:
        def pause(self):
            calls.append(("pause", None))

        def play(self):
            calls.append(("play", None))

    panel = PlayerPanel()
    panel._active_backend = "qt"
    panel._audio_out = FakeAudio()
    panel._player = FakePlayer()
    panel.pause()
    assert calls[:2] == [("mute", True), ("pause", None)]
    panel.play()
    assert calls[-2:] == [("mute", False), ("play", None)]
    panel.close()


def _case_editor_set_playhead_skips_hidden_word_view():
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.subs_editor import SubsEditor
    from subs.models import WordTimestamp

    editor = SubsEditor()
    editor.set_project(SubtitleProject(sentences=[
        Sentence(text="测试", start_time=0.0, end_time=1.0, words=[
            WordTimestamp(text="测", start_time=0.0, end_time=0.5),
            WordTimestamp(text="试", start_time=0.5, end_time=1.0),
        ]),
    ]))
    # 默认句级页激活 → 字级页不可见 → 不调用其 set_playhead
    with patch.object(editor._words_view, "set_playhead") as wp:
        editor.set_playhead(0.5)
    wp.assert_not_called()
    editor.close()


def test_playback_follow_pack():
    """test_playback_follow_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_playback_highlight_decoupled_and_moved()
    _case_user_seek_moved_selects_row_like_table_click()
    _case_playback_follows_sentence_and_word_view()


def test_playback_render_pack(tmp_path):
    """播放渲染 8 场景：含沉浸、Qt 即时静音暂停、mpv 路由与字幕生成。"""
    _case_video_stage_renders_frame_directly()
    _case_player_panel_caches()
    _case_prime_preview_hold_release()
    _case_mpv_backend_renders_audio_subtitles_and_falls_back_without_waiting(tmp_path)
    _case_mpv_subtitle_generation()
    _case_player_focus_mode_and_compact_controls(tmp_path)
    _case_qt_pause_mutes_before_waiting_for_media_backend()
    _case_editor_set_playhead_skips_hidden_word_view()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
