"""core.text_utils — 字级标点回填 / 零时长塌陷字修复 / 纯词切分与按句切分的共享实现

提供：
- extract_pure_words: 提取纯文本中的发音词（过滤标点与特殊符号；汉字/韩语逐字，
  日语假名按发音拍 mora 归并，英文/数字整词切分）
- sanitize_word_timestamps: 保证字级单调性与最小发音时长
- merge_punct_into_words: 标点插回字级序列，无标点相邻字无缝平铺，完全忽略并过滤 ♪ ♫ 等音乐符号
- attach_words_to_sentences / attach_words_proportional: 全量字级按句切分回填
  （三级策略：纯词数精确 → 字符级序列对齐 → 比例回退兜底）
"""

from __future__ import annotations

import copy
import logging
import re
from typing import List, Optional, Tuple

from subs.models import Sentence, WordTimestamp

logger = logging.getLogger(__name__)

# 标点与特殊符号全集（含常规标点、特殊音乐符号 ♪ ♫、破折号、引号、括号、日语 ー・〜 等）
# 增补 ー(U+30FC 长音符) ・(U+30FB 片假名间隔号) 〜(U+301C 波线/延音号)——
# 它们不发音成词，若当词处理会在 MMS 对齐中产生伪 'a' token；
# 作标点处理则可按间隙/留白规则回填时间槽（is_punct=True，导出跳过逐字动效）。
_SENT_INNER_PUNCT_RE = re.compile(
    r"([，。！？!?；;：:,，\.、…—～~·ー・〜《》〈〉（）()【】\[\]{}“”‘’\"'`♪♫♬♩#&@\-_/\\*^%$])"
)

# 纯音乐装饰符号集（在字级精度与时间戳中必须彻底忽略，不作为标点插入）
_MUSIC_SYMBOLS = set("♪♫♬♩#")

# 日语小假名（拗音等）：并入前一发音拍（きゃ→kya 1 拍，而非 ki+ya 2 拍）
_SMALL_KANA = frozenset("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮヵヶ")
# 促音：开启新发音拍并并入其后基字（っと→tto、っしゃ→ssha）；句末孤立促音回并前拍
_SOKUON = frozenset("っッ")


def _is_punct_char(ch: str) -> bool:
    """判断单个字符是否为标点或特殊符号。"""
    return bool(_SENT_INNER_PUNCT_RE.match(ch))


def _split_word_run(run: List[str]) -> List[str]:
    """把一段「无标点、无空白」的字符流切成发音词单位。

    extract_pure_words 与 merge_punct_into_words 的共享实现（两处必须同规则，
    否则「句纯词数 == 对齐词数」的计数自洽会被打破）：
    - 连续 ASCII 字母数字（英文/数字）→ 整词；
    - 日语假名按**发音拍（mora）**归并：小假名并入当前拍；促音开启新拍并入其后基字；
      句末孤立促音回并前一拍（あっ→「あっ」，顿挫气口不独立成词）；
    - 其余 CJK（汉字/韩语音节块等）→ 逐字（与既有行为一致）。
    """
    words: List[str] = []
    current_latin: List[str] = []

    def flush_latin() -> None:
        if current_latin:
            words.append("".join(current_latin))
            current_latin.clear()

    current_mora: List[str] = []

    def flush_mora() -> None:
        if current_mora:
            words.append("".join(current_mora))
            current_mora.clear()

    for ch in run:
        if ch.isascii() and ch.isalnum():
            flush_mora()
            current_latin.append(ch)
            continue
        flush_latin()
        if ch in _SMALL_KANA:
            # 小假名并入当前拍（当前拍为空时自成一拍，病态输入不炸）
            current_mora.append(ch)
        elif ch in _SOKUON:
            # 促音开启新拍：先收走已完整的当前拍
            flush_mora()
            current_mora.append(ch)
        else:
            # 基字（汉字/普通假名/其余非 ASCII）：当前拍「等待基字的纯促音」时并入，
            # 否则当前拍已完整（含基字或小假名）→ 先收走再开新拍
            if current_mora and not all(c in _SOKUON for c in current_mora):
                flush_mora()
            current_mora.append(ch)
    flush_latin()
    flush_mora()

    # 句末孤立促音回并前拍（あっ / ずっ）：它本身无新发音，是前一拍的急停
    if len(words) >= 2 and words[-1] and all(c in _SOKUON for c in words[-1]):
        words[-2] += words[-1]
        words.pop()
    return words


