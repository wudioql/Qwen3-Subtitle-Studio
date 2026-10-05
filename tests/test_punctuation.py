"""tests/test_punctuation.py — 标点切句 / 回填 / 导出 / 句尾标点（纯逻辑 + 命令层，零 Qt 顶层）。

覆盖：
- is_punct 字段与标点回填（merge_punct_into_words 回填并打标；动画过滤剥标点；
  converter.merge_punct_words 透明透传；逐词 cue 过滤）；
- 标点导出（ASS karaoke 标点不入 kf；Enhanced LRC 标点不在字级时间戳内）；
- 标点切句（中/英/日切点；中文顿号保护；英文无标点硬切回退；SegmentationPrefs 持久化）；
- 句尾标点零时间延伸（句末标点 end=前字 end，不制造句间重叠；句中标点仍填间隙）；
- strip_trailing_punct（删句尾连续标点段、保留句中标点、不标脏、无标点 no-op）；
- StripTrailingPunctCommand（redo/undo 按 sid 定位、锁定句跳过）；
- 切句器保护：域名 / 缩写 / 数值点不误判为句号、超长英文硬切对齐词边界、
  CJK 逐字基线不变（被测函数属 ``core.asr_engine``，本地与云端后端共用同一路径）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

from subs.models import Sentence, SubtitleProject, WordTimestamp
from subs.converter import (
    WordHighlightStyle,
    _filter_animation_words,
    merge_punct_words,
    iter_sentence_word_cues,
)
from subs.exporters import to_lrc
from core.text_utils import merge_punct_into_words
from core.asr_engine import (
    TranscribeConfig,
    _split_text_by_punct,
    _text_only_to_sentences,
    _attach_words_to_sentences,
    _hard_split,
    _is_domain_dot,
)
from core.app_config import SegmentationPrefs

pytestmark = pytest.mark.logic


def _make_zh_words(chars: str) -> list[WordTimestamp]:
    return [
        WordTimestamp(text=ch, start_time=i * 0.1, end_time=(i + 1) * 0.1, language="Chinese")
        for i, ch in enumerate(chars)
    ]


def _zh_words(chars, start=0.0, step=0.2):
    return [WordTimestamp(text=c, start_time=start + i * step, end_time=start + (i + 1) * step)
            for i, c in enumerate(chars)]


# ═════════════════════════════════════════════════════════════
# 1. is_punct 字段与标点回填 / 导出
# ═════════════════════════════════════════════════════════════

def test_punct_marking_filtering_transparency():
    # 1. is_punct 原子字段：默认 False，构造时显式传入生效
    #    （持久化往返见 test_segmentation_prefs_and_persistence）
    assert WordTimestamp(text="你", start_time=0.0, end_time=0.1).is_punct is False
    assert WordTimestamp(text=",", start_time=0.1, end_time=0.1,
                         is_punct=True).is_punct is True

    # 2. merge_punct_into_words 回填并打标
    text = "你好,世界。"
    merged = merge_punct_into_words(text, _make_zh_words("你好世界"))
    assert len(merged) == 6
    assert [w.is_punct for w in merged] == [False, False, True, False, False, True]
    assert (merged[2].text, merged[5].text) == (",", "。")

    # 3. 动画过滤剥掉标点词
    filt = _filter_animation_words(merged)
    assert len(filt) == 4 and all(not x.is_punct for x in filt)

    # 4. converter.merge_punct_words 透明透传（不改词不重标）
    out = merge_punct_words(merged)
    assert [w.text for w in out] == ["你", "好", ",", "世", "界", "。"]
    assert [w.is_punct for w in out] == [w.is_punct for w in merged]

    # 5. 逐词 cue 过滤：4 cue 且无 highlight 包装标点
    sent = Sentence(text=text, start_time=0.0, end_time=0.4, language="zh", words=merged)
    cues = list(iter_sentence_word_cues(sent, WordHighlightStyle()))
    assert len(cues) == 4
    assert all("highlight" not in c.current_word_text for c in cues)


def test_punct_export_karaoke_and_lrc():
    from subs.ass_karaoke import build_karaoke_line_text
    text = "你好,世界。"
    merged = merge_punct_into_words(text, _make_zh_words("你好世界"))
    sent = Sentence(text=text, start_time=0.0, end_time=0.4, language="zh", words=merged)

    # ASS karaoke：标点不入 kf 标签，可见文本与原文一致
    sent.ass_style = "Default"
    line = build_karaoke_line_text(sent, k_mode="kf", style=WordHighlightStyle())
    assert line.count("{\\kf") == 4
    assert re.sub(r"\{\\kf\d+\}", "", line) == sent.text

    # Enhanced LRC：4 个字级尖括号时间戳，标点不生成字级时间戳但在行内保留
    proj = SubtitleProject(audio_path="dummy.wav", sentences=[sent], source_language="zh")
    lrc = to_lrc(proj, enhanced=True)
    assert lrc.count("<") == 4 and lrc.count(">") == 4
    assert "," not in "".join(re.findall(r"<([^>]*)>", lrc))
    for ch in ("你", "好", "世", "界", ",", "。"):
        assert ch in lrc


def test_segmentation_prefs_and_persistence():
    # SegmentationPrefs 语言优先限度
    prefs = SegmentationPrefs(enabled=True, per_lang={
        "zh": {"max_chars": 16, "max_duration_sec": 8.0},
        "en": {"max_chars": 12, "max_duration_sec": 6.0},
    })
    assert prefs.get_limits("zh") == (16, 8.0)
    assert prefs.get_limits("en") == (12, 6.0)
    assert prefs.get_limits("ja") == (0, 0.0)
    prefs_off = SegmentationPrefs(enabled=False, per_lang={"zh": {"max_chars": 16, "max_duration_sec": 8.0}})
    assert prefs_off.get_limits("zh") == (0, 0.0)

    # is_punct 持久化透传
    text = "你好,世界。"
    merged = merge_punct_into_words(text, _make_zh_words("你好世界"))
    proj_p = SubtitleProject(audio_path="x.wav",
                             sentences=[Sentence(text=text, start_time=0.0, end_time=0.4,
                                                 language="zh", words=merged)])
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        tmp = f.name
    try:
        proj_p.save_json(tmp)
        data = json.loads(open(tmp, encoding="utf-8").read())
        assert data["sentences"][0]["words"][2]["is_punct"] is True
        proj2 = SubtitleProject.load_json(tmp)
        assert proj2.sentences[0].words[2].is_punct is True
        assert proj2.sentences[0].words[0].is_punct is False
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ═════════════════════════════════════════════════════════════
# 2. 标点切句
# ═════════════════════════════════════════════════════════════
ASR_TEXT = "青紫色的风掠过指尖,金线牡丹在呼吸间流转,墨香未干,茶烟已绕过雕花窗,今夜的月色可愿与我共采一段流光。"


def test_split_text_by_punct_languages():
    # 中文：4 个 `,` + 1 个 `。` = 5 段
    parts = _split_text_by_punct(ASR_TEXT, min_chars_per_split=4)
    assert len(parts) == 5, f"预期 5 段，得 {len(parts)}"
    assert parts[0] == "青紫色的风掠过指尖," and parts[-1] == "今夜的月色可愿与我共采一段流光。"

    # 英文：常見标点全部成切点
    en = "Hello, world. This is a test! Right? Yes, definitely."
    assert len(_split_text_by_punct(en, min_chars_per_split=4)) >= 5

    # 日文：顿号「、」不切；「。」句末强切 → 2 段
    jp = "今日は良い天気です、散歩に行きましょう。桜が綺麗です、本当に。"
    parts_jp = _split_text_by_punct(jp, min_chars_per_split=4)
    assert parts_jp == ["今日は良い天気です、散歩に行きましょう。", "桜が綺麗です、本当に。"]

    # 中文顿号保护：列举顿号不切
    zh_list = "我喜欢经济、新闻、体育这些话题，娱乐也很有趣。"
    parts_zh = _split_text_by_punct(zh_list, min_chars_per_split=4)
    assert parts_zh == ["我喜欢经济、新闻、体育这些话题，", "娱乐也很有趣。"]


def test_merge_and_attach_end_to_end():
    # 单句回填：拼接文本恒等 + 时间单调
    sent1_text = "青紫色的风掠过指尖,"
    n = len(sent1_text) - 1  # 8 字
    step = 1.0 / n
    words = [
        WordTimestamp(text=ch, start_time=round(i * step, 3),
                      end_time=round((i + 1) * step, 3), language="Chinese")
        for i, ch in enumerate(sent1_text[:-1])
    ]
    merged = merge_punct_into_words(sent1_text, words)
    assert len(merged) == n + 1 and merged[n].text == ","
    assert "".join(w.text for w in merged) == sent1_text
    prev = -1.0
    for w in merged:
        assert w.start_time >= prev - 0.001 and w.end_time >= w.start_time
        prev = w.end_time

    # 端到端：纯文本切句 → attach 纯词 → 标点回填 → 句/字时间检查
    cfg = TranscribeConfig()
    sentences = _text_only_to_sentences(ASR_TEXT, total_sec=13.0, cfg=cfg, project_language="Chinese")
    assert len(sentences) == 5
    step2 = 13.0 / 46
    all_words = [
        WordTimestamp(text=ch, start_time=round(i * step2, 3),
                      end_time=round((i + 1) * step2, 3), language="Chinese")
        for i, ch in enumerate(c for c in ASR_TEXT if c not in ",。")
    ]
    _attach_words_to_sentences(sentences, all_words)
    for s in sentences:
        if s.words:
            s.words = merge_punct_into_words(s.text, s.words)
            s.fix_times_from_words()
    assert sum(len(s.words) for s in sentences) == 51          # 46 字 + 5 标点
    assert "".join(w.text for s in sentences for w in s.words) == ASR_TEXT
    for i in range(len(sentences) - 1):
        assert sentences[i].end_time - sentences[i + 1].start_time <= 0.05
    prev = -1.0
    for s in sentences:
        for w in s.words:
            assert w.start_time >= prev - 0.001, f"字级非单调：{w.text!r}"
            prev = w.start_time


def test_no_punct_english_fallback():
    cfg = TranscribeConfig()
    en_text = "Hello world this is a test of the segmentation without any punctuation marks"
    sentences_en = _text_only_to_sentences(en_text, total_sec=10.0, cfg=cfg, project_language="English")
    expected_min = max(1, len(en_text) // 24 - 1)
    assert len(sentences_en) >= expected_min, f"硬切结果太少：{len(sentences_en)}"


def test_punct_timestamps_monotonic_and_no_overlap():
    text = "墨香未干，茶烟已绕过雕花窗。"
    raw_words = [
        WordTimestamp(text="墨", start_time=1.000, end_time=1.200),
        WordTimestamp(text="香", start_time=1.200, end_time=1.400),
        WordTimestamp(text="未", start_time=1.400, end_time=1.600),
        WordTimestamp(text="干", start_time=1.600, end_time=1.850),
        WordTimestamp(text="茶", start_time=2.000, end_time=2.200),
        WordTimestamp(text="烟", start_time=2.200, end_time=2.400),
        WordTimestamp(text="已", start_time=2.400, end_time=2.600),
        WordTimestamp(text="绕", start_time=2.600, end_time=2.800),
        WordTimestamp(text="过", start_time=2.800, end_time=3.000),
        WordTimestamp(text="雕", start_time=3.000, end_time=3.200),
        WordTimestamp(text="花", start_time=3.200, end_time=3.400),
        WordTimestamp(text="窗", start_time=3.400, end_time=3.650),
    ]
    merged = merge_punct_into_words(text, raw_words)
    assert len(merged) == 14

    comma = merged[4]
    assert comma.text == "，" and comma.is_punct is True
    assert 1.850 <= comma.start_time <= comma.end_time <= 2.000

    period = merged[13]
    assert period.text == "。" and abs(period.start_time - 3.650) < 1e-3
    # 句末标点零时间延伸（end 紧贴前字），不再 +0.18s 尾随制造句间重叠
    assert abs(period.end_time - period.start_time) < 1e-6

    for i in range(1, len(merged)):
        assert merged[i].end_time >= merged[i].start_time
        assert merged[i].start_time >= merged[i - 1].start_time


def test_decoration_symbols_no_karaoke_effect():
    """特殊装饰字符（♪ ♫ ♬ ♩ #）不应拥有卡拉OK效果（与标点行为一致）。"""
    from subs.converter import _filter_animation_words
    # 装饰字符应被过滤（不参与逐字动效）
    decoration_words = [
        WordTimestamp(text="♪", start_time=0.0, end_time=0.1),
        WordTimestamp(text="♫", start_time=0.1, end_time=0.2),
        WordTimestamp(text="#", start_time=0.2, end_time=0.3),
    ]
    filt = _filter_animation_words(decoration_words)
    # 装饰字符不应出现在动画过滤后的结果中（因为它们被视为不参与动效的字符）
    assert len(filt) == 0 or all(w.text not in ("♪", "♫", "#") for w in filt)

    # 装饰字符在 _is_punct_only 中应被识别（与标点行为一致）
    for sym in ("♪", "♫", "♬", "♩", "#"):
        from subs.converter import _is_punct_only
        assert _is_punct_only(sym) is True, f"装饰字符 {sym!r} 应被识别为不参与动效"

    # 测试包含装饰字符的句子：装饰字符应被过滤，不获得逐字高亮
    words_with_decoration = [
        WordTimestamp(text="♪", start_time=0.0, end_time=0.1, is_punct=False),
        WordTimestamp(text="你", start_time=0.1, end_time=0.3),
        WordTimestamp(text="好", start_time=0.3, end_time=0.5),
        WordTimestamp(text="♫", start_time=0.5, end_time=0.6, is_punct=False),
    ]
    filtered = _filter_animation_words(words_with_decoration)
    # 只有真实发音字（你、好）应保留，装饰字符应被过滤
    assert [w.text for w in filtered] == ["你", "好"]


