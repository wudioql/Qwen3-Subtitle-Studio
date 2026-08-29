"""libmpv 阻塞隔离与命令合并回归（纯逻辑，零 Qt/libmpv）。"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401

import threading
import time

import pytest

from ui.mpv_worker import MpvWorker

pytestmark = pytest.mark.logic


def test_slow_init_times_out_without_blocking_caller():
    failed = []
    fatal = threading.Event()

    def slow_factory():
        time.sleep(0.20)
        return object()

    started = time.monotonic()
    worker = MpvWorker(
        slow_factory,
        on_ready=lambda: None,
        on_fatal=lambda msg: (failed.append(msg), fatal.set()),
        init_timeout=0.03,
    )
    assert time.monotonic() - started < 0.02, "构造不得在调用/UI 线程等待 libmpv"
    assert fatal.wait(0.15), "初始化 watchdog 应触发 Qt 回退"
    assert "超过" in failed[0] and "回退 Qt" in failed[0]
    assert worker.failed


def test_hung_command_is_non_blocking_and_triggers_fallback():
    ready = threading.Event()
    fatal = threading.Event()
    failed = []
    worker = MpvWorker(
        object,
        on_ready=ready.set,
        on_fatal=lambda msg: (failed.append(msg), fatal.set()),
        init_timeout=0.2,
    )
    assert ready.wait(0.2)

    started = time.monotonic()
    assert worker.submit("play", lambda _p: time.sleep(0.20), timeout=0.03)
    assert time.monotonic() - started < 0.02, "播放调用只应入队，不得卡住界面"
    assert fatal.wait(0.15)
    assert "play" in failed[0]
    assert worker.failed


def test_shutdown_never_joins_hung_native_terminate():
    ready = threading.Event()
    worker = MpvWorker(
        object,
        on_ready=ready.set,
        on_fatal=lambda _msg: None,
        init_timeout=0.2,
    )
    assert ready.wait(0.2)

    started = time.monotonic()
    worker.close(lambda _p: time.sleep(0.20), timeout=0.03)
    assert time.monotonic() - started < 0.02, "关闭窗口不得等待 libmpv terminate/join"


def test_native_mpv_host_is_isolated_before_creating_hwnd():
    """防止 HWND 原生属性扩散到 Fluent ComboBox，导致 Popup transient 警告。"""
    backend_source = (PROJECT_ROOT / "ui" / "mpv_backend.py").read_text(encoding="utf-8")
    dont_ancestors = backend_source.index("WA_DontCreateNativeAncestors")
    make_native = backend_source.index("WA_NativeWindow", dont_ancestors)
    assert dont_ancestors < make_native
    assert 'force_window="immediate"' in backend_source
    assert "audio_display=False" in backend_source
    # --wid 会创建覆盖 Qt host 的 mpv 子窗口；点击必须由 mpv 自身回传。
    assert "input_default_bindings=False" in backend_source
    assert "input_vo_keyboard=True" in backend_source
    assert '@player.on_key_press("MOUSE_BTN0")' in backend_source
    assert "self._bridge.surface_clicked" in backend_source

    panel_source = (PROJECT_ROOT / "ui" / "player_panel.py").read_text(encoding="utf-8")
    assert "on_surface_click=self._request_focus_mode_toggle" in panel_source

    main_source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    sibling_policy = main_source.index("AA_DontCreateNativeWidgetSiblings")
    app_creation = main_source.index("app = QApplication(sys.argv)")
    assert sibling_policy < app_creation


def test_subtitle_replacement_requests_nonfatal_current_frame_redraw():
    """保存模板后即使 mpv 正暂停，也必须让 libass 立刻重算当前帧。"""
    source = (PROJECT_ROOT / "ui" / "mpv_backend.py").read_text(encoding="utf-8")
    assign_track = source.index("self._subtitle_track_id = parsed_id")
    redraw_call = source.index("self._request_subtitle_redraw(player)", assign_track)
    redraw_method = source.index("def _request_subtitle_redraw", redraw_call)
    assert assign_track < redraw_call < redraw_method
    redraw_body = source[redraw_method:source.index("def _remove_subtitle_track", redraw_method)]
    assert 'player.command("seek", "0", "relative+exact")' in redraw_body
    assert "except Exception" in redraw_body  # 重绘失败不得触发 mpv 后端 fatal 回退

    export_source = (PROJECT_ROOT / "ui" / "export_panel.py").read_text(encoding="utf-8")
    assert "_refresh_player_preview(self)" in export_source


def test_player_panel_stable_subdomains_are_split_behind_facade():
    """大面板拆分后旧导入名不变，stage/字幕管线/Qt runtime 各自单一职责。"""
    panel_path = PROJECT_ROOT / "ui" / "player_panel.py"
    panel_source = panel_path.read_text(encoding="utf-8")
    stage_source = (PROJECT_ROOT / "ui" / "player_stage.py").read_text(encoding="utf-8")
    preview_source = (
        PROJECT_ROOT / "ui" / "player_subtitle_preview.py"
    ).read_text(encoding="utf-8")
    qt_source = (PROJECT_ROOT / "ui" / "player_qt_runtime.py").read_text(encoding="utf-8")
    media_source = (PROJECT_ROOT / "ui" / "qt_media.py").read_text(encoding="utf-8")
    surface_source = (
        PROJECT_ROOT / "ui" / "player_focus_surface.py"
    ).read_text(encoding="utf-8")

    assert len(panel_source.splitlines()) < 500
    assert "class _VideoSubtitleStage" not in panel_source
    assert "from .player_stage import _VideoSubtitleStage" in panel_source
    assert "class _VideoSubtitleStage" in stage_source
    assert "class SubtitlePreviewMixin" in preview_source
    assert "class QtPlaybackRuntimeMixin" in qt_source
    assert "HAS_QT_MULTIMEDIA" in media_source
    assert "class FocusClickHost" in surface_source
    assert "class PlayerFocusSurfaceMixin" in surface_source
    assert "mousePressEvent" in surface_source and "mouseReleaseEvent" in surface_source
    assert "installEventFilter" not in surface_source
    assert "self.has_playable_media()" in surface_source
    assert "widget.clicked.connect(self._request_focus_mode_toggle)" in surface_source
    assert "GetAsyncKeyState" not in surface_source
    assert "_native_click_timer" not in surface_source
    assert "_btn_focus" not in surface_source
    assert "clicked = Signal()" in stage_source
    assert (
        "class PlayerPanel(PlayerFocusSurfaceMixin, SubtitlePreviewMixin, "
        "QtPlaybackRuntimeMixin, QWidget)" in panel_source
    )
    assert '__all__ = ["PlayerPanel", "_VideoSubtitleStage"]' in panel_source


def test_player_focus_mode_hides_siblings_without_reparenting_native_host():
    source = (
        PROJECT_ROOT / "ui" / "main_window" / "player_focus.py"
    ).read_text(encoding="utf-8")
    assert "self._main_h_split.sizes()" in source
    assert "self._left_v_split.sizes()" in source
    assert "self._top_split.sizes()" in source
    for name in ("self.editor", "self.waveform", "self._export_panel"):
        assert name in source
    assert "self.menuBar()" in source
    assert "self._main_toolbar" in source
    assert "self.statusBar()" in source
    assert ".setParent(" not in source and "reparent" not in source.lower()
    assert "Qt.Key.Key_Escape" in source

    panel_source = (PROJECT_ROOT / "ui" / "player_panel.py").read_text(encoding="utf-8")
    surface_source = (
        PROJECT_ROOT / "ui" / "player_focus_surface.py"
    ).read_text(encoding="utf-8")
    assert "preview_bar.addStretch(1)" in panel_source
    assert "bar.addStretch(1)" in surface_source
    assert "root.addLayout(self._transport_bar)" in panel_source
    assert "self._mode_combo.setFixedWidth" in panel_source


def test_qt_pause_silences_audio_before_backend_pause_and_restores_on_play():
    panel_source = (PROJECT_ROOT / "ui" / "player_panel.py").read_text(encoding="utf-8")
    pause_start = panel_source.index("def pause(self)")
    pause_end = panel_source.index("def stop(self)", pause_start)
    pause_body = panel_source[pause_start:pause_end]
    assert pause_body.index("self._silence_qt_pause_buffer()") < pause_body.index(
        "self._player.pause()"
    )
    play_start = panel_source.index("def play(self)")
    play_end = pause_start
    play_body = panel_source[play_start:play_end]
    assert play_body.index("self._restore_qt_pause_audio()") < play_body.index(
        "self._player.play()"
    )

    qt_source = (PROJECT_ROOT / "ui" / "player_qt_runtime.py").read_text(encoding="utf-8")
    assert "def _silence_qt_pause_buffer" in qt_source
    assert "def _restore_qt_pause_audio" in qt_source
    assert "was_priming = self._priming or self._stage._prime_hold" in qt_source


def test_content_edits_rebuild_current_qt_and_mpv_subtitle():
    """正文/时间 UndoCommand 的 on_change 必须同时刷新当前 Qt 缓存和 mpv 轨。"""
    player_source = (PROJECT_ROOT / "ui" / "player_panel.py").read_text(encoding="utf-8")
    stage_source = (PROJECT_ROOT / "ui" / "player_stage.py").read_text(encoding="utf-8")
    cache_start = stage_source.index("def refresh_content")
    cache_end = stage_source.index("def set_time", cache_start)
    cache_method = stage_source[cache_start:cache_end]
    assert "self._subtitle_sig = ()" in cache_method
    assert "self._subtitle_pix = None" in cache_method

    method_start = player_source.index("def refresh_subtitle_content")
    method_end = player_source.index("def set_preview_time", method_start)
    method = player_source[method_start:method_end]
    assert "self._stage.refresh_content()" in method
    assert "self._rebuild_mpv_subtitle()" in method

    editing_source = (
        PROJECT_ROOT / "ui" / "main_window" / "editing.py"
    ).read_text(encoding="utf-8")
    refresher_start = editing_source.index("def _make_row_refresher")
    refresher_end = editing_source.index("def _refresh_single_row", refresher_start)
    assert "self.player.refresh_subtitle_content()" in editing_source[
        refresher_start:refresher_end
    ]
    full_start = editing_source.index("def _refresh_after_edit")
    assert "self.player.refresh_subtitle_content()" in editing_source[
        full_start:refresher_start
    ]


def test_high_frequency_seek_keeps_only_latest_pending_value():
    ready = threading.Event()
    blocker_started = threading.Event()
    release = threading.Event()
    latest_done = threading.Event()
    values = []

    worker = MpvWorker(
        object,
        on_ready=ready.set,
        on_fatal=lambda _msg: None,
        init_timeout=0.2,
        queue_limit=8,
    )
    assert ready.wait(0.2)

    def blocker(_p):
        blocker_started.set()
        release.wait(0.2)

    assert worker.submit("blocker", blocker, timeout=0.5)
    assert blocker_started.wait(0.2)
    for value in range(100):
        assert worker.submit(
            "seek", lambda _p, v=value: (values.append(v), latest_done.set()),
            timeout=0.2, coalesce_key="seek",
        )
    release.set()
    assert latest_done.wait(0.2)
    assert values == [99]
    worker.close(lambda _p: None)