def extract_pure_words(text: str) -> List[str]:
    """从包含标点和特殊符号的文本中，提取纯发音词列表。

    规则（与 `_split_word_run` 共享）：
    - 汉字/韩语音节块：按单字符（字）切分；日语假名按发音拍（mora）归并；
    - 连续 ASCII 字母数字（英文/数字）：按完整单词切分；
    - 过滤所有标点、特殊音乐符号（如 ♪ ♫）与空白字符。
    """
    words: List[str] = []
    run: List[str] = []
    for ch in (text or "").strip():
        if _is_punct_char(ch) or ch.isspace():
            if run:
                words.extend(_split_word_run(run))
                run = []
        else:
            run.append(ch)
    if run:
        words.extend(_split_word_run(run))
    return [w for w in words if w.strip()]


def sanitize_word_timestamps(
    words: List[WordTimestamp],
    *,
    min_duration: float = 0.030,
) -> List[WordTimestamp]:
    """修复单句内部字级时间戳：保证单调性与最小发音时长，绝不越过句界。"""
    if not words:
        return []

    n = len(words)
    fixed = [copy.deepcopy(w) for w in words]

    # 保证每个片段 start >= prev.start + min_duration，且 end >= start + min_duration。
    # 标点（is_punct）豁免最小时长：句末标点允许零时长（紧贴前字），
    # 避免「句末标点 end 越过下一句首字 start」制造句间重叠。
    for i in range(n):
        w = fixed[i]
        if i > 0:
            prev_w = fixed[i - 1]
            if w.start_time < prev_w.start_time + min_duration:
                w.start_time = prev_w.start_time + min_duration
        if not w.is_punct and w.end_time < w.start_time + min_duration:
            w.end_time = w.start_time + min_duration

    # 平抑重叠
    for i in range(n - 1):
        if fixed[i].end_time > fixed[i + 1].start_time:
            fixed[i].end_time = fixed[i + 1].start_time

    for w in fixed:
        w.start_time = round(w.start_time, 3)
        w.end_time = round(w.end_time, 3)

    return fixed


