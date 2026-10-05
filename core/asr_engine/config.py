"""core.asr_engine.config —— 转写配置与基础辅助（语言短名互转、进度上报、时长上限）。

``TranscribeConfig`` 是引擎唯一的对外配置对象，本地与云端两条路径共用它。
这里不放任何切句逻辑——那在 ``.splitting`` 与 ``.sentences`` 里。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..app_config import SegmentationPrefs
from ..language_utils import LANG_SHORT_TO_FULL as _LANG_SHORT_TO_FULL

if TYPE_CHECKING:  # 仅类型检查需要；运行时在云端分支内懒加载，本地路径零额外 import
    from ..cloud_asr import CloudASRConfig

logger = logging.getLogger(__name__)


#: 本地 ASR 模型的**官方 id**。仅用于查cloud_asr 的模型能力表（逐句语种补丁范围），
#: 不参与任何路径解析——本地实际权重目录由 model_manager / 设置页决定。
#: 与云端``Qwen/Qwen3-ASR-1.7B`` 同名：同一个模型在两条后端上能力一致，
#: 因此逐句语种补丁对本地与云端 Qwen3 同时生效。
LOCAL_ASR_MODEL_ID = "Qwen/Qwen3-ASR-1.7B"


# 短名 → 全名的 _LANG_SHORT_TO_FULL / _resolve_language 现移入 language_utils.py
# 本模块继续用带下划线的别名维持内部 API 不变


#: **非中日韩**语种的默认单句字符上限（2026-10-04）。
#:
#: 为什么需要：``TranscribeConfig.max_sentence_chars = 24`` 是按**汉字**定的中文字幕
#: 上限（注释里写明「通用上限 20~26」），而英文/西/法等语言按**空格分词**计量，
#: 同一阈值只有约 4 个单词。中英混说素材实测：修复语种判定后英文句虽被判为 English，
#: 仍被这个 24 字符阈值从 ``Englis|h`` 处劈开，碎片无意义。
#:
#: 取 84 的依据：英文字幕单句常见上限约 12~15 词≈70~90 字符，与中文 24 字
#: （约一屏一句）在**观感长度**上对齐。**中日韩不在表内**——它们的用户预期就是
#: 现有阈值，一字不变地保持原行为。
_LATIN_SCRIPT_MAX_CHARS = 84

#: 逐字型语言（中文系）沿用用户/全局配置，不在此表覆盖。
_CJK_LANGS = frozenset({"zh", "yue", "ja", "ko"})


def _default_max_chars_for(lang_full: str) -> int:
    """该语种的默认单句字符上限；未知语种 / 中日韩 → 0（表示用 cfg.max_*）。

    返回 0 而非一个数，是为了让 :func:`_text_only_to_sentences` 里的回退链
    保持「per_lang → 语言默认 → cfg.max_*」三级的单一入口，不在此处重复判断。
    """
    short = _full_to_short_lang(lang_full)
    if not short or short in _CJK_LANGS:
        return 0
    return _LATIN_SCRIPT_MAX_CHARS


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
    # 识别后端："local"（默认，本地 Qwen3-ASR-1.7B） | "cloud"（SiliconFlow HTTP，省显存）
    # 详见 core/cloud_asr/ —— 云端只负责出文本，字级时间戳仍由本地对齐器产出。
    asr_backend: str = "local"
    cloud_asr: Optional[CloudASRConfig] = None   # 仅云端模式使用；本地为 None


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
