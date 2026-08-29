"""core.align_engine.sentence — 单句对齐（Qwen / MMS / 超长子切）。"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, List, Optional

import numpy as np

from subs.models import Sentence, WordTimestamp

from core.constants import (
    ALIGNER_MAX_DURATION,
    ALIGN_WIN_EXTEND,
    MMS_HEAD_EXTEND,
    MMS_TAIL_EXTEND_MAX,
)
from core.model_manager import ModelManager
from core.task_control import raise_if_cancelled

from .common import _crop_audio, _infer_full_language
from .config import AlignConfig, AudioSource

logger = logging.getLogger("core.align_engine")

def get_mms_aligner(*args, **kwargs):
    """运行时经包入口查找，保证 patch(\"core.align_engine.get_mms_aligner\") 生效。"""
    import core.align_engine as _ae
    return _ae.get_mms_aligner(*args, **kwargs)


def align_sentence_raw(
    audio: AudioSource,
    text: str,
    language_full: str,
    *,
    model_manager: ModelManager,
) -> List[dict]:
    """底层对齐原语（单次调用，不切片）。返回 [{text, start_time, end_time}, ...]。"""
    text = (text or "").strip()
    if not text or not language_full:
        return []

    import torch  # 懒加载：仅真正跑 Qwen 对齐时才需要

    ali_proc, ali_model = model_manager.get_aligner()
    with torch.inference_mode():
        kwargs: dict[str, Any] = {"audio": audio, "transcript": text, "language": language_full}
        processor_kwargs: dict[str, Any] = {}
        if isinstance(audio, tuple):
            samples_raw = np.asarray(audio[0], dtype=np.float32)
            sr_arg = int(audio[1])
            kwargs["audio"] = samples_raw
            processor_kwargs["sampling_rate"] = sr_arg
        elif isinstance(audio, Path):
            kwargs["audio"] = str(audio)
        if processor_kwargs:
            align_inputs, word_lists = ali_proc.prepare_forced_aligner_inputs(
                **kwargs, processor_kwargs=processor_kwargs)
        else:
            align_inputs, word_lists = ali_proc.prepare_forced_aligner_inputs(**kwargs)

        target_dev = torch.device(model_manager.device)
        align_inputs = align_inputs.to(target_dev, model_manager.dtype)
        logits = ali_model(**align_inputs).logits
        batch_timestamps = ali_proc.decode_forced_alignment(
            logits=logits,
            input_ids=align_inputs["input_ids"],
            word_lists=word_lists,
            timestamp_token_id=ali_model.config.timestamp_token_id,
        )
    if not batch_timestamps:
        return []
    return [
        {
            "text": str(it.get("text", "")),
            "start_time": round(float(it.get("start_time", 0.0)), 3),
            "end_time": round(float(it.get("end_time", 0.0)), 3),
        }
        for it in batch_timestamps[0]
    ]


# ══════════════════════════════════════════════════════════════════
# 单句对齐
# ══════════════════════════════════════════════════════════════════

def align_sentence(
    sentence: Sentence,
    audio_np: np.ndarray,
    sample_rate: int,
    *,
    model_manager: ModelManager,
    cfg: AlignConfig | None = None,
    inferred_language_full: Optional[str] = None,
    prev_sentence_end: Optional[float] = None,
    next_sentence_start: Optional[float] = None,
    prev_sentence: Optional[Sentence] = None,
    next_sentence: Optional[Sentence] = None,
) -> List[WordTimestamp]:
    """对单个 Sentence 对齐 → 返回该句范围内的 WordTimestamp（已修正为全局时间）。

    prev_sentence_end / next_sentence_start：邻句边界（全局秒），裁剪窗的
    **稳定锚**——不随本句重对齐的产出漂移。裁剪窗在句界基础上向两侧扩展
    （ALIGN_WIN_EXTEND / MMS 尾侧 MMS_TAIL_EXTEND_MAX）再被邻句锚截住：
    句界被手动拖错（拖短）时真实发音仍在窗内，重对齐能把首/尾字恢复到
    正确位置；窗口不被当前句界奴役，也不产生随产出漂移的棘轮。
    next_sentence_start 同时是 MMS 末字长拖音的搜索上界锚，None（最后一句/
    直接调用）时由 MMS 内部用「末字元音起点 + 固定前瞻」封顶。

    prev_sentence / next_sentence（可选，新）：邻句**对象**。MMS 后端据此做
    **上下文强制对齐**（把邻句文本一并交给对齐器，前句尾音/后句开口由邻句
    tokens 吸收），把单句孤对齐的句首/尾漂移压到与全文对齐同一精度。邻句为
    脏句（文本已改、音频尚未重对齐）时其文本与音频不一致，自动退化为孤对齐。
    """
    cfg = cfg or AlignConfig()
    raise_if_cancelled(cfg.cancel_cb)
    lang_full = inferred_language_full or _infer_full_language(
        cfg.source_language, sentence.language, ""
    )
    if not lang_full:
        raise ValueError(
            f"无法推断语言（sentence.language={sentence.language!r}, "
            f"cfg.source_language={cfg.source_language!r}）。请显式指定语言。"
        )
    text = (sentence.text or "").strip()
    if not text:
        return []
    duration = sentence.end_time - sentence.start_time
    if duration <= 0.0:
        logger.warning("[Align] 句时长非法: %s", sentence)
        return []

    if duration > ALIGNER_MAX_DURATION:
        return _align_long_sentence(
            sentence, audio_np, sample_rate,
            model_manager=model_manager, cfg=cfg, lang_full=lang_full,
        )

    # 裁剪窗（两后端通用头侧逻辑）：句首向前扩展 ALIGN_WIN_EXTEND，被前句尾
    # （稳定锚）截住——句首被手动拖短后真实起音仍在窗内，重对齐能恢复；
    # 无前句时只扩不截（媒体开头由 _crop_audio 自身钳 0）。
    # 邻句对象优先于时间锚（调用方二者都传；测试/直接调用只传时间锚）。
    if prev_sentence is not None and prev_sentence_end is None:
        prev_sentence_end = float(prev_sentence.end_time)
    if next_sentence is not None and next_sentence_start is None:
        next_sentence_start = float(next_sentence.start_time)
    win_start = sentence.start_time - ALIGN_WIN_EXTEND
    if prev_sentence_end is not None:
        win_start = max(win_start, prev_sentence_end)

    # 1. MMS 歌词/长拖音对齐后端
    if cfg.align_backend == "mms":
        mms = get_mms_aligner()
        if not mms.is_available():
            raise FileNotFoundError(
                f"找不到 MMS-300M-FA ONNX 对齐模型：{mms.model_dir}\n"
                f"请检查模型目录或在设置中切换为 Qwen3-Aligner 对齐后端。"
            )
        # 末字拖音搜索上界（全局秒）：下一句起点是稳定锚——重对齐本句不会改变它，
        # 因此上界不随本句上次产出漂移（无棘轮）；无下一句时不设，由 MMS 内部
        # 用「末字元音起点 + MMS_TAIL_EXTEND_MAX」封顶（元音起点由 Viterbi 按文本
        # 锚定，同样收敛）。
        tail_limit = next_sentence_start

        # 上下文强制对齐（仅 MMS）：把邻句文本一并交给对齐器，前句尾音/后句开口
        # 由邻句 tokens 吸收——单句孤对齐的句首/尾漂移根因（窗首尾的异质发音被
        # CTC 塞给本句首/末字）。邻句为脏句（文本已改、音频未重对齐）时其文本与
        # 音频不一致，退化为孤对齐。
        prev_ctx = (
            prev_sentence
            if (prev_sentence is not None and not prev_sentence.is_dirty
                and (prev_sentence.text or "").strip())
            else None
        )
        next_ctx = (
            next_sentence
            if (next_sentence is not None and not next_sentence.is_dirty
                and (next_sentence.text or "").strip())
            else None
        )
        use_ctx = prev_ctx is not None or next_ctx is not None

        if use_ctx:
            # 窗首覆盖前句（让前句文本吸收自己的尾音）；无前句时仅 0.5s 前瞻
            # （MMS_HEAD_EXTEND，避免片头音乐/噪声被塞给首字）。
            head = (
                float(prev_ctx.start_time)
                if prev_ctx is not None else sentence.start_time - MMS_HEAD_EXTEND
            )
            tail = (
                float(next_ctx.end_time)
                if next_ctx is not None else sentence.end_time + MMS_TAIL_EXTEND_MAX
            )
        else:
            # 孤对齐：句尾 + 前瞻，被后句头截短；句首由 win_start 锚定。
            head = win_start
            tail = sentence.end_time + MMS_TAIL_EXTEND_MAX
            if tail_limit is not None:
                tail = min(tail, tail_limit)

        # MMS 路径 pad_before=0：MMS 是**强制对齐**（窗内所有发音都要分配给传入
        # 文本的 tokens），pad_before(0.12s) 会把前句尾音的最后 0.12s 混进窗。
        cropped, actual_start, _ = _crop_audio(
            audio_np, sample_rate,
            head, tail,
            pad_before=0.0, pad_after=0.0,
        )
        if cropped.size == 0:
            logger.warning("[Align] 裁剪后音频为空: %s", sentence)
            return []
        # 若外层（align_project / dirty / full / AlignWorker）已持有
        # using_mms_aligner（_mms_ctx_depth>0），不要再包一层把 Session 中途卸掉；
        # 仅在「单独调用 align_sentence」时自管上下文，保证 ORT 显存被还回。
        # MagicMock 等测试替身无真实 depth → 不包，直接用 get_mms_aligner()。
        try:
            owns_mms_ctx = int(getattr(model_manager, "_mms_ctx_depth", 0) or 0) == 0
        except Exception:  # noqa: BLE001
            owns_mms_ctx = False
        mms_ctx = (
            model_manager.using_mms_aligner(progress_cb=cfg.progress_cb)
            if owns_mms_ctx else None
        )
        if mms_ctx is not None:
            mms = mms_ctx.__enter__()
        try:
            if use_ctx:
                mms_words = mms.align_with_context(
                    (cropped, sample_rate),
                    prev_ctx.text if prev_ctx is not None else "",
                    text,
                    next_ctx.text if next_ctx is not None else "",
                    language=lang_full,
                    offset_sec=actual_start,
                )
            else:
                mms_words = mms.align(
                    (cropped, sample_rate), text,
                    language=lang_full, offset_sec=actual_start,
                    tail_limit_sec=tail_limit,
                )
                # 首字下界钳制（双保险）：即使窗内仍混入前句尾音，首字 start 也不得
                # 早于前句尾——排除「句首漂移到前句范围」；句首拖短后的真实发音
                # 恒在前句尾之后，此钳制不影响恢复。
                if mms_words and prev_sentence_end is not None:
                    first = mms_words[0]
                    if float(first.start_time) < float(prev_sentence_end):
                        first.start_time = round(float(prev_sentence_end), 3)
                        if float(first.end_time) < float(first.start_time) + 0.030:
                            first.end_time = round(float(first.start_time) + 0.030, 3)
            return mms_words if mms_words else []
        finally:
            if mms_ctx is not None:
                mms_ctx.__exit__(None, None, None)

    # Qwen 后端窗口：尾侧同样向后扩展、被后句头（稳定锚）截住——句尾被拖短后
    # 真实收音仍在窗内；Qwen 强制对齐按文本吸附发音区间，窗内多出的静音/邻界
    # 余量不构成「给多少吃多少」，无需额外上界。
    win_end = sentence.end_time + ALIGN_WIN_EXTEND
    if next_sentence_start is not None:
        win_end = min(win_end, next_sentence_start)
    cropped, actual_start, _ = _crop_audio(
        audio_np, sample_rate,
        win_start, win_end,
        pad_before=cfg.pad_before, pad_after=cfg.pad_after,
    )
    if cropped.size == 0:
        logger.warning("[Align] 裁剪后音频为空: %s", sentence)
        return []

    # 2. Qwen3-Aligner 对齐后端
    # 直接 API 调用的兜底门（各批量入口已在开工前统一预检过）
    import core.align_engine as _ae
    dep_msg = _ae.check_segmenter_dependency(lang_full)
    if dep_msg:
        raise ImportError(dep_msg)
    import core.align_engine as _ae
    items = _ae.align_sentence_raw(
        (cropped, sample_rate), text, lang_full, model_manager=model_manager,
    )
    offset = actual_start
    words: List[WordTimestamp] = []
    for it in items:
        words.append(WordTimestamp(
            text=it["text"],
            start_time=round(it["start_time"] + offset, 3),
            end_time=round(it["end_time"] + offset, 3),
            language=lang_full,
        ))
    return words


def _split_long_text_bounded(
    text: str,
    *,
    duration: float,
    max_duration: float,
    min_chars: int,
) -> List[str]:
    """把超长句文本切成与时间窗上限匹配的非空连续片段。

    优先在标点/空白之后切；找不到时退化到字符边界。每片按字符比例分配
    时长后都不超过 ``max_duration``。若文本短到无法形成足够多的非空片段，
    明确报错而不是把同一问题递归回 ``align_sentence``。
    """
    text = text or ""
    if not text:
        return []
    if duration <= max_duration:
        return [text]

    total_chars = len(text)
    # floor 保证按字符比例分时后单块不会越过模型硬上限。
    max_chars = int(total_chars * max_duration / duration)
    if max_chars < 1:
        raise ValueError(
            f"句时长 {duration:.1f}s 超过对齐器上限，但文本仅 {total_chars} 字符，"
            "无法切成足够多的非空子块；请先手动拆分该句。"
        )

    preferred = {
        i + 1 for i, ch in enumerate(text)
        if ch.isspace() or re.fullmatch(r"[。！？!?；;,，：:\n]", ch)
    }
    parts: List[str] = []
    start = 0
    soft_min = max(1, int(min_chars))
    while start < total_chars:
        hard_end = min(total_chars, start + max_chars)
        if hard_end >= total_chars:
            parts.append(text[start:])
            break

        # 在硬上限内尽量取最后一个自然边界；min_chars 只是软偏好，不能为了它
        # 让分块超过模型时长上限。
        natural = [
            pos for pos in preferred
            if start + min(soft_min, max_chars) <= pos <= hard_end
        ]
        cut = natural[-1] if natural else hard_end
        if cut <= start:  # 防御：循环必须严格前进
            raise ValueError("超长句切分未能缩小问题；请手动拆分该句。")
        parts.append(text[start:cut])
        start = cut

    if not parts or any(not p for p in parts):
        raise ValueError("超长句切分产生空片段；请手动拆分该句。")
    return parts


def _align_long_sentence(
    sentence: Sentence,
    audio_np: np.ndarray,
    sample_rate: int,
    *,
    model_manager: ModelManager,
    cfg: AlignConfig,
    lang_full: str,
) -> List[WordTimestamp]:
    """句时长 > 上限的兜底：自然边界优先、字符边界保底，且保证问题严格缩小。"""
    text = sentence.text or ""
    duration = float(sentence.end_time - sentence.start_time)
    parts = _split_long_text_bounded(
        text,
        duration=duration,
        max_duration=ALIGNER_MAX_DURATION,
        min_chars=cfg.subchunk_min_chars,
    )

    total_chars = sum(len(p) for p in parts)
    s0 = float(sentence.start_time)
    et = float(sentence.end_time)
    base = s0
    all_words: List[WordTimestamp] = []
    for i, p in enumerate(parts):
        raise_if_cancelled(cfg.cancel_cb)
        if i == len(parts) - 1:
            seg_end = et
        else:
            seg_end = base + len(p) / max(1, total_chars) * duration
        sub = Sentence(text=p, start_time=base, end_time=seg_end, language=sentence.language)
        if sub.duration > ALIGNER_MAX_DURATION + 1e-6:
            raise ValueError(
                f"超长句子块仍有 {sub.duration:.1f}s，超过上限；请手动拆分该句。"
            )
        # 子块边界是字符比例估算的临时切分（非用户句界），互为邻界锚传入。
        import core.align_engine as _ae
        ws = _ae.align_sentence(
            sub, audio_np, sample_rate,
            model_manager=model_manager, cfg=cfg,
            inferred_language_full=lang_full,
            prev_sentence_end=base, next_sentence_start=seg_end,
        )
        all_words.extend(ws)
        base = seg_end
    return all_words
