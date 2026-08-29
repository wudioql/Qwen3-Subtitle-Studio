"""core.asr_engine — 基于原生 transformers API 的 ASR + 时间戳转写引擎

对外：
    transcribe(media_path, model_manager, cfg) → SubtitleProject

流程（原生 API）：
    1) 提取音频 → 16kHz mono WAV
    2) AutoModelForMultimodalLM.generate → processor.decode(return_format="parsed")
       → 得到 {language, transcription}
    3) 对齐（**总是执行**，否则句级没有准确时间戳）：
       pipeline 调独立 ForcedAligner → 字级时间戳
    4) 字级 → _words_to_sentences 按标点分句并合并短句
    5) 根据 cfg.return_word_timestamps 决定是否保留 words
       （False 时只清空 words 保留句级 start/end，True 时全部保留）
    6) 返回 SubtitleProject

备注：单次上限 1200s（20 分钟）。本工具不做超长自动切块（已明确否决该需求）：
      超过上限时 transcribe() 直接报错，指引先用 FFmpeg 把媒体切成 ≤20 分钟片段。
"""

from __future__ import annotations

import logging
import re as _re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

from subs.models import Sentence, SubtitleProject, WordTimestamp

from .app_config import SegmentationPrefs
from .audio_io import load_audio as _load_audio
from .audio_io import prepare_audio, probe_native_audio
from .align_engine import (
    AlignConfig,
    align_full_text,
)
from .constants import ASR_MAX_DURATION, DEFAULT_SAMPLE_RATE
from .language_utils import LANG_SHORT_TO_FULL as _LANG_SHORT_TO_FULL
from .model_manager import ModelManager
from .task_control import raise_if_cancelled
# text_utils 三个带下划线别名是本模块的再导出契约（tests 与外部按此路径引用，勿当未用 import 删）
from .text_utils import attach_words_to_sentences as _attach_words_to_sentences  # noqa: F401  # M7 共享实现
from .text_utils import merge_punct_into_words as _merge_punct_into_words        # noqa: F401  # M7 共享实现
from .text_utils import sanitize_word_timestamps as _sanitize_word_timestamps    # noqa: F401

logger = logging.getLogger(__name__)


# 短名 → 全名的 _LANG_SHORT_TO_FULL / _resolve_language 现移入 language_utils.py
# 本模块继续用带下划线的别名维持内部 API 不变


@dataclass
class TranscribeConfig:
    """ASR 转写配置 — 所有字段都允许手动调整，见 core/app_config.load_preferences/save_preferences 持久化。"""
    source_language: str = "auto"             # auto / zh / en / ja / ko ...
    # 是否**保留**字级时间戳（对齐总是会执行以获得准确的句级 start/end）
    #   True ：每个 Sentence.words 保留字级（卡拉OK/字幕特效用）
    #   False：对齐后丢弃字级，只保留句级 start/end（内存更小，纯句级字幕场景）
    return_word_timestamps: bool = True
    context: str = ""                         # 热词/背景（传给 apply_transcription_request 的 prompt）
    max_new_tokens: int = 512                 # generate 上限
    use_cache: bool = True                    # generate 的 kv cache（生成用，省显存关）
    # 输出句级相关
    fallback_min_sentence_sec: float = 2.0
    fallback_max_sentence_sec: float = 15.0
    # 分句参数 —— 三段式：① 按标点切分 → ② 短句合并(min_*) → ③ 超长硬切(max_*)
    min_sentence_chars: int = 4               # 合并时最小字数（小于就并到下一句）
    min_sentence_sec: float = 0.3             # 合并时最小时长（秒，小于就并到下一句）
    max_sentence_chars: int = 24              # 硬切单句最大字数（中文字幕通用上限 20~26；设 0 关闭此限制）
    max_sentence_sec: float = 8.0             # 硬切单句最大时长（秒；设 0 关闭此限制）
    align_backend: str = "qwen"               # "qwen" (Qwen3-Aligner) | "mms" (MMS-300M-FA-ONNX 歌词长拖音)
    # 对齐参数（仅当 return_word_timestamps=True 时生效）
    align_pad_before: float = 0.12   # 与 align_pad_after 对称（声学上下文，产出钳回句界）
    align_pad_after: float = 0.12
    # 进度
    progress_cb: Optional[Callable[[int, int, str], None]] = None
    cancel_cb: Optional[Callable[[], bool]] = None  # Worker 合作式取消；不持久化
    # Phase 4 v3: 分句偏好（按语言覆盖 max_*；None 或 enabled=False → 不限制）
    seg_prefs: Optional[SegmentationPrefs] = None


