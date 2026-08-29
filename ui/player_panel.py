"""ui.player_panel — 播放与字幕预览面板

职责（本面板只预览；编辑在句表/波形）：
- 视频：QMediaPlayer + **QVideoSink** 把帧画进 QWidget（**不用 QVideoWidget**）
- 字幕：与视频在**同一 paintEvent** 里绘制 → Windows 上不会被原生 HWND 视频窗盖住
- 可选 libmpv：异步预热，视频/纯音频都可在嵌入画布用 libass 预览；命令走 worker + watchdog
- 六档预览模式（含所选卡拉OK模板效果，持久化；模板档不叠加基础 k-tag 扫过）

关于 ``[hevc] Failed setup for format d3d11``：
- 这是 Qt/FFmpeg 尝试 **D3D11 硬解 HEVC 失败** 的日志，与「是否安装微软 HEVC 扩展」
  不是一回事——商店扩展主要给 Movies & TV / 部分 UWP，**不保证** Qt 硬解成功。
- 可靠做法：进程启动前禁用硬解设备（见 main.py），走软解；装扩展也挡不住这条日志时
  可忽略（软解仍可播），或把预览片源转成 H.264。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox

from subs.media_types import VIDEO_SUFFIXES

from .player_focus_surface import FocusClickHost, PlayerFocusSurfaceMixin
from .player_qt_runtime import QtPlaybackRuntimeMixin
from .player_stage import _VideoSubtitleStage
from .player_subtitle_preview import SubtitlePreviewMixin
from .qt_media import (
    HAS_QT_MULTIMEDIA as _HAS_QT_MULTIMEDIA,
    QAudioOutput,
    QMediaPlayer,
)
from .subtitle_overlay import PREVIEW_MODES

logger = logging.getLogger(__name__)


class PlayerPanel(PlayerFocusSurfaceMixin, SubtitlePreviewMixin, QtPlaybackRuntimeMixin, QWidget):
    """播放 + 字幕预览面板。"""

    position_changed = Signal(float)
    duration_changed = Signal(float)
    state_playing = Signal()
    state_paused = Signal()
    state_stopped = Signal()
    focus_mode_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("player_panel")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._media_path: Optional[Path] = None
        self._is_video = False
        self._soft_decode_retried = False
        self._priming = False           # 静默预卷进行中：不对外暴露播放态/播放头/音频
        self._prime_was_muted = False   # 预卷前用户的静音状态，结束时还原
        self._qt_pause_muted = False    # pause 前先静音，消除音频 sink 缓冲尾音
        self._qt_pause_previous_muted = False
        self._focus_mode = False
        self._focus_click_targets: set[QWidget] = set()
        self._last_focus_toggle_request = 0.0

        self._player = None
        self._audio_out = None
        if _HAS_QT_MULTIMEDIA:
            self._player = QMediaPlayer(self)
            self._audio_out = QAudioOutput(self)
            self._player.setAudioOutput(self._audio_out)
            self._player.positionChanged.connect(self._on_position_ms)
            self._player.durationChanged.connect(
                lambda ms: self.duration_changed.emit(ms / 1000.0))
            self._player.playbackStateChanged.connect(self._on_state)
            try:
                self._player.errorOccurred.connect(self._on_player_error)
            except Exception:  # noqa: BLE001
                pass
            # 媒体元数据/缓冲就绪后预卷一帧，避免导入后一直黑屏占位
            try:
                self._player.mediaStatusChanged.connect(self._on_media_status)
            except Exception:  # noqa: BLE001
                logger.debug("[播放器] mediaStatusChanged 不可用")

        # ── 可选 mpv 真渲染后端 ──────────────────────────────────
        # 只做零原生调用的快速探测；首次加载视频时才在 daemon worker 异步
        # import/初始化 libmpv。Qt 先可用，任何 mpv 阻塞都不能卡住 GUI。
        self._project = None
        self._user_wants_play = False
        self._active_backend = "qt"
        self._mpv = None
        self._mpv_ready = False
        self._mpv_failed_reason = ""
        self._mpv_host = None
        try:
            from .mpv_backend import probe_libmpv

            self._mpv_probe = probe_libmpv()
        except Exception as e:  # noqa: BLE001
            logger.warning("[播放器] mpv 快速探测失败: %s", e)
            self._mpv_probe = None
        if self._mpv_probe is not None and self._mpv_probe.candidate:
            self._mpv_host = FocusClickHost(self)
            self._mpv_host.setObjectName("player_mpv_host")
            # mpv 只需要这个画布拥有 HWND；禁止 Qt 因其原生化而连带把祖先/
            # ComboBox 等兄弟控件变成 native window，否则 QFluentWidgets 的
            # Popup 会把非顶层 ComboBoxClassWindow 当 transient parent 并报警。
            self._mpv_host.setAttribute(
                Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True,
            )
            self._mpv_host.setMinimumHeight(160)
            self._mpv_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self._mpv_host.hide()
            self._install_focus_click_target(self._mpv_host)

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        self._root_layout = root

        # 顶部右对齐紧凑组：空白留在左侧，下拉框按最长选项定宽而不横向拉伸。
        preview_bar = QHBoxLayout()
        preview_bar.setSpacing(6)
        preview_bar.addStretch(1)
        preview_bar.addWidget(CaptionLabel("预览字幕", self))
        self._mode_combo = ComboBox(self)
        for key, zh in PREVIEW_MODES.items():
            self._mode_combo.addItem(zh, userData=key)
        self._mode_combo.setToolTip(
            "画面上字幕预览方式；‘所选模板’只显示模板展开后的 fx 效果，不叠加基础 k-tag 扫过。"
            "raw/额外代码在 Qt 兼容预览中可能降级，libmpv 以展开后的 ASS 为准。"
        )
        text_width = max(
            QFontMetrics(self._mode_combo.font()).horizontalAdvance(label)
            for label in PREVIEW_MODES.values()
        )
        self._mode_combo.setFixedWidth(max(230, min(310, text_width + 48)))
        self._mode_combo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        preview_bar.addWidget(self._mode_combo)
        self._backend_label = CaptionLabel("", self)
        self._backend_label.setMinimumWidth(0)
        self._backend_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        preview_bar.addWidget(self._backend_label)
        self._update_backend_label()
        self._preview_bar = preview_bar
        root.addLayout(preview_bar)

        self._stage = _VideoSubtitleStage(self)
        self._install_focus_click_target(self._stage)
        root.addWidget(self._stage, 1)
        if self._mpv_host is not None:
            # 初始化完成前始终显示 Qt stage；ready 后再原子切换，避免黑屏/启动卡死。
            root.addWidget(self._mpv_host, 1)
            self._mpv_host.hide()

        # 底部独立控制条：播放/暂停/停止三键整体居中。
        self._transport_bar = self._build_transport_bar()
        root.addLayout(self._transport_bar)

        # 兼容旧测试/外部属性名
        self.subtitle_overlay = self._stage
        self._video_widget = self._stage
        self._audio_backdrop = self._stage
        self._canvas = self._stage

        if self._player is not None and self._stage.video_sink() is not None:
            try:
                self._player.setVideoSink(self._stage.video_sink())
            except Exception as e:  # noqa: BLE001
                logger.warning("[播放器] setVideoSink 失败: %s — 将无画面仅字幕", e)

        self._prefer_software_video_decode()
        self._stage.refresh_styles()
        self._restore_mode_pref()
        self._show_video_surface(False)
        self._update_focus_click_hint()

        # UI 事件循环启动后立即异步预热；打开窗口时先显示“mpv 初始化…”，
        # ready 后即显示“libmpv 已就绪”，无需等用户导入视频才知道能力状态。
        if self._mpv_probe is not None and self._mpv_probe.candidate:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self._ensure_mpv_started)

    # ── 后端生命周期 / 路由 ─────────────────────────────────────
    def _update_backend_label(self, detail: str = "") -> None:
        """显示“能力/当前媒体”状态；正文保持短，完整原因放 tooltip。"""
        label = getattr(self, "_backend_label", None)
        if label is None:
            return
        probe = getattr(self, "_mpv_probe", None)
        if self._mpv_failed_reason:
            text = "Qt · mpv 回退"
            tooltip = self._mpv_failed_reason
        elif self._active_backend == "mpv" and self._mpv_ready:
            if self._is_video:
                text = "libmpv/libass"
                tooltip = detail or "当前视频由独立 mpv worker + libass 渲染"
            else:
                text = "libmpv · 音频"
                tooltip = detail or "音频由 mpv 播放；force-window 画布使用 libass 渲染字幕"
        elif self._mpv_ready and self._media_path is None:
            text = "libmpv 已就绪"
            tooltip = detail or "libmpv/libass 已在后台初始化完成，等待导入媒体"
        elif (
            (self._mpv is not None and not self._mpv_ready)
            or (probe is not None and probe.candidate)
        ):
            text = "mpv 初始化…"
            tooltip = detail or "Qt 界面保持可用；libmpv 正在 daemon worker 初始化"
        else:
            text = "Qt 兼容"
            tooltip = detail or (probe.reason if probe is not None else "未启用 libmpv")
        label.setText(text)
        label.setToolTip(tooltip)

    def _activate_backend(self, backend: str) -> None:
        use_mpv = (
            backend == "mpv"
            and self._mpv is not None
            and self._mpv_ready
            and self._mpv_host is not None
        )
        self._active_backend = "mpv" if use_mpv else "qt"
        if self._active_backend == "mpv":
            self._stage.hide()
            self._mpv_host.show()
        else:
            if self._mpv_host is not None:
                self._mpv_host.hide()
            self._stage.show()
        self._update_backend_label()

    def _load_qt_media(self, *, position: float = 0.0, resume: bool = False) -> None:
        """把当前媒体交给 QMediaPlayer；用于默认路径和 mpv watchdog 回退。"""
        if self._media_path is None:
            return
        self._activate_backend("qt")
        self._show_video_surface(self._is_video)
        self._stage.clear_frame()
        if self._player is not None:
            self._prefer_software_video_decode()
            sink = self._stage.video_sink()
            if sink is not None:
                try:
                    self._player.setVideoSink(sink)
                except Exception:  # noqa: BLE001
                    logger.debug("[播放器] Qt fallback setVideoSink 失败")
            self._player.setSource(QUrl.fromLocalFile(str(self._media_path.resolve())))
            if position > 0:
                self._player.setPosition(int(position * 1000))
            if resume:
                self._restore_qt_pause_audio()
                self._player.play()
        self._stage.update()

    def _ensure_mpv_started(self) -> None:
        """异步预热 mpv worker；本方法不执行任何同步原生 mpv 调用。"""
        probe = getattr(self, "_mpv_probe", None)
        if (
            self._mpv is not None
            or self._mpv_host is None
            or probe is None
            or not probe.candidate
            or self._mpv_failed_reason
        ):
            return
        try:
            from .mpv_backend import MpvBackend

            self._mpv = MpvBackend(
                self._mpv_host,
                on_time=self._on_mpv_time,
                on_duration=self._on_mpv_duration,
                on_playing=self._on_mpv_playing,
                on_ready=self._on_mpv_ready,
                on_failed=self._on_mpv_failed,
                on_surface_click=self._request_focus_mode_toggle,
            )
            self._update_backend_label()
        except Exception as e:  # noqa: BLE001
            self._on_mpv_failed(f"mpv worker 启动失败：{type(e).__name__}: {e}")

    def _on_mpv_ready(self) -> None:
        """GUI 线程 slot：后台初始化成功后，仅对当前视频切换后端。"""
        if self._mpv is None or self._mpv.failed:
            return
        self._mpv_ready = True
        logger.info("[播放器] libmpv 真渲染后端已异步启用: %s", self._mpv.dll_path)
        if self._media_path is None:
            self._update_backend_label("libmpv/libass 已就绪，等待导入媒体")
            return
        # 视频与纯音频都切到 mpv。纯音频由 force-window 提供空白 VO，字幕仍由
        # libass 真渲染；QMediaPlayer 只作为 mpv 不可用/超时后的回退。
        position = self._player.position() / 1000.0 if self._player is not None else 0.0
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        self._activate_backend("mpv")
        if not self._mpv.load(self._media_path):
            self._on_mpv_failed("mpv load 命令未能入队")
            return
        if position > 0:
            self._mpv.seek(position)
        self._rebuild_mpv_subtitle()
        if self._user_wants_play:
            self._mpv.play()

    def _on_mpv_failed(self, message: str) -> None:
        """GUI 线程 slot：初始化/命令超时或失败时立即回退 Qt，绝不等待 worker。"""
        message = str(message or "未知 libmpv 错误")
        was_active = self._active_backend == "mpv"
        position = self._mpv.position() if self._mpv is not None else 0.0
        self._mpv_ready = False
        self._mpv_failed_reason = message
        logger.error("[播放器] %s", message)
        if was_active and self._media_path is not None:
            self._load_qt_media(position=position, resume=self._user_wants_play)
        else:
            self._activate_backend("qt")
        self._update_backend_label()

    def load(self, media_path: str | Path) -> None:
        self._media_path = Path(media_path)
        self._update_focus_click_hint()
        if not self._media_path.is_file():
            return
        self._soft_decode_retried = False
        self._preview_primed = False
        if self._active_backend == "mpv" and self._mpv_ready and self._mpv is not None:
            self._mpv.pause()  # 非阻塞；切媒体先阻止旧视频继续出声
        self._user_wants_play = False
        self._cancel_prime_timer()
        self._end_priming()
        self._is_video = self._media_path.suffix.lower() in VIDEO_SUFFIXES
        self._show_video_surface(self._is_video)

        if self._mpv_ready and self._mpv is not None:
            self._activate_backend("mpv")
            if self._mpv.load(self._media_path):
                self._rebuild_mpv_subtitle()
                return
            self._on_mpv_failed("mpv load 命令未能入队")

        # mpv 未就绪时 Qt 先立即可用；视频/音频都继续等待后台 mpv 预热。
        self._load_qt_media()
        self._ensure_mpv_started()

    def set_project(self, project) -> None:
        self._project = project
        self._stage.set_project(project)
        if self._active_backend == "mpv" and self._mpv_ready:
            self._rebuild_mpv_subtitle()

    def refresh_subtitle_styles(self) -> None:
        """样式（ASS 文字样式 / 逐字高亮 / 卡拉OK模板 / k-tag）变更后刷新字幕预览。

        导出面板的样式控件在变更后调用本方法，让六档预览立即反映新样式。
        """
        self._stage.refresh_styles()
        if self._active_backend == "mpv" and self._mpv_ready:
            self._rebuild_mpv_subtitle()

    def refresh_subtitle_content(self) -> None:
        """正文或时间被原地编辑后，立即刷新 Qt 缓存及 mpv/libass 字幕轨。"""
        self._stage.refresh_content()
        if self._active_backend == "mpv" and self._mpv_ready:
            self._rebuild_mpv_subtitle()

    def set_preview_time(self, t: float) -> None:
        self._stage.set_time(t)

    def _mpv_command_or_fallback(self, name: str, accepted: bool) -> bool:
        if accepted:
            return True
        self._on_mpv_failed(f"mpv {name} 命令未能入队；已回退 Qt")
        return False

    def play(self) -> None:
        self._user_wants_play = True
        self._cancel_prime_timer()
        self._end_priming()
        if self._active_backend == "mpv" and self._mpv_ready and self._mpv is not None:
            if self._mpv_command_or_fallback("play", self._mpv.play()):
                self.state_playing.emit()
                return
        if self._player is not None:
            self._prefer_software_video_decode()
            self._restore_qt_pause_audio()
            self._player.play()
        self.state_playing.emit()

    def pause(self) -> None:
        self._user_wants_play = False
        self._cancel_prime_timer()
        self._end_priming()
        if self._active_backend == "mpv" and self._mpv_ready and self._mpv is not None:
            if self._mpv_command_or_fallback("pause", self._mpv.pause()):
                self.state_paused.emit()
                return
        if self._player is not None:
            self._silence_qt_pause_buffer()
            self._player.pause()
        self.state_paused.emit()

    def stop(self) -> None:
        self._user_wants_play = False
        self._cancel_prime_timer()
        self._end_priming()
        if self._active_backend == "mpv" and self._mpv_ready and self._mpv is not None:
            if self._mpv_command_or_fallback("stop", self._mpv.stop()):
                self.state_stopped.emit()
                return
        if self._player is not None:
            self._player.stop()
        # 停止后保留最后一帧作静帧预览（比清空成黑底体验更好）
        self.state_stopped.emit()

    def seek(self, seconds: float) -> None:
        target = max(0.0, float(seconds))
        if self._active_backend == "mpv" and self._mpv_ready and self._mpv is not None:
            if self._mpv_command_or_fallback("seek", self._mpv.seek(target)):
                return
        if self._player is not None:
            self._player.setPosition(int(target * 1000))
        self._stage.set_time(target)

    def position(self) -> float:
        if self._active_backend == "mpv" and self._mpv_ready and self._mpv is not None:
            return self._mpv.position()
        if self._player is None:
            return 0.0
        return self._player.position() / 1000.0

    def toggle_play_pause(self) -> None:
        if self._active_backend == "mpv":
            if self._user_wants_play:
                self.pause()
            else:
                self.play()
            return
        if self._player is None:
            return
        from PySide6.QtMultimedia import QMediaPlayer as _Q
        if self._player.playbackState() == _Q.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def play_pause(self) -> None:
        self.toggle_play_pause()

    def duration(self) -> float:
        if self._active_backend == "mpv" and self._mpv_ready and self._mpv is not None:
            return self._mpv.duration()
        if self._player is None:
            return 0.0
        return self._player.duration() / 1000.0

    def preview_mode(self) -> str:
        return self._stage.mode()

    def shutdown_mpv(self) -> None:
        """非阻塞请求释放 mpv；关闭窗口绝不等待可能卡住的原生 terminate。"""
        backend, self._mpv = self._mpv, None
        self._mpv_ready = False
        self._active_backend = "qt"
        if backend is not None:
            try:
                backend.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[mpv] 异步 shutdown 请求失败: %s", exc)


__all__ = ["PlayerPanel", "_VideoSubtitleStage"]
