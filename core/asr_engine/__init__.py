"""core.asr_engine — 基于原生 transformers API 的 ASR + 时间戳转写引擎（包入口）

对外：
    transcribe(media_path, model_manager, cfg) → SubtitleProject

子模块：
    config     TranscribeConfig、语言短名互转、进度上报、时长上限
    splitting  切句文本层：标点判定、域名/缩写/小数点保护、硬切、按标点切分
    sentences  纯文本 / 带时间片段 → Sentence 列表（逐句语种判定、短句合并）
    pipeline   主流程：准备输入 → 取文本（本地 / 云端二选一）→ 强制对齐 → 分句

流程（原生 API）：
    1) 提取音频 → 16kHz mono WAV
    2) 取文本 → {language, transcription}
       本地 `_local_transcribe_text`（Qwen3-ASR-1.7B，需显存）
       云端 `_cloud_transcribe_text`（HTTP，见 core/cloud_asr/）
    3) 对齐（**总是执行**，否则句级没有准确时间戳）→ 字级时间戳
    4) 字级 → `_words_to_sentences` 按标点分句并合并短句
    5) 按 `cfg.return_word_timestamps` 决定是否保留 words
    6) 返回 SubtitleProject

备注：单次上限 1200s（20 分钟）。本工具不做超长自动切块（已明确否决该需求）：
      超过上限时 transcribe() 直接报错，指引先用 FFmpeg 把媒体切成 ≤20 分钟片段。

本 ``__init__`` **只做再导出，不放实现**。因此：
* ``from core.asr_engine import transcribe, TranscribeConfig`` 等旧路径一律照旧可用；
* 但 monkeypatch 要打**名字被查找的模块**——``transcribe`` 定义在 ``.pipeline``，
  它按 ``pipeline.__dict__`` 解析 ``prepare_audio`` / ``align_full_text``，
  所以补丁要打 ``core.asr_engine.pipeline``（见 AGENTS.md §6.1）。
"""

from __future__ import annotations

from ..text_utils import attach_words_to_sentences as _attach_words_to_sentences  # noqa: F401
from .config import (
    LOCAL_ASR_MODEL_ID,
    TranscribeConfig,
    _CJK_LANGS,
    _default_max_chars_for,
    _FULL_TO_SHORT,
    _full_to_short_lang,
    _is_video,
    _LATIN_SCRIPT_MAX_CHARS,
    _map_align_progress,
    _report,
    _resolve_language,
)
from .pipeline import (
    _cloud_transcribe_text,
    _local_transcribe_text,
    _prepare_asr_input,
    transcribe,
)
from .sentences import (
    _sentences_from_segments,
    _text_only_to_sentences,
    _words_to_sentences,
)
from .splitting import (
    _ABBREV_HINTS,
    _CJK_CHAR_RE,
    _CJK_TEXT_RE,
    _DOMAIN_CHAR_RE,
    _DOMAIN_TLD_HINTS,
    _hard_split,
    _HAS_ANY_PUNCT_RE,
    _has_any_punct,
    _is_cjk_dominant,
    _is_domain_dot,
    _LATIN_TEXT_RE,
    _next_word,
    _seg_chars,
    _seg_dur,
    _SENT_END_PUNCT_RE,
    _split_segment_overflow,
    _split_text_by_punct,
    _WEAK_PUNCT_RE,
    _WORD_CHAR_RE,
)

__all__ = [
    "TranscribeConfig",
    "transcribe",
    "LOCAL_ASR_MODEL_ID",
    # 测试 / 内部仍会直接引用（与 core.align_engine 的 __all__ 同例）：
    "_resolve_language",
    "_words_to_sentences",
    "_text_only_to_sentences",
    "_sentences_from_segments",
    "_split_text_by_punct",
    "_hard_split",
    "_is_domain_dot",
    "_prepare_asr_input",
    "_local_transcribe_text",
    "_cloud_transcribe_text",
    "_attach_words_to_sentences",
    "_default_max_chars_for",
    "_full_to_short_lang",
    "_map_align_progress",
    "_report",
    "_is_video",
    "_has_any_punct",
    "_is_cjk_dominant",
    "_seg_chars",
    "_seg_dur",
    "_split_segment_overflow",
    "_next_word",
    "_LATIN_SCRIPT_MAX_CHARS",
    "_CJK_LANGS",
    "_FULL_TO_SHORT",
    "_SENT_END_PUNCT_RE",
    "_WEAK_PUNCT_RE",
    "_HAS_ANY_PUNCT_RE",
    "_WORD_CHAR_RE",
    "_CJK_TEXT_RE",
    "_LATIN_TEXT_RE",
    "_DOMAIN_CHAR_RE",
    "_CJK_CHAR_RE",
    "_DOMAIN_TLD_HINTS",
    "_ABBREV_HINTS",
]