# ═════════════════════════════════════════════════════════════
# 3. 句尾标点零时间延伸
# ═════════════════════════════════════════════════════════════

def test_trailing_punct_zero_duration():
    from core.text_utils import sanitize_word_timestamps

    words = _zh_words("你好世界")   # 0.0~0.8
    merged = merge_punct_into_words("你好世界。", words)
    assert merged[-1].text == "。" and merged[-1].is_punct
    assert merged[-1].end_time == merged[-1].start_time == 0.8   # 零时长，紧贴尾字

    # sanitize 不把标点拉回 30ms
    fixed = sanitize_word_timestamps(merged)
    assert fixed[-1].end_time == fixed[-1].start_time

    # 句界 = 尾字 end（无重叠）
    sent = Sentence(text="你好世界。", start_time=0.0, end_time=0.8, language="zh", words=fixed)
    sent.fix_times_from_words()
    assert sent.end_time == 0.8


def test_inner_punct_still_fills_gap():
    # 句中标点（逗号）仍在字间填间隙、不豁免最小时长
    words = _zh_words("你好世界")
    merged = merge_punct_into_words("你好，世界", words)
    comma = merged[2]
    assert comma.text == "，" and comma.is_punct
    assert comma.start_time == 0.4   # 前字「好」的 end
    assert comma.end_time >= comma.start_time


