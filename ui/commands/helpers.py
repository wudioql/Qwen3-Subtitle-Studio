"""ui.commands.helpers — sid 定位与拆分/合并纯逻辑（无 Qt 依赖）。"""
from __future__ import annotations

import copy
import logging
from typing import List, Optional, Tuple

from subs.models import Sentence, SubtitleProject, WordTimestamp

logger = logging.getLogger("ui.commands")

def _bind_sentence_and_word_edges(
    sentence: Sentence,
    *,
    new_start: Optional[float] = None,
    new_end: Optional[float] = None,
    min_duration: float = 0.030,
) -> None:
    """句界与最外层 word/标点界保持同一时间；内部字界不动。"""
    old_start = float(sentence.start_time)
    old_end = float(sentence.end_time)
    start = float(old_start if new_start is None else new_start)
    end = float(old_end if new_end is None else new_end)
    start = max(0.0, start)
    if end < start + min_duration:
        if new_start is not None and new_end is None:
            start = max(0.0, end - min_duration)
        else:
            end = start + min_duration

    if sentence.words and new_start is not None and new_end is not None and old_end > old_start:
        # 表格一次提交完整 start/end 时，整句 words 按比例平移/缩放，保持内部结构。
        scale = (end - start) / (old_end - old_start)
        for word in sentence.words:
            word.start_time = round(start + (word.start_time - old_start) * scale, 3)
            word.end_time = round(start + (word.end_time - old_start) * scale, 3)
    elif sentence.words:
        first = sentence.words[0]
        last = sentence.words[-1]
        if new_start is not None:
            start = min(start, float(first.end_time) - min_duration)
            start = max(0.0, start)
            first.start_time = round(start, 3)
        if new_end is not None:
            end = max(end, float(last.start_time) + min_duration)
            last.end_time = round(end, 3)

    sentence.start_time = round(start, 3)
    sentence.end_time = round(end, 3)


def _find_by_sid(project: SubtitleProject, sid: int) -> Optional[int]:
    if sid < 0:
        return None
    for i, s in enumerate(project.sentences):
        if s.sid == sid:
            return i
    return None


def _resolve_row(project: SubtitleProject, sid: int, fallback_idx: int) -> Optional[int]:
    """按 sid 定位句子当前行号；sid 失效时退回构造时的行号（越界则 None）。

    注意：redo() 末尾的 project.sort() 会按 start_time 重排句子顺序，
    仅靠构造时的行号，undo() 时该行号上可能已是**另一句**（实测会把时间
    错改到邻句头上）。sid 全程不变，是跨 sort 的稳定身份；
    拆/合/删命令早已全部走 sid，本次把编辑/拖拽类命令对齐到同一策略。
    """
    idx = _find_by_sid(project, sid)
    if idx is not None:
        return idx
    if 0 <= fallback_idx < len(project.sentences):
        logger.warning(
            "[commands] sid=%s 定位失败，退回行号 %s（句子可能已被删除/替换）",
            sid, fallback_idx,
        )
        return fallback_idx
    return None


# ═══════════════════════════════════════════════════════════════
# 拆分 / 合并 纯逻辑（不依赖 Qt，可单独测试）
# ═══════════════════════════════════════════════════════════════
def _split_sentence_at(sent: Sentence, cut_time: float) -> Tuple[Sentence, Sentence]:
    """在 cut_time 把单句拆为 2 句。

    规则：
    - 左半 start = sent.start_time, end = cut_time
    - 右半 start = cut_time, end = sent.end_time
    - words 按 w.start_time < cut_time 划归（边界词归右半）
    - text 用**原始字符切片**（保留 words 之外的空白/音乐符号），
      切点 = 左半最后一个词在 text 中的结束字符位置——与合句「沿用时间戳」对称，
      不丢弃拆前字级。

    Args:
        sent: 原句
        cut_time: 切点（秒）。如果落在 sent 范围外，silent 不报错，
                  直接得到左右两半（左半可能 text==''，右半包含全部 words）。
                  这是用户决策：手动操作 silent，无校验。
    """
    text = sent.text or ""
    if not sent.words:
        # 无字级：直接按 cut_time 切，text 平均拆（粗略）
        # 安全兜底：左半取前半字符，右半取后半
        mid = len(text) // 2
        left = Sentence(
            text=text[:mid], start_time=sent.start_time, end_time=cut_time,
            words=[], language=sent.language, speaker=sent.speaker,
        )
        right = Sentence(
            text=text[mid:], start_time=cut_time, end_time=sent.end_time,
            words=[], language=sent.language, speaker=sent.speaker,
        )
        left.is_dirty = True
        right.is_dirty = True
        return left, right

    # 有字级：按 start_time 切（沿用字级时间戳）
    left_words: List[WordTimestamp] = []
    right_words: List[WordTimestamp] = []
    for w in sent.words:
        if w.start_time < cut_time:
            left_words.append(w)
        else:
            right_words.append(w)

    # text 用原始字符切片：保留 words 之外的空白/音乐符号（不再 join words 丢失空格）。
    # 切点 = 右半第一个词的起始字符位置——词间空白/标点归左半，避免右半前导空格；
    # 无右半词时左半吞下全部文本。
    cursor = 0
    for w in left_words:
        pos = text.find(w.text, cursor)
        if pos < 0:
            pos = cursor
        cursor = pos + len(w.text)
    if right_words:
        pos = text.find(right_words[0].text, cursor)
        cut_char = pos if pos >= 0 else cursor
    else:
        cut_char = len(text)
    left_text = text[:cut_char]
    right_text = text[cut_char:]

    left_end = left_words[-1].end_time if left_words else cut_time
    right_start = right_words[0].start_time if right_words else cut_time

    left = Sentence(
        text=left_text,
        start_time=sent.start_time,
        end_time=left_end,
        words=list(left_words),
        language=sent.language,
        speaker=sent.speaker,
        is_dirty=True,
    )
    right = Sentence(
        text=right_text,
        start_time=right_start,
        end_time=sent.end_time,
        words=list(right_words),
        language=sent.language,
        speaker=sent.speaker,
        is_dirty=True,
    )
    return left, right