def merge_punct_into_words(
    sentence_text: str,
    words: List[WordTimestamp],
) -> List[WordTimestamp]:
    """把 ASR/歌词文本中的标点插回纯词序列中；**彻底忽略并过滤 ♪ ♫ 等音乐装饰符号**。

    **对齐后处理真源**（与 ``subs.converter.merge_punct_words`` **不是**同一函数）：
    - 本函数：按句文本回填标点 word，并标 ``is_punct=True``（导出时跳过逐字动效）。
    - ``merge_punct_words``：导出侧把「隐式纯标点」word 并入前字；显式 ``is_punct`` 透传。
    禁止交叉替换调用。

    规则：
    1. 音乐装饰符号（♪ ♫ ♬ ♩ #）在字级精度中被完全忽略，绝不生成 WordTimestamp；
    2. 无标点相邻字：前字 end_time 自动后延至后字 start_time（消除发音间隙）；
    3. 句内常规标点（如逗号、顿号、分号）：自然填补前后两词之间的停顿间隙 [prev.end, next.start]；
    4. 句末标点（无后续词）：**零时间延伸**（end 紧贴前字）——不制造句间重叠、不侵占真实停顿；
       标点字符仍在 text 里整句显示，逐字导出本就「原位显示、不参与动效」。
    """
    if not words or not sentence_text:
        return list(words) if words else []

    # 1. 过滤掉单纯的音乐装饰符号
    clean_text = "".join(ch for ch in sentence_text if ch not in _MUSIC_SYMBOLS)

    # 2. 提取纯词并校验
    pure_words = extract_pure_words(clean_text)
    if len(pure_words) != len(words):
        return list(words)

    # 3. 扫描文本构建 token 序列（与 extract_pure_words 共用 _split_word_run，
    #    保证「c」类 token 与 extract 的纯词一一对应，含日语 mora 归并）
    tokens: List[Tuple[str, str]] = []
    run: List[str] = []

    def flush_run() -> None:
        if run:
            for unit in _split_word_run(run):
                tokens.append(("c", unit))
            run.clear()

    for ch in clean_text:
        if _is_punct_char(ch):
            flush_run()
            tokens.append(("p", ch))
        elif ch.isspace():
            flush_run()
        else:
            run.append(ch)
    flush_run()

    final: List[WordTimestamp] = []
    w_i = 0
    for kind, text_chunk in tokens:
        if kind == "c":
            w = copy.deepcopy(words[w_i])
            # 无标点相邻字：前字 end 自动延展至后字 start
            if final and not final[-1].is_punct:
                if final[-1].end_time < w.start_time:
                    final[-1].end_time = w.start_time
            final.append(w)
            w_i += 1
            continue

        # 常规标点处理
        if not final:
            base = words[0].start_time
            s = max(0.0, base - 0.05)
            e = base
        else:
            prev_w = final[-1]
            next_w = words[w_i] if w_i < len(words) else None
            s = prev_w.end_time
            if next_w is not None:
                if next_w.start_time > prev_w.end_time:
                    e = next_w.start_time
                else:
                    e = s + 0.040
            else:
                # 句末标点（无后续词）：零时间延伸，end 紧贴前字。
                # 旧实现给句末强标点 +0.18s 尾随留白——在「语义断句但无停顿」的
                # 歌词场景，句 end（= 标点 end）会越过下一句首字 start，制造句间
                # 重叠。标点字符仍在 text 里整句显示，逐字导出本就「原位显示、
                # 不参与动效」，零时长不损失任何可见性；有真实停顿时也不侵占停顿。
                e = s

        if e < s:
            e = s

        final.append(WordTimestamp(
            text=text_chunk,
            start_time=round(s, 3),
            end_time=round(e, 3),
            language=words[0].language if words else "",
            is_punct=True,
        ))

    # 保底时长校验：标点（is_punct）豁免——句末标点允许零时长，不侵占发音时间
    for w in final:
        if not w.is_punct and w.end_time < w.start_time + 0.030:
            w.end_time = w.start_time + 0.030
        w.start_time = round(w.start_time, 3)
        w.end_time = round(w.end_time, 3)

    return final


# attach 序列对齐的字符归一化剔除集：Qwen 后端 `_is_kept_char` 把撇号保留在词内
# （don't = 1 词），本项目 extract 视撇号为标点（don't → don + t 两词）；
# 两侧统一剔除撇号并小写化后，字符流才能逐项对位（对齐结论只用于时间插值，不改词面）。
_ALIGN_NORM_DROP = frozenset("'’ʻʼ＇")


def _norm_align_chars(s: str) -> str:
    """attach 序列对齐用的字符归一化：小写 + 剔除撇号（见 `_ALIGN_NORM_DROP`）。"""
    return "".join(ch for ch in s.lower() if ch not in _ALIGN_NORM_DROP)