def _resolve_language(short: str) -> Optional[str]:
    """轻包装：language_utils.resolve_language + 本模块 logger 告警。"""
    s = (short or "auto").lower()
    if s not in _LANG_SHORT_TO_FULL:
        logger.warning("[ASR] 未知语言 %s，视为 auto", short)
        return None
    return _LANG_SHORT_TO_FULL[s]


# 全名（如 "Chinese"）→ 短码（如 "zh"）反查表，供 SegmentationPrefs（per_lang 键用短码）查询
_FULL_TO_SHORT = {v.lower(): k for k, v in _LANG_SHORT_TO_FULL.items() if v is not None}


def _full_to_short_lang(lang: str) -> str:
    """把语言「全名或短码」归一化为短码（如 "Chinese"→"zh"、"zh"→"zh"）。

    若识别不出则原样返回（调用方据此走全局 cfg.max_* 兜底）。
    """
    if not lang:
        return lang or ""
    s = str(lang).strip()
    # 已经是短码
    if s.lower() in _LANG_SHORT_TO_FULL:
        return s.lower()
    # 全名反查短码
    return _FULL_TO_SHORT.get(s.lower(), s)


def _map_align_progress(done: int, total: int, lo: float, hi: float) -> float:
    """把对齐子阶段的进度 (done,total) 线性映射到 [lo, hi]（H4 修复，防止 done 超过总步数）。"""
    if total <= 0:
        return lo
    frac = max(0.0, min(1.0, done / total))
    return lo + frac * (hi - lo)


def _report(cfg: TranscribeConfig, done: int, total: int, desc: str) -> None:
    if cfg.progress_cb is not None:
        try:
            cfg.progress_cb(done, total, desc)
        except Exception:
            logger.exception("[ASR] 进度回调异常")


def _is_video(p: Path) -> bool:
    from subs.media_types import VIDEO_SUFFIXES
    return p.suffix.lower() in VIDEO_SUFFIXES


# ═══════════════════════════════════════════════════════════════
# 纯逻辑辅助：text-only + 文本语言短名 → Sentence 列表
# ═══════════════════════════════════════════════════════════════

# Qwen3-ASR 原生输出会带中英日韩全套标点（句末强标点 + 句中弱标点 + 空白），所以这一组 regex 越全越好
# 强句末：必须切分（中文/日韩的 。！？!？;；\n + 英文 !?）
#   英文 . 不放强句末（缩写 e.g. / U.S. 会被误切）→ 放弱句中按 follow 切
_SENT_END_PUNCT_RE = _re.compile(r"([。！？!?；;\n])")
# 弱句中：可能切分（**仅逗号 / 冒号 / 句号 / 叹号 / 问号 / 分号**——避免破坏列表项并列）
# 顿号「、」永远是词内并列，**绝不切**。
_WEAK_PUNCT_RE = _re.compile(r"([，,:：;.。！!?；;])")
# 「任意可切分标点」 → ASR 文本里能识别的标点
_HAS_ANY_PUNCT_RE = _re.compile(r"[，。！？!?；;：:,\.\s]")


def _has_any_punct(text: str) -> bool:
    """任意可切分标点（句中 + 句末 + 空白）都算「有标点」→ 优先走 ASR 标点切分。"""
    return bool(text) and bool(_HAS_ANY_PUNCT_RE.search(text))


def _seg_chars(seg: list[WordTimestamp]) -> int:
    return sum(len(w.text) for w in seg)