# ═════════════════════════════════════════════════════════════
# 4. strip_trailing_punct 纯函数
# ═════════════════════════════════════════════════════════════

def test_strip_trailing_punct():
    from core.text_utils import strip_trailing_punct

    # 删句尾标点（字符 + 时间），句界回落尾字
    merged = merge_punct_into_words("你好世界。", _zh_words("你好世界"))
    sent = Sentence(text="你好世界。", start_time=0.0, end_time=0.8, language="zh", words=merged)
    sent.fix_times_from_words()
    assert strip_trailing_punct(sent) is True
    assert sent.text == "你好世界"
    assert [w.text for w in sent.words] == ["你", "好", "世", "界"]
    assert sent.end_time == 0.8
    assert sent.is_dirty is False                # 不标脏：删除只移除标点，无需重对齐

    # 句中标点保留，只删句尾
    merged2 = merge_punct_into_words("你好，世界。", _zh_words("你好世界"))
    sent2 = Sentence(text="你好，世界。", start_time=0.0, end_time=0.8, language="zh", words=merged2)
    assert strip_trailing_punct(sent2) is True
    assert sent2.text == "你好，世界"
    assert "，" in "".join(w.text for w in sent2.words)

    # 连续标点段全删
    merged3 = merge_punct_into_words("你好世界？！", _zh_words("你好世界"))
    sent3 = Sentence(text="你好世界？！", start_time=0.0, end_time=0.8, language="zh", words=merged3)
    assert strip_trailing_punct(sent3) is True
    assert sent3.text == "你好世界"

    # 无句尾标点 → no-op
    sent4 = Sentence(text="你好世界", start_time=0.0, end_time=0.8, language="zh",
                     words=_zh_words("你好世界"))
    assert strip_trailing_punct(sent4) is False
    assert sent4.text == "你好世界"


