"""ui.workflow_controller — ASR 转写与 Aligner 对齐工作流控制器

职责：
- 调度 TranscribeWorker 与 AlignWorker
- UI 与 Worker 统一只支持三种对齐：``dirty``（修改句 Ctrl+R）、
  ``sentences``（选中句）、``full``（全文 Ctrl+Shift+R）。
- Worker 生命周期、忙碌互斥、状态栏/进度、推理期间动作禁用
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from core.asr_engine import TranscribeConfig
from workers import AlignWorker, TranscribeWorker

if TYPE_CHECKING:
    from core.align_engine import AlignConfig
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)

class WorkflowController:
    """工作流控制器：管理 ASR/Aligner 异步推理流程。"""

    def __init__(self, win: MainWindow) -> None:
        self._win = win
        self._running_worker = None
        self._worker_failed = False
        self._worker_cancelled = False
        self._close_when_idle = False
        self._task_started_at = 0.0
        self._progress_desc = ""
        self._progress_counts = ""
        self._done_label = "完成"
        self._elapsed_timer = QTimer(win)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._refresh_elapsed_text)

    @property
    def running_worker(self):
        return self._running_worker

    def is_busy(self) -> bool:
        return self._running_worker is not None and self._running_worker.isRunning()

    def ensure_no_running_worker(self) -> bool:
        if self.is_busy():
            QMessageBox.information(self._win, "忙碌中", "当前有任务在运行；可等待完成或使用“工具 → 取消当前任务”。")
            return False
        return True

    def ensure_concrete_language(self) -> bool:
        proj = self._win._project
        if proj is None:
            return False
        missing = [i for i, s in enumerate(proj.sentences) if not (s.language or "").strip()]
        if missing:
            QMessageBox.information(
                self._win, "需要选择语言",
                f"有 {len(missing)} 句尚未设置语言（如第 {missing[0]+1} 句）。\n"
                "重对齐按**各句语言**逐句执行（未设置的项目语言兜底）：\n"
                "请在句级视图的语言列逐句选择——本项目 11 种语言（中/粤/英/日/韩/\n"
                "法/德/意/葡/俄/西）可任意逐句混排，重对齐会按语言分段各自处理。\n"
                "单语项目可用「应用到全部」一键统一设置后再重对齐。"
            )
            return False
        return True

    def set_actions_project_state(self, *, has_media: bool, has_sentences: bool) -> None:
        w = self._win
        w._act_transcribe.setEnabled(has_media)
        w._act_import_subtitle.setEnabled(True)
        w._act_align_dirty.setEnabled(has_sentences)
        w._act_align_sel.setEnabled(has_sentences)
        w._act_align_full.setEnabled(has_sentences)
        w._export_panel.refresh_states(has_sentences)
        w._act_open.setEnabled(True)
        # 有句即可保存工程（纯字幕工程也允许）；打开工程始终可用
        if hasattr(w, "_act_save_project"):
            w._act_save_project.setEnabled(has_sentences or has_media)
        if hasattr(w, "_act_open_project"):
            w._act_open_project.setEnabled(True)
        if hasattr(w, "_act_relink_media"):
            w._act_relink_media.setEnabled(has_sentences)
        if hasattr(w, "_act_cancel_task"):
            w._act_cancel_task.setEnabled(False)

    def bind_and_start_worker(self, worker, *, mode_label: str, done_label: str = "完成") -> None:
        self._running_worker = worker
        self._worker_failed = False
        self._worker_cancelled = False
        self._task_started_at = time.monotonic()
        self._progress_desc = f"{mode_label}…"
        self._progress_counts = ""
        self._done_label = done_label
        self._elapsed_timer.start()
        w = self._win
        self._render_progress_text()
        w._sb_progress.setRange(0, 0)
        w._sb_progress.show()
        w._sb_progress.setValue(0)

        worker.progress.connect(self._on_worker_progress)
        worker.failed.connect(self._on_worker_failed)
        worker.cancelled.connect(self._on_worker_cancelled)
        worker.finished.connect(self._on_worker_done)
        worker.start()

        w._act_transcribe.setEnabled(False)
        w._act_align_dirty.setEnabled(False)
        w._act_align_sel.setEnabled(False)
        w._act_align_full.setEnabled(False)
        w._act_open.setEnabled(False)
        w._act_import_subtitle.setEnabled(False)
        if hasattr(w, "_act_relink_media"):
            w._act_relink_media.setEnabled(False)
        if hasattr(w, "_act_cancel_task"):
            w._act_cancel_task.setEnabled(True)
        if hasattr(w, "_act_save_project"):
            w._act_save_project.setEnabled(False)
        if hasattr(w, "_act_open_project"):
            w._act_open_project.setEnabled(False)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _render_progress_text(self) -> None:
        elapsed = self._format_elapsed(time.monotonic() - self._task_started_at)
        counts = f" · {self._progress_counts}" if self._progress_counts else ""
        self._win._sb_mode.setText(
            f"模式：{self._progress_desc}{counts} · 已用时 {elapsed}"
        )

    def _refresh_elapsed_text(self) -> None:
        if self.is_busy():
            self._render_progress_text()

    def _on_worker_progress(self, done: int, total: int, desc: str) -> None:
        w = self._win
        self._progress_desc = str(desc)
        if total <= 0:
            # 模型权重 I/O、GPU 搬运、单次 tensor forward 没有可信百分比。
            w._sb_progress.setRange(0, 0)
            self._progress_counts = ""
        else:
            w._sb_progress.setRange(0, 100)
            pct = int(100 * done / total)
            w._sb_progress.setValue(max(0, min(100, pct)))
            self._progress_counts = f"{done}/{total}"
        self._render_progress_text()
        w._sb_vram.setText(w._model_manager.status_text())

    def _on_worker_failed(self, msg: str) -> None:
        self._worker_failed = True
        QMessageBox.critical(self._win, "执行失败", msg)

    def _on_worker_cancelled(self) -> None:
        self._worker_cancelled = True
        self._win._sb_mode.setText("模式：任务已取消，正在收尾…")

    def _on_worker_done(self) -> None:
        """QThread 无论成功、失败或取消都必须走到的唯一 UI 收尾。"""
        w = self._win
        self._elapsed_timer.stop()
        w._sb_progress.hide()
        w._sb_progress.setRange(0, 100)
        w._sb_progress.setValue(0)
        elapsed = self._format_elapsed(time.monotonic() - self._task_started_at)
        if self._worker_failed:
            w._sb_mode.setText(f"模式：执行失败（界面已恢复） · 用时 {elapsed}")
        elif self._worker_cancelled:
            w._sb_mode.setText(f"模式：任务已取消 · 用时 {elapsed}")
        else:
            w._sb_mode.setText(f"模式：{self._done_label} · 用时 {elapsed}")
        w._sb_vram.setText(w._model_manager.status_text())
        self._running_worker = None
        media_paths = (
            (w._project.source_media_path, w._project.audio_path)
            if w._project is not None else ()
        )
        has_media = any(path and Path(path).is_file() for path in media_paths)
        self.set_actions_project_state(
            has_media=has_media,
            has_sentences=bool(w._project and w._project.sentences),
        )
        if self._close_when_idle:
            self._close_when_idle = False
            QTimer.singleShot(0, w.close)

    def start_transcribe(self) -> None:
        w = self._win
        if w._project is None or not w._project.source_media_path:
            QMessageBox.information(w, "提示", "请先打开一个媒体文件。")
            return
        inference_path = w._project.audio_path or w._project.source_media_path
        if not Path(inference_path).is_file():
            QMessageBox.information(w, "媒体文件缺失", "请先使用“文件 → 重新关联媒体”修复媒体路径。")
            return
        if not self.ensure_no_running_worker():
            return

        # 使用已提取的音频（若导入时提取了人声，audio_path 已指向人声音频；否则为原音频）
        media_path = inference_path

        cfg = TranscribeConfig()
        try:
            from core.app_config import apply_asr_prefs, load_preferences
            prefs = load_preferences()
            apply_asr_prefs(cfg, prefs.asr)
            # 设置页「分句」偏好注入转写配置
            cfg.seg_prefs = prefs.segmentation
        except Exception:
            logger.debug("[ASR] 应用偏好设置失败，使用默认配置")
        backend = w._align_backend.currentData() if getattr(w, "_align_backend", None) else "qwen"
        cfg.align_backend = backend or "qwen"
        # 工具栏「识别语言」是用户的即时权威选择（已同步持久化到
        # prefs.asr.source_language），这里显式覆盖一次，兜底偏好保存失败的情形
        toolbar_lang = w._global_lang.currentData() if getattr(w, "_global_lang", None) else None
        cfg.source_language = toolbar_lang or "auto"

        worker = TranscribeWorker(
            media_path,
            model_manager=w._model_manager,
            cfg=cfg,
            source_media_path=w._project.source_media_path,
            parent=w,
        )
        worker.project.connect(self._on_project_result)
        self.bind_and_start_worker(worker, mode_label="识别 + 对齐")

    def _on_project_result(self, project) -> None:
        self._win._apply_project(project)
        self._win._mark_project_modified()

    def _get_current_align_config(self) -> AlignConfig:
        w = self._win
        from core.align_engine import AlignConfig
        cfg = AlignConfig()
        # 偏好注入：subchunk_min_chars / pad_before / pad_after 等来自 preferences.json
        try:
            from core.app_config import apply_align_prefs, load_preferences
            apply_align_prefs(cfg, load_preferences().align)
        except Exception:
            logger.debug("[Align] 应用偏好设置失败，使用默认配置")
        # 工具栏「识别语言」只作用于 ASR 识别，不直传对齐配置；
        # 对齐语言按「句级语言 → 项目语言」决议（align_engine/worker 内统一）。
        cfg.source_language = "auto"
        # 对齐后端以工具栏即时选择为权威（偏好里的值仅是它的持久化镜像）。
        backend = w._align_backend.currentData() if getattr(w, "_align_backend", None) else "qwen"
        cfg.align_backend = backend or "qwen"
        return cfg

    def start_align_dirty(self) -> None:
        """重对齐 is_dirty 且未锁定的句子（mode=dirty）。"""
        w = self._win
        if w._project is None or not w._project.sentences:
            return
        if not self.ensure_no_running_worker():
            return
        if not w._project.alignable_dirty_indices() and all(s.has_word_level() for s in w._project.sentences):
            w._sb_mode.setText("模式：当前没有需要重对齐的脏句")
            return
        if not self.ensure_concrete_language():
            return

        cfg = self._get_current_align_config()
        backend_label = "MMS-FA 歌词" if cfg.align_backend == "mms" else "Qwen3"
        worker = AlignWorker(
            w._project, mode="dirty",
            model_manager=w._model_manager, cfg=cfg, parent=w,
        )
        worker.sentence_aligned.connect(w._on_dirty_sentence_aligned)
        self.bind_and_start_worker(worker, mode_label=f"修改内容重对齐 ({backend_label})")

    def start_align_full(self) -> None:
        w = self._win
        if w._project is None or not w._project.sentences:
            w._sb_mode.setText("模式：没有可重对齐的句子")
            return
        if not self.ensure_no_running_worker():
            return
        if not self.ensure_concrete_language():
            return

        cfg = self._get_current_align_config()
        backend_label = "MMS-FA 歌词" if cfg.align_backend == "mms" else "Qwen3"
        worker = AlignWorker(
            w._project, mode="full",
            model_manager=w._model_manager, cfg=cfg, parent=w,
        )
        worker.project.connect(self._on_project_result)
        self.bind_and_start_worker(worker, mode_label=f"全文重对齐 ({backend_label})")

    def start_align_selected(self) -> None:
        w = self._win
        if w._project is None or not w._project.sentences:
            return
        rows = w.editor.selected_rows() if hasattr(w.editor, "selected_rows") else []
        if not rows:
            w._sb_mode.setText("模式：请先在表格中选中要重对齐的句")
            return
        self.run_align_for_indices(rows, label=f"重对齐选中 {len(rows)} 句")

    def realign_single_sentence(self, idx: int) -> None:
        w = self._win
        if w._project is None or not (0 <= idx < len(w._project.sentences)):
            return
        self.run_align_for_indices([idx], label=f"重对齐第 {idx+1} 句")

    def run_align_for_indices(self, rows: list, *, label: str) -> None:
        w = self._win
        if not self.ensure_no_running_worker():
            return
        if not self.ensure_concrete_language():
            return
        cfg = self._get_current_align_config()
        backend_label = "MMS-FA" if cfg.align_backend == "mms" else "Qwen3"
        worker = AlignWorker(
            w._project, mode="sentences", indices=rows,
            model_manager=w._model_manager, cfg=cfg, parent=w,
        )
        worker.sentence_aligned.connect(w._on_dirty_sentence_aligned)
        self.bind_and_start_worker(worker, mode_label=f"{label} ({backend_label})")

    def request_cancel(self, *, close_when_idle: bool = False) -> bool:
        """请求在下一个安全点取消；不强杀模型前向或 QThread。"""
        if not self.is_busy():
            return False
        self._close_when_idle = self._close_when_idle or close_when_idle
        self._progress_desc = "正在取消（将在当前安全点停止）…"
        self._progress_counts = ""
        self._render_progress_text()
        if hasattr(self._win, "_act_cancel_task"):
            self._win._act_cancel_task.setEnabled(False)
        self._running_worker.requestInterruption()
        return True

    def shutdown(self) -> bool:
        """仅在无任务时允许窗口继续销毁；忙碌时请求取消并延后关闭。"""
        if self.is_busy():
            self.request_cancel(close_when_idle=True)
            return False
        return True


__all__ = ["WorkflowController"]