def _seg_dur(seg: list[WordTimestamp]) -> float:
    if not seg:
        return 0.0
    return seg[-1].end_time - seg[0].start_time


def _split_segment_overflow(
    seg: list[WordTimestamp],
    *,
    max_chars: int,
    max_sec: float,
) -> list[list[WordTimestamp]]:
    """把一个 word segment 按 max_chars / max_sec 切成若干不溢出的子段（字级边界安全切）。

    规则：
      - max_chars > 0：单段字数不超过 max_chars
      - max_sec > 0：单段时长不超过 max_sec
      - 两个条件**任一超限就切**；都=0 不切
      - 边界永远切在两个 word 之间（不会把一个字/词切成两半）
    """
    if not seg:
        return []
    if (max_chars <= 0 or _seg_chars(seg) <= max_chars) and (
        max_sec <= 0 or _seg_dur(seg) <= max_sec
    ):
        return [list(seg)]

    out: list[list[WordTimestamp]] = []
    cur: list[WordTimestamp] = []
    cur_chars = 0
    cur_start = 0.0
    first = True
    for w in seg:
        # 评估：把 w 放进 cur 后是否会超限
        next_chars = cur_chars + len(w.text)
        next_start = cur_start if cur else w.start_time
        next_end = w.end_time
        next_dur = next_end - next_start
        chars_ok = (max_chars <= 0) or (next_chars <= max_chars)
        dur_ok = (max_sec <= 0) or (next_dur <= max_sec)
        # cur 非空 + (加了 w 超限) → 先把 cur 收走
        if (not first) and (not chars_ok or not dur_ok):
            out.append(list(cur))
            cur = [w]
            cur_chars = len(w.text)
            cur_start = w.start_time
        else:
            cur.append(w)
            cur_chars = next_chars
            if first:
                cur_start = w.start_time
                first = False
    if cur:
        out.append(list(cur))
    return out


# ─────────────────────────────────────────────────────────────
# 标点切分 + 标点 word 注入（核心修复）
# ─────────────────────────────────────────────────────────────

def _split_text_by_punct(
    text: str,
    *,
    min_chars_per_split: int = 4,
) -> list[str]:
    """把 ASR 原始带标点文本切句，保留标点本身。

    切分规则（按优先级）：
      1) 强句末标点「。！？!？;；\\n」 → 必切
      2) 弱标点「，,、：:」+ 后随 ≥ min_chars_per_split 个非标点字符 → 切（避免「经济、新闻」被劈开）
      3) 没有标点 → 原样整段返回

    Returns:
        List[str]，每个元素是一段含尾部标点的完整子句，例如：
            '青紫色的风掠过指尖，'  '金线牡丹在呼吸间流转，'  '墨香未干，'  '茶烟已绕过雕花窗。'
    """
    if not text:
        return []
    # 一次性扫描：分两类标点（强/弱），记录位置 + 类型
    cuts: list[tuple[int, str]] = []  # (位置之后, 切分标点字符)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if _SENT_END_PUNCT_RE.fullmatch(ch):
            cuts.append((i + 1, ch))
            i += 1
            continue
        if _WEAK_PUNCT_RE.fullmatch(ch):
            # 弱标点：统计后续非标点字符数（空格跳过但不中断计数——支持英文 `,` 切句）
            j = i + 1
            follow = 0
            while j < n and not _WEAK_PUNCT_RE.fullmatch(text[j]):
                if text[j] not in " \t":
                    follow += 1
                j += 1
            if follow >= max(2, min_chars_per_split):
                cuts.append((i + 1, ch))
            i += 1
            continue
        i += 1

    if not cuts:
        return [text]

    # 边界去重（连续多个标点只取第一个的右边界作为切点）
    boundaries: list[int] = [0]
    last = 0
    for pos, _ in cuts:
        if pos > last:
            boundaries.append(pos)
            last = pos
    if last < n:
        boundaries.append(n)

    parts: list[str] = []
    for a, b in zip(boundaries, boundaries[1:]):
        seg = text[a:b]
        if seg.strip():
            parts.append(seg)
    return parts