def test_strip_trailing_punct_preserves_dirty_state():
    """删除句尾标点不改脏：原脏保持脏、原净保持净（无需因删标点触发重对齐）。"""
    from core.text_utils import strip_trailing_punct

    dirty = Sentence(text="改过的。", start_time=0.0, end_time=0.6, language="zh",
                     words=merge_punct_into_words("改过的。", _zh_words("改过的")),
                     is_dirty=True)
    assert strip_trailing_punct(dirty) is True
    assert dirty.is_dirty is True


# ═════════════════════════════════════════════════════════════
# 5. 命令层（Qt 函数内懒 import）
# ═════════════════════════════════════════════════════════════

def test_strip_trailing_punct_command_undo_and_lock():
    pytest.importorskip("PySide6", exc_type=ImportError)
    from ui.commands import StripTrailingPunctCommand

    p = SubtitleProject(
        sentences=[
            Sentence(text="第一句。", start_time=0.0, end_time=0.8, language="zh",
                     words=merge_punct_into_words("第一句。", _zh_words("第一句"))),
            Sentence(text="锁定的。", start_time=1.0, end_time=1.8, language="zh",
                     words=merge_punct_into_words("锁定的。", _zh_words("锁定的", 1.0))),
        ],
    )
    p.sentences[1].is_locked = True
    for s in p.sentences:
        s.fix_times_from_words()
    orig_texts = [s.text for s in p.sentences]

    notified = []
    cmd = StripTrailingPunctCommand(p, [0, 1], lambda: notified.append(True))
    cmd.redo()
    assert p.sentences[0].text == "第一句"     # 未锁定句删除
    assert "。" in p.sentences[1].text         # 锁定句跳过（保留句号）
    assert cmd._changed == 1
    assert notified

    cmd.undo()
    assert [s.text for s in p.sentences] == orig_texts


