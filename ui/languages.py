"""ui.languages — 共享语言选项（短码 ↔ 英文全名 ↔ 中文显示名 双向映射）。

UI 里原本在 subs_editor / settings_dialog / main_window 三处各维护一份语言列表，
极易漂移。这里统一一份，所有下拉/批量设置都从这里取。

支持（Qwen3-ForcedAligner 官方 11 种，与 core.language_utils 单一真源同步）：
- 短码：zh, en, ja, ko, yue, fr, de, it, pt, ru, es
- 英文全名：Chinese, English, Japanese, Korean, Cantonese, French, German, Italian, Portuguese, Russian, Spanish
- 中文显示名：中文, 英语, 日语, 韩语, 粤语, 法语, 德语, 意大利语, 葡萄牙语, 俄语, 西班牙语
"""

from __future__ import annotations

from typing import List, Tuple

from core.language_utils import LANG_SHORT_TO_FULL

# (短码, 中文名)。第一项 ("","（未设置）") 仅用于句级语言列；
# 全局/ASR 语言用 ALL_LANGUAGES（含 auto、不含空）。
SENTENCE_LANGUAGES: List[Tuple[str, str]] = [
    ("",    "（未设置）"),
    ("zh",  "中文"),
    ("en",  "英语"),
    ("ja",  "日语"),
    ("ko",  "韩语"),
    ("yue", "粤语"),
    ("fr",  "法语"),
    ("de",  "德语"),
    ("it",  "意大利语"),
    ("pt",  "葡萄牙语"),
    ("ru",  "俄语"),
    ("es",  "西班牙语"),
]

# 全局/ASR 语言（含 auto，不含空）
GLOBAL_LANGUAGES: List[Tuple[str, str]] = [
    ("auto", "自动检测"),
] + [(code, name) for code, name in SENTENCE_LANGUAGES if code]

# 英文全名小写 → 标准短码映射（如 "chinese" -> "zh", "english" -> "en"）
_FULL_TO_SHORT = {v.lower(): k for k, v in LANG_SHORT_TO_FULL.items() if v is not None}


def code_to_name(code_or_name: str) -> str:
    """标准短码或英文全名 → 中文显示名；未知原样返回。"""
    s = (code_or_name or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low == "auto":
        return "自动检测"
    # 1. 直接查短码（不区分大小写）；2. 查英文全名（"Chinese" -> "zh" -> "中文"）
    short = low if any(c == low for c, _ in SENTENCE_LANGUAGES) else _FULL_TO_SHORT.get(low)
    if short:
        for c, n in SENTENCE_LANGUAGES:
            if c == short:
                return n
    return s


def name_to_code(name_or_code: str) -> str:
    """中文显示名或英文全名 → 标准短码；找不到原样返回。"""
    s = (name_or_code or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low == "auto" or s == "自动检测":
        return "auto"
    for c, n in SENTENCE_LANGUAGES:
        if n == s or (c and c == low):
            return c
    short = _FULL_TO_SHORT.get(low)
    if short:
        return short
    return s


__all__ = [
    "SENTENCE_LANGUAGES",
    "GLOBAL_LANGUAGES",
    "code_to_name",
    "name_to_code",
]
