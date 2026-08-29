"""core.align_engine.full — 全文重对齐（单段 / 静音切块）。"""
from __future__ import annotations

import copy
import logging
from typing import List, Optional, Tuple

from subs.models import Sentence, SubtitleProject, WordTimestamp

from core import audio_io
from core.constants import (
    ALIGNER_MAX_DURATION,
    ALIGN_CHUNK_MAX_DURATION,
    ALIGN_CHUNK_MIN_DURATION,
    ALIGN_CHUNK_OVERLAP,
    DEFAULT_SAMPLE_RATE,
)
from core.model_manager import ModelManager
from core.task_control import raise_if_cancelled
from core.text_utils import attach_words_to_sentences as _attach_words_to_sentences

from .common import (
    _crop_audio,
    _infer_full_language,
    apply_seam_snaps,
    commit_aligned_words,
    preflight_segmenter_deps,
)
from .config import AlignConfig, _report
import core.align_engine as _ae  # 符号经包查找，兼容 patch("core.align_engine.*")

logger = logging.getLogger("core.align_engine")

def get_mms_aligner(*args, **kwargs):
    """运行时经包入口查找，保证 patch(\"core.align_engine.get_mms_aligner\") 生效。"""
    import core.align_engine as _ae
    return _ae.get_mms_aligner(*args, **kwargs)


def align_full_text(
    project: SubtitleProject,
    *,
    model_manager: ModelManager,
    cfg: Optional[AlignConfig] = None,
) -> SubtitleProject:
    """全文重对齐：把 project 里所有 sentence.text 拼成一段文本整段推断。"""
    cfg = cfg or AlignConfig()
    raise_if_cancelled(cfg.cancel_cb)
    if not project.sentences:
        return project

    media_dur = float(project.media_duration or 0.0)

    # 短媒体：一次跑完
    if media_dur <= ALIGNER_MAX_DURATION:
        return _align_full_text_single(project, model_manager=model_manager, cfg=cfg)

    # 长媒体：自动切块 → 逐块对齐 → 合并去重
    logger.info(
        "[Align] full-text 长媒体切块：media_duration=%.1fs > %ds，启用自动切块",
        media_dur, int(ALIGNER_MAX_DURATION),
    )
    return _align_full_text_chunked(project, model_manager=model_manager, cfg=cfg)


def _resolve_language_segments(
    project: SubtitleProject,
    cfg: AlignConfig,
) -> List[Tuple[str, List[Sentence]]]:
    """把有文本的句子按「决议语言」切成连续同语言段。

    决议顺序：cfg.source_language(≠auto) > 句级语言 > 项目语言。
    单语项目 → 恰好 1 段（行为与分段前等价）；混语项目 → 每段一次对齐调用，
    各自携带自己的语言提示——Qwen 后端 API 一次调用仅支持单语言；MMS 后端
    则用于 K1 日语读音路由与 word.language 标注。
    空文本句不参与对齐（无段可归）；任一句语言无法决议 → ValueError。
    """
    segments: List[Tuple[str, List[Sentence]]] = []
    for i, s in enumerate(project.sentences):
        if not (s.text or "").strip():
            continue
        lang = _infer_full_language(cfg.source_language, s.language, project.source_language)
        if not lang:
            raise ValueError(
                f"无法推断第 {i+1} 句的语言（sentence.language={s.language!r}, "
                f"project.source_language={project.source_language!r}, "
                f"cfg.source_language={cfg.source_language!r}）。请显式设置句级或项目语言。"
            )
        if segments and segments[-1][0] == lang:
            segments[-1][1].append(s)
        else:
            segments.append((lang, [s]))
    return segments