# ═════════════════════════════════════════════════════════════
# 6. 切句器：域名 / 缩写 / 数值点保护 + 词边界硬切
#
# 2026-10-05 从 tests/test_cloud_asr.py 迁来。这些用例当初是**云端真机实测**
# 暴露的问题，但被测函数（_split_text_by_punct / _is_domain_dot / _hard_split /
# _text_only_to_sentences）全在 core.asr_engine，本地后端走的是同一条路径——
# 按「被测函数所属模块」归属，放这里而不是云端文件。
# ═════════════════════════════════════════════════════════════

# 背景：英文句号「.」被刻意从强句末降级为弱标点（避免切碎 e.g. / U.S.），
# 代价是 URL 会被腰斩。实测 en.wikipedia.org → "en." + "wikipedia.org."，
# 同一段文本被切成 12 段。下面这组用例把修复后的行为钉住。
_MIXED_URL_TEXT = (
    "青紫色的风掠过指尖，金线牡丹在呼吸间流转。墨香未干，茶烟已绕过雕花窗。"
    "今夜的月色，可愿与我共裁一段流光？Politics and the English language from "
    "Wikipedia, the free encyclopedia at en.wikipedia.org. Politics and the "
    "English language, 1946, is one."
)


def test_split_keeps_url_domain_intact():
    """域名内部的点不能当句号——en.wikipedia.org 必须是完整的一段。"""
    parts = _split_text_by_punct(_MIXED_URL_TEXT)
    assert any("en.wikipedia.org." in p for p in parts), parts
    # 绝不能出现被腰斩的碎片
    assert not any(p.strip().rstrip(".") in ("en", "wikipedia", "org") for p in parts), parts


