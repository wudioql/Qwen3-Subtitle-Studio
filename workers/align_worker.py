"""workers.align_worker — ForcedAligner 对齐 QThread 封装

支持三种模式（导入字幕后须先设好各句语言，再手动触发对齐；无自动对齐）：

1) `mode="sentences"`：只重对齐某些句子索引（选中句 / 字级「重对齐此句」）。
   - 入参：project + indices (list[int])
   - 每完成一句发 sentence_aligned(idx)，UI 只刷新对应行；最后发 finished_ok()
   - 锁定句跳过

2) `mode="dirty"`：自动取 `project.alignable_dirty_indices()`（is_dirty 且未锁定）。
   - 适用：工具栏「修改句重对齐」(Ctrl+R)
   - 调用 align_dirty_only；成功句清 is_dirty，锁定句与失败句保持原状
   - 入参：project（显式 indices 会被忽略并打 warning）
   - 发 sentence_aligned(idx) for each re-aligned sentence

3) `mode="full"`：**全文重对齐**——把 project 所有 sentence.text 拼成一段文本整段推断，
   再按现有句子切回各句（core.align_engine.align_full_text）。
   - 适用：工具栏「全文重对齐」(Ctrl+Shift+R)
   - 短媒体（≤ ALIGNER_MAX_DURATION=300s）一次跑完；超长自动静音切块合并
     （单块 ≤240s、重叠 1.5s）——**不是**硬性 300s 上限报错
   - 锁定句严格保护
   - 入参：project
   - 发 project signal；完成后清未锁定句的 is_dirty

使用示例（用户改了第 3/7 句后只重跑两句）：
    worker = AlignWorker(project, mode="sentences", indices=[3,7], model_manager=mm, parent=self)
    worker.sentence_aligned.connect(lambda idx: self._refresh_row(idx))
    worker.start()

使用示例（用户改完所有手动操作后点「全文重对齐」）：
    worker = AlignWorker(project, mode="full", model_manager=mm, parent=self)
    worker.project.connect(lambda p: self._fill_editor(p))
    worker.start()
"""

from __future__ import annotations

import copy
import logging
import traceback
from typing import Iterable, List, Optional

from PySide6.QtCore import QThread, Signal

from subs.models import SubtitleProject
from core import audio_io
from core.constants import DEFAULT_SAMPLE_RATE
from core.align_engine import (
    AlignConfig,
    _infer_full_language,
    align_dirty_only,
    align_full_text,
    align_sentence,
    preflight_segmenter_deps,
)
from core.align_engine.common import commit_aligned_words
from core.mms_aligner import get_mms_aligner
from core.model_manager import ModelManager
from core.task_control import TaskCancelled, raise_if_cancelled

logger = logging.getLogger(__name__)


