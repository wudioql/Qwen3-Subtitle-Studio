"""core.align_engine.full — 全文重对齐（策略层：派发 / 单段 / 切块 / 两阶段）。

本模块只做**策略选择与编排**；语言决议、字级切回与事务提交等原语在 ``words``，
切块机制在 ``chunking``（本模块单向依赖二者）。

分段按 **Qwen 分词器族**收敛（``core.language_utils.tokenizer_family``）：同族语言
（中/粤/英/法/德… 共用官方默认「CJK 逐字 + 空格词」分词路径）合并成一次调用，
只有 ja / ko 与其它语言同段混排时才真正需要分开调用。

**音频窗来源只有两种**：① 静音点 / 长度切块；② 两阶段里阶段一产出的**真实时间**。
句级时间**只**用于把文本分组到块或对齐调用，**绝不**用于定义音频裁剪窗——首次
全文重对齐时句级时间多为 ASR / 纯文本导入的**占位值**（字符占比均分），按它裁
音频会让段内内容完全对不上（已知缺陷）。

MMS-FA 后端语言**不进模型**（只影响罗马化读音），因此完全不做语言裁剪：整段
一次调用，逐句语言经 ``word_languages`` 传入。
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple

from subs.models import Sentence, SubtitleProject

from core import audio_io
from core.constants import ALIGNER_MAX_DURATION, DEFAULT_SAMPLE_RATE
from core.language_utils import default_family_representative, tokenizer_family
from core.model_manager import ModelManager
from core.task_control import raise_if_cancelled

from .chunking import _commit_collected, _media_span, _plan_jobs, _run_jobs
from .common import _crop_audio, preflight_segmenter_deps
from .config import AlignConfig, _report
from .words import (
    _assign_words,
    _build_word_languages,
    _commit_one,
    _commit_span,
    _conflict_segments,
    _dominant_family,
    _finish_full_text,
    _language_setter,
    _qwen_words,
    _resolve_language_segments,
)

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


# ══════════════════════════════════════════════════════════════════
# 单段全文重对齐（media ≤ ALIGNER_MAX_DURATION）
# ══════════════════════════════════════════════════════════════════

def _align_full_text_single(
    project: SubtitleProject,
    *,
    model_manager: ModelManager,
    cfg: AlignConfig,
) -> SubtitleProject:
    """全文重对齐（单块级，media ≤ 300s）。

    - **MMS 后端**：语言不进模型 → 整段一次调用、零裁剪，逐句语言经
      ``word_languages`` 传给罗马化。
    - **Qwen 单族**（含中/粤/英混排）：一次调用、零裁剪（占位时间只被产出覆盖，
      不会被拿去裁音频）。
    - **Qwen 多族冲突**（ja/ko 与其它语言同段混排）：两阶段——阶段一用整段音频
      + 全文拿**真实时间**，阶段二在真实时间窗上按族精修。

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

    all_sents = [s for _, sents in segments for s in sents]
    families = {tokenizer_family(lang) for lang, _ in segments}
    language_of = _language_setter(cfg, project, segments[0][0])

    if cfg.align_backend == "mms":
        total_steps = 2
        with align_ctx:
            committed_sids = _align_mms_single(
                project, cfg, mms, audio_np, sr, all_sents, segments,
                language_of=language_of, total_steps=total_steps,
            )
        log_label = "full-text single(mms)"
    elif len(families) == 1:
        total_steps = 2
        _report(cfg, 0, total_steps, f"全文重对齐：整段单次（{segments[0][0]} · {len(all_sents)} 句）...")
        committed_sids = set()
        with align_ctx:
            full_text = " ".join(s.text.strip() for s in all_sents)
            raw_words = _qwen_words(audio_np, sr, full_text, segments[0][0], 0.0, model_manager)
            committed_sids = _commit_span(
                all_sents, raw_words, language_of=language_of, context="单族整段",
            )
        log_label = f"full-text single(单族 {segments[0][0]})"
    else:
        total_steps = 1 + sum(
            1 for lang, _ in segments if tokenizer_family(lang) != _dominant_family(segments)
        ) + 1
        with align_ctx:
            committed_sids = _align_two_stage(
                project, cfg, segments, audio_np, sr,
                model_manager=model_manager, language_of=language_of,
                total_steps=total_steps, chunked=False,
                mms=None, silence_points=None, media_dur=media_dur,
            )
        log_label = "full-text single(多族两阶段)"

    return _finish_full_text(
        project, cfg, committed_sids, total_steps=total_steps, log_label=log_label,
    )


