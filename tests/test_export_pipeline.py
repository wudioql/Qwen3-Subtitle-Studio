"""tests/test_export_pipeline.py — subs/ 导出全链路冒烟（前身为 tests/phase3_subs_smoke.py 手跑脚本）

不加载模型权重；纯构造假数据跑（压缩：14 → 9，同族并入同测）。原清单：
    T1.  time_fmt 4 种格式双向编解码一致
    T2.  merge_punct_words 合并标点逻辑（对齐 sample SRT #9「指尖,」合并）
    T3.  iter_sentence_word_cues 生成条数 = 合并后字数量；3 段切片与 sentence.text 拼接回 == 原句
    T4.  iter_ass_t_segments 每句段数 = 合并后字数量；时间非递减
    T5.  to_srt(mode="per_word")：cue 条数 == 总字数；每条 cue 都有唯一索引 + 正确时间码格式
    T6.  to_vtt(mode="per_word")：首行 WEBVTT FILE；cue 条数 == 总字数；时间正确
    T7.  to_ass(mode="per_word", strategy="split")：Dialogue 行数 == 字数（=逐字拆事件）
    T8.  to_ass(mode="per_word", strategy="t")：Dialogue 行数 == 句数（=单句事件+\\t）
    T9.  WordHighlightStyle 切换 bold/italic/underline：SRT/VTT 真的出现对应 <b>/<i>/<u>；ASS 真的出现 \\b1/\\i1/\\u1
    T10. to_lrc(enhanced=True)：每行都有字级 <mm:ss.xx> 单时间戳嵌入 + 句首 [mm:ss.xx]
    T11. to_ass_karaoke：Dialogue 行数 == 句数；每句里包含至少 len(words) 个 \\kf 标签
    T12. 无 word-level 降级：SRT/VTT/ASS 不抛异常，自动 per_sentence；k-tag ASS 抛 ValueError
    T13. LRC 句级读回：write → read 回 sentences 条数一致；时间容差 ±50ms
    T14. 标点合并后不丢文本：合并 words 的 text 拼回 == 原 words 的 text 拼回
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

from subs.ass_karaoke import build_karaoke_line_text
from subs import (
    AssKaraokeHeader,  # noqa: F401 — 包表面契约保留位
    LrcMeta,
    Sentence,
    SubtitleProject,
    WordHighlightStyle,
    WordTimestamp,
    ass_time,
    lrc_time,
    merge_punct_words,
    parse_ass_time,
    parse_lrc_text,
    parse_lrc_time,
    parse_srt_time,
    parse_vtt_time,
    read_lrc_file,
    srt_time,
    to_ass,
    to_ass_karaoke,
    to_ass_karaoke_applied,
    to_lrc,
    to_srt,
    to_vtt,
    vtt_time,
    write_lrc_file,
)

pytestmark = pytest.mark.logic


# ─────────────────────────────────────────────────────────────
# 构造测试项目：仿 usable-subtitle-sample/test-short-talk 前两句 + 英文
# 中文每字 40ms；英文逐词 200ms
# ─────────────────────────────────────────────────────────────
def _build_project() -> SubtitleProject:
    zh_sent = "青紫色的风，从指尖，拂过脸颊。"
    zh_words_text = ["青", "紫", "色", "的", "风", "，", "从", "指", "尖", "，", "拂", "过", "脸", "颊", "。"]
    t = 0.540
    zh_ws: list[WordTimestamp] = []
    for ch in zh_words_text:
        dur = 0.060 if ch in "，。" else 0.040
        zh_ws.append(
            WordTimestamp(text=ch, start_time=t, end_time=t + dur, language="zh")
        )
        t += dur
    s1 = Sentence(
        text=zh_sent,
        start_time=zh_ws[0].start_time,
        end_time=zh_ws[-1].end_time,
        words=zh_ws,
        language="zh",
    )

    en_sent = "Hello word level world"
    en_words_text = ["Hello", " ", "word", " ", "level", " ", "world"]
    t = 2.000
    en_ws: list[WordTimestamp] = []
    for w in en_words_text:
        dur = 0.030 if w.isspace() else 0.220
        en_ws.append(
            WordTimestamp(text=w, start_time=t, end_time=t + dur, language="en")
        )
        t += dur
    s2 = Sentence(
        text=en_sent,
        start_time=en_ws[0].start_time,
        end_time=en_ws[-1].end_time,
        words=en_ws,
        language="en",
    )

    proj = SubtitleProject(
        audio_path=str(PROJECT_ROOT / ".temp" / "fake.wav"),
        media_duration=s2.end_time + 1.0,
        sentences=[s1, s2],
        source_language="auto",
    )
    proj.sort()
    return proj


def _build_project_no_word() -> SubtitleProject:
    return SubtitleProject(
        audio_path="fake.wav",
        media_duration=5.0,
        sentences=[
            Sentence(text="没有字级的句子A", start_time=0.0, end_time=2.0),
            Sentence(text="没有字级的句子B", start_time=2.5, end_time=4.5),
        ],
    )


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────
def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _count_srt_indices(srt: str) -> int:
    # 以行号索引格式统计（开头是数字，后面空行 / 换行）
    count = 0
    lines = srt.splitlines()
    for line in lines:
        if line.isdigit():
            n = int(line)
            # 下一行必须是时间码
            count += 1
            _assert(n == count, f"srt index mismatch: expect {count} got {n}")
    return count


def _count_vtt_cues(vtt: str) -> int:
    # 统计 "-->" 出现次数
    return vtt.count("-->")


def _count_ass_dialogues(ass: str) -> int:
    return sum(1 for line in ass.splitlines() if line.startswith("Dialogue:"))


def _count_kf(text: str) -> int:
    # 数标准 ASS karaoke 标签：\kf / \ko / \k
    return len(re.findall(r"\\k(?:f|o)?\d+", text))


# ─────────────────────────────────────────────────────────────
# T1~T14
# ─────────────────────────────────────────────────────────────


def _case_timefmt_roundtrip():
    for t_s in (0.540, 1.0, 12.345, 61.5, 3661.999, 0.0):
        # SRT
        s = srt_time(t_s)
        back = parse_srt_time(s)
        _assert(abs(back - t_s) < 0.001, f"srt roundtrip {t_s}: {s} -> {back}")
        # VTT
        v = vtt_time(t_s)
        back = parse_vtt_time(v)
        _assert(abs(back - t_s) < 0.001, f"vtt roundtrip {t_s}: {v} -> {back}")
        # ASS
        a = ass_time(t_s)
        back = parse_ass_time(a)
        # ASS 是厘秒，容差 0.005s
        _assert(abs(back - t_s) < 0.006, f"ass roundtrip {t_s}: {a} -> {back}")
        # LRC
        lrc_stamp = lrc_time(t_s)
        back = parse_lrc_time(lrc_stamp)
        # LRC 写厘秒，容差 0.01s（1 cs）
        _assert(abs(back - t_s) < 0.011, f"lrc roundtrip {t_s}: {lrc_stamp} -> {back}")


def _case_merge_punct_and_no_text_loss():
    # T2「指尖,」合并
    ws = [
        WordTimestamp("指", 1.0, 1.04),
        WordTimestamp("尖", 1.04, 1.08),
        WordTimestamp("，", 1.08, 1.14),  # 标点，紧跟 "尖" 后面
        WordTimestamp("风", 1.2, 1.24),
        WordTimestamp(" ", 1.24, 1.26),
        WordTimestamp("拂", 1.3, 1.34),
    ]
    merged = merge_punct_words(ws)
    _assert(len(merged) < len(ws), f"punct must be merged: {len(merged)} vs {len(ws)}")
    # 标点和前面的 "尖" 并在一起
    joined_merged = "".join(w.text for w in merged)
    joined_orig = "".join(w.text for w in ws)
    _assert(joined_merged == joined_orig, f"T14 punct merge text lost: {joined_merged!r} != {joined_orig!r}")
    # 「指尖,」应为一项
    any_tip = any("尖" in w.text and "，" in w.text for w in merged)
    _assert(any_tip, f"T2 expected 「指尖,」 merged, got: {[w.text for w in merged]}")

    # T14 混合标点零文本丢失
    ws = [
        WordTimestamp("H", 0, 0.01),
        WordTimestamp("i", 0.01, 0.03),
        WordTimestamp("!", 0.03, 0.05),
        WordTimestamp(" ", 0.05, 0.06),
        WordTimestamp("「", 0.06, 0.07),
        WordTimestamp("中", 0.07, 0.11),
        WordTimestamp("文", 0.11, 0.15),
        WordTimestamp("」", 0.15, 0.17),
        WordTimestamp("。", 0.17, 0.20),
    ]
    merged = merge_punct_words(ws)
    orig = "".join(w.text for w in ws)
    back = "".join(w.text for w in merged)
    _assert(back == orig, f"punct merge lost text\n orig={orig!r}\n back={back!r}")


def _case_per_word_cues_and_t_segments():
    # T3 逐词 cue 条数与三段拼回
    from subs.converter import iter_sentence_word_cues as _fn
    proj = _build_project()
    # merge_punct 构造参数已弃用（不影响导出行为），不再传入
    style = WordHighlightStyle(underline=True)
    total = 0
    for s in proj.sentences:
        cues = list(_fn(s, style))
        words_merged = merge_punct_words(list(s.words))
        _assert(len(cues) == len(words_merged), f"cues {len(cues)} vs merged words {len(words_merged)}")
        for cue in cues:
            seg = cue.segments
            rejoined = seg.before + seg.current + seg.after
            _assert(rejoined == s.text, f"cues text slice broken:\n  got = {rejoined!r}\n  exp = {s.text!r}")
        total += len(cues)

    # T4 \\t 段数与单调 + 拼回
    from subs.converter import iter_ass_t_segments as _fn
    proj = _build_project()
    style = WordHighlightStyle()
    for s in proj.sentences:
        segs = _fn(s, style)
        words_merged = merge_punct_words(list(s.words))
        _assert(len(segs) == len(words_merged), f"ass_t segments {len(segs)} vs words {len(words_merged)}")
        # 段内时间单调
        prev = -1
        for seg in segs:
            _assert(seg.relative_t1_ms >= prev, "seg t not monotonic")
            prev = seg.relative_t2_ms
        # 拼回文本
        out = ""
        last = 0
        for seg in segs:
            out += s.text[last : seg.text_start]
            out += seg.text
            last = seg.text_end
        out += s.text[last:]
        _assert(out == s.text, f"ass_t text slice broken:\n  got={out!r}\n  exp={s.text!r}")


def _case_srt_vtt_export():
    # T5 SRT per_word / per_sentence
    proj = _build_project()
    style = WordHighlightStyle(underline=True)
    srt = to_srt(proj, style, mode="per_word")
    idx = _count_srt_indices(srt)
    expected = sum(len(merge_punct_words(list(s.words))) for s in proj.sentences)
    _assert(idx == expected, f"srt cues count: {idx} vs expected {expected}")
    _assert(",540" in srt, "srt timecode missing comma ms (zh first start 0.540)")
    # per_sentence 模式
    s2 = to_srt(proj, style, mode="per_sentence")
    _assert(_count_srt_indices(s2) == len(proj.sentences), "srt per_sentence count mismatch")

    # T6 VTT 头与 cue 数
    proj = _build_project()
    style = WordHighlightStyle(underline=True)
    vtt = to_vtt(proj, style, mode="per_word")
    _assert(vtt.startswith("WEBVTT FILE"), f"vtt header: {vtt.splitlines()[0]!r}")
    expected = sum(len(merge_punct_words(list(s.words))) for s in proj.sentences)
    got = _count_vtt_cues(vtt)
    _assert(got == expected, f"vtt cues: {got} vs {expected}")
    _assert(".540" in vtt, "vtt timecode missing dot ms")


def _case_ass_strategies_split_and_t():
    # T7 strategy=split：逐字 Dialogue
    proj = _build_project()
    style = WordHighlightStyle(underline=True)
    ass = to_ass(proj, style, mode="per_word", strategy="split")
    expected = sum(len(merge_punct_words(list(s.words))) for s in proj.sentences)
    got = _count_ass_dialogues(ass)
    _assert(got == expected, f"ass split dialogues: {got} vs {expected}")
    _assert("PlayResX: 1920" in ass, "ass header missing")
    _assert(r"\1c&H4FD5FF&" in ass, "ass split missing visible current-word color")

    # T8 strategy=t：单句事件 + \\t
    proj = _build_project()
    style = WordHighlightStyle(underline=True)
    ass = to_ass(proj, style, mode="per_word", strategy="t")
    got = _count_ass_dialogues(ass)
    n_sent = len(proj.sentences)
    _assert(got == n_sent, f"ass t dialogues: {got} vs sentences {n_sent}")
    _assert("\\t(" in ass, "ass t mode missing \\t( tag")
    _assert(r"\1c&H4FD5FF&" in ass, "ass t mode missing highlight color transform")
    _assert(r"\1c&HFFFFFF&" in ass, "ass t mode missing base color restore")


def _case_style_switch():
    proj = _build_project()
    full = WordHighlightStyle(bold=True, italic=True, underline=True, strike=True)
    srt = to_srt(proj, full, mode="per_word")
    for tag in ("<b>", "<i>", "<u>", "<s>"):
        _assert(tag in srt, f"srt missing tag {tag}")
    vtt = to_vtt(proj, full, mode="per_word")
    for tag in ("<b>", "<i>", "<u>", "<s>"):
        _assert(tag in vtt, f"vtt missing tag {tag}")
    u = WordHighlightStyle(underline=True)
    srt2 = to_srt(proj, u, mode="per_word")
    _assert("<u>" in srt2 and "<b>" not in srt2 and "<i>" not in srt2, "srt underline only broken")
    ass = to_ass(proj, full, mode="per_word", strategy="split")
    for tag in (r"\b1", r"\i1", r"\u1", r"\s1"):
        _assert(tag in ass, f"ass split missing override {tag}")


def _case_lrc_enhanced_and_roundtrip():
    # T10 enhanced 字级标签 + 标准模式无 < >
    proj = _build_project()
    lrc = to_lrc(proj, enhanced=True)
    n_lines = len([line for line in lrc.splitlines() if line.strip()])
    _assert(n_lines == len(proj.sentences), f"enhanced lrc lines {n_lines} vs sentences {len(proj.sentences)}")
    for line in lrc.splitlines():
        if not line.strip():
            continue
        _assert(line.startswith("["), f"lrc line must start with [: {line[:20]!r}")
        # Enhanced LRC 使用兼容型单开始时间标签 <mm:ss.xx>，不是旧版 <start,end> 区间
        _assert("," not in "".join(re.findall(r"<([^>]*)>", line)),
                "enhanced lrc must not use legacy <start,end> ranges")
    from subs.converter import _filter_animation_words
    n_emb = len(re.findall(r"<\d{1,3}:\d{2}\.\d{2}>", lrc))
    expected = sum(len(_filter_animation_words(list(s.words)))
                   for s in proj.sentences if s.has_word_level())
    _assert(n_emb == expected, f"enhanced lrc inline tags: {n_emb} vs words {expected}")
    # 现役 Enhanced LRC 必须能读回自身纯文本（只降级句级，不残留 inline tag）。
    read_back, _ = parse_lrc_text(lrc)
    _assert(
        [s.text for s in read_back] == [s.text for s in proj.sentences],
        f"enhanced lrc self-roundtrip text drift: {[s.text for s in read_back]!r}",
    )
    # 非 enhanced 模式：不允许 <,>
    lrc2 = to_lrc(proj, enhanced=False)
    _assert("<" not in lrc2, "non-enhanced lrc must not have <...> inline")

    # T13 LRC 句级读写回环 + meta 透传
    proj = _build_project()
    meta = LrcMeta(
        ti="测试标题",
        ar="测试艺术家",
        al="测试专辑",
        offset_ms=0,
        extra={"length": "00:03.20"},
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test.lrc"
        write_lrc_file(proj, path, enhanced=False, meta=meta)
        sentences_b, meta_b = read_lrc_file(path)
    _assert(meta_b.ti == "测试标题", f"ti lost: {meta_b.ti!r}")
    _assert(meta_b.ar == "测试艺术家", "ar lost")
    _assert(meta_b.al == "测试专辑", "al lost")
    _assert(meta_b.offset_ms == 0, f"offset lost: {meta_b.offset_ms}")
    _assert(meta_b.extra.get("length") == "00:03.20", "extra length lost")
    _assert(len(sentences_b) == len(proj.sentences), f"lrc sentence count: {len(sentences_b)} vs {len(proj.sentences)}")
    for a, b in zip(proj.sentences, sentences_b):
        _assert(abs(a.start_time - b.start_time) < 0.050, f"lrc start drift: {a.start_time} vs {b.start_time}")


def _case_ass_karaoke():
    proj = _build_project()
    ass = to_ass_karaoke(proj, k_mode="kf")
    got = _count_ass_dialogues(ass)
    n_sent = len(proj.sentences)
    _assert(got == n_sent, f"karaoke ass dialogues: {got} vs sentences {n_sent}")
    total_k = _count_kf(ass)
    expected_min = sum(len(merge_punct_words(list(s.words))) for s in proj.sentences)
    _assert(total_k >= expected_min, f"karaoke \\kf tags: {total_k} < min {expected_min}")
    _assert("[V4+ Styles]" in ass, "karaoke ass missing [V4+ Styles]")
    _assert("Source Han Sans SC" in ass, "karaoke default style font missing")


def _case_ass_karaoke_applied():
    from subs.karaoke_template import KaraokeTemplate, KaraokeTemplatePrefs

    project = _build_project()
    template_prefs = KaraokeTemplatePrefs(templates=[KaraokeTemplate(
        name="成品弹跳", modifiers=["all"], use_pos=True,
        scale_enabled=True, scale_percent=116,
    )])
    applied = to_ass_karaoke_applied(
        project,
        k_mode="kf",
        template_prefs=template_prefs,
        coord_provider=lambda index, _syl, _line: (100 + index * 20, 200, 960, 540),
    )
    assert "Style: Default-furigana" in applied
    assert any(
        line.startswith("Comment:") and ",karaoke,{\\kf" in line
        for line in applied.splitlines()
    )
    dialogues = [line for line in applied.splitlines() if line.startswith("Dialogue:")]
    assert dialogues and all(",fx," in line and r"{\kf" not in line for line in dialogues)
    assert r"\fscx116" in applied

    no_effect = KaraokeTemplatePrefs(templates=[KaraokeTemplate(enabled=False)])
    with pytest.raises(ValueError, match="尚未选择"):
        to_ass_karaoke_applied(project, template_prefs=no_effect)


def _case_fallback_no_word_level():
    proj = _build_project_no_word()
    style = WordHighlightStyle(underline=True)
    srt = to_srt(proj, style, mode="per_word")
    vtt = to_vtt(proj, style, mode="per_word")
    ass_split = to_ass(proj, style, mode="per_word", strategy="split")
    ass_t = to_ass(proj, style, mode="per_word", strategy="t")
    _assert(_count_srt_indices(srt) == len(proj.sentences), "srt fallback no-word count mismatch")
    _assert(srt.startswith("1\n"), "SRT 降级不得写入不存在于规范的 NOTE header")
    _assert(_count_vtt_cues(vtt) == len(proj.sentences), "vtt fallback no-word count mismatch")
    _assert(_count_ass_dialogues(ass_split) == len(proj.sentences), "ass split fallback mismatch")
    _assert(_count_ass_dialogues(ass_t) == len(proj.sentences), "ass t fallback mismatch")
    lrc = to_lrc(proj, enhanced=True)
    _assert("<" not in lrc, "no-word enhanced lrc must not contain <start,end>")
    with pytest.raises(ValueError):
        to_ass_karaoke(proj)


def _case_export_special_text_is_escaped_and_roundtrips():
    """用户正文不能注入 HTML/ASS；字面反斜杠、花括号和真实换行需可逆。"""
    from subs.ass_style import AssStylePrefs
    from subs.subtitle_io import _parse_ass, _parse_srt, _parse_vtt

    text = "A <B> & C {\\pos(1,2)} literal\\N\n下一行"
    sentence = Sentence(
        text=text,
        start_time=0.0,
        end_time=2.0,
        ass_style="Bad,Style",
        speaker="Name,Injected\nX",
    )
    project = SubtitleProject(sentences=[sentence])

    srt = to_srt(project, mode="per_sentence")
    vtt = to_vtt(project, mode="per_sentence")
    assert "&lt;B&gt; &amp; C" in srt
    assert "&lt;B&gt; &amp; C" in vtt
    assert "<B>" not in srt and "<B>" not in vtt
    # 导入会去高亮标签并 html.unescape；多行 cue 按既有 CJK 规则拼接。
    assert _parse_srt(srt)[0][2] == text.replace("\n", "")
    assert _parse_vtt(vtt)[0][2] == text.replace("\n", "")

    ass_style = AssStylePrefs(name="Bad,Style", font_name="Font,Name", primary_color="#GGGGGG")
    ass = to_ass(project, mode="per_sentence", ass_style=ass_style)
    dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
    assert "Bad，Style,Name，Injected X" in dialogue
    assert r"\{\\pos(1,2)\}" in dialogue
    assert r"literal\\N\N下一行" in dialogue
    assert "Style: Bad，Style,Font，Name" in ass
    assert "&H00FFFFFF" in ass  # 非法颜色安全回退

    entries = _parse_ass(ass)
    assert len(entries) == 1
    assert entries[0][2] == text


# ── 聚合入口 ──────────────────────────────────────────────────────

def _case_timefmt_and_cue_pack():
    """时间码与 cue 生成 3 合 1：roundtrip / merge_punct 无文本损失 / 逐字 cue 与 t 段。"""
    _case_timefmt_roundtrip()
    _case_merge_punct_and_no_text_loss()
    _case_per_word_cues_and_t_segments()


def _case_export_formats_pack():
    """多格式导出 4 合 1：SRT/VTT / ASS 两策略 / 样式切换 / LRC 增强往返。"""
    _case_srt_vtt_export()
    _case_ass_strategies_split_and_t()
    _case_style_switch()
    _case_lrc_enhanced_and_roundtrip()


def _case_karaoke_and_fallback_pack():
    """k-tag 源、应用模板后成品与无字级降级。"""
    _case_ass_karaoke()
    _case_ass_karaoke_applied()
    _case_fallback_no_word_level()


# ═════════════════════════════════════════════════════════════
# Aegisub k-tag / Karaoke Templater 兼容（原 test_ass_karaoke_aegisub.py）
# ═════════════════════════════════════════════════════════════
def _sentence() -> Sentence:
    # 人为加入字间空隙和显式标点，验证旧版的两类问题：时间漂移、标点丢失。
    return Sentence(
        text="你，好！",
        start_time=1.00,
        end_time=1.80,
        language="zh",
        words=[
            WordTimestamp("你", 1.00, 1.18, language="zh"),
            WordTimestamp("，", 1.18, 1.26, language="zh", is_punct=True),
            WordTimestamp("好", 1.40, 1.62, language="zh"),
            WordTimestamp("！", 1.62, 1.80, language="zh", is_punct=True),
        ],
    )

def _case_standard_tags_and_template_placement():
    # ── line uses standard tags and preserves text ─────────────────────────
    sentence = _sentence()
    line = build_karaoke_line_text(
        sentence, k_mode="kf", style=WordHighlightStyle()  # merge_punct 参数已弃用，不再传入
    )
    assert "\\km" not in line
    assert line.count("\\kf") == 2
    visible = re.sub(r"\{\\k(?:f|o)?\d+\}", "", line)
    assert visible == sentence.text
    durations = [int(v) for v in re.findall(r"\\kf(\d+)", line)]
    # 第一个字从 1.00 延伸到第二字 1.40 = 40cs；第二字延伸到句尾 = 40cs。
    assert durations == [40, 40]
    assert sum(durations) == round(sentence.duration * 100)

    # ── export places template in events not project garbage ─────────────────────────
    sentence = _sentence()
    project = SubtitleProject(audio_path="fake.wav", sentences=[sentence], media_duration=2.0)
    # 默认始终附带示例模板，Aegisub 打开后可直接 Apply karaoke template。
    ass = to_ass_karaoke(project, k_mode="ko")
    events = ass.split("[Events]", 1)[1]
    assert "\\ko" in events
    assert ",,{\\ko" in events  # 源 Dialogue 保持空 Effect

    # 模板必须位于 Events 而非 Project Garbage。
    garbage = ass.split("[Aegisub Project Garbage]", 1)[1].split("[Events]", 1)[0]
    assert "template syl all" in events
    assert "Comment:" in events
    assert "template syl" not in garbage
    assert "Effect, Text" in events

    # 低层 API 仍允许显式关闭，供第三方集成；桌面 UI 不再暴露该开关。
    clean = to_ass_karaoke(project, k_mode="ko", include_automation_template=False)
    clean_events = clean.split("[Events]", 1)[1]
    assert "template syl" not in clean_events
    assert "Comment:" not in clean_events


def _case_media_paths_and_uppercase_k_alias(tmp_path):
    video = tmp_path / "clip.mp4"
    video.touch()
    sentence = _sentence()
    project = SubtitleProject(
        source_media_path=str(video),
        video_path=str(video),
        audio_path=str(tmp_path / "cache.wav"),
        sentences=[sentence],
    )
    ass = to_ass_karaoke(project, k_mode="K")
    resolved = str(video.resolve())
    assert f"Video File: {resolved}" in ass
    assert f"Audio File: {resolved}" in ass
    assert "Video File: ?dummy" not in ass
    assert "{\\K40}" in ass

    audio = tmp_path / "song.flac"
    audio.touch()
    audio_project = SubtitleProject(
        source_media_path=str(audio), audio_path=str(audio), sentences=[_sentence()],
    )
    audio_ass = to_ass_karaoke(audio_project)
    assert f"Audio File: {audio.resolve()}" in audio_ass
    assert "Video File: ?dummy" in audio_ass


def _case_punct_no_effect_and_legacy_km_migration():
    # ── punctuation without flag never gets effect ─────────────────────────
    # 即使第三方数据忘记设置 is_punct，Unicode 标点也必须被过滤。
    sentence = Sentence(
        text="你，好！", start_time=0.0, end_time=0.8, language="zh",
        words=[
            WordTimestamp("你", 0.0, 0.2),
            WordTimestamp("，", 0.2, 0.3),  # 故意 is_punct=False
            WordTimestamp("好", 0.4, 0.6),
            WordTimestamp("！", 0.6, 0.8),
        ],
    )
    project = SubtitleProject(audio_path="fake.wav", sentences=[sentence])
    style = WordHighlightStyle(underline=False)
    split = to_ass(project, style=style, strategy="split")
    dialogues = [line for line in split.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogues) == 2
    assert not any(r"\1c&H4FD5FF&，" in line or r"\1c&H4FD5FF&！" in line for line in dialogues)
    karaoke = to_ass_karaoke(project)
    assert karaoke.count("{\\kf") == 2
    enhanced = to_lrc(project, enhanced=True)
    assert enhanced.count("<") == 2
    assert "<00:00.00,00:" not in enhanced

    # ── legacy km is migrated to standard k ─────────────────────────
    line = build_karaoke_line_text(_sentence(), k_mode="km")  # type: ignore[arg-type]
    assert "\\km" not in line
    assert "\\k40" in line

# ═════════════════════════════════════════════════════════════
# 导出对话框路径/文件名记忆（原 test_export_path_memory.py）
# ═════════════════════════════════════════════════════════════
@pytest.mark.ui
def test_export_path_and_stem_memory():
    pytest.importorskip("PySide6", exc_type=ImportError)
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.main_window import MainWindow

    win = MainWindow()

    with tempfile.TemporaryDirectory() as tmp_dir1, tempfile.TemporaryDirectory() as tmp_dir2:
        media1 = Path(tmp_dir1) / "Avatar.mp4"
        media1.touch()

        # 1. 载入媒体 1: Avatar.mp4
        p1 = SubtitleProject(
            source_media_path=str(media1),
            media_duration=120.0,
            sentences=[Sentence(text="I see you.", start_time=1.0, end_time=3.0)],
        )
        win._apply_project(p1)

        # 初始默认记忆路径与文件名
        assert win._last_export_dir == str(tmp_dir1)
        assert win._last_export_stem == "Avatar"

        # 2. 模拟用户导出为自定义路径
        custom_out = Path(tmp_dir2) / "avatar_final.srt"
        with patch("ui.export_controller.QFileDialog.getSaveFileName", return_value=(str(custom_out), "SubRip (*.srt)")):
            win._on_export_requested("srt")

        # 用户选择的新目录和文件名被成功记忆
        assert win._last_export_dir == str(tmp_dir2)
        assert win._last_export_stem == "avatar_final"

        # 3. 再次导出 ASS，验证建议路径自动使用记忆的 avatar_final.ass
        captured_proposed: list[str] = []

        def fake_get_save(parent, title, proposed, filt):
            captured_proposed.append(proposed)
            return (proposed, filt)

        with patch("ui.export_controller.QFileDialog.getSaveFileName", side_effect=fake_get_save):
            win._on_export_requested("ass_sentence")

        assert len(captured_proposed) == 1
        assert captured_proposed[0] == str(Path(tmp_dir2) / "avatar_final.ass"), \
            f"Expected avatar_final.ass, got {captured_proposed[0]}"

        # 4. 导入新媒体: ShapeOfYou.mp3，验证记忆自动重置
        media2 = Path(tmp_dir1) / "ShapeOfYou.mp3"
        media2.touch()
        p2 = SubtitleProject(
            source_media_path=str(media2),
            media_duration=240.0,
            sentences=[Sentence(text="Club isn't the best place.", start_time=2.0, end_time=5.0)],
        )
        win._apply_project(p2)

        assert win._last_export_dir == str(tmp_dir1)
        assert win._last_export_stem == "ShapeOfYou"

        captured_proposed.clear()
        with patch("ui.export_controller.QFileDialog.getSaveFileName", side_effect=fake_get_save):
            win._on_export_requested("lrc")

        assert len(captured_proposed) == 1
        assert captured_proposed[0] == str(Path(tmp_dir1) / "ShapeOfYou.lrc"), \
            f"Expected ShapeOfYou.lrc, got {captured_proposed[0]}"

    win.close()
    print("test_export_path_and_stem_memory PASSED ✔")


def test_export_all_formats_pack():
    """test_export_all_formats_pack：合并 4 个场景（断言逐条保留，见各 _case_*）。"""
    _case_timefmt_and_cue_pack()
    _case_export_formats_pack()
    _case_karaoke_and_fallback_pack()
    _case_export_special_text_is_escaped_and_roundtrips()


def test_ass_karaoke_aegisub_pack(tmp_path):
    """test_ass_karaoke_aegisub_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_standard_tags_and_template_placement()
    _case_media_paths_and_uppercase_k_alias(tmp_path=tmp_path)
    _case_punct_no_effect_and_legacy_km_migration()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