class AlignWorker(QThread):
    """封装对齐器的 QThread；支持指定索引 / 脏句 / 全文重对齐 三种模式。"""

    progress = Signal(int, int, str)         # done, total, desc
    project = Signal(object)                  # SubtitleProject
    sentence_aligned = Signal(int)            # idx（sentences / dirty 模式）
    failed = Signal(str)
    cancelled = Signal()
    finished_ok = Signal()
    log = Signal(int, str)

    def __init__(
        self,
        project: SubtitleProject,
        *,
        mode: str = "full",                   # "sentences" | "dirty" | "full"
        indices: Optional[Iterable[int]] = None,
        model_manager: ModelManager,
        cfg: Optional[AlignConfig] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        assert mode in ("sentences", "dirty", "full"), f"unknown mode: {mode!r}"
        if mode == "sentences" and indices is None:
            raise ValueError("mode=sentences 需要传 indices（要重对齐的句子下标列表）")
        if mode == "dirty" and indices is not None:
            logger.warning(
                "[AlignWorker] mode=dirty 忽略显式 indices"
                "（使用 project.alignable_dirty_indices()）"
            )

        self._project = project
        self._mode = mode
        self._indices: Optional[List[int]] = list(indices) if indices is not None else None
        self._mm = model_manager
        self._cfg = copy.copy(cfg) if cfg is not None else AlignConfig()
        self._cfg.progress_cb = self._on_progress
        self._cfg.cancel_cb = self.isInterruptionRequested

    # ── 内部 ──────────────────────────────────────────────

    def _on_progress(self, done: int, total: int, desc: str) -> None:
        self.progress.emit(int(done), int(total), str(desc))

    # ── 线程主函数 ───────────────────────────────────────

    def run(self) -> None:
        try:
            if self._mode == "sentences":
                self._run_sentences_mode()
            elif self._mode == "full":
                self._run_full_mode()
            else:  # "dirty"
                self._run_dirty_mode()
            self.finished_ok.emit()
        except TaskCancelled:
            logger.info("[AlignWorker] 用户取消")
            self.cancelled.emit()
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("[AlignWorker] 失败")
            self.log.emit(logging.ERROR, tb)
            self.failed.emit(f"{type(e).__name__}: {e}")

    def _run_sentences_mode(self) -> None:
        assert self._indices is not None
        if not self._indices:
            logger.warning("[AlignWorker] sentences 模式空 indices，直接返回")
            return

        # 预检：本模式逐句 try/except，若不 fail-fast，缺 nagisa/soynlp
        # 会把逐句 ImportError 吞成「完成但无字级」；开工前统一响亮报错
        if self._cfg.align_backend != "mms":
            _langs = {
                _infer_full_language(
                    self._cfg.source_language, sent.language, self._project.source_language,
                )
                for idx in self._indices
                if 0 <= idx < len(self._project.sentences)
                for sent in (self._project.sentences[idx],)
                if not sent.is_locked and (sent.text or "").strip()
            }
            preflight_segmenter_deps([la for la in _langs if la], backend=self._cfg.align_backend)

        if self._cfg.align_backend == "mms":
            mms = get_mms_aligner()
            if not mms.is_available():
                raise FileNotFoundError(
                    f"找不到 MMS-300M-FA ONNX 对齐模型：{mms.model_dir}\n"
                    "请检查模型目录或在设置中切换为 Qwen3-Aligner 对齐后端。"
                )
            align_ctx = self._mm.using_mms_aligner(progress_cb=self._cfg.progress_cb)
        else:
            align_ctx = self._mm.using_aligner(progress_cb=self._cfg.progress_cb)

        # 后端预检通过后才读整段音频，缺模型时真正 fail-fast。
        audio_np, sr = audio_io.load_audio(
            self._project.audio_path, mono=True, target_sr=DEFAULT_SAMPLE_RATE)
        total = len(self._indices)
        attempted = 0
        committed = 0
        with align_ctx:
            for pos, idx in enumerate(self._indices):
                raise_if_cancelled(self._cfg.cancel_cb)
                if idx < 0 or idx >= len(self._project.sentences):
                    logger.warning("[AlignWorker] 跳过越界 idx=%d", idx)
                    continue
                sent = self._project.sentences[idx]
                if sent.is_locked:
                    logger.info("[AlignWorker] 句 %d 已锁定保护，跳过重对齐", idx)
                    continue
                if not (sent.text or "").strip():
                    continue
                attempted += 1
                self._on_progress(pos + 1, total + 1, f"对齐句 {idx+1} (第 {pos+1}/{total})")
                try:
                    # 语言决议：句级语言优先，项目语言回落。
                    # 工具栏「识别语言」只服务 ASR，不再直传对齐配置。
                    lang_full = _infer_full_language(
                        self._cfg.source_language, sent.language, self._project.source_language,
                    )
                    prv_sent = (self._project.sentences[idx - 1]
                                if idx - 1 >= 0 else None)
                    nxt_sent = (self._project.sentences[idx + 1]
                                if idx + 1 < len(self._project.sentences) else None)
                    words = align_sentence(
                        sent, audio_np, sr,
                        model_manager=self._mm,
                        cfg=self._cfg,
                        inferred_language_full=lang_full,
                        prev_sentence_end=(prv_sent.end_time if prv_sent else None),
                        next_sentence_start=(nxt_sent.start_time if nxt_sent else None),
                        prev_sentence=prv_sent,
                        next_sentence=nxt_sent,
                    )
                except TaskCancelled:
                    raise
                except Exception as e:
                    logger.exception("[AlignWorker] 句 %d 异常", idx)
                    self.log.emit(logging.WARNING, f"align sentence[{idx}] failed: {e!r}")
                    continue

                if commit_aligned_words(
                        sent, words,
                        next_start=(nxt_sent.start_time if nxt_sent else None),
                    ):
                    committed += 1
                    self.sentence_aligned.emit(idx)
                else:
                    logger.warning(
                        "[AlignWorker] 句 %d 对齐产出为空，保留旧字级与脏标记", idx
                    )

        # 全部实际尝试句都失败 → 明确报错（UI 弹「执行失败」），不再静默「完成」。
        if attempted > 0 and committed == 0:
            raise RuntimeError(
                "重对齐失败：所选句均未能产出字级时间戳（已保留旧结果）。"
            )

    def _run_dirty_mode(self) -> None:
        """只重对齐未锁定脏句；在工程副本成功完成后再提交。"""
        original_sids = [
            self._project.sentences[i].sid
            for i in self._project.alignable_dirty_indices()
        ]
        if not original_sids:
            logger.info("[AlignWorker] dirty 模式无待对齐脏句（可能已全部确认或锁定），直接返回")
            self._on_progress(0, 1, "无待对齐脏句")
            self._on_progress(1, 1, "完成")
            return

        self._on_progress(0, len(original_sids) + 1, f"加载对齐器（{len(original_sids)} 待对齐脏句）")
        working_project = copy.deepcopy(self._project)
        align_dirty_only(
            working_project,
            model_manager=self._mm,
            cfg=self._cfg,
        )

        candidates = {sentence.sid: sentence for sentence in working_project.sentences}
        committed_sids: set[int] = set()
        for index, original in enumerate(self._project.sentences):
            if original.sid not in original_sids:
                continue
            candidate = candidates.get(original.sid)
            if candidate is None or candidate.is_dirty:
                continue
            self._project.sentences[index] = copy.deepcopy(candidate)
            committed_sids.add(original.sid)

        self._project.sort()
        if not committed_sids:
            # 全部脏句都未成功提交 → 明确报错（UI 弹「执行失败」），不再静默「完成」。
            raise RuntimeError(
                "重对齐失败：所有待对齐句均未能产出字级时间戳（已保留旧结果）。"
            )
        index_by_sid = {sentence.sid: i for i, sentence in enumerate(self._project.sentences)}
        for sid in committed_sids:
            self.sentence_aligned.emit(index_by_sid[sid])
        self._on_progress(len(original_sids) + 1, len(original_sids) + 1, "完成")

    def _run_full_mode(self) -> None:
        """全文重对齐。"""
        logger.info(
            "[AlignWorker] full 模式：%d 句 / media_duration=%.1fs",
            len(self._project.sentences),
            float(self._project.media_duration or 0.0),
        )
        # 全文任务在工程副本上运行；取消/异常时 UI 仍持有完整旧项目，成功才整体替换。
        working_project = copy.deepcopy(self._project)
        proj = align_full_text(
            working_project,
            model_manager=self._mm,
            cfg=self._cfg,
        )
        self.project.emit(proj)
