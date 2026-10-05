"""core.asr_engine.sentences —— 把「纯文本」或「带时间的片段」组装成 Sentence 列表。

⚠️ 勿与 ``core/align_engine/sentence.py`` 混淆（单复数相对、职责不同）：
**本模块**是 ASR 侧的**句子组装**（片段 → Sentence 列表）；
``align_engine/sentence`` 是**单句强制对齐**（Qwen / MMS / 超长子切）。

三个入口：
    _sentences_from_segments   对齐器已给字级时间 → 按语言段 + 标点切
    _text_only_to_sentences    只有纯文本（云端 / 纯文本 ASR）→ 均分时间后切
    _words_to_sentences        ASR 直接给了带时间戳的词 → 合并短句成句

逐句语种判定（``sentence_language_fn``）**只由云端路径传入**；本地不传 →
本地行为逐字不变（见 AGENTS.md §3「逐句语种判定」）。
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from subs.models import Sentence, WordTimestamp

from ..cloud_asr import (
    split_mixed_script_tail,
)
from ..language_utils import LANG_SHORT_TO_FULL as _LANG_SHORT_TO_FULL
from .config import TranscribeConfig, _default_max_chars_for, _full_to_short_lang
from .splitting import (
    _SENT_END_PUNCT_RE,
    _WORD_CHAR_RE,
    _hard_split,
    _has_any_punct,
    _is_cjk_dominant,
    _seg_chars,
    _seg_dur,
    _split_segment_overflow,
    _split_text_by_punct,
)


# ═══════════════════════════════════════════════════════════════
# 句子构造：文本 / 带时间片段 → Sentence 列表（含逐句语种判定与短句合并）
# ═══════════════════════════════════════════════════════════════


def _sentences_from_segments(
    segments: Sequence,
    *,
    cfg: TranscribeConfig,
    project_language: str,
    sentence_language_fn: Optional[Callable[[str], Optional[str]]],
    total_sec: float,
) -> List[Sentence]:
    """把服务端返回的句级片段直接转成 :class:`Sentence`。

    为什么优先于标点切句：片段是模型自己按语句/说话人切好的，边界比标点可靠。
    2026-10-04 实测 Diarize 在 26.7s 中英混合素材上给出 5 段（中文 3 + 英文 1 + 杂音 1），
    每段带 start/end，与人工轴误差 <0.1s；而同一份文本走标点切句会被 URL 切碎成 12 段。

    时间戳是**服务端给的真实时间**，不是字符权重占位——对齐阶段会用 aligner 的
    字级时间覆盖句级时间，所以这里保留服务端值即可，不必归一化到 total_sec。
    """
    out: List[Sentence] = []
    for seg in segments:
        raw = str(getattr(seg, "text", "") or "").strip()
        if not raw:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        end = float(getattr(seg, "end", 0.0) or 0.0)
        if end <= start:
            end = start + 0.05
        lang = project_language
        if sentence_language_fn is not None:
            lang = sentence_language_fn(raw) or project_language
        out.append(Sentence(
            text=raw,
            start_time=round(start, 3),
            end_time=round(min(end, total_sec) if total_sec > 0 else end, 3),
            words=[],
            language=lang,
            speaker=str(getattr(seg, "speaker", "") or ""),
        ))
    return out


def _text_only_to_sentences(
    text: str,
    *,
    total_sec: float,
    cfg: TranscribeConfig,
    project_language: str,
    sentence_language_fn: Optional[Callable[[str], Optional[str]]] = None,
    segments: Optional[Sequence] = None,
    glue_model_id: Optional[str] = None,
) -> List[Sentence]:
    """带/不带标点通用分句：
        - 有标点：按强标点切句 → 短句**不合并**（保留 ASR 原始切分）；再做 max_* 硬切兜底
        - 无标点：按 max_chars / max_sec 硬切（自动按字边界切），字符权重分配时间

    短句合并（min_*）仅在「无标点兜底路径」里使用；带标点时 **尊重 ASR 切句**，
    因为 ASR 的标点位置本身已经体现了「说话停顿 / 句子边界」，硬合并会破坏语义。

    ``sentence_language_fn`` 用于**逐句**判定语种，签名是「一段文本 → 语言全名或 None」。
    它存在的唯一理由：中英混说素材（技术分享、访谈）里 ASR 返回的单一 language
    只能代表其中一种，逐句判定才能让英文句走英文对齐器、中文句走中文对齐器。
    返回 None（判不出）时该句回落到 ``project_language``；传 None 则全程沿用
    ``project_language``——**本地路径完全不传，对齐行为与改动前逐字一致**。

    ``segments`` 是服务端给的句级片段（目前只有 Diarize 系列会返回）。**非空时优先于
    标点切句**：它由模型自己切好、边界比标点可靠、还自带时间戳。逐句语种判定对它
    尤其重要——Diarize 实测会自己按语言分段，但项目级 language 仍是单一值。
    传空/ None 时走原有的标点切句路径，行为与改动前完全一致。

    ``glue_model_id`` 是产生该文本的 ASR 模型 id，供 :func:`split_mixed_script_tail`
    查询补丁适用范围；为 None 时不做黏连块拆分（本地路径行为不变）。
    """
    if segments:
        from_segments = _sentences_from_segments(
            segments, cfg=cfg, project_language=project_language,
            sentence_language_fn=sentence_language_fn, total_sec=total_sec,
        )
        if from_segments:
            return from_segments

    if not text:
        return []

    has_punct = _has_any_punct(text)
    min_c = max(0, int(cfg.min_sentence_chars))
    # 黏连块拆分只在「确实来自受支持的云端模型」时启用；本地路径传None，行为不变。
    _glue_model_id = glue_model_id or ""

    # 句级语种必须**先于**硬切决定（2026-10-04 重排）。原先顺序是
    # 「按 max_c 硬切 → 再判语种」，导致英文在拿到 en 判定**之前**就已被中文字幕
    # 上限（默认 24 字符≈1.5 个单词）切碎，判出 en 也没有意义——实测混合素材
    # 15 句里英文碎成 9 句（`Politics and the English` / 从 `Englis|h` 处劈开）。
    # per_lang 同理：它按语种查表，语种没定就永远查不到 en 条目。
    def _limits_for(lang_full: str, *, split: bool = False) -> tuple[int, float]:
        """按（整段或逐句）语种取 (max_chars, max_sec)。

        三级回退：**per_lang（用户按语言配的）→ 语言默认 → cfg.max_\\***。
        顺序不能颠倒：用户显式配的永远优先。

        ``split=True`` 表示该语种是**逐句判定改判出来的**（与整段语种不同），
        此时才允许启用 :data:`_LATIN_SCRIPT_MAX_CHARS` 这个语言默认上限。
        限定范围是刻意的：整段语种沿用既有 cfg.max_* 行为**一字不变**，
        纯英文素材的硬切结果与改动前完全一致（tests/test_punctuation.py 守着这条）。
        补丁只负责「中/粤整段里切出来的英文句」——那才是中文字幕阈值用错的地方。

        ``SegmentationPrefs.per_lang`` 的 key 是**短码**（en/zh/yue），而这里传入的
        可能是「全名」（Chinese/English），故必须先反查成短码再查表。
        """
        short_lang = _full_to_short_lang(lang_full)
        per_c, per_s = 0, 0.0
        if cfg.seg_prefs is not None and cfg.seg_prefs.enabled:
            per_c, per_s = cfg.seg_prefs.get_limits(short_lang)
        if per_c > 0:
            cut_c = per_c
        elif split:
            lang_default = _default_max_chars_for(lang_full)
            cut_c = lang_default if lang_default > 0 else max(0, int(cfg.max_sentence_chars))
        else:
            cut_c = max(0, int(cfg.max_sentence_chars))
        return (
            cut_c,
            per_s if per_s > 0 else max(0.0, float(cfg.max_sentence_sec)),
        )

    def _resolve_lang(sentence: str) -> tuple[str, bool]:
        """单句语种 → (语种, 是否为逐句改判)。

        判不出（或未启用逐句判定）时沿用整段语种并返回 False，表示该句
        **不该**享受语言默认阈值——整段行为必须与改动前一致。
        """
        if sentence_language_fn is not None:
            guessed = sentence_language_fn(sentence)
            if guessed and guessed != project_language:
                return guessed, True
        return project_language, False

    def _expand_glued_block(part: str) -> list[tuple[str, str]] | None:
        """把「中文句尾 + 英文句首」的黏连块展开成 ``[(片段, 语种), ...]``。

        2026-10-04 真机实测（SenseVoice，26.7s 中英混合素材）：模型把
        ``…一段流光politics and the English language from Wikipedia…`` 转写成一个
        **无标点**的黏连块，含汉字 → :func:`split_zh_en_sentence_language` 按
        「含汉字即沿用整段语种」返回 ``None`` → 整块 144 字符全被标Chinese，
        英文部分送了中文对齐器（日志实证 ``11 句 / 1 语言段``）。

        这里先探测黏连并展开，让英文尾进入拉丁分支（84 字符上限 + 词边界硬切），
        中文头保持原路径不变。判据极保守（见 :func:`split_mixed_script_tail`），
        粤语与「中文夹几个英文词」的情形一律返回 ``None``，行为不变。

        **中文头必须自己再走一遍逗号切分**（不能直接入``typed``）：黏连块整体
        因含大量拉丁字母而在 :func:`_is_cjk_dominant` 里判为False，原逻辑对它走
        的是「拉丁超上限才从逗号切」；若把中文头原样放行，中文逗号切分会整个
        丢失，`墨香未干，茶烟已绕过雕花窗，` 会粘成 31 字再被硬切在
        「共|采」之间——这是引入本函数时实测抓到的回归。
        """
        if sentence_language_fn is None:
            return None
        pieces = split_mixed_script_tail(part, project_language, _glue_model_id)
        if pieces is None:
            return None
        head, tail = pieces
        out: list[tuple[str, str]] = []
        # 中文头：按中文逗号切（与 CJK 主导分支同一条路径），语种沿用整段
        head_lang = _resolve_lang(head)[0] if _is_cjk_dominant(head) else project_language
        for piece in _split_text_by_punct(head, min_chars_per_split=max(3, min_c)):
            piece_lang = project_language if _is_wordless(piece) else head_lang
            out.append((piece, piece_lang))
        # 英文尾：整段判English（后续由拉丁分支按84 字符上限 + 词边界硬切处理）
        out.append((tail, _LANG_SHORT_TO_FULL["en"]))
        return out

    def _is_wordless(seg: str) -> bool:
        """该片段是否**没有任何文字**（只有数字 / 标点 / 空白），如 ``1946,``。

        语种判定按文字脚本统计，数字与标点不计数，所以这类碎片必然判不出语种。
        若直接回落整段语种，英文句里的 ``1946,`` 会被贴上中文标签送进中文对齐器，
        时间轴直接错。故按用户拍板走「继承前一句语种」兜底。
        """
        return not _WORD_CHAR_RE.search(seg)

    sent_langs: list[str] = []
    if has_punct:
        # === 切分（2026-10-04 重排，详见 _split_text_by_punct / _is_cjk_dominant）===
        # 先按强标点切出**语义句界**，再逐句决定要不要按逗号细分：
        #   - CJK 主导的句子 → 按原逻辑连弱标点一起切（中文逗号是真句界，行为逐字不变）
        #   - 拉丁主导的句子 → **只有超该句语种上限**才从逗号二次切
        # 顺序不能颠倒：语种判定必须在硬切之前（见上），否则英文在拿到 en 判定之前
        # 就已被中文字幕上限切碎，判出 en 也没有意义。
        strong_parts = _split_text_by_punct(
            text, min_chars_per_split=max(3, min_c), strong_only=True,
        )
        typed: list[tuple[str, str]] = []
        prev_lang = project_language
        for p in strong_parts:
            # 先探中英黏连块：含汉字的整块会被判成中文，展开后英文尾才能走拉丁分支
            glued = _expand_glued_block(p)
            if glued is not None:
                for piece, piece_lang in glued:
                    typed.append((piece, piece_lang))
                    prev_lang = piece_lang
                continue
            lang, is_split = _resolve_lang(p)
            if _is_cjk_dominant(p):
                # 中文系：连逗号一起切，**完全走原路径**（守卫 test_cjk_max_chars_behaviour_unchanged）
                for piece in _split_text_by_punct(
                    p, min_chars_per_split=max(3, min_c),
                ):
                    piece_lang = prev_lang if _is_wordless(piece) else _resolve_lang(piece)[0]
                    typed.append((piece, piece_lang))
                    prev_lang = piece_lang
                continue
            # 拉丁系：默认整句保留，只有超上限才从逗号二次切。
            # 这同时让「单句最大字数」第一次真正对英文生效。
            cut_c, _ = _limits_for(lang, split=is_split)
            if cut_c > 0 and len(p) > cut_c:
                for piece in _split_text_by_punct(
                    p, min_chars_per_split=max(3, min_c),
                ):
                    piece_lang = prev_lang if _is_wordless(piece) else _resolve_lang(piece)[0]
                    typed.append((piece, piece_lang))
                    prev_lang = piece_lang
            else:
                typed.append((p, lang))
                prev_lang = lang
        # 细分之后仍超上限的（无逗号可切的超长句）→ 硬切兜底。
        hard_cut: list[str] = []
        sent_langs: list[str] = []
        for seg, lang in typed:
            cut_c, _ = _limits_for(lang, split=(lang != project_language))
            if cut_c > 0 and len(seg) > cut_c:
                for piece in _hard_split(seg, cut_c):
                    hard_cut.append(piece)
                    sent_langs.append(lang)
            else:
                hard_cut.append(seg)
                sent_langs.append(lang)
    else:
        # === 无标点路径：整段只有一段，「短句合并」无多段可并（原实现是死代码），只做 max_* 硬切 ===
        only_lang, only_split = _resolve_lang(text)
        max_c, _ = _limits_for(only_lang, split=only_split)
        hard_cut = _hard_split(text, max_c) if max_c > 0 else [text]
        sent_langs = [only_lang] * len(hard_cut)

    # 时长上限取各句语种对应上限的**最大值**：多语混合时整段时长不宜被最短那句拖累
    max_s = max(_limits_for(lang)[1] for lang in set(sent_langs))

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
        # 逐句语种已在硬切前定好（sent_langs 与 hard_cut 一一对应）：
        # 硬切可能把一句拆成多片，这些片段共享原句语种，不能再按片段重新判
        sent_lang = sent_langs[idx] if idx < len(sent_langs) else project_language
        sents.append(Sentence(
            text=p, start_time=round(t, 3), end_time=round(et, 3),
            words=[], language=sent_lang,
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
