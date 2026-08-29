"""workers.transcribe_worker — ASR + 对齐 QThread 封装

使用示例（UI 层）：
    mm = ModelManager()
    worker = TranscribeWorker(
        media_path=r"D:/Music/song.mp3",
        model_manager=mm,
        cfg=TranscribeConfig(source_language="auto"),
        parent=self,   # 保证 Qt 父对象生命周期正确
    )
    worker.progress.connect(lambda d, t, desc: self.statusBar().showMessage(f"{d}/{t} {desc}"))
    worker.project.connect(self._on_project_ready)   # 拿 SubtitleProject 填表格
    worker.failed.connect(lambda msg: QMessageBox.critical(self, "识别失败", msg))
    worker.cancelled.connect(lambda: self.statusBar().showMessage("已取消"))
    worker.finished.connect(lambda: self.ui.spinner.hide())  # 公共收尾必须接 QThread.finished
    worker.start()
    #   ⚠️ 不要手动 deleteLater；父对象销毁时一起回收，或等 start() 自然结束后 QThread 自动 cleanup
"""

from __future__ import annotations

import copy
import logging
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from subs.models import SubtitleProject
from core.asr_engine import TranscribeConfig, transcribe
from core.model_manager import ModelManager
from core.task_control import TaskCancelled

logger = logging.getLogger(__name__)


class TranscribeWorker(QThread):
    """封装 asr_engine.transcribe() 的 QThread。

    所有长时工作（FFmpeg 提取 + ASR generate + ForcedAligner 对齐）都在这里跑；
    核心 UI 事件循环不会被阻塞。

    线程安全：本对象对外只暴露 Signal；progress_cb 是在本线程里 emit，但 Qt Signal
    默认跨线程排队，UI 线程的 slot 顺序一致，没问题。
    """

    progress = Signal(int, int, str)       # done, total, desc
    project = Signal(object)                # SubtitleProject（用 object 省掉跨库注册）
    failed = Signal(str)
    cancelled = Signal()
    finished_ok = Signal()
    log = Signal(int, str)                  # level (logging.*), message

    def __init__(
        self,
        media_path: str | Path,
        *,
        model_manager: ModelManager,
        cfg: Optional[TranscribeConfig] = None,
        source_media_path: str | Path | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._media_path = Path(media_path)
        self._source_media_path = Path(source_media_path) if source_media_path is not None else self._media_path
        self._mm = model_manager
        # M3: copy 一份 cfg 再改，避免覆盖调用方传入的同一个 cfg 对象（复用场景会丢原回调）
        self._cfg = copy.copy(cfg) if cfg is not None else TranscribeConfig()
        # 动态回调统一由 Worker 注入，不污染调用方配置对象。
        self._cfg.progress_cb = self._on_progress
        self._cfg.cancel_cb = self.isInterruptionRequested

    # ── 内部 ──────────────────────────────────────────────

    def _on_progress(self, done: int, total: int, desc: str) -> None:
        # 直接 forward。Signal 跨线程安全。
        self.progress.emit(int(done), int(total), str(desc))

    # ── 线程主函数 ───────────────────────────────────────

    def run(self) -> None:  # noqa: D401 — Qt 约定，返回 None
        try:
            logger.info("[TranscribeWorker] 开始: %s", self._media_path)
            proj: SubtitleProject = transcribe(
                self._media_path,
                model_manager=self._mm,
                cfg=self._cfg,
                source_media_path=self._source_media_path,
            )
            logger.info(
                "[TranscribeWorker] 成功：%d 句，字级=%s",
                len(proj.sentences),
                any(s.has_word_level() for s in proj.sentences),
            )
            self.project.emit(proj)
            self.finished_ok.emit()
        except TaskCancelled:
            logger.info("[TranscribeWorker] 用户取消")
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 — 必须兜底，否则 Qt 直接 abort
            tb = traceback.format_exc()
            logger.exception("[TranscribeWorker] 失败")
            self.log.emit(logging.ERROR, tb)
            self.failed.emit(f"{type(e).__name__}: {e}")