def _align_full_text_single(
    project: SubtitleProject,
    *,
    model_manager: ModelManager,
    cfg: AlignConfig,
) -> SubtitleProject:
    """全文重对齐（单块级，media ≤ 300s；按语言分段）。

    每个语言段先在独立句子副本上切回字级，验证非空后再提交到项目；空产出
    或异常不会清掉旧 words，也不会错误清除 dirty。
    """
    cfg = cfg or AlignConfig()
    raise_if_cancelled(cfg.cancel_cb)
    if not project.sentences:
        return project

    media_dur = float(project.media_duration or 0.0)
    if media_dur > ALIGNER_MAX_DURATION:
        raise ValueError(
            f"全文重对齐要求媒体时长 ≤ {int(ALIGNER_MAX_DURATION)}s；当前 {media_dur:.1f}s。"
            "暂不支持 >5 分钟单段项目，请先用手动拆分 / 合并。",
        )

    segments = _resolve_language_segments(project, cfg)
    if not segments:
        return project

    preflight_segmenter_deps((lang for lang, _ in segments), backend=cfg.align_backend)

    mms = None
    if cfg.align_backend == "mms":
        mms = get_mms_aligner()
        if not mms.is_available():
            raise FileNotFoundError(
                f"找不到 MMS-300M-FA ONNX 对齐模型：{mms.model_dir}\n"
                "请检查模型目录或在设置中切换为 Qwen3-Aligner 对齐后端。"
            )
        align_ctx = model_manager.using_mms_aligner(progress_cb=cfg.progress_cb)
    else:
        align_ctx = model_manager.using_aligner(progress_cb=cfg.progress_cb)

    audio_np, sr = audio_io.load_audio(
        project.audio_path, mono=True, target_sr=DEFAULT_SAMPLE_RATE)
    n_seg = len(segments)
    _report(cfg, 0, n_seg + 1, "全文重对齐：拼接文本 + 加载对齐器...")

    committed_sids: set[int] = set()
    with align_ctx as mms_or_ref:
        for seg_i, (seg_lang, seg_sents) in enumerate(segments):
            raise_if_cancelled(cfg.cancel_cb)
            _report(
                cfg, seg_i + 1, n_seg + 1,
                f"全文重对齐：语言段 {seg_i+1}/{n_seg}（{seg_lang} · {len(seg_sents)} 句）",
            )
            full_text = " ".join(s.text.strip() for s in seg_sents)

            if n_seg == 1:
                seg_audio, seg_offset = audio_np, 0.0
            else:
                seg_start = min(s.start_time for s in seg_sents)
                seg_end = max(s.end_time for s in seg_sents)
                seg_audio, actual_start, _ = _crop_audio(
                    audio_np, sr, seg_start, seg_end,
                    pad_before=cfg.pad_before, pad_after=cfg.pad_after,
                )
                if seg_audio.size == 0:
                    logger.warning("[Align] 语言段 %d 裁剪后为空，保留原状", seg_i)
                    continue
                seg_offset = actual_start

            if cfg.align_backend == "mms":
                raw_words = mms_or_ref.align(
                    (seg_audio, sr), full_text,
                    language=seg_lang, offset_sec=seg_offset,
                )
            else:
                items = _ae.align_sentence_raw(
                    (seg_audio, sr), full_text, seg_lang,
                    model_manager=model_manager,
                )
                raw_words = [
                    WordTimestamp(
                        text=it["text"],
                        start_time=round(it["start_time"] + seg_offset, 3),
                        end_time=round(it["end_time"] + seg_offset, 3),
                        language=seg_lang,
                    )
                    for it in items
                ] if items else []

            if not raw_words:
                logger.warning(
                    "[Align] 语言段 %d(%s) 对齐产出为空，保留原字级与脏标记",
                    seg_i, seg_lang,
                )
                continue

            candidates = [copy.deepcopy(s) for s in seg_sents]
            for candidate in candidates:
                candidate.words = []
            assigned = _attach_words_to_sentences(candidates, raw_words)
            for original, candidate in zip(seg_sents, assigned):
                if original.is_locked:
                    continue
                if not commit_aligned_words(candidate, list(candidate.words)):
                    logger.warning(
                        "[Align] 语言段 %d 的句 sid=%d 未得到字级，保留原状",
                        seg_i, original.sid,
                    )
                    continue
                original.words = copy.deepcopy(candidate.words)
                original.start_time = candidate.start_time
                original.end_time = candidate.end_time
                original.timed = candidate.timed
                original.is_dirty = False
                committed_sids.add(original.sid)

    # 空文本没有可对齐内容，清脏即可；锁定句和失败句保持原状。
    for sentence in project.sentences:
        if not sentence.is_locked and not (sentence.text or "").strip():
            sentence.is_dirty = False

    if not committed_sids:
        logger.warning("[Align] full-text 对齐没有提交任何新字级，项目保持原状")

    project.sort()
    apply_seam_snaps(project, sentence_sids=committed_sids)
    _report(cfg, n_seg + 1, n_seg + 1, "完成")
    logger.info(
        "[Align] full-text single 完成：%d 句 / %d 语言段 / %d 句成功提交",
        len(project.sentences), n_seg, len(committed_sids),
    )
    return project

