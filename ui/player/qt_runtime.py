"""PlayerPanel 的 QMediaPlayer 软解、首帧预卷与模式持久化子域。"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QUrl

from .qt_media import HAS_QT_MULTIMEDIA as _HAS_QT_MULTIMEDIA
from .subtitle_overlay import PREVIEW_MODES

logger = logging.getLogger(__name__)


class QtPlaybackRuntimeMixin:
    def _layout_canvas_layers(self) -> None:
        self._stage.update()

    def _silence_qt_pause_buffer(self) -> None:
        """同步静音先于 QMediaPlayer.pause，掩蔽音频 sink 已排队的尾部样本。"""
        if self._audio_out is None or self._qt_pause_muted:
            return
        try:
            self._qt_pause_previous_muted = bool(self._audio_out.isMuted())
            if not self._qt_pause_previous_muted:
                self._audio_out.setMuted(True)
            self._qt_pause_muted = True
        except Exception:  # noqa: BLE001
            self._qt_pause_muted = False

    def _restore_qt_pause_audio(self) -> None:
        if self._audio_out is None or not self._qt_pause_muted:
            return
        try:
            self._audio_out.setMuted(self._qt_pause_previous_muted)
        except Exception:  # noqa: BLE001
            pass
        self._qt_pause_muted = False

    def _prefer_software_video_decode(self) -> None:
        os.environ["QT_FFMPEG_DECODING_HW_DEVICE_TYPES"] = ""
        os.environ["QT_FFMPEG_HW_DECODING"] = "0"
        if self._player is None:
            return
        for attr, val in (
            ("hwAudioDecoding", False),
            ("hardwareDecoding", False),
            ("hwVideoDecoding", False),
        ):
            try:
                if hasattr(self._player, attr):
                    setattr(self._player, attr, val)
            except Exception:  # noqa: BLE001
                pass

    def _on_player_error(self, *args) -> None:
        if self._active_backend != "qt":
            return
        err = args[0] if args else None
        msg = ""
        try:
            if self._player is not None:
                msg = str(self._player.errorString() or "")
        except Exception:  # noqa: BLE001
            msg = repr(err)
        logger.warning("[播放器] error=%r msg=%s", err, msg)
        if self._soft_decode_retried or self._player is None or self._media_path is None:
            return
        self._soft_decode_retried = True
        self._prefer_software_video_decode()
        pos = self._player.position()
        was_playing = False
        try:
            from PySide6.QtMultimedia import QMediaPlayer as _Q
            was_playing = self._player.playbackState() == _Q.PlaybackState.PlayingState
        except Exception:  # noqa: BLE001
            pass
        self._stage.clear_frame()
        self._player.setSource(QUrl.fromLocalFile(str(self._media_path.resolve())))
        if pos > 0:
            self._player.setPosition(pos)
        if was_playing:
            self._restore_qt_pause_audio()
            self._player.play()
        logger.info("[播放器] 已软解重载媒体")

    def _on_position_ms(self, ms: int) -> None:
        if self._active_backend != "qt" or getattr(self, "_priming", False):
            return   # 非 active Qt / 静默预卷期间播放头与字幕时间不动
        t = ms / 1000.0
        self._stage.set_time(t)
        self.position_changed.emit(t)

    def _on_media_status(self, status) -> None:
        """媒体加载就绪后预卷一帧，消除「导入后一直显示解码中」的空窗。"""
        if self._active_backend != "qt":
            return   # mpv 后端接管，无需 QMediaPlayer 预卷
        if not _HAS_QT_MULTIMEDIA or self._player is None or not self._is_video:
            return
        try:
            from PySide6.QtMultimedia import QMediaPlayer as _Q
            ready = status in (
                _Q.MediaStatus.LoadedMedia,
                _Q.MediaStatus.BufferedMedia,
                getattr(_Q.MediaStatus, "BufferingMedia", _Q.MediaStatus.LoadedMedia),
            )
        except Exception:  # noqa: BLE001
            ready = True
        if not ready:
            return
        if getattr(self, "_preview_primed", False):
            return
        # 已在播则不必预卷
        try:
            from PySide6.QtMultimedia import QMediaPlayer as _Q
            if self._player.playbackState() == _Q.PlaybackState.PlayingState:
                self._preview_primed = True
                return
        except Exception:  # noqa: BLE001
            pass
        self._prime_preview_frame()

    def _cancel_prime_timer(self) -> None:
        timer = getattr(self, "_prime_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._prime_timer = None

    def _end_priming(self) -> None:
        """结束静默预卷：退出 priming 态、恢复音量、释放滞留首帧。幂等。"""
        was_priming = self._priming or self._stage._prime_hold
        self._priming = False
        self._stage._prime_hold = False
        self._stage.release_prime_frame()
        if was_priming and self._audio_out is not None:
            try:
                self._audio_out.setMuted(self._prime_was_muted)
            except Exception:  # noqa: BLE001
                pass

    def _prime_preview_frame(self) -> None:
        """静默逼出 QVideoSink 首帧：不出声、不进播放态、不动播放头。

        原理：QVideoSink 只在实际走帧时才产出画面，因此仍需短暂 play，
        但在此期间**静音 + 抑制播放头/播放态信号 + 不刷新画面**，仅在结束时
        把滞留的首帧作为静帧显示——用户只会看到首帧出现，不会“播放一下又停”。
        （用户已点播放则跳过。）
        """
        if self._active_backend != "qt":
            return   # mpv 后端自首帧，无需预卷
        if self._player is None or not self._is_video:
            return
        if getattr(self, "_preview_primed", False):
            return
        if getattr(self, "_user_wants_play", False):
            return
        self._preview_primed = True
        try:
            self._priming = True
            self._stage._prime_hold = True
            self._stage._prime_frame = None
            if self._audio_out is not None:
                self._prime_was_muted = self._audio_out.isMuted()
                self._audio_out.setMuted(True)
            self._player.setPosition(0)
            self._player.pause()  # 确保不在播
            self._player.play()
            from PySide6.QtCore import QTimer
            self._cancel_prime_timer()
            self._prime_timer = QTimer(self)
            self._prime_timer.setSingleShot(True)
            # 120ms：软解 HEVC 出首帧通常够用；仍不够则用户一点播放就会有画面
            self._prime_timer.timeout.connect(self._finish_prime_preview)
            self._prime_timer.start(120)
        except Exception as e:  # noqa: BLE001
            logger.debug("[播放器] 预卷首帧失败: %s", e)
            self._end_priming()

    def _finish_prime_preview(self) -> None:
        self._prime_timer = None
        if self._player is None:
            self._end_priming()
            return
        # 用户已点播放：不要 pause 打断（play() 已接管并结束预卷）
        if getattr(self, "_user_wants_play", False):
            self._end_priming()
            return
        try:
            from PySide6.QtMultimedia import QMediaPlayer as _Q
            if self._player.playbackState() == _Q.PlaybackState.PlayingState:
                self._player.pause()
            # 回到开头：预卷期间播放头不应离开 0，且保证下次播放从头开始
            self._player.setPosition(0)
        except Exception as e:  # noqa: BLE001
            logger.debug("[播放器] 结束预卷失败: %s", e)
        self._stage.set_time(0.0)
        self._end_priming()
        self._stage.update()

    def _show_video_surface(self, video: bool) -> None:
        self._is_video = bool(video)
        self._stage.set_is_video(video)

    def _on_mode_changed(self, _idx: int) -> None:
        mode = self._mode_combo.currentData() or "sentence"
        self._stage.set_mode(mode)
        if self._active_backend == "mpv" and self._mpv_ready:
            self._rebuild_mpv_subtitle()
        try:
            from core.app_config import load_preferences, save_preferences
            prefs = load_preferences()
            prefs.player_preview_mode = mode
            save_preferences(prefs)
        except Exception:  # noqa: BLE001
            logger.debug("[播放器] 保存预览模式失败")

    def _restore_mode_pref(self) -> None:
        try:
            from core.app_config import load_preferences
            mode = str(load_preferences().player_preview_mode or "sentence")
        except Exception:  # noqa: BLE001
            mode = "sentence"
        i = self._mode_combo.findData(mode)
        self._mode_combo.blockSignals(True)
        self._mode_combo.setCurrentIndex(i if i >= 0 else 0)
        self._mode_combo.blockSignals(False)
        self._stage.set_mode(mode if mode in PREVIEW_MODES else "sentence")

    def _on_state(self, state) -> None:
        if self._active_backend != "qt" or not _HAS_QT_MULTIMEDIA or self._player is None:
            return
        if getattr(self, "_priming", False):
            return   # 静默预卷不进真实播放态
        from PySide6.QtMultimedia import QMediaPlayer as _Q
        if state == _Q.PlaybackState.PlayingState:
            self.state_playing.emit()
        elif state == _Q.PlaybackState.PausedState:
            self.state_paused.emit()
        else:
            self.state_stopped.emit()

    @staticmethod
    def has_qt_multimedia() -> bool:
        return _HAS_QT_MULTIMEDIA


__all__ = ["QtPlaybackRuntimeMixin"]