def _split_sentence_at_char(sent: Sentence, char_index: int) -> Optional[Tuple[Sentence, Sentence]]:
    """在 sent.text 的 char_index 处按「文本光标」拆为 2 句。

    与 _split_sentence_at（按时间切）不同，这里按**字符索引**切文本：
    - text 切成 left_text = text[:char_index] / right_text = text[char_index:]
    - words 按「累计字数 ≤ char_index」尽量划分到左/右
    - 有字级时用字级边界时间；无字级/文本被改过时按字符占比分配时间

    Args:
        sent: 原句
        char_index: 文本光标位置（字符索引）

    Returns:
        (left, right) 两新句；若 char_index 在边界导致某半为空，返回 None（无效拆分）
    """
    text = sent.text or ""
    char_index = max(0, min(char_index, len(text)))
    left_text = text[:char_index]
    right_text = text[char_index:]
    if not left_text or not right_text:
        return None  # 光标在首/尾，切不出两半

    # words 沿原句文本定位整词划分（沿用拆前字级时间戳）：
    # 用 find 逐词定位其在 text 中的位置，整词归左/右——切点落在词中间时整词归右。
    # 即使 words 拼接 != text（句含空白/音乐符号，或文本被手改过），也能正确划分，
    # 不再整句丢弃字级时间戳（旧实现仅当拼接相等才保留，否则 words 全清）。
    left_words: List[WordTimestamp] = []
    right_words: List[WordTimestamp] = []
    if sent.words:
        cursor = 0
        for w in sent.words:
            pos = text.find(w.text, cursor)
            if pos < 0:
                pos = cursor
            w_end = pos + len(w.text)
            if w_end <= char_index:
                left_words.append(w)
            else:
                right_words.append(w)
            cursor = w_end

    # 时间
    if left_words:
        left_end = left_words[-1].end_time
        right_start = right_words[0].start_time if right_words else left_end
    else:
        total = len(text) or 1
        frac = char_index / total
        mid = sent.start_time + frac * (sent.end_time - sent.start_time)
        left_end = mid
        right_start = mid

    left = Sentence(
        text=left_text,
        start_time=sent.start_time,
        end_time=left_end,
        words=left_words,
        language=sent.language,
        speaker=sent.speaker,
        is_dirty=True,
    )
    right = Sentence(
        text=right_text,
        start_time=right_start,
        end_time=sent.end_time,
        words=right_words,
        language=sent.language,
        speaker=sent.speaker,
        is_dirty=True,
    )
    return left, right


def _merge_sentences(sentences: List[Sentence]) -> Sentence:
    """合并多句为 1 句。

    规则：
    - text: 简单拼接（" ".join → 避免黏连；但中文场景不必要）
    - start: 第一句 start_time
    - end: 最后一句 end_time
    - words: 全部 words 按 start_time 排序合并
    - 时间 gap 不回填（用户决策：silent，不校验 gap/overlap）
    - 新句 is_dirty=True

    Args:
        sentences: 至少 2 句（调用方负责；< 2 不报错，按 1 句处理）
    """
    if not sentences:
        return Sentence(text="", start_time=0.0, end_time=0.0)
    if len(sentences) == 1:
        return copy.deepcopy(sentences[0])

    s0 = sentences[0]
    merged_words: List[WordTimestamp] = []
    for s in sentences:
        merged_words.extend(s.words)
    merged_words.sort(key=lambda w: (w.start_time, w.end_time))

    lang = (s0.language or "").lower()
    parts = [s.text for s in sentences if s.text]
    if lang in ("chinese", "zh", "japanese", "ja", "korean", "ko", "cantonese", "yue"):
        text = "".join(parts)
    elif lang:
        text = " ".join(parts)
    else:
        # 语言未设置（如外部导入字幕 language=""）→ 按内容判断，
        # 不再一律按 CJK 无空格拼接（旧行为下英文导入句合并会黏成 "HelloWorld"）
        text = " ".join(parts) if any(" " in p for p in parts) else "".join(parts)

    merged = Sentence(
        text=text,
        start_time=s0.start_time,
        end_time=sentences[-1].end_time,
        words=merged_words,
        language=s0.language,
        speaker=s0.speaker,
        is_dirty=True,
    )
    return merged
