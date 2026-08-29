"""tests/test_punctuation.py — 标点切句 / 回填 / 导出 / 句尾标点（纯逻辑 + 命令层，零 Qt 顶层）。

覆盖：
- is_punct 字段与标点回填（merge_punct_into_words 回填并打标；动画过滤剥标点；
  converter.merge_punct_words 透明透传；逐词 cue 过滤）；
- 标点导出（ASS karaoke 标点不入 kf；Enhanced LRC 标点不在字级时间戳内）；
- 标点切句（中/英/日切点；中文顿号保护；英文无标点硬切回退；SegmentationPrefs 持久化）；
- 句尾标点零时间延伸（句末标点 end=前字 end，不制造句间重叠；句中标点仍填间隙）；
- strip_trailing_punct（删句尾连续标点段、保留句中标点、不标脏、无标点 no-op）；
- StripTrailingPunctCommand（redo/undo 按 sid 定位、锁定句跳过）。
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

def _case_punct_marking_filtering_transparency():
    # 1. is_punct 原子字段
    w = WordTimestamp(text="你", start_time=0.0, end_time=0.1)
    assert w.is_punct is False
    w.is_punct = True
    assert w.is_punct is True

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


def _case_punct_export_karaoke_and_lrc():
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


def _case_segmentation_prefs_and_persistence():
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


def _case_split_text_by_punct_languages():
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


def _case_merge_and_attach_end_to_end():
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


def _case_no_punct_english_fallback():
    cfg = TranscribeConfig()
    en_text = "Hello world this is a test of the segmentation without any punctuation marks"
    sentences_en = _text_only_to_sentences(en_text, total_sec=10.0, cfg=cfg, project_language="English")
    expected_min = max(1, len(en_text) // 24 - 1)
    assert len(sentences_en) >= expected_min, f"硬切结果太少：{len(sentences_en)}"


def _case_punct_timestamps_monotonic_and_no_overlap():
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


def _case_decoration_symbols_no_karaoke_effect():
    """特殊装饰字符（♪ ♫ ♬ ♩ #）不应拥有卡拉OK效果（与标点行为一致）。"""
    from subs.converter import _DECORATION_SYMBOLS, _filter_animation_words
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
    text_with_decoration = "♪你好♫"
    words_with_decoration = [
        WordTimestamp(text="♪", start_time=0.0, end_time=0.1, is_punct=False),
        WordTimestamp(text="你", start_time=0.1, end_time=0.3),
        WordTimestamp(text="好", start_time=0.3, end_time=0.5),
        WordTimestamp(text="♫", start_time=0.5, end_time=0.6, is_punct=False),
    ]
    filtered = _filter_animation_words(words_with_decoration)
    # 只有真实发音字（你、好）应保留，装饰字符应被过滤
    assert [w.text for w in filtered] == ["你", "好"]


def _case_punct_marking_pack():
    """标点标记与导出 2 合 1：标记/过滤/透明性 / karaoke 与 LRC 导出。"""
    _case_punct_marking_filtering_transparency()
    _case_punct_export_karaoke_and_lrc()
    _case_decoration_symbols_no_karaoke_effect()


def _case_segmentation_pack():
    """分句 2 合 1：SegmentationPrefs 持久化 / 多语言标点切分。"""
    _case_segmentation_prefs_and_persistence()
    _case_split_text_by_punct_languages()


def _case_punct_attach_pack():
    """回填端到端 3 合 1：merge+attach / 英文无标点回退 / 时间戳单调无重叠。"""
    _case_merge_and_attach_end_to_end()
    _case_no_punct_english_fallback()
    _case_punct_timestamps_monotonic_and_no_overlap()


# ═════════════════════════════════════════════════════════════
# 3. 句尾标点零时间延伸
# ═════════════════════════════════════════════════════════════

def _case_trailing_punct_zero_duration():
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


def _case_inner_punct_still_fills_gap():
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

def _case_strip_trailing_punct_pack():
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


def _case_strip_trailing_punct_preserves_dirty_state():
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


def test_punct_flow_pack():
    """test_punct_flow_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_punct_marking_pack()
    _case_segmentation_pack()
    _case_punct_attach_pack()


def test_trailing_punct_pack():
    """test_trailing_punct_pack：合并 4 个场景（断言逐条保留，见各 _case_*）。"""
    _case_trailing_punct_zero_duration()
    _case_inner_punct_still_fills_gap()
    _case_strip_trailing_punct_pack()
    _case_strip_trailing_punct_preserves_dirty_state()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