def _align_full_text_chunked(
    project: SubtitleProject,
    *,
    model_manager: ModelManager,
    cfg: AlignConfig,
) -> SubtitleProject:
    """全文重对齐（media > 300s）：按语言分段、段内静音切块、事务式提交。

    每个重叠块在独立句子副本上工作；候选按“原句中心距块中心”选择，避免
    多个块共享同一 ``Sentence`` 导致先前候选被后续块原地覆盖。
    """
    segments = _resolve_language_segments(project, cfg)
    if not segments:
        return project

    preflight_segmenter_deps((lang for lang, _ in segments), backend=cfg.align_backend)

    if cfg.align_backend == "mms":
        mms = get_mms_aligner()
        if not mms.is_available():
            raise FileNotFoundError(
                f"找不到 MMS-300M-FA ONNX 对齐模型：{mms.model_dir}\n"
                "请检查模型目录或在设置中切换为 Qwen3-Aligner 对齐后端。"
            )
        align_ctx = model_manager.using_mms_aligner(progress_cb=cfg.progress_cb)
    else:
        mms = None
        align_ctx = model_manager.using_aligner(progress_cb=cfg.progress_cb)

    audio_np, sr = audio_io.load_audio(
        project.audio_path, mono=True, target_sr=DEFAULT_SAMPLE_RATE)
    silence_points = audio_io.detect_silence_points(
        audio_np, sr, threshold_db=-30.0, min_silence_sec=0.5,
    )

    original_centers = {
        sentence.sid: (float(sentence.start_time) + float(sentence.end_time)) / 2.0
        for sentence in project.sentences
    }

    jobs: List[Tuple[str, float, float, List[Sentence]]] = []
    for seg_lang, seg_sents in segments:
        seg_start = min(s.start_time for s in seg_sents)
        seg_end = max(s.end_time for s in seg_sents)
        span = seg_end - seg_start
        shifted = [sp - seg_start for sp in silence_points if seg_start < sp < seg_end]
        plan = audio_io.build_split_plan(
            span, shifted,
            max_duration=ALIGN_CHUNK_MAX_DURATION,
            min_duration=ALIGN_CHUNK_MIN_DURATION,
            overlap_sec=ALIGN_CHUNK_OVERLAP,
        )
        for cs, ce in plan.chunk_ranges:
            c0, c1 = seg_start + cs, seg_start + ce
            chunk_sents = [
                sentence for sentence in seg_sents
                if sentence.start_time < c1 and sentence.end_time > c0
            ]
            if chunk_sents:
                jobs.append((seg_lang, c0, c1, chunk_sents))

    n_jobs = len(jobs)
    logger.info(
        "[Align] 切块计划：%d 语言段 / %d 块，overlap=%.1fs",
        len(segments), n_jobs, ALIGN_CHUNK_OVERLAP,
    )
    _report(cfg, 0, n_jobs + 1, f"全文重对齐：{len(segments)} 语言段 / {n_jobs} 块…")
    if not jobs:
        logger.warning("[Align] 长媒体没有生成可执行块，项目保持原状")
        _report(cfg, 1, 1, "完成（无可执行块）")
        return project

    # sid -> (候选句快照, 原句中心到块中心的距离)
    collected: dict[int, tuple[Sentence, float]] = {}

    with align_ctx:
        for job_i, (job_lang, chunk_start, chunk_end, chunk_sentences) in enumerate(jobs):
            raise_if_cancelled(cfg.cancel_cb)
            _report(
                cfg, job_i + 1, n_jobs + 1,
                f"全文重对齐：块 {job_i+1}/{n_jobs}（{job_lang} · {chunk_start:.0f}-{chunk_end:.0f}s）",
            )
            block_center = (chunk_start + chunk_end) / 2.0

            cropped, actual_start, _ = _crop_audio(
                audio_np, sr, chunk_start, chunk_end,
                pad_before=cfg.pad_before, pad_after=cfg.pad_after,
            )
            if cropped.size == 0:
                logger.warning("[Align] 块 %d 裁剪后为空，保留原状", job_i)
                continue

            full_text = " ".join(sentence.text.strip() for sentence in chunk_sentences)
            if cfg.align_backend == "mms":
                raw_words = mms.align(
                    (cropped, sr), full_text,
                    language=job_lang, offset_sec=actual_start,
                )
            else:
                items = _ae.align_sentence_raw(
                    (cropped, sr), full_text, job_lang,
                    model_manager=model_manager,
                )
                raw_words = [
                    WordTimestamp(
                        text=it["text"],
                        start_time=round(it["start_time"] + actual_start, 3),
                        end_time=round(it["end_time"] + actual_start, 3),
                        language=job_lang,
                    )
                    for it in items
                ] if items else []

            if not raw_words:
                logger.warning("[Align] 块 %d 对齐产出为空，保留原状", job_i)
                continue

            candidates = [copy.deepcopy(sentence) for sentence in chunk_sentences]
            for candidate in candidates:
                candidate.words = []
            assigned = _attach_words_to_sentences(candidates, raw_words)

            for original, candidate in zip(chunk_sentences, assigned):
                if original.is_locked:
                    continue
                if not commit_aligned_words(candidate, list(candidate.words)):
                    logger.warning(
                        "[Align] 块 %d 的句 sid=%d 未得到字级，忽略该候选",
                        job_i, original.sid,
                    )
                    continue
                distance = abs(original_centers[original.sid] - block_center)
                existing = collected.get(original.sid)
                if existing is None or distance < existing[1]:
                    collected[original.sid] = (copy.deepcopy(candidate), distance)

    committed_sids: set[int] = set()
    for original in project.sentences:
        if original.is_locked:
            continue
        entry = collected.get(original.sid)
        if entry is None:
            if not (original.text or "").strip():
                original.is_dirty = False
            continue
        candidate, _distance = entry
        original.words = copy.deepcopy(candidate.words)
        original.start_time = candidate.start_time
        original.end_time = candidate.end_time
        original.timed = candidate.timed
        original.is_dirty = False
        committed_sids.add(original.sid)

    project.sort()
    apply_seam_snaps(project, sentence_sids=committed_sids)
    _report(cfg, n_jobs + 1, n_jobs + 1, "完成")
    logger.info(
        "[Align] full-text chunked 完成：%d 句 / %d 语言段 %d 块 / %d 句成功提交",
        len(project.sentences), len(segments), n_jobs, len(committed_sids),
    )
    return project