def _text_only_to_sentences(
    text: str,
    *,
    total_sec: float,
    cfg: TranscribeConfig,
    project_language: str,
) -> List[Sentence]:
    """带/不带标点通用分句：
        - 有标点：按强标点切句 → 短句**不合并**（保留 ASR 原始切分）；再做 max_* 硬切兜底
        - 无标点：按 max_chars / max_sec 硬切（自动按字边界切），字符权重分配时间

    短句合并（min_*）仅在「无标点兜底路径」里使用；带标点时 **尊重 ASR 切句**，
    因为 ASR 的标点位置本身已经体现了「说话停顿 / 句子边界」，硬合并会破坏语义。
    """
    if not text:
        return []

    has_punct = _has_any_punct(text)
    min_c = max(0, int(cfg.min_sentence_chars))

    # Phase 4 v3: per-lang 上限覆盖（SegmentationPrefs.enabled 且有该语言条目 → 用 per-lang；
    # 否则 fallback cfg.max_*。两条都 0 → 不限制）
    # 注意：project_language 是「全名」（如 "Chinese"），而 SegmentationPrefs.per_lang 的 key
    # 是「短码」（如 "zh"），必须先反查成短码再查询，否则永远匹配不上（修复前 per-lang 失效）。
    short_lang = _full_to_short_lang(project_language)
    per_lang_c, per_lang_s = 0, 0.0
    if cfg.seg_prefs is not None and cfg.seg_prefs.enabled:
        per_lang_c, per_lang_s = cfg.seg_prefs.get_limits(short_lang)
    max_c = per_lang_c if per_lang_c > 0 else max(0, int(cfg.max_sentence_chars))
    max_s = per_lang_s if per_lang_s > 0 else max(0.0, float(cfg.max_sentence_sec))

    if has_punct:
        # === 带标点路径：尊重 ASR 切句，min_* 短句合并禁用 ===
        # min_chars_per_split 至少 3，避免「经济、新闻」这种 2~3 字并列被强行切开
        parts = _split_text_by_punct(text, min_chars_per_split=max(3, min_c))
        # 仅做 max_* 硬切兜底（ASR 偶尔给出一整段很长的无强标点子句）
        hard_cut: list[str] = []
        for p in parts:
            if max_c > 0 and len(p) > max_c:
                for i in range(0, len(p), max_c):
                    hard_cut.append(p[i:i + max_c])
            else:
                hard_cut.append(p)
    else:
        # === 无标点路径：整段只有一段，「短句合并」无多段可并（原实现是死代码），只做 max_* 硬切 ===
        hard_cut = [text]
        if max_c > 0 and len(text) > max_c:
            hard_cut = [text[i:i + max_c] for i in range(0, len(text), max_c)]

    # 时间分配：按字符数占比分时长；单段超过 max_sec 就 clamp 到 max_sec；剩余顺延
    total_chars = sum(len(p) for p in hard_cut) or 1
    t = 0.0
    sents: list[Sentence] = []
    remain = total_sec
    n = len(hard_cut)
    for idx, p in enumerate(hard_cut):
        frac = len(p) / total_chars
        ideal_dur = frac * total_sec
        if max_s > 0:
            ideal_dur = min(ideal_dur, max_s)
        # fallback min/max 兜底（仅在「无标点路径」生效；带标点路径通常 ASR 已分好句子）
        if not has_punct:
            ideal_dur = max(cfg.fallback_min_sentence_sec, ideal_dur)
            if cfg.fallback_max_sentence_sec > 0:
                ideal_dur = min(cfg.fallback_max_sentence_sec, ideal_dur)
        # 最后一段吃掉剩余
        if idx == n - 1:
            et = total_sec
        else:
            et = min(total_sec, t + ideal_dur)
            et = min(et, t + max(0.0, remain))
        if et <= t:
            et = t + 0.05
        sents.append(Sentence(
            text=p, start_time=round(t, 3), end_time=round(et, 3),
            words=[], language=project_language,
        ))
        remain -= (et - t)
        t = et
    return sents


