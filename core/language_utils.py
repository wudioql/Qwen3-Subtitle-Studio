"""core.language_utils — 通用语言名映射工具（避免 asr ↔ align 循环导入）。

语言域 = **Qwen3-ForcedAligner 官方支持的 11 种**（transformers
`FORCED_ALIGNER_LANGUAGES`：Chinese/English/Cantonese/French/German/Italian/
Japanese/Korean/Portuguese/Russian/Spanish）。ASR 模型虽能识别更多语言，
但对齐器无法为它们产出字级时间戳，项目统一只承诺这 11 种（tests 钉样与上游同步）。
"""

from __future__ import annotations

from typing import Optional


# 短名 → 原生 API 用的全名（和原生 ASR apply_transcription_request / forced aligner 的 language 参数一致）
LANG_SHORT_TO_FULL: dict[str, Optional[str]] = {
    "auto": None,
    "zh":  "Chinese",   "en": "English",    "yue": "Cantonese",
    "fr":  "French",    "de": "German",     "it":  "Italian",
    "ja":  "Japanese",  "ko": "Korean",     "pt":  "Portuguese",
    "ru":  "Russian",   "es": "Spanish",
}


def resolve_language(short: str) -> Optional[str]:
    s = (short or "auto").lower()
    if s not in LANG_SHORT_TO_FULL:
        # 不直接 log — 调用方决定是否 warn（各模块 logger 名不同）
        return None
    return LANG_SHORT_TO_FULL[s]


# ══════════════════════════════════════════════════════════════════
# Qwen3-ForcedAligner 分词器族
#
# 官方 `Qwen3ASRProcessor.split_words_for_alignment` **只按 language 分支**：
#   "japanese" → nagisa 形态切分；"korean" → soynlp；
#   **其余全部（含 None / Chinese / English / French…）走同一条
#   「CJK 逐字 + 空格词」默认路径**，输出逐字等价
#   （实测 `今天我们来聊聊 attention 机制` 在 None 与 Chinese 下完全相同）。
#
# 所以「按语言分段」是过度的：真正需要分开调用的只有 ja / ko 与其它语言同段
# 混排的情况（同一段里传 Japanese 会把中文切坏，传 Chinese 会让日语退化）。
# 按**分词器族**分组即可——同族语言合并成一次调用不改变任何分词结果，
# 中/粤/英混排因此收敛成 1 段（零裁剪），行为与单语项目一致。
# ══════════════════════════════════════════════════════════════════

DEFAULT_TOKENIZER_FAMILY = "default"

_TOKENIZER_FAMILY_BY_LANG: dict[str, str] = {
    "Japanese": "ja",
    "Korean": "ko",
}

# "default" 族的代表语言：驱动 Qwen 默认分词路径；固定取一个合法的
# FORCED_ALIGNER_LANGUAGES 值即可（该族内所有语言对分词器等价）。
_DEFAULT_FAMILY_REPRESENTATIVE = "Chinese"


def tokenizer_family(language_full: str) -> str:
    """语言全名 → Qwen 分词器族（``"ja"`` / ``"ko"`` / ``"default"``）。

    未知/空值一律归 ``"default"``（与 Qwen 默认分词路径一致）。
    """
    return _TOKENIZER_FAMILY_BY_LANG.get(
        (language_full or "").strip(), DEFAULT_TOKENIZER_FAMILY
    )


def default_family_representative() -> str:
    """``"default"`` 族用来驱动 Qwen 分词器的代表语言全名（``"Chinese"``）。"""
    return _DEFAULT_FAMILY_REPRESENTATIVE


__all__ = [
    "LANG_SHORT_TO_FULL",
    "resolve_language",
    "DEFAULT_TOKENIZER_FAMILY",
    "tokenizer_family",
    "default_family_representative",
]