def _attach_words_char_aligned(
    sentences: List[Sentence],
    words: List[WordTimestamp],
) -> Optional[List[Sentence]]:
    """纯词计数不等时的「字符级序列对齐」精确切句（n:1 / 1:n 归并）。

    典型粒度差异（两侧切法不同、但拼起来仍是同一字符串）：
    - ja(nagisa)/ko(soynlp) 形态素分词 vs 本项目逐字/发音拍：咲き/まし/た ↔ 咲/き/ま/し/た；
    - en 撇号 don't（对齐器 1 词）↔ don + t（本项目 2 词）；
    - en 连字符被上游吞掉不切断 wellknown（1 词）↔ well + known（2 词）；
    - 反向 n:1：形态素比发音拍更细时多个对齐词覆盖 1 个纯词。

    若两侧「归一化字符流」完全一致，则把 aligner 词的时间跨度按字符区间
    线性映射回各句纯词（1:n 按字符数比例细分、n:1 合并取 [首.start, 尾.end]，
    混合情形按区间重叠比例插值），并让 merge_punct 正常回填标点/无缝平铺。
    字符流不一致（内容真漂移，而非粒度差异）→ 返回 None，交比例回退兜底。
    """
    expected_per_sent = [extract_pure_words(s.text or "") for s in sentences]
    exp_words = [w for lst in expected_per_sent for w in lst]
    if not exp_words or not words:
        return None

    exp_stream = _norm_align_chars("".join(exp_words))
    got_stream = _norm_align_chars("".join((w.text or "") for w in words))
    if not exp_stream or exp_stream != got_stream:
        return None

    # 各句纯词在归一化字符流中的区间：[(句下标, 词文本, 起点, 终点)]
    exp_ranges: List[Tuple[int, str, int, int]] = []
    pos = 0
    for si, lst in enumerate(expected_per_sent):
        for wtxt in lst:
            n = len(_norm_align_chars(wtxt))
            if n:
                exp_ranges.append((si, wtxt, pos, pos + n))
                pos += n
    if len(exp_ranges) != len(exp_words):   # 归一化后为空的纯词无法插值，防御回退
        return None

    # aligner 词在字符流中的区间；归一化后为空的词（纯撇号等）无字符位，直接忽略
    #（其时间槽由相交的相邻词自然覆盖，内容本就是本项目模型里的标点）
    ali_ranges: List[Tuple[int, int, int]] = []  # (words 下标, 起点, 终点)
    pos = 0
    for i, w in enumerate(words):
        n = len(_norm_align_chars(w.text or ""))
        if n:
            ali_ranges.append((i, pos, pos + n))
            pos += n

    per_sent_words: List[List[WordTimestamp]] = [[] for _ in sentences]
    ai = 0
    for si, wtxt, a, b in exp_ranges:
        while ai < len(ali_ranges) and ali_ranges[ai][2] <= a:
            ai += 1
        # 收集与 [a,b) 相交的 aligner 词（字符流已校验一致 ⇒ 必有完整覆盖）
        overlaps: List[int] = []
        j = ai
        while j < len(ali_ranges) and ali_ranges[j][1] < b:
            overlaps.append(j)
            j += 1
        if not overlaps:                    # 理论不可达，防御
            return None
        first_w = words[ali_ranges[overlaps[0]][0]]
        last_w = words[ali_ranges[overlaps[-1]][0]]
        f0, f1 = ali_ranges[overlaps[0]][1], ali_ranges[overlaps[0]][2]
        l0, l1 = ali_ranges[overlaps[-1]][1], ali_ranges[overlaps[-1]][2]
        s_raw = float(first_w.start_time) + (a - f0) / (f1 - f0) * (
            float(first_w.end_time) - float(first_w.start_time))
        e_raw = float(last_w.start_time) + (b - l0) / (l1 - l0) * (
            float(last_w.end_time) - float(last_w.start_time))
        e_raw = max(s_raw, e_raw)
        per_sent_words[si].append(WordTimestamp(
            text=wtxt,
            start_time=round(s_raw, 3),
            end_time=round(e_raw, 3),
            language=first_w.language or sentences[si].language or "",
        ))

    logger.info(
        "[attach] 序列对齐切句：%d 纯词 ↔ %d 对齐词（粒度差异 n:1/1:n 已按字符区间插值），"
        "未走比例回退",
        len(exp_words), len(words),
    )
    for s, lst in zip(sentences, per_sent_words):
        if lst:
            s.words = merge_punct_into_words(s.text, lst)
            s.fix_times_from_words()
            s.timed = True
    return sentences


def attach_words_to_sentences(
    sentences: List[Sentence],
    words: List[WordTimestamp],
) -> List[Sentence]:
    """把 aligner 全量字级 words 切回对应句（三级策略）。

    1. 各句纯词总数 == 对齐词数 → 精确按数切（零漂移主路径）；
    2. 计数不等先尝试字符级序列对齐（两侧粒度不同但拼写同一字符串时
       仍精确切分，典型：Qwen 后端 ja/ko 形态素、en 撇号/连字符）；
    3. 字符流也不一致 → 比例分配兜底（附 WARNING 日志）。
    """
    if not sentences or not words:
        return sentences

    sent_ranges: List[Tuple[int, int]] = []
    cursor = 0
    for s in sentences:
        n = len(extract_pure_words(s.text))
        sent_ranges.append((cursor, cursor + n))
        cursor += n

    total_needed = cursor
    if total_needed == len(words):
        for s, (s_idx, e_idx) in zip(sentences, sent_ranges):
            raw_s_words = list(words[s_idx:e_idx])
            if raw_s_words:
                s.words = merge_punct_into_words(s.text, raw_s_words)
                s.fix_times_from_words()
                s.timed = True
        return sentences

    # 计数不等 ≠ 只能比例回退——先做字符级序列对齐
    aligned = _attach_words_char_aligned(sentences, words)
    if aligned is not None:
        return aligned

    # 字符流真不一致：比例分配兜底
    return attach_words_proportional(sentences, words)