def test_split_url_text_recovers_more_than_a_dozen_segments():
    """修复前同一段文本被切成 12 段（含 URL 碎片 + 孤立逗号），修复后收敛到 11 段。

    注意别误判成「3 段」：中文侧的逗号本来就是合法切分点
    （青紫色…，/ 金线…。/ 墨香未干，…共 6 段），英文侧占 5 段。
    这里要防的是**英文侧的 5 段里有3 段是URL 碎片**（en. / wikipedia.org. / 粘连句），
    修复后它们应当合并。
    """
    parts = _split_text_by_punct(_MIXED_URL_TEXT)
    assert len(parts) == 11
    # 英文侧 5 段：3 句 + 2 句被逗号切开的续句
    en_parts = [p for p in parts if p.strip() and "一" not in p and "风掠过" not in p]
    assert not any(p.strip().rstrip(".") in ("en", "wikipedia", "org") for p in en_parts)
    # 「Wikipedia,」与「1946,」是合法切分（弱标点后跟足量字符）
    assert any("from Wikipedia," in p for p in parts)


def test_split_preserves_chinese_behaviour():
    """中文切句必须逐字不变——这是本改动唯一的「不能碰」基线。"""
    zh = "青瓷色的风掠过指尖，惊现牡丹在呼吸间流转。墨香味甘茶烟已绕过雕花窗。今夜的月色，可愿与我共裁一段流光？"
    assert _split_text_by_punct(zh) == [
        "青瓷色的风掠过指尖，", "惊现牡丹在呼吸间流转。",
        "墨香味甘茶烟已绕过雕花窗。", "今夜的月色，", "可愿与我共裁一段流光？",
    ]


def test_split_preserves_plain_english_behaviour():
    """标准英文的切分必须与改动前一致（用户原本就认为英文断句没问题）。"""
    en = ("The quick brown fox jumps over the lazy dog. Politics and the English "
          "language come from Wikipedia. It is a free encyclopedia.")
    assert _split_text_by_punct(en) == [
        "The quick brown fox jumps over the lazy dog.",
        " Politics and the English language come from Wikipedia.",
        " It is a free encyclopedia.",
    ]


def test_split_keeps_abbreviations_and_decimals():
    """缩写与小数点内部的点同样不是句号。"""
    assert len(_split_text_by_punct(
        "This is important, e.g. the first item. Then we continue with more text here."
    )) == 2
    assert len(_split_text_by_punct(
        "The U.S. government announced it today. People around the world reacted quickly."
    )) == 2
    assert len(_split_text_by_punct(
        "The value is 3.14159 exactly. This sentence continues with more words after it."
    )) == 2


def test_split_repairs_missing_space_after_period():
    """``sentence.Another`` 是漏了空格的句号（模型漏标），必须能切开。"""
    parts = _split_text_by_punct(
        "This is the end of a sentence.Another sentence begins right here with new words."
    )
    assert len(parts) == 2
    assert parts[0].endswith("sentence.")


def test_word_ending_in_e_is_not_mistaken_for_abbreviation():
    """反回归：以 e/i 结尾的**普通英文单词**的句号必须能切句。

    起因（GUI 真机实测 SenseVoice 输出）::

        今天我们来聊聊英文识别的问题。
        Politics and the English language is a West Germanic language.它到底该怎么切分才正确。

    修复前``language.`` 被判成「缩写点」→ 句界消失 → 英文与后面的中文粘成一块
    （``counts["han"] > 0``）→ 整块退回Chinese → 再被 24 字中文字幕上限
    从单词中间劈开（``Po|litics``）。这就是用户报的「英文被当成中文、
    单词字母硬切」。

    真正的 ``e.g.`` / ``i.e.`` 仍必须不可切——判据是那个 e/i **前面也是点**。
    """
    # 普通英文单词的句号：可切
    for frag in ("language.它到底", "Germanic language.它", "the. 它", "be.接下来"):
        pos = frag.index(".")
        assert _is_domain_dot(frag, pos) is False, frag
    # 真缩写：不可切
    for frag in ("e.g. The", "i.e. The"):
        pos = frag.rindex(".")
        assert _is_domain_dot(frag, pos) is True, frag


