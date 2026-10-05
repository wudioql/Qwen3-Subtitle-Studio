"""core.asr_engine.pipeline —— 主流程：准备输入 → 取文本 → 强制对齐 → 分句。

对外只有 :func:`transcribe`。取文本那一步二分：
    本地 ``_local_transcribe_text``（Qwen3-ASR-1.7B，需显存）
    云端 ``_cloud_transcribe_text``（HTTP，见 core/cloud_asr/）
**两条路都只产出纯文本**，字级时间戳一律由本地强制对齐器给出。

⚠️ 本模块按自身 globals 解析 ``prepare_audio`` / ``align_full_text``，所以打补丁要打
``core.asr_engine.pipeline``，不是包入口（包入口只是再导出，见 AGENTS.md §6.1）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from subs.models import SubtitleProject

from ..align_engine import AlignConfig, align_full_text
from ..audio_io import load_audio as _load_audio
from ..audio_io import prepare_audio, probe_native_audio
from ..cloud_asr import split_zh_en_sentence_language, supports_zh_en_sentence_split
from ..constants import ASR_MAX_DURATION, DEFAULT_SAMPLE_RATE
from ..language_utils import LANG_SHORT_TO_FULL as _LANG_SHORT_TO_FULL
from ..model_manager import ModelManager
from ..task_control import raise_if_cancelled
from .config import (
    LOCAL_ASR_MODEL_ID,
    TranscribeConfig,
    _is_video,
    _map_align_progress,
    _report,
    _resolve_language,
)
from .sentences import _text_only_to_sentences

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 主流程：准备输入 → 取文本（本地 / 云端二选一）→ 强制对齐 → 分句
# ═══════════════════════════════════════════════════════════════


def _prepare_asr_input(media_path: Path) -> tuple[np.ndarray | None, Path, float]:
    """WAV 零复制：soundfile 直读媒体（wav/flac/ogg/aiff/mp3…）不
    让 FFmpeg 向 .temp/ 落 16k 副本——内存重采样到 16k mono 后直接把 numpy
    交给 Qwen3ASRProcessor（apply_transcription_request 原生接受 ndarray）。
    容器类/非常规格式（mp4/mkv/m4a/aac…）probe 返回 None → 走带缓存名的
    prepare_audio 提取（同媒体重复运行命中 .temp 缓存，不再每次新建 uuid 副本）。

    返回 (audio_np 或 None, wav_path 供 project.audio_path 记录, 时长秒)。
    """
    native = probe_native_audio(media_path)
    if native is not None:
        audio_np, sr = _load_audio(media_path, mono=True, target_sr=DEFAULT_SAMPLE_RATE)
        if sr != DEFAULT_SAMPLE_RATE:  # 双保险：重采样合同不成立则回退提取
            logger.warning("[ASR] 内存重采样未达 %dHz，回退 FFmpeg 提取", DEFAULT_SAMPLE_RATE)
        else:
            logger.info(
                "[ASR] 原生可读媒体直读（零 .temp 副本）: %s, %.2fs, %dHz→%dHz(内存)",
                media_path, native.duration, native.sample_rate, sr,
            )
            return audio_np, media_path, float(native.duration)
    wav_path, info = prepare_audio(media_path, sample_rate=DEFAULT_SAMPLE_RATE)
    logger.info("[ASR] 音频提取完成: %s, %.2fs, %dHz",
                wav_path, info.duration, info.sample_rate)
    return None, wav_path, float(info.duration)


def _local_transcribe_text(
    audio_in: Optional[np.ndarray],
    wav_path: Path,
    *,
    cfg: TranscribeConfig,
    model_manager: ModelManager,
    lang_full: Optional[str],
) -> tuple[str, str]:
    """本地 Qwen3-ASR 路径：把 1.7B 激活进显存并 generate 出文本。

    返回 ``(raw_text, raw_lang, segments)``。这段逻辑是从 ``transcribe`` 第 2 步原样搬来的，
    抽成函数只是为了给云端分支让位——**不要往这里塞任何云端判断**。

    第三项恒为空列表：本地走的是纯 generate，**拿不到服务端那种句级时间戳**
    （实测云端 Qwen3-ASR 同样只返回单个 text + 单一 language）。
    保留这个位置是为了让两条路径的返回形状一致，主流程不必分支。
    """
    with model_manager.using_asr(progress_cb=cfg.progress_cb) as (asr_proc, asr_model):
        _report(cfg, 0, 0, "ASR 推理：正在生成文本（耗时取决于音频长度）…")
        asr_kwargs: dict = {}
        if lang_full is not None:
            asr_kwargs["language"] = lang_full
        if cfg.context:
            asr_kwargs["prompt"] = cfg.context
        inputs = asr_proc.apply_transcription_request(
            audio=audio_in if audio_in is not None else str(wav_path),
            **asr_kwargs,
        ).to(asr_model.device, asr_model.dtype)

        import torch  # 懒加载：仅真正跑 ASR 推理时才需要
        with torch.inference_mode():
            out_ids = asr_model.generate(
                **inputs,
                max_new_tokens=cfg.max_new_tokens,
                use_cache=cfg.use_cache,
            )
        gen_ids = out_ids[:, inputs["input_ids"].shape[1]:]
        parsed = asr_proc.decode(gen_ids, return_format="parsed")[0]
        return (
            (parsed.get("transcription") or "").strip(),
            parsed.get("language") or "",
            [],
        )


def _cloud_transcribe_text(
    audio_in: Optional[np.ndarray],
    wav_path: Path,
    cfg: TranscribeConfig,
) -> tuple[str, str, list]:
    """云端 SiliconFlow 路径：HTTP 上传拿文本，**全程不加载本地 ASR 权重**。

    与本地路径的合同一致：产出 ``(纯文本, 语言全名, 服务端句级片段)``。本地给
    ``"Chinese"``，云端 :func:`core.cloud_asr.normalize_language` 也产出 ``"Chinese"``，
    因此下游的分句 / 语言反查 / 强制对齐一行都不必改。字级时间戳仍由本地对齐器负责
    （云端实测无任何模型提供字级时间戳，``verbose_json`` 直接返回 400）。

    第三个返回值是**可选**的句级片段列表，只有 ``XingChenASR-Diarize`` 系列会填
    （其它模型返回单个大 text，列表为空）。它自带 start/end 且已按语句切好，
    主流程会优先采用；空列表时行为与从前完全一致。

    Raises:
        ``core.cloud_asr.CloudASRError`` 的子类（401 无 Key / 402 余额不足 /
        429 限流 / 400-404 参数或模型名问题 / 网络异常）。按产品决策
        **不自动回落本地**——静默回落会让用户在毫无察觉的情况下重新吃满显存，
        这与「用云端就是为了省显存」的目标直接矛盾。

    Side effects:
        把观测到的平台用量写回 ``cfg.cloud_asr.observed_usage_seconds``，
        供调用方累加本地台账。官方没有可编程的用量查询接口，只能客户端自己记。
    """
    from ..cloud_asr import CloudASRConfig, transcribe_cloud  # 懒加载：本地路径不需要

    cloud_cfg = cfg.cloud_asr or CloudASRConfig()
    # 多数云端模型不返回 language，此时靠用户在工具栏显式指定的语言兜底；
    # 两者皆无时 cloud_asr 会抛出可操作的 CloudASRLanguageError（而不是等到对齐阶段才炸）。
    cloud_cfg.source_language = cfg.source_language or "auto"

    result = transcribe_cloud(
        audio_in if audio_in is not None else wav_path,
        cfg=cloud_cfg,
        sample_rate=DEFAULT_SAMPLE_RATE,
        cancel_cb=cfg.cancel_cb,
        progress_cb=cfg.progress_cb,
    )
    cloud_cfg.observed_usage_seconds = result.usage_seconds
    if cfg.cloud_asr is None:
        cfg.cloud_asr = cloud_cfg          # 让调用方能读回用量台账
    logger.info(
        "[ASR] 云端返回：%d 字 / lang=%r / usage=%.2fs / segments=%d / trace=%s",
        len(result.text), result.language, result.usage_seconds,
        len(result.segments), result.trace_id or "-",
    )
    # segments 只有 Diarize 系列会给，但它自带 start/end 且**已按说话人/语句切好**，
    # 比标点切句准得多（实测中文 3 段 + 英文 1 段，误差 <0.1s）。带回给主流程。
    return result.text, result.language, result.segments


def transcribe(
    media_path: str | Path,
    *,
    model_manager: ModelManager,
    cfg: Optional[TranscribeConfig] = None,
    source_media_path: str | Path | None = None,
) -> SubtitleProject:
    """对任意音/视频文件执行 ASR → 可选对齐 → SubtitleProject。"""
    if cfg is None:
        cfg = TranscribeConfig()
    raise_if_cancelled(cfg.cancel_cb)

    total_steps = 4  # 提取音频 → 激活 ASR → 推理 → 对齐
    _report(cfg, 0, total_steps, "提取音频并重采样...")

    lang_full = _resolve_language(cfg.source_language)
    media_path = Path(media_path)
    logger.info("[ASR] 开始处理: %s (short_lang=%s force_lang=%r keep_words=%s)",
                media_path, cfg.source_language, lang_full, cfg.return_word_timestamps)

    # 1) 准备 16kHz mono 输入（直读媒体零 .temp 副本，详见 _prepare_asr_input）
    audio_in, wav_path, total_sec = _prepare_asr_input(media_path)
    raise_if_cancelled(cfg.cancel_cb)

    # 长音频守卫：Qwen3-ASR 单次上限 1200s（20 分钟）。本工具不做超长自动切块
    # （已明确否决该需求，避免切块拼接带来的低质量结果），超长请先用 FFmpeg 外部切片。
    if total_sec > ASR_MAX_DURATION:
        raise ValueError(
            f"音频时长 {total_sec:.1f}s 超过 Qwen3-ASR 单次上限 {int(ASR_MAX_DURATION)}s"
            f"（{int(ASR_MAX_DURATION/60)} 分钟）。本工具不提供超长自动切块，"
            "请先用 FFmpeg 把媒体切成 ≤ 20 分钟的片段后再识别。"
        )
    # 2) ASR 按后端分流。
    #
    #    云端模式的**要害**是完全不碰 model_manager：一旦踏进 using_asr() 上下文，
    #    本地 1.7B 就已经被搬进显存，省显存的目标当场作废。所以必须是整块跳过，
    #    而不是进到上下文里再判断走哪条路。
    if cfg.asr_backend == "cloud":
        _report(cfg, 1, total_steps, "上传音频到云端 ASR（本地模型不加载）…")
        raise_if_cancelled(cfg.cancel_cb)
        raw_text, raw_lang, raw_segments = _cloud_transcribe_text(audio_in, wav_path, cfg)
    else:
        _report(cfg, 1, total_steps, "激活 ASR 模型...")
        raise_if_cancelled(cfg.cancel_cb)
        raw_text, raw_lang, raw_segments = _local_transcribe_text(
            audio_in, wav_path, cfg=cfg, model_manager=model_manager, lang_full=lang_full,
        )

    # after ASR: 本地 asr park → RAM；单次 generate 不可安全强杀，取消在此安全点生效。
    raise_if_cancelled(cfg.cancel_cb)
    _report(cfg, 2, total_steps, "ASR 推理完成，准备时间对齐…")
    logger.info("[ASR] 文本结果：len=%d language=%r", len(raw_text), raw_lang)

    # 3) 对外语言短名
    first_lang_full = str(raw_lang).split(",")[0]
    _rev_map = {v: k for k, v in _LANG_SHORT_TO_FULL.items() if v is not None}
    src_short = (
        _rev_map.get(first_lang_full)
        or (cfg.source_language if cfg.source_language != "auto" else (first_lang_full or "auto"))
    )

    # 语言域 = Qwen3-ForcedAligner 官方 11 种。检测到范围外语言（如 Thai）
    # 时及早报清晰错误——否则会在对齐阶段才以「无法推断项目语言」的谜面炸掉。
    # （支持的检出语必能被 _rev_map 反查为短码；查不到即范围外，不再走 resolve 以免误发告警日志）
    if src_short != "auto" and src_short.lower() not in _LANG_SHORT_TO_FULL:
        raise ValueError(
            f"ASR 检测到语言 {first_lang_full!r}，不在本工具支持的 11 种语言内"
            "（中/英/粤/法/德/意/日/韩/葡/俄/西）：\n"
            "识别模型能转写它，但对齐器无法为其产出字级时间戳。\n"
            "请改用支持语言的媒体，或保留 auto 以外的识别语言重试。"
        )

    # 4) 构造句级占位：以「ASR 标点切句」为第一真源（保留完整标点 + 合理句子边界）
    #    时间用字符权重占位 → 对齐后用 aligner 字级精确时间覆盖
    #
    # 逐句语种判定：**本地与云端都要开**。中英混说素材里 ASR 只能返回一个 language
    # （2026-10-04 本地 Qwen3 实测 26.66s 混合素材 → language='Chinese'，含 12s 纯英文），
    # 不逐句判定则英文句会被当中文送去中文对齐器。这是模型层面的**已知硬性限制**，
    # 本函数只做定点缓解：整段为中/粤时切出纯英文句，其余原样沿用整段语种
    # （保护粤语不被误标成中文普通话）。其它语言的混入**不做判定**，见
    # cloud_asr.split_zh_en_sentence_language 的适用范围说明。
    #
    # 适用范围刻意只含 Qwen3-ASR（本地默认 + 云端同 id）与 SenseVoiceSmall：
    # 依据是这两个模型实测能正确转写英文原文，而非「理论上能推出什么」。
    # 云端非适用模型仍走原逻辑（Diarize 的 segments 自带 language，另走它那条路）。
    asr_model_id = (
        cfg.cloud_asr.model if (cfg.asr_backend == "cloud" and cfg.cloud_asr is not None)
        else LOCAL_ASR_MODEL_ID
    )
    per_sentence_lang: Optional[Callable[[str], Optional[str]]] = None
    if supports_zh_en_sentence_split(asr_model_id):
        per_sentence_lang = lambda s: split_zh_en_sentence_language(  # noqa: E731
            s, first_lang_full, asr_model_id,
        )
    sentences = _text_only_to_sentences(
        raw_text, total_sec=total_sec, cfg=cfg, project_language=first_lang_full,
        sentence_language_fn=per_sentence_lang,
        segments=raw_segments,
        glue_model_id=asr_model_id,
    )
    source_path = Path(source_media_path) if source_media_path is not None else media_path
    project = SubtitleProject(
        source_media_path=str(source_path),
        audio_path=str(wav_path),
        video_path=str(source_path) if _is_video(source_path) else None,
        media_duration=total_sec,
        sample_rate=DEFAULT_SAMPLE_RATE,
        source_language=src_short,
        sentences=sentences,
    )

    # 5) 对齐（总是执行）：采用全局连续对齐获得全局一致的精确字级与句级时间戳
    raise_if_cancelled(cfg.cancel_cb)
    _report(cfg, 3, total_steps, "激活对齐器...")
    def _relay_align_progress(done: int, total: int, desc: str) -> None:
        """把对齐器的进度转发到 ASR 的四步刻度上。

        ``total <= 0`` 是**不确定进度**（对齐模型 park→RAM / 权重加载等，耗时不可知）。
        此前这里写成 ``... if t > 0 else None``，把这一整类进度**整个丢掉**了：
        UI 会一直停在「激活对齐器...」上不动，而这段恰是全链路里最长的等待之一
        ——用户看到的是「卡住」，实际是「有信息但没上报」。

        修法：不确定进度原样转发文案（``total=0``，UI 据此转忙碌条 + 已用时）；
        可确定进度才线性映射到第 3→4 步。**绝不**把不可知耗时编成百分比
        （AGENTS.md §3「禁止伪造百分比」）。
        """
        if total <= 0:
            _report(cfg, 0, 0, desc)
            return
        _report(cfg, round(_map_align_progress(done, total, 3.0, float(total_steps))),
                total_steps, desc)

    align_cfg = AlignConfig(
        source_language=cfg.source_language,
        align_backend=cfg.align_backend,
        pad_before=cfg.align_pad_before,
        pad_after=cfg.align_pad_after,
        progress_cb=_relay_align_progress,
        cancel_cb=cfg.cancel_cb,
    )
    project = align_full_text(project, model_manager=model_manager, cfg=align_cfg)
    raise_if_cancelled(cfg.cancel_cb)

    # 6) 如果不要字级，清空 words 只保留句级 start/end
    if not cfg.return_word_timestamps:
        for s in project.sentences:
            s.words = []

    project.sort()
    _report(cfg, total_steps, total_steps, "完成")
    logger.info(
        "[ASR] 完成：language=%s text_len=%d sentences=%d word_level=%s",
        src_short, len(raw_text), len(project.sentences),
        any(s.has_word_level() for s in project.sentences),
    )
    return project
