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