def test_chinese_numeric_dot_is_not_a_sentence_boundary():
    """**用户明确要求**：中文里的点不能被当英文切坏——数值内部必须完整。"""
    text = "这套设备售价 3.5 万元，误差 0.8%，转角 45 度即可。"
    # 逐点检查：3.5 / 0.8 内部的点都不是句号
    for i, ch in enumerate(text):
        if ch == ".":
            assert _is_domain_dot(text, i), f"位置 {i} 的数值点未被保护：{text!r}"
    sents = _text_only_to_sentences(
        text, total_sec=10.0, cfg=TranscribeConfig(), project_language="Chinese",
    )
    joined = "".join(s.text for s in sents)
    assert "3.5" in joined and "0.8" in joined, [s.text for s in sents]


def test_hard_split_breaks_latin_at_word_boundary_not_mid_word():
    """反回归：超长英文的兜底硬切必须**对齐词边界**，不得从单词中间劈开。

    修复前用 ``seg[i:i+cut_c]`` 盲切，实测把连字符词 ``Anglo-Saxon`` 拆成
    ``...in the`` / `` Anglo-Saxon...``——字幕上是断词，对齐器则拿到一个
    不存在的「词」。
    """
    text = ("Politics and the English language is a West Germanic language that originated in "
            "the Anglo-Saxon peoples who lived in what is now England and parts of Scotland")
    pieces = _hard_split(text, 84)
    assert len(pieces) > 1, "超长英文应被切开"
    # 无损校验：只有充当切点的那个空白被丢弃，所有非空白字符必须原样保留
    assert re.sub(r"\s+", "", "".join(pieces)) == re.sub(r"\s+", "", text), "切分丢字符"
    for p in pieces:
        assert p == p.strip(), f"切分产生多余空白：{p!r}"
        assert p, "不得产出空片段"
    assert any("Anglo-Saxon" in p for p in pieces), \
        [p for p in pieces if "Anglo" in p]
    # 不得把连字符词劈开（``Anglo-`` / ``Saxon`` 分居两段）
    assert not any(p.startswith("Saxon") or p.startswith("Anglo-") for p in pieces), pieces


def test_hard_split_keeps_cjk_unchanged():
    """中文仍按字数硬切——行为必须与改动前逐字一致（有回归风险的那一侧）。"""
    text = "青紫色的风掠过指尖金线牡丹在呼吸间流转墨香未干茶烟已绕过雕花窗"
    assert _hard_split(text, 10) == [text[i:i + 10] for i in range(0, len(text), 10)]


def test_cjk_comma_still_splits_as_before():
    """**回归守卫**：中文逗号仍是真句界，切分逐字不变（英文侧改动不得反噬中文）。"""
    zh = "青紫色的风掠过指尖，金线牡丹在呼吸间流转。墨香未干，茶烟已绕过雕花窗。"
    sents = _text_only_to_sentences(
        zh, total_sec=13.76, cfg=TranscribeConfig(), project_language="Chinese",
    )
    assert [s.text for s in sents] == [
        "青紫色的风掠过指尖，", "金线牡丹在呼吸间流转。", "墨香未干，", "茶烟已绕过雕花窗。",
    ], [s.text for s in sents]


def test_cjk_max_chars_behaviour_unchanged():
    """**回归守卫**：中日韩语种的切分必须与改动前逐字一致。

    新的语言默认阈值只作用于非中日韩语种（见 _CJK_LANGS）。中文素材在
    ``max_sentence_chars=24`` 下的切分结果必须仍是原样，否则本轮就动了
    既有中文行为——那是用户明确不允许的。
    """
    text = "青紫色的风掠过指尖，金线牡丹在呼吸间流转。墨香未干，茶烟已绕过雕花窗。"
    for lang in ("Chinese", "Cantonese", "Japanese", "Korean"):
        sents = _text_only_to_sentences(
            text, total_sec=13.76, cfg=TranscribeConfig(), project_language=lang,
        )
        assert [s.text for s in sents] == [
            "青紫色的风掠过指尖，", "金线牡丹在呼吸间流转。", "墨香未干，", "茶烟已绕过雕花窗。",
        ], f"{lang} 切分被改动：{[s.text for s in sents]}"
        # 语种原样沿用
        assert all(s.language == lang for s in sents)


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
