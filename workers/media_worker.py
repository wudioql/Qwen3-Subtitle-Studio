"""workers.media_worker — 媒体准备（探测 / FFmpeg 提取 / 人声分离）QThread 封装

把「打开媒体 / 重新关联媒体」时的耗时准备移出 UI 主线程：

1. soundfile 原生直读探测（wav/flac/ogg/aiff/mp3…）；
2. 容器类/非常规格式（mp4/mkv/m4a/aac…）经 FFmpeg 提取 16kHz mono WAV；
3. 可选：Kim_Vocal_2 人声分离（``extract_vocals_to_wav``）。

合作式取消：仅在安全点（提取前后、人声分离阶段边界与逐块循环）生效；
不强杀 FFmpeg 子进程 / ONNX 前向。结果经 ``prepared`` 信号回传，收尾统一走
``QThread.finished``（与 TranscribeWorker / AlignWorker 约定一致）。

同步核心 ``prepare_media_sync`` 独立成纯函数，供 Worker 与测试直接调用。
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal

from core import audio_io
from core.task_control import TaskCancelled, raise_if_cancelled
from core.vocal_separator import extract_vocals_to_wav

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[int, int, str], None]]
CancelCallback = Optional[Callable[[], bool]]


def prepare_media_sync(
    media_path: str | Path,
    *,
    do_extract_vocals: bool = False,
    progress_cb: ProgressCallback = None,
    cancel_cb: CancelCallback = None,
) -> tuple:
    """同步核心：probe → 提取 → 可选人声分离。返回 (audio_path, info, vocal_extracted, fallback_msg)。

    - audio_path：提取出的 16k WAV（None = 原生可直读，或提取失败）；
    - info：AudioInfo（None = 无法读取媒体）；
    - vocal_extracted：人声分离是否成功产出；
    - fallback_msg：非空 = 人声分离失败/回退（调用方应提示并将原音频兜底）。

    提取失败**非致命**（沿用「原音频兜底」语义，仅记日志）；取消抛 ``TaskCancelled``。
    """
    media_path = Path(media_path)
    audio_path: Optional[Path] = None
    info = None
    # 1) 原生直读探测（probe 内部已吞异常返回 None）
    info = audio_io.probe_native_audio(media_path)
    # 2) 不可直读 → FFmpeg 提取（失败非致命）
    if info is None:
        try:
            raise_if_cancelled(cancel_cb)
            audio_path, info = audio_io.prepare_audio(media_path, sample_rate=16000)
            raise_if_cancelled(cancel_cb)
        except TaskCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[media-prep] 提取音频失败，继续使用原媒体: %r", exc)
            audio_path = None
            info = None
    # 3) 可选人声分离（失败非致命，带回退消息）
    vocal_extracted = False
    fallback_msg = ""
    if do_extract_vocals:
        try:
            raise_if_cancelled(cancel_cb)
            vocal_path = extract_vocals_to_wav(
                media_path,
                progress_cb=progress_cb,
                allow_fallback=False,
                cancel_cb=cancel_cb,
            )
            audio_path = vocal_path
            vocal_extracted = True
        except TaskCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            fallback_msg = f"{type(exc).__name__}: {exc}"
            logger.warning("[media-prep] 人声分离失败，使用原音频: %s", fallback_msg)
    return audio_path, info, vocal_extracted, fallback_msg


class MediaPrepWorker(QThread):
    """媒体准备 QThread。"""

    progress = Signal(int, int, str)
    prepared = Signal(object, object, bool)   # (audio_path|None, AudioInfo|None, vocal_extracted)
    vocal_fallback = Signal(str)              # 人声分离失败、将使用原音频（非致命，UI 提示）
    failed = Signal(str)
    cancelled = Signal()
    finished_ok = Signal()
    log = Signal(int, str)

    def __init__(
        self,
        media_path: str | Path,
        *,
        do_extract_vocals: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_path = Path(media_path)
        self._do_extract_vocals = bool(do_extract_vocals)

    def _on_progress(self, done: int, total: int, desc: str) -> None:
        self.progress.emit(int(done), int(total), str(desc))

    def run(self) -> None:  # noqa: D401 — Qt 约定，返回 None
        try:
            logger.info(
                "[MediaPrepWorker] 开始准备媒体: %s (do_vocals=%s)",
                self._media_path, self._do_extract_vocals,
            )
            self._on_progress(0, 0, "正在探测/提取音频…")
            audio_path, info, vocal_extracted, fallback_msg = prepare_media_sync(
                self._media_path,
                do_extract_vocals=self._do_extract_vocals,
                progress_cb=self._on_progress,
                cancel_cb=self.isInterruptionRequested,
            )
            if fallback_msg:
                self.vocal_fallback.emit(fallback_msg)
            self.prepared.emit(audio_path, info, vocal_extracted)
            self.finished_ok.emit()
        except TaskCancelled:
            logger.info("[MediaPrepWorker] 用户取消")
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 — 必须兜底，否则 Qt 直接 abort
            tb = traceback.format_exc()
            logger.exception("[MediaPrepWorker] 失败")
            self.log.emit(logging.ERROR, tb)
            self.failed.emit(f"{type(e).__name__}: {e}")


__all__ = ["MediaPrepWorker", "prepare_media_sync"]