def attach_words_proportional(
    sentences: List[Sentence],
    words: List[WordTimestamp],
) -> List[Sentence]:
    """比例分配兜底。

    回退路径不静默——边界句首尾可能 ±1 词漂移、
    且 `fix_times_from_words` 会把句界跟着错词缩放到错误位置，
    必须留日志（可见性守门的第一道）。
    """
    if not sentences or not words:
        return sentences

    logger.warning(
        "[attach] 精确切句失败（各句共需 %d 纯词，实得 %d 词），已按比例回退分配；"
        "句界处的词级内容可能漂移（建议重对齐/改用 MMS 后端消除系统差异）",
        sum(len(extract_pure_words(s.text or "")) for s in sentences),
        len(words),
    )

    total_chars = sum(max(1, len(extract_pure_words(s.text))) for s in sentences)
    total_words = len(words)
    cursor = 0

    for i, s in enumerate(sentences):
        n_chars = max(1, len(extract_pure_words(s.text)))
        if i == len(sentences) - 1:
            take = total_words - cursor
        else:
            take = int(round(total_words * (n_chars / total_chars)))
            take = max(0, min(total_words - cursor, take))

        raw_s_words = list(words[cursor : cursor + take])
        cursor += take
        if raw_s_words:
            s.words = merge_punct_into_words(s.text, raw_s_words)
            s.fix_times_from_words()
            s.timed = True

    return sentences


def words_content_match(sentence: Sentence) -> bool:
    """字级内容一致性不变量：非标点词序列应等于句文本纯词流。

    False 即「字级串行漂移」：常见于 ① 句文本已改未重对齐（旧 words 保留）、
    ② attach 比例回退把词摊错句、③ 外部项目自携 words 未校验。
    无字级（words 为空）返回 True——缺字级是已声明状态（字级页有空态提示），
    不属于本不变量担心的「内容悄悄对不上」。
    """
    if not getattr(sentence, "words", None):
        return True
    want = extract_pure_words(getattr(sentence, "text", "") or "")
    got = [w.text for w in sentence.words if not w.is_punct]
    return want == got


def strip_trailing_punct(sentence: Sentence) -> bool:
    """批量删除句尾标点（**字符 + 对应时间**）：原地修改，返回是否发生删除。

    目标场景：歌曲「语义断句但中间无停顿」时，ASR 自动加的句尾标点（。！？… 等）
    多余——既显示碍眼，其时间槽又可能与前句尾字重叠。本函数把句尾**连续
    ``is_punct`` 段**整体删除（``words`` 弹掉对应标点 word，``text`` 从末尾去掉
    对应标点字符），并 ``fix_times_from_words`` 让句尾回落到最后一个真实发音字。

    仅动句尾；句中标点（逗号/顿号，是节奏）不受影响。
    **不标脏**：删除只移除标点 word/字符，不改变任何发音字的时间戳，无需重对齐；
    句子的原 ``is_dirty`` 状态保持不变。锁定句由调用方决定是否跳过。
    """
    words = sentence.words
    if not words:
        return False
    dropped: List[WordTimestamp] = []
    while words and words[-1].is_punct:
        dropped.append(words.pop())
    if not dropped:
        return False
    dropped.reverse()   # 还原为原文顺序（从 text 末尾逐一删除）
    text = sentence.text or ""
    for w in dropped:
        pos = text.rfind(w.text)
        if pos >= 0:
            text = text[:pos] + text[pos + 1:]
    sentence.text = text
    sentence.fix_times_from_words()
    return True


__all__ = [
    "extract_pure_words",
    "sanitize_word_timestamps",
    "merge_punct_into_words",
    "attach_words_to_sentences",
    "attach_words_proportional",
    "words_content_match",
    "strip_trailing_punct",
]
