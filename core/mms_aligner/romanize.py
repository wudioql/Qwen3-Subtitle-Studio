"""core.mms_aligner.romanize — 数字拼读、汉字判定、假名/uroman 罗马化表。"""
from __future__ import annotations

import logging
import re
from typing import Tuple

logger = logging.getLogger("core.mms_aligner")

# 问题：MMS 词表只有 31 个拉丁字母 token，uroman 把数字原样保留为数字字符 →
# 纯数字词（歌词里的 "2024" 之类）罗马化后 0 个有效 token，只能塞 1 个强制
# 占位 'a'，CTC 被迫把多个音节的发音压进一个伪 token 槽 → 该词时间戳不可信。
# 对策：罗马化前把每位数字展开为该语言的拼读词，token 数重新与真实发音长度
# 近似成正比。采用「逐位读法」：年份/编号等场景的主流念法（2024 → 中「二零二四」、
# 英 "two zero two four"），整体读法（two thousand twenty-four）算术复杂且各语
# 差异大，本工具歌词/字幕场景不需要。
# 拼写一律预折叠成纯 ascii（é→e、ü→u），保证 uroman 缺失的兜底路径也能产出
# 词表内 token；中/粤**必须直接给拼音**——汉字数字会被 uroman 逆向回转成阿拉伯
# 数字（"二"→"2"），写汉字等于没展开（沙箱实证：uroman("二零二四")→"2·2·4"）。
# 粤读沿用普通话拼音是本管线既有约定（uroman 无粤拼数据；Qwen 后端的粤语读音
# 由对齐器自身给出，不走本管线）。
_DIGIT_SPELLINGS: dict[str, Tuple[str, ...]] = {
    "chinese":    ("ling", "yi", "er", "san", "si", "wu", "liu", "qi", "ba", "jiu"),
    "cantonese":  ("ling", "yi", "er", "san", "si", "wu", "liu", "qi", "ba", "jiu"),
    "japanese":   ("ゼロ", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"),
    "korean":     ("영", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"),
    "english":    ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"),
    "french":     ("zero", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf"),
    "german":     ("null", "eins", "zwei", "drei", "vier", "funf", "sechs", "sieben", "acht", "neun"),
    "italian":    ("zero", "uno", "due", "tre", "quattro", "cinque", "sei", "sette", "otto", "nove"),
    "portuguese": ("zero", "um", "dois", "tres", "quatro", "cinco", "seis", "sete", "oito", "nove"),
    "russian":    ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"),
    "spanish":    ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"),
}
# 全角数字 → 半角（２０２４ 这类输入同样要走拼读展开）
_FULLWIDTH_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")
_DIGIT_RUN_RE = re.compile(r"[0-9]+")


def _contains_kanji(text: str) -> bool:
    """是否含 CJK 统一汉字（仅日文词判定为 True 时才走 pykakasi 读音）。"""
    return any(
        (0x4E00 <= ord(c) <= 0x9FFF) or (0x3400 <= ord(c) <= 0x4DBF)
        or (0xF900 <= ord(c) <= 0xFAFF)
        for c in text
    )


__all__ = [
    "_DIGIT_SPELLINGS",
    "_FULLWIDTH_DIGIT_TRANS",
    "_DIGIT_RUN_RE",
    "_contains_kanji",
]