def _words_to_sentences(
    words: List[WordTimestamp],
    *,
    cfg: TranscribeConfig,
    project_language: str,
) -> List[Sentence]:
    """字级 → 句级：三段式
        1) 按句末标点切分（仅对 word.text 里有标点的）
        2) 短句按 min_sentence_chars / min_sentence_sec 合并
        3) 超长段按 max_sentence_chars / max_sentence_sec 硬切（字级边界安全）

    主要用于「Aligner 单独产出 + 标点已被 _merge_punct_into_words 插回」的情况；
    纯英日韩无标点时退化为「按 max_* 硬切」的行为。
    """
    if not words:
        return []
    max_c = max(0, int(cfg.max_sentence_chars))
    max_s = max(0.0, float(cfg.max_sentence_sec))
    min_c = max(0, int(cfg.min_sentence_chars))
    min_s = max(0.0, float(cfg.min_sentence_sec))

    # Stage 1: 按标点切
    cut_indices: List[int] = []
    for i, w in enumerate(words):
        if _SENT_END_PUNCT_RE.search(w.text):
            cut_indices.append(i)
    if not cut_indices or cut_indices[-1] != len(words) - 1:
        cut_indices.append(len(words) - 1)

    raw: List[List[WordTimestamp]] = []
    prev = 0
    for end in cut_indices:
        raw.append(words[prev:end + 1])
        prev = end + 1

    # Stage 2: 短句合并
    merged: List[List[WordTimestamp]] = []
    buf: List[WordTimestamp] = []
    buf_chars = 0
    buf_dur = 0.0

    def flush() -> None:
        nonlocal buf, buf_chars, buf_dur
        if buf:
            merged.append(list(buf))
        buf = []
        buf_chars = 0
        buf_dur = 0.0

    for seg in raw:
        seg_chars = _seg_chars(seg)
        seg_dur = _seg_dur(seg)
        if not buf:
            buf = list(seg)
            buf_chars = seg_chars
            buf_dur = seg_dur
        else:
            too_short = (buf_chars < min_c) or (buf_dur < min_s)
            if too_short:
                buf.extend(seg)
                buf_chars += seg_chars
                buf_dur = buf[-1].end_time - buf[0].start_time
            else:
                flush()
                buf = list(seg)
                buf_chars = seg_chars
                buf_dur = seg_dur
    flush()

    # Stage 3: 超长硬切（字级边界）
    final: list[list[WordTimestamp]] = []
    for seg in merged:
        final.extend(_split_segment_overflow(seg, max_chars=max_c, max_sec=max_s))

    sents: List[Sentence] = []
    for seg in final:
        text = "".join(w.text for w in seg)
        sents.append(Sentence(
            text=text,
            start_time=round(seg[0].start_time, 3),
            end_time=round(seg[-1].end_time, 3),
            words=list(seg),
            language=project_language,
        ))
    return sents


# ─────────────────────────────────────────────────────────────
# ASR 标点切句 + Aligner 字级对齐 + 标点 word 插回 → 最终 SubtitleProject
# ─────────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════
# 对外主接口
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
    _report(cfg, 1, total_steps, "激活 ASR 模型...")

    # 2) ASR：原生 generate + decode(parsed)
    raise_if_cancelled(cfg.cancel_cb)
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
        raw_text = (parsed.get("transcription") or "").strip()
        raw_lang = parsed.get("language") or ""

    # after with: asr park → RAM；单次 generate 不可安全强杀，取消在此安全点生效。
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
    sentences = _text_only_to_sentences(
        raw_text, total_sec=total_sec, cfg=cfg, project_language=first_lang_full,
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
    align_cfg = AlignConfig(
        source_language=cfg.source_language,
        align_backend=cfg.align_backend,
        pad_before=cfg.align_pad_before,
        pad_after=cfg.align_pad_after,
        progress_cb=lambda d, t, x: _report(cfg, round(_map_align_progress(d, t, 3.0, float(total_steps))), total_steps, x) if t > 0 else None,
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