def _align_mms_single(
    project: SubtitleProject,
    cfg: AlignConfig,
    mms,
    audio_np, sr,
    all_sents: List[Sentence],
    segments: List[Tuple[str, List[Sentence]]],
    *,
    language_of: Callable[[Sentence], str],
    total_steps: int,
) -> set[int]:
    """MMS 后端整段单次调用（零裁剪、逐句读音语言）。"""
    if len(segments) > 1:
        logger.info(
            "[Align] MMS 后端忽略语言分段：%d 段合并为整段单次调用（语言不进模型）",
            len(segments),
        )
    _report(cfg, 0, total_steps, f"全文重对齐：MMS 整段强制对齐（{len(all_sents)} 句）...")
    full_text = " ".join(s.text.strip() for s in all_sents)
    raw_words = mms.align(
        (audio_np, sr), full_text,
        language=segments[0][0],
        word_languages=_build_word_languages(all_sents, project, cfg),
        offset_sec=0.0,
    )
    return _commit_span(all_sents, raw_words, language_of=language_of, context="MMS 整段")


# ══════════════════════════════════════════════════════════════════
# 长媒体全文重对齐（media > ALIGNER_MAX_DURATION）：静音切块
# ══════════════════════════════════════════════════════════════════

def _align_full_text_chunked(
    project: SubtitleProject,
    *,
    model_manager: ModelManager,
    cfg: AlignConfig,
) -> SubtitleProject:
    """全文重对齐（media > 300s）：静音切块、事务式提交。

    与单段路径同构：MMS / 单族 = 一个 group 覆盖整段媒体；多族冲突 = 两阶段
    （阶段一真实时间 → 阶段二按族精修）。每个重叠块在独立句子副本上工作；
    候选按「原句中心距块中心」选择，避免多个块共享同一 ``Sentence`` 导致先前
    候选被后续块原地覆盖。
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
    media_dur = float(project.media_duration or 0.0)
    silence_points = audio_io.detect_silence_points(
        audio_np, sr, threshold_db=-30.0, min_silence_sec=0.5,
    )

    original_centers = {
        sentence.sid: (float(sentence.start_time) + float(sentence.end_time)) / 2.0
        for sentence in project.sentences
    }

    all_sents = [s for _, sents in segments for s in sents]
    families = {tokenizer_family(lang) for lang, _ in segments}
    language_of = _language_setter(cfg, project, segments[0][0])
    media_span = _media_span(media_dur, all_sents)
    log_label = "full-text chunked"

    with align_ctx:
        if cfg.align_backend == "mms" or len(families) == 1:
            if cfg.align_backend == "mms" and len(segments) > 1:
                logger.info(
                    "[Align] MMS 后端忽略语言分段：%d 段合并为单组整段切块",
                    len(segments),
                )
            jobs = _plan_jobs(
                segments[0][0], all_sents, media_span, silence_points,
            )
            total_steps = len(jobs) + 1
            _report(cfg, 0, total_steps, f"全文重对齐：{len(jobs)} 块…")
            if not jobs:
                logger.warning("[Align] 长媒体没有生成可执行块，项目保持原状")
                _report(cfg, 1, 1, "完成（无可执行块）")
                return project
            collected = _run_jobs(
                jobs, project=project, cfg=cfg, audio_np=audio_np, sr=sr,
                model_manager=model_manager, mms=mms, original_centers=original_centers,
            )
            committed_sids = _commit_collected(project, collected, language_of=language_of)
            log_label = f"full-text chunked(单族 {segments[0][0]}, {len(jobs)} 块)"
        else:
            committed_sids = _align_two_stage(
                project, cfg, segments, audio_np, sr,
                model_manager=model_manager, language_of=language_of,
                total_steps=0, chunked=True,
                mms=None, silence_points=silence_points, media_dur=media_dur,
                original_centers=original_centers,
            )
            log_label = "full-text chunked(多族两阶段)"

    return _finish_full_text(
        project, cfg, committed_sids, total_steps=1, log_label=log_label,
    )


# ══════════════════════════════════════════════════════════════════
# 多族冲突的两阶段（单段 / 切块共用）
# ══════════════════════════════════════════════════════════════════

def _align_two_stage(
    project: SubtitleProject,
    cfg: AlignConfig,
    segments: List[Tuple[str, List[Sentence]]],
    audio_np, sr,
    *,
    model_manager: ModelManager,
    language_of: Callable[[Sentence], str],
    total_steps: int,
    chunked: bool,
    mms,
    silence_points: Optional[List[float]],
    media_dur: float,
    original_centers: Optional[Dict[int, float]] = None,
) -> set[int]:
    """多族冲突（ja/ko 与其它语言同段混排）下的两阶段全文重对齐。

    阶段一：整段音频 + 全文一次对齐（语言取 default 族代表）→ 得**真实时间**。
    阶段二：对每个非主导族段，用**阶段一真实时间**裁窗 + 各自语言精修。

    绝不使用句级占位时间裁音频。阶段二若拿不到真实时间锚 → 跳过并告警
    （保留原状），不用占位时间兜底。
    """
    dominant_family = _dominant_family(segments)
    default_langs = [lang for lang, _ in segments if tokenizer_family(lang) == "default"]
    phase1_lang = default_langs[0] if default_langs else default_family_representative()
    conflicts = _conflict_segments(segments, dominant_family)

    all_sents = [s for _, sents in segments for s in sents]
    n_steps = total_steps or (1 + len(conflicts) + 1)
    _report(
        cfg, 0, n_steps,
        f"全文重对齐：多语言分族（{len(segments)} 段），阶段一粗对齐…",
    )

    def _family_of(sentence: Sentence) -> str:
        return tokenizer_family(language_of(sentence))

    # ── 阶段一 ────────────────────────────────────────────────
    real_span: Dict[int, Tuple[float, float]] = {}
    committed: set[int] = set()

    if chunked:
        assert silence_points is not None
        jobs1 = _plan_jobs(
            phase1_lang, all_sents, _media_span(media_dur, all_sents), silence_points,
        )
        collected1 = _run_jobs(
            jobs1, project=project, cfg=cfg, audio_np=audio_np, sr=sr,
            model_manager=model_manager, mms=mms,
            original_centers=original_centers or {},
        )
        committed |= _commit_collected(
            project, collected1, language_of=language_of,
            accept=lambda s: _family_of(s) == dominant_family,
        )
        by_sid = {s.sid: s for s in project.sentences}
        for sid, (candidate, _d) in collected1.items():
            sentence = by_sid.get(sid)
            if sentence is not None and _family_of(sentence) != dominant_family:
                real_span[sid] = (float(candidate.start_time), float(candidate.end_time))
    else:
        full_text = " ".join(s.text.strip() for s in all_sents)
        raw1 = _qwen_words(audio_np, sr, full_text, phase1_lang, 0.0, model_manager)
        for original, candidate in _assign_words(all_sents, raw1):
            if not candidate.words:
                continue
            if _family_of(original) == dominant_family:
                if _commit_one(
                    original, candidate, language_of=language_of, context="阶段一主导族",
                ):
                    committed.add(original.sid)
            else:
                real_span[original.sid] = (
                    float(candidate.start_time), float(candidate.end_time),
                )

    _report(
        cfg, 1, n_steps,
        f"全文重对齐：阶段一完成（{phase1_lang} 全文），精修 {len(conflicts)} 个语言族段…",
    )

    # ── 阶段二 ────────────────────────────────────────────────
    for step, (seg_lang, seg_sents) in enumerate(conflicts, start=1):
        raise_if_cancelled(cfg.cancel_cb)
        spans = [real_span[s.sid] for s in seg_sents if s.sid in real_span]
        if not spans:
            logger.warning(
                "[Align] 语言族段 %s 在阶段一未取得真实时间，跳过精修（保留原状）", seg_lang,
            )
            _report(cfg, step + 1, n_steps, f"全文重对齐：{seg_lang} 段无真实时间锚，跳过")
            continue

        win = (min(a for a, _ in spans), max(b for _, b in spans))
        _report(
            cfg, step + 1, n_steps,
            f"全文重对齐：精修 {seg_lang} 段（{len(seg_sents)} 句 · {win[0]:.1f}-{win[1]:.1f}s）…",
        )
        target_sids = {s.sid for s in seg_sents}

        if chunked:
            assert silence_points is not None
            jobs2 = _plan_jobs(seg_lang, seg_sents, win, silence_points)
            collected2 = _run_jobs(
                jobs2, project=project, cfg=cfg, audio_np=audio_np, sr=sr,
                model_manager=model_manager, mms=mms,
                original_centers=original_centers or {},
            )
            committed |= _commit_collected(
                project, collected2, language_of=language_of,
                accept=lambda s: s.sid in target_sids,
            )
        else:
            cropped, actual_start, _ = _crop_audio(
                audio_np, sr, win[0], win[1],
                pad_before=cfg.pad_before, pad_after=cfg.pad_after,
            )
            if cropped.size == 0:
                logger.warning("[Align] 语言族段 %s 真实窗裁后为空，跳过", seg_lang)
                continue
            seg_text = " ".join(s.text.strip() for s in seg_sents)
            raw2 = _qwen_words(cropped, sr, seg_text, seg_lang, actual_start, model_manager)
            committed |= _commit_span(
                seg_sents, raw2, language_of=language_of, context=f"阶段二 {seg_lang}",
            )

    return committed
