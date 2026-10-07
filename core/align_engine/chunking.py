"""core.align_engine.chunking — 长媒体切块机制（计划 / 执行 / 收集）。

只承载「把一段音频窗切成块 → 逐块对齐 → 收集候选」的**机制**，不含策略选择
（策略在 ``full`` 里）。依赖 ``words``（字级切回 / 提交）与 ``common``（裁剪）。

**音频窗来源只有两种**：① 静音点 / 长度切块（``_plan_jobs``）；② 两阶段里阶段一
产出的**真实时间**（调用方传入的 ``win``）。句级时间**只**用于把句子分配进块
（文本分组），**绝不**用于定义裁剪窗——首次全文重对齐时句级时间多为占位值。
"""
from __future__ import annotations

import copy
import logging
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from subs.models import Sentence, SubtitleProject

from core import audio_io
from core.constants import (
    ALIGN_CHUNK_MAX_DURATION,
    ALIGN_CHUNK_MIN_DURATION,
    ALIGN_CHUNK_OVERLAP,
)
from core.model_manager import ModelManager
from core.task_control import raise_if_cancelled

from .common import _crop_audio
from .config import AlignConfig, _report
from .words import _assign_words, _build_word_languages, _qwen_words

logger = logging.getLogger("core.align_engine")


def _plan_jobs(
    group_lang: str,
    group_sents: List[Sentence],
    span: Tuple[float, float],
    silence_points: List[float],
) -> List[Tuple[str, float, float, List[Sentence]]]:
    """把一组句子的 ``span`` 区间按静音点切成对齐块。

    ``span`` 只能来自「整段媒体」或「阶段一真实时间」；句级时间**只**用于把
    句子分配进块（文本分组）。
    """
    span_start, span_end = span
    span_len = span_end - span_start
    if span_len <= 0:
        return []
    shifted = [sp - span_start for sp in silence_points if span_start < sp < span_end]
    plan = audio_io.build_split_plan(
        span_len, shifted,
        max_duration=ALIGN_CHUNK_MAX_DURATION,
        min_duration=ALIGN_CHUNK_MIN_DURATION,
        overlap_sec=ALIGN_CHUNK_OVERLAP,
    )
    jobs: List[Tuple[str, float, float, List[Sentence]]] = []
    for cs, ce in plan.chunk_ranges:
        c0, c1 = span_start + cs, span_start + ce
        chunk_sents = [
            s for s in group_sents
            if s.start_time < c1 and s.end_time > c0
        ]
        if chunk_sents:
            jobs.append((group_lang, c0, c1, chunk_sents))
    return jobs


def _media_span(media_dur: float, group_sents: Sequence[Sentence]) -> Tuple[float, float]:
    """整段媒体窗 ``[0, max(media_dur, 末句 end)]``（占位时间铺满整段，安全兜底）。"""
    ends = [float(s.end_time) for s in group_sents]
    return 0.0, max([float(media_dur), *ends])


def _run_jobs(
    jobs: List[Tuple[str, float, float, List[Sentence]]],
    *,
    project: SubtitleProject,
    cfg: AlignConfig,
    audio_np, sr,
    model_manager: ModelManager,
    mms,
    original_centers: Dict[int, float],
) -> Dict[int, Tuple[Sentence, float]]:
    """逐块对齐；每句保留「原句中心距块中心最近」的候选快照（避免块覆盖）。

    返回 ``{sid: (候选句, 距离)}``。
    """
    collected: Dict[int, Tuple[Sentence, float]] = {}
    for job_i, (job_lang, chunk_start, chunk_end, chunk_sents) in enumerate(jobs):
        raise_if_cancelled(cfg.cancel_cb)
        _report(
            cfg, job_i + 1, len(jobs),
            f"全文重对齐：块 {job_i+1}/{len(jobs)}（{job_lang} · {chunk_start:.0f}-{chunk_end:.0f}s）",
        )
        block_center = (chunk_start + chunk_end) / 2.0

        cropped, actual_start, _ = _crop_audio(
            audio_np, sr, chunk_start, chunk_end,
            pad_before=cfg.pad_before, pad_after=cfg.pad_after,
        )
        if cropped.size == 0:
            logger.warning("[Align] 块 %d 裁剪后为空，保留原状", job_i)
            continue

        full_text = " ".join(sentence.text.strip() for sentence in chunk_sents)
        if cfg.align_backend == "mms":
            raw_words = mms.align(
                (cropped, sr), full_text,
                language=job_lang,
                word_languages=_build_word_languages(chunk_sents, project, cfg),
                offset_sec=actual_start,
            )
        else:
            raw_words = _qwen_words(cropped, sr, full_text, job_lang, actual_start, model_manager)

        if not raw_words:
            logger.warning("[Align] 块 %d 对齐产出为空，保留原状", job_i)
            continue

        for original, candidate in _assign_words(chunk_sents, raw_words):
            if original.is_locked or not candidate.words:
                continue
            distance = abs(original_centers[original.sid] - block_center)
            existing = collected.get(original.sid)
            if existing is None or distance < existing[1]:
                collected[original.sid] = (copy.deepcopy(candidate), distance)
    return collected


def _commit_collected(
    project: SubtitleProject,
    collected: Dict[int, Tuple[Sentence, float]],
    *,
    language_of: Callable[[Sentence], str],
    accept: Optional[Callable[[Sentence], bool]] = None,
) -> set[int]:
    """把逐块收集到的候选提交到项目（按句回填 word.language）。"""
    committed: set[int] = set()
    for original in project.sentences:
        if original.is_locked:
            continue
        entry = collected.get(original.sid)
        if entry is None:
            if not (original.text or "").strip():
                original.is_dirty = False
            continue
        if accept is not None and not accept(original):
            continue
        candidate, _distance = entry
        lang = language_of(original)
        for w in candidate.words:
            w.language = lang
        original.words = copy.deepcopy(candidate.words)
        original.start_time = candidate.start_time
        original.end_time = candidate.end_time
        original.timed = candidate.timed
        original.is_dirty = False
        committed.add(original.sid)
    return committed
