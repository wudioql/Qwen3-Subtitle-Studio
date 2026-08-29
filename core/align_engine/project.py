"""core.align_engine.project — 整项目按句对齐 / 仅脏句对齐。"""
from __future__ import annotations

import logging
from typing import Optional

from subs.models import SubtitleProject

from core import audio_io
from core.constants import DEFAULT_SAMPLE_RATE
from core.model_manager import ModelManager
from core.task_control import TaskCancelled, raise_if_cancelled
from .common import (
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


def _alignment_context(model_manager: ModelManager, cfg: AlignConfig):
    """任务开工前确定后端；MMS 缺失时禁止误入 Qwen 上下文。"""
    if cfg.align_backend == "mms":
        mms = get_mms_aligner()
        if not mms.is_available():
            raise FileNotFoundError(
                f"找不到 MMS-300M-FA ONNX 对齐模型：{mms.model_dir}\n"
                "请检查模型目录或在设置中切换为 Qwen3-Aligner 对齐后端。"
            )
        return model_manager.using_mms_aligner(progress_cb=cfg.progress_cb)
    return model_manager.using_aligner(progress_cb=cfg.progress_cb)


def align_project(
    project: SubtitleProject,
    *,
    model_manager: ModelManager,
    cfg: Optional[AlignConfig] = None,
) -> SubtitleProject:
    """整个 project 原地补 words；已有的 words 跳过（避免覆盖用户手动修改）。"""
    cfg = cfg or AlignConfig()
    raise_if_cancelled(cfg.cancel_cb)

    if not project.sentences:
        return project

    # 预检：候选集与下方主循环的跳过条件保持一致
    # （锁定 / 已有字级未脏 / 空文本 的句不会进入对齐，不为它们要包）
    if cfg.align_backend != "mms":
        _langs = {
            _infer_full_language(cfg.source_language, s.language, project.source_language)
            for s in project.sentences
            if not s.is_locked and (s.text or "").strip()
            and not (s.words and not s.is_dirty)
        }
        preflight_segmenter_deps([la for la in _langs if la], backend=cfg.align_backend)

    align_ctx = _alignment_context(model_manager, cfg)
    audio_np, sr = audio_io.load_audio(  # 内存重采样兜底：audio_path 不必须是 16k 提取件
        project.audio_path, mono=True, target_sr=DEFAULT_SAMPLE_RATE)
    total = len(project.sentences)

    _report(cfg, 0, total + 1, "加载对齐器...")
    with align_ctx:
        for idx, sent in enumerate(project.sentences):
            raise_if_cancelled(cfg.cancel_cb)
            _report(cfg, idx + 1, total + 1, f"对齐句 {idx+1}/{total}")
            if sent.is_locked:
                logger.debug("[Align] 跳过锁定句 %d", idx)
                continue
            if sent.words and not sent.is_dirty:
                logger.debug("[Align] 跳过句 %d（已有 words，未脏）", idx)
                continue
            if not (sent.text or "").strip():
                continue
            try:
                # 语言决议：句级语言优先，项目语言回落
                this_lang = _infer_full_language(
                    cfg.source_language, sent.language, project.source_language
                )
                prv_sent = project.sentences[idx - 1] if idx - 1 >= 0 else None
                nxt_sent = (project.sentences[idx + 1]
                            if idx + 1 < len(project.sentences) else None)
                words = _ae.align_sentence(
                    sent, audio_np, sr,
                    model_manager=model_manager, cfg=cfg,
                    inferred_language_full=this_lang,
                    prev_sentence_end=(prv_sent.end_time if prv_sent else None),
                    next_sentence_start=(nxt_sent.start_time if nxt_sent else None),
                    prev_sentence=prv_sent,
                    next_sentence=nxt_sent,
                )
            except TaskCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("[Align] 句 %d 失败: %s", idx, e)
                continue
            if not commit_aligned_words(
                    sent, words,
                    next_start=(nxt_sent.start_time if nxt_sent else None),
                ):
                logger.warning("[Align] 句 %d 对齐产出为空，保留旧字级与脏标记", idx)

    project.sort()
    # 整项目接缝收尾：前句尾→后句首（小间隙）、后句首→前句尾（小重叠）对称吸附。
    apply_seam_snaps(project)
    _report(cfg, total + 1, total + 1, "完成")
    logger.info(
        "[Align] 完成：%d 句 / %d 句已补齐字级",
        total, sum(1 for s in project.sentences if s.has_word_level()),
    )
    return project


# ══════════════════════════════════════════════════════════════════
# 只重对齐脏句
# ══════════════════════════════════════════════════════════════════

def align_dirty_only(
    project: SubtitleProject,
    *,
    model_manager: ModelManager,
    cfg: Optional[AlignConfig] = None,
) -> SubtitleProject:
    """只对齐 project.alignable_dirty_indices() 列出的句（跳过已锁定与已确认句）；其他句原样保留。"""
    cfg = cfg or AlignConfig()
    raise_if_cancelled(cfg.cancel_cb)
    dirty = project.alignable_dirty_indices()
    if not dirty:
        logger.info("[Align] 无待对齐脏句（可能已全部确认或锁定），直接返回")
        _report(cfg, 0, 1, "无待对齐脏句")
        _report(cfg, 1, 1, "完成")
        return project

    # 预检：同 align_project，候选集与主循环跳过条件一致
    if cfg.align_backend != "mms":
        _langs = {
            _infer_full_language(
                cfg.source_language, project.sentences[i].language, project.source_language
            )
            for i in dirty
            if 0 <= i < len(project.sentences)
            and not project.sentences[i].is_locked
            and (project.sentences[i].text or "").strip()
        }
        preflight_segmenter_deps([la for la in _langs if la], backend=cfg.align_backend)

    align_ctx = _alignment_context(model_manager, cfg)
    audio_np, sr = audio_io.load_audio(  # 内存重采样兜底：audio_path 不必须是 16k 提取件
        project.audio_path, mono=True, target_sr=DEFAULT_SAMPLE_RATE)

    total = len(dirty)
    _report(cfg, 0, total + 1, f"加载对齐器（{total} 待对齐脏句）")
    with align_ctx:
        for pos, idx in enumerate(dirty):
            raise_if_cancelled(cfg.cancel_cb)
            if idx < 0 or idx >= len(project.sentences):
                logger.warning("[Align] 越界 idx=%d，跳过", idx)
                continue
            sent = project.sentences[idx]
            if sent.is_locked:
                logger.debug("[Align] 跳过锁定句 %d", idx)
                continue
            _report(cfg, pos + 1, total + 1, f"对齐脏句 {idx+1} (第 {pos+1}/{total})")
            if not (sent.text or "").strip():
                logger.debug("[Align] 脏句 %d 文本为空，跳过", idx)
                continue
            try:
                # 语言决议：句级语言优先，项目语言回落
                this_lang = _infer_full_language(
                    cfg.source_language, sent.language, project.source_language
                )
                prv_sent = project.sentences[idx - 1] if idx - 1 >= 0 else None
                nxt_sent = (project.sentences[idx + 1]
                            if idx + 1 < len(project.sentences) else None)
                words = _ae.align_sentence(
                    sent, audio_np, sr,
                    model_manager=model_manager, cfg=cfg,
                    inferred_language_full=this_lang,
                    prev_sentence_end=(prv_sent.end_time if prv_sent else None),
                    next_sentence_start=(nxt_sent.start_time if nxt_sent else None),
                    prev_sentence=prv_sent,
                    next_sentence=nxt_sent,
                )
            except TaskCancelled:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("[Align] 脏句 %d 失败: %s", idx, e)
                continue
            if not commit_aligned_words(
                    sent, words,
                    next_start=(nxt_sent.start_time if nxt_sent else None),
                ):
                logger.warning("[Align] 脏句 %d 对齐产出为空，保留旧字级与脏标记", idx)

    project.sort()
    # 整项目接缝收尾：前句尾→后句首（小间隙）、后句首→前句尾（小重叠）对称吸附。
    apply_seam_snaps(project)
    _report(cfg, total + 1, total + 1, "完成")
    logger.info(
        "[Align] dirty 模式完成：%d 待对齐句 / 当前脏=%d / 锁定=%d",
        total, project.dirty_count(), len(project.locked_indices()),
    )
    return project
