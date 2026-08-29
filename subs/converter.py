"""subs.converter — Sentence/WordTimestamp → 逐字 cue 生成器（纯逻辑）

命名说明：本模块不做「格式转换」，职责是把句/字级时间戳数据转为
无格式依赖的逐字 cue 中间表示（供 exporters / ass_karaoke 消费）；
文件名沿用历史（4 处 import + 文档引用），语义以本 docstring 为准。

对外核心：

- iter_sentence_word_cues(sentence, style): 生成 1 字 1 cue 的列表（整句文本 + 当前字高亮 seg）
- merge_punct_words(words): 把纯标点 word 合并到前一字（减少 cue 数，视觉更连贯，对齐 sample 第9条）
- iter_ass_t_segments(sentence, style): 生成逐字 \\t 动画所需的时间区段（ASS \\t 方案专用）
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterator, List, Optional

from .ass_utils import sanitize_ass_tag_payload
from .models import Sentence, WordTimestamp


# ─────────────────────────────────────────────────────────────
# 高亮样式
# ─────────────────────────────────────────────────────────────
@dataclass
class WordHighlightStyle:
    """当前高亮字的样式组合。

    ``ass_highlight_color`` 是非 k-tag ASS 的基础可见高亮；四个 bool 是可选的
    b/i/u/s 附加字形。``ass_extra`` 只对 ASS 一字一行生效。
    """

    bold: bool = False
    italic: bool = False
    underline: bool = True
    strike: bool = False
    ass_extra: str = ""
    # 非 k-tag ASS 的当前字颜色。B/I/U/S 全关时仍有明确可见的逐字效果；
    # SRT/VTT 不强行写非标准颜色标签，仍只使用上面的 HTML 字形标签。
    ass_highlight_color: str = "#FFD54F"
    merge_punct: bool = True  # ⛔ 已弃用：导出已固定为"标点显示但不参与逐字高亮"，此字段仅为旧 API 兼容保留，不影响导出行为

    # 导出 SRT/VTT 使用 HTML tag 列表
    def html_tags_open(self) -> str:
        parts: list[str] = []
        if self.bold:
            parts.append("<b>")
        if self.italic:
            parts.append("<i>")
        if self.underline:
            parts.append("<u>")
        if self.strike:
            parts.append("<s>")
        return "".join(parts)

    def html_tags_close(self) -> str:
        parts: list[str] = []
        if self.strike:
            parts.append("</s>")
        if self.underline:
            parts.append("</u>")
        if self.italic:
            parts.append("</i>")
        if self.bold:
            parts.append("</b>")
        return "".join(parts)

    # 导出 ASS plain 使用 override tag
    def ass_color_override(self) -> str:
        """返回标准 ASS override 颜色，如 ``\\1c&H4FD5FF&``。"""
        raw = (self.ass_highlight_color or "#FFD54F").strip().lstrip("#")
        if len(raw) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in raw):
            raw = "FFD54F"
        r, g, b = raw[0:2], raw[2:4], raw[4:6]
        return rf"\1c&H{b}{g}{r}&".upper().replace("\\1C", "\\1c")

    def ass_override_open(self) -> str:
        tags: list[str] = [self.ass_color_override()]
        if self.bold:
            tags.append(r"\b1")
        if self.italic:
            tags.append(r"\i1")
        if self.underline:
            tags.append(r"\u1")
        if self.strike:
            tags.append(r"\s1")
        extra = sanitize_ass_tag_payload(self.ass_extra)
        if extra:
            tags.append(extra)
        return "{" + "".join(tags) + "}"

    def ass_override_close(self) -> str:
        # 回到当前行 Style 定义；避免后续字继承高亮属性
        return r"{\r}"


# ─────────────────────────────────────────────────────────────
# 工具：判断一个 WordTimestamp 是否「纯标点 / 纯空白」
# ─────────────────────────────────────────────────────────────
# 我们的「标点」扩展到：Unicode P* 类别 + 常见 CJK 全角符号 + ASCII 空白
# 特殊装饰符号（音乐装饰符号 ♪ ♫ ♬ ♩ 及相关特殊字符 #）
# 这些字符应被视为“不参与逐字动效”的字符（与标点行为对齐：可见但无动画）
_DECORATION_SYMBOLS = set("♪♫♬♩#")

_PUNCT_RE = re.compile(r"^\s+$")


def _is_punct_only(text: str) -> bool:
    if not text:
        return True
    if _PUNCT_RE.match(text):
        return True
    # 特殊装饰字符：不参与逐字动效，但保留可见（与标点行为一致）
    if any(ch in _DECORATION_SYMBOLS for ch in text):
        return True
    return all(
        unicodedata.category(ch).startswith("P")
        or unicodedata.category(ch).startswith("Z")
        or ch in (" ", "\t", "\n", "\r")
        for ch in text
    )


# ─────────────────────────────────────────────────────────────
# 合并标点到前一字（在 words 列表层面）
# ─────────────────────────────────────────────────────────────
def merge_punct_words(words: List[WordTimestamp]) -> List[WordTimestamp]:
    """把纯标点/空白 WordTimestamp 合并到前一字，返回新列表。

    **导出侧**工具（与 ``core.text_utils.merge_punct_into_words`` **不是**同一函数）：
    - 对齐后处理请用 ``merge_punct_into_words``（按句文本回填 + ``is_punct=True``）。
    - 本函数只整理已有 word 列表：隐式纯标点并入前字；显式 ``is_punct`` 透传。
    禁止交叉替换调用。

    - 标点 w：text 拼接、end_time 延到自己的 end；start 不变（沿用前一字）
    - 开头就是标点：保留独立一个（没字可并，避免漏文本）
    - 连续多个标点：一次性合并
    - Phase 4 v3：is_punct=True 的 word（ASR 标点回填产物）**不参与合并**，
      保持独立 word 透传——这样导出器可以按 is_punct 过滤跳过逐字动效。
      同时它仍消耗时间（end_time 正常），文本渲染时按 words 顺序自然显示。
    """
    if not words:
        return []
    out: list[WordTimestamp] = []
    carry: Optional[WordTimestamp] = None

    def _flush_carry() -> None:
        """把 carry 收尾：合并到上一个真实字（紧跟着就合），否则作为独立 word 保留。"""
        nonlocal carry
        if carry is None:
            return
        if out and (out[-1].end_time <= carry.start_time + 0.020):
            # 紧跟前字 → 合并到前字（保留 is_punct=False 标记，标记是"来源隐式"）
            out[-1].text += carry.text
            out[-1].end_time = max(out[-1].end_time, carry.end_time)
        else:
            # 开头标点 / 离上字较远 → 独立保留
            out.append(carry)
        carry = None

    for w in words:
        # Phase 4 v3: 显式标点 word 永远不参与合并，独立透传（保留 is_punct=True）
        if w.is_punct:
            _flush_carry()
            out.append(WordTimestamp(
                text=w.text,
                start_time=w.start_time,
                end_time=w.end_time,
                language=w.language,
                speaker=w.speaker,
                is_punct=True,
            ))
            continue
        # 隐式纯标点（_is_punct_only）走原合并逻辑
        if _is_punct_only(w.text):
            if carry is None:
                carry = WordTimestamp(
                    text=w.text,
                    start_time=w.start_time,
                    end_time=w.end_time,
                    language=w.language,
                    speaker=w.speaker,
                )
            else:
                carry.text += w.text
                carry.end_time = max(carry.end_time, w.end_time)
            continue
        # 当前字不是标点：先把 carry 的隐式标点并入自己
        merged = WordTimestamp(
            text=w.text,
            start_time=w.start_time,
            end_time=w.end_time,
            language=w.language,
            speaker=w.speaker,
        )
        if carry is not None:
            _flush_carry()
        out.append(merged)
    # 末尾：处理残留 carry
    if carry is not None:
        if out and (out[-1].end_time <= carry.start_time + 0.020):
            out[-1].text += carry.text
            out[-1].end_time = max(out[-1].end_time, carry.end_time)
        else:
            out.append(carry)
    return out


# ─────────────────────────────────────────────────────────────
# Phase 4 v3: 导出器辅助：过滤掉 is_punct=True 的 word（不参与逐字动效）
# ─────────────────────────────────────────────────────────────
def _filter_animation_words(words: List[WordTimestamp]) -> List[WordTimestamp]:
    """过滤所有标点/空白 word，使其只显示、不参与任何逐字效果。

    不能只依赖 ``is_punct``：导入的旧项目或第三方字级数据可能没有正确设置
    该字段。这里同时使用 Unicode 标点判断，保证非 k-tag ASS、k-tag ASS、
    逐字 SRT/VTT 和增强 LRC 都不会把标点当作当前高亮字。
    """
    return [w for w in words if not w.is_punct and not _is_punct_only(w.text)]


# ─────────────────────────────────────────────────────────────
# 1 字 1 cue 生成（整句文本 + 当前字高亮）
# ─────────────────────────────────────────────────────────────
@dataclass
class HighlightSegment:
    """整句文本按「当前字 = i」切分后的 3 段（不含任何标签，标签由 exporter 加）。"""

    before: str
    current: str
    after: str


@dataclass
class PerWordCue:
    start_time: float
    end_time: float
    # 用于生成文本：3 段 + 原句 text（用户若需要不高亮纯文本可以用原句）
    segments: HighlightSegment
    original_sentence_text: str
    # 当前字在合并后的 words 列表的 index（0-based），供 exporter 排错/测试
    word_index: int
    # 当前字合并后的完整 text（与 segments.current 一致）
    current_word_text: str


def _sentence_words_for(
    s: Sentence, style: WordHighlightStyle
) -> List[WordTimestamp]:
    """返回参与逐字动效的真实字/词；标点和空白始终排除。

    ``style.merge_punct`` 仅为旧 API/配置兼容保留，不再影响导出。此前绝大多数
    ASR 标点已有 ``is_punct=True``，导致该开关看起来几乎没有作用；现在行为
    简化为唯一规则：标点保留在句子文本中，但永远没有逐字高亮或 k-tag。
    """
    words = list(s.words) if s.words else []
    return _filter_animation_words(words)


def _slice_sentence_text_into_segments(
    sentence_text: str, words: List[WordTimestamp], idx: int
) -> HighlightSegment:
    """按 word.text 在原句中的出现顺序，把 sentence.text 切成 before/current/after。

    注意：这里不做 difflib 重对齐。如果 words 的拼接 text 与 sentence.text 不一致（比如
    校对时手动改了 sentence.text 但没改 words），我们退化为「按 words[i].text 直接截取，
    剩余丢到 after」。对 99% 正常流程（对齐 → 导出，未手动改句）是精确的。
    """
    target = words[idx].text
    # 计算前 idx 个字一共覆盖了多少原句字符
    cursor = 0
    for j, w in enumerate(words):
        # 尝试定位 w.text 在 sentence.text[cursor:] 中的出现
        pos = sentence_text.find(w.text, cursor)
        if pos < 0:
            # 退化：假设 w 占 len(w.text) 字符（可能对不上，但避免导出失败）
            pos = cursor
        if j < idx:
            cursor = pos + len(w.text)
            continue
        if j == idx:
            before = sentence_text[:pos]
            current_end = pos + len(w.text)
            current = sentence_text[pos:current_end]
            after = sentence_text[current_end:]
            # 理论上 current 应该 == target；如果不等，说明 sentence.text 和 words
            # 已经不一致了，强制按 words 来（保证 cue 高亮时长至少对应到 word duration）
            if not current:
                current = target
            return HighlightSegment(before=before, current=current, after=after)
    # 退化兜底（idx 越界了）
    return HighlightSegment(before="", current=target, after="")


def iter_sentence_word_cues(
    sentence: Sentence, style: WordHighlightStyle
) -> Iterator[PerWordCue]:
    """生成 1 字 1 cue 的 iterator（SRT/VTT/ASS_plain 拆事件 三家共用）。

    - 无 word-level 时：yield 1 条「句级」cue（before/current/after 按整句塞 current）
    - 有 word-level 时：按合并标点后的列表 yield
    """
    words = _sentence_words_for(sentence, style)
    if not words:
        yield PerWordCue(
            start_time=sentence.start_time,
            end_time=sentence.end_time,
            segments=HighlightSegment(
                before="", current=sentence.text, after=""
            ),
            original_sentence_text=sentence.text,
            word_index=-1,
            current_word_text=sentence.text,
        )
        return
    for i, w in enumerate(words):
        segs = _slice_sentence_text_into_segments(sentence.text, words, i)
        yield PerWordCue(
            start_time=w.start_time,
            end_time=w.end_time,
            segments=segs,
            original_sentence_text=sentence.text,
            word_index=i,
            current_word_text=w.text,
        )


# ─────────────────────────────────────────────────────────────
# ASS \\t 动画专用：按时间顺序出 word 区段
# ─────────────────────────────────────────────────────────────
@dataclass
class AssTSegment:
    """每个字对应一个区段：
        start/end   = 字的绝对时间（秒）
        text        = 这一字在文本里原封不动的字（含被合并的标点）
        relative_t1 / relative_t2 = 相对于 Dialogue line 起点的毫秒（ASS \\t 用）
        text_start  = 这一字在整句 text 的起点索引（用于 before/current/after 切分）
        text_end    = 这一字在整句 text 的终点索引
    """

    start_time: float
    end_time: float
    text: str
    relative_t1_ms: int
    relative_t2_ms: int
    text_start: int
    text_end: int


def iter_ass_t_segments(
    sentence: Sentence, style: WordHighlightStyle
) -> List[AssTSegment]:
    """为 ASS 逐字「\\t 单事件方案」生成字区列表（1 句 = 1 Dialogue）。

    无 word-level 时返回空列表（调用方应降级为句级单行 Dialogue，不加 \\t）。
    """
    words = _sentence_words_for(sentence, style)
    if not words:
        return []
    line_start_ms = int(round(sentence.start_time * 1000))

    segs: list[AssTSegment] = []
    cursor = 0
    for w in words:
        pos = sentence.text.find(w.text, cursor)
        if pos < 0:
            pos = cursor
        ts = pos
        te = pos + len(w.text)
        r1 = int(round(w.start_time * 1000)) - line_start_ms
        r2 = int(round(w.end_time * 1000)) - line_start_ms
        r1 = max(0, r1)
        r2 = max(r1, r2)
        segs.append(
            AssTSegment(
                start_time=w.start_time,
                end_time=w.end_time,
                text=sentence.text[ts:te] or w.text,
                relative_t1_ms=r1,
                relative_t2_ms=r2,
                text_start=ts,
                text_end=te,
            )
        )
        cursor = te
    return segs


# ─────────────────────────────────────────────────────────────
# Enhanced LRC A2 精确字级：字级 inline 时间
# ─────────────────────────────────────────────────────────────
@dataclass
class LrcEnhancedWord:
    text: str
    start_time: float
    end_time: float


def iter_lrc_enhanced_words(
    sentence: Sentence, style: WordHighlightStyle
) -> List[LrcEnhancedWord]:
    words = _sentence_words_for(sentence, style)
    if not words:
        return []
    return [
        LrcEnhancedWord(
            text=w.text,
            start_time=w.start_time,
            end_time=w.end_time,
        )
        for w in words
    ]
