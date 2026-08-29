"""Karaoke Templater 应用器（纯逻辑，零 Qt）。

钉住 Aegisub Apply 的关键结构：零号 syllable、syl/line/char 展开、变量单位、
原 k-tag Comment/effect=karaoke、生成 Dialogue/effect=fx、furigana 样式与缺字级
失败。Aegisub 参考结构已固化为本文件断言，不依赖会话期 golden 资产。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401

import pytest

from subs.karaoke_template import KaraokeTemplate
from subs.karaoke_templater import (
    LuaExpressionError,
    apply_template,
    apply_template_to_project,
    split_syllables,
)
from subs.models import Sentence, SubtitleProject, WordTimestamp

pytestmark = pytest.mark.logic


def _sentence(text="你好世界"):
    words = [
        WordTimestamp(
            text=char,
            start_time=index * 0.25,
            end_time=index * 0.25 + 0.25,
            language="zh",
        )
        for index, char in enumerate(text)
    ]
    return Sentence(text=text, start_time=0.0, end_time=1.0, language="zh", words=words)


def _form_template(**kwargs) -> KaraokeTemplate:
    base = {
        "use_pos": False,
        "fad_in_ms": 0,
        "fad_out_ms": 0,
        "scale_enabled": True,
        "scale_percent": 120,
    }
    base.update(kwargs)
    return KaraokeTemplate(**base)


def _coord(_index, _syl, _line):
    return (100, 200, 960, 540)


def _dialogues(lines):
    return [line for line in lines if line.startswith("Dialogue:")]


def _case_split_syllables_from_k_tag_truth():
    syllables = split_syllables(_sentence("你好世界"))
    assert [item.text for item in syllables] == ["", "你", "好", "世", "界"]
    assert [item.start_ms for item in syllables] == [0, 0, 250, 500, 750]
    assert [item.end_ms for item in syllables] == [0, 250, 500, 750, 1000]
    assert [item.index for item in syllables] == [0, 1, 2, 3, 4]


def _case_syl_matches_aegisub_shape():
    template = _form_template(use_pos=True)
    lines = apply_template(_sentence("你好世界"), template, coord_provider=_coord)
    assert len(lines) == 5  # kara[0] 空行 + 4 个真实 syllable
    assert all(line.startswith("Dialogue: ") and ",fx," in line for line in lines)
    assert all(r"\alpha&HFF&" not in line for line in lines)
    assert all("$" not in line for line in lines)

    # Aegisub syl 模板每行只含自身：首行是零号空 syllable，第二行只显示“你”。
    assert lines[0].endswith("}") and "你好世界" not in lines[0]
    assert lines[1].endswith("你")
    assert not any(char in lines[1].rsplit("}", 1)[-1] for char in "好世界")
    assert r"\pos(100,200)" in lines[0]


def _case_line_and_char_semantics():
    line_template = _form_template(template_class="line", use_pos=False)
    line_output = apply_template(_sentence("你好世界"), line_template, coord_provider=_coord)
    assert len(line_output) == 1
    # 不钉整串脆弱格式，只钉一条 Dialogue 中每 syllable 都有模板标签与完整正文。
    assert line_output[0].count(r"\fscx120") == 4
    assert all(char in line_output[0] for char in "你好世界")

    # char 继承所属 syllable 的整段时间；中文单字源仍是 4 条，不生成 kara[0]。
    char_template = _form_template(template_class="char", use_pos=False)
    char_output = apply_template(_sentence("你好世界"), char_template, coord_provider=_coord)
    assert len(char_output) == 4
    assert char_output[0].endswith("你")


def _case_modifiers_and_safe_expressions():
    sentence = Sentence(
        text=" 你好世界",
        start_time=0.0,
        end_time=1.0,
        language="zh",
        words=[
            WordTimestamp(
                text=char,
                start_time=0.25 + index * 0.1875,
                end_time=0.25 + (index + 1) * 0.1875,
            )
            for index, char in enumerate("你好世界")
        ],
    )
    syllables = split_syllables(sentence)
    assert [item.text for item in syllables[:2]] == ["", " "]
    template = _form_template(modifiers=["all", "noblank"])
    assert len(apply_template(sentence, template, coord_provider=_coord)) == 4

    notext = _form_template(modifiers=["all", "notext"])
    assert "你好世界" not in "\n".join(
        apply_template(_sentence("你好世界"), notext, coord_provider=_coord)
    )

    # 内置模板所需的简单算术可安全展开，不执行 eval/Lua。
    safe = KaraokeTemplate(
        mode="raw",
        raw_text=(
            "Comment: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,"
            r"template syl noblank,{\t($start,!$start+120!,\alpha&H00&)}"
        ),
    )
    safe_lines = apply_template(_sentence("你好世界"), safe, coord_provider=_coord)
    assert r"\t(0,120,\alpha&H00&)" in safe_lines[0]

    lua = KaraokeTemplate(
        mode="raw",
        raw_text=(
            "Comment: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,"
            r"template syl noblank,{\t($start,$end,!math.floor(1+1)!)}"
        ),
    )
    with pytest.raises(LuaExpressionError):
        apply_template(_sentence("你好世界"), lua, coord_provider=_coord)


def _case_punctuation_stays_in_place_without_template_effects():
    sentence = Sentence(
        text="你好，world!",
        start_time=0.0,
        end_time=1.0,
        words=[
            WordTimestamp("你", 0.0, 0.2),
            WordTimestamp("好", 0.2, 0.45),
            WordTimestamp("，", 0.45, 0.5, is_punct=True),
            WordTimestamp("world", 0.5, 0.9),
            WordTimestamp("!", 0.9, 1.0, is_punct=True),
        ],
    )
    lines = apply_template(sentence, _form_template(use_pos=True), coord_provider=_coord)
    punctuation_lines = [
        line for line in lines
        if line.split(",", 9)[-1] in (
            r"{\an5\pos(100,200)}，",
            r"{\an5\pos(100,200)}!",
        )
    ]
    assert len(punctuation_lines) == 2
    assert all(
        not any(tag in line for tag in (r"\fad", r"\fscx", r"\bord", r"\blur", r"\t("))
        for line in punctuation_lines
    )

    template_lines = [line for line in lines if line not in punctuation_lines]
    visible_parts = [line.split(",", 9)[-1].rsplit("}", 1)[-1] for line in template_lines]
    assert all("，" not in text and "!" not in text for text in visible_parts)
    assert any(text == "好" for text in visible_parts)
    assert any(text == "world" for text in visible_parts)
    good_line = next(line for line in template_lines if line.endswith("好"))
    world_line = next(line for line in template_lines if line.endswith("world"))
    # k-tag 源仍覆盖到下一字/句尾，但模板动画严格截止真实字 end，不吞标点时间。
    assert r"\t(325,450,\fscx100\fscy100)" in good_line
    assert r"\t(700,900,\fscx100\fscy100)" in world_line


def _case_project_document_matches_apply_structure():
    project = SubtitleProject(
        source_media_path="clip.mp4",
        sentences=[_sentence("你好世界")],
    )
    document = apply_template_to_project(
        project,
        _form_template(use_pos=False),
        coord_provider=_coord,
    )
    assert "[Script Info]" in document
    assert "[Aegisub Project Garbage]" in document
    assert "Style: Default-furigana" in document
    assert document.count("Comment:") == 2  # template + 原 k-tag karaoke
    assert document.count("Dialogue:") == 5  # kara[0] + 4 syllable fx
    assert "template syl" in document
    assert ",karaoke,{\\kf" in document
    assert all(
        ",fx," in line and r"{\kf" not in line and r"\alpha&HFF&" not in line
        for line in _dialogues(document.splitlines())
    )
    source_pos = document.index(",karaoke,{\\kf")
    fx_pos = document.index(",fx,")
    assert source_pos < fx_pos  # 原 Comment 保持在前，生成 fx 追加到 Events 尾部


def _case_qss_golden_shape_without_reference_asset():
    import json

    from subs.ass_style import AssStylePrefs

    project = SubtitleProject.from_dict(json.loads(
        (PROJECT_ROOT / "test-short-talk.qss.json").read_text(encoding="utf-8")
    ))
    template = KaraokeTemplate(
        name="参考模板",
        template_class="syl",
        modifiers=["all"],
        anchor=5,
        use_pos=True,
        fad_in_ms=80,
        fad_out_ms=80,
        scale_enabled=True,
        scale_percent=116,
        glow_enabled=True,
        glow_bord=4,
        glow_blur=2,
    )
    document = apply_template_to_project(
        project,
        template,
        k_mode="K",
        ass_style=AssStylePrefs(name="Default", font_name="江城圆体 500W", font_size=60),
        coord_provider=_coord,
    )
    # Aegisub golden 已提炼成稳定结构合同：1 template + 6 karaoke Comment、
    # 52 条模板 fx；现役成品另为 6 个句末标点各生成一条基础样式定位行。
    assert sum(line.startswith("Comment:") for line in document.splitlines()) == 7
    assert sum(",karaoke," in line for line in document.splitlines()) == 6
    assert sum(line.startswith("Dialogue:") for line in document.splitlines()) == 58
    punctuation_lines = [
        line for line in document.splitlines()
        if line.startswith("Dialogue:")
        and line.endswith(("，", "。", "？"))
        and r"\fscx" not in line
    ]
    assert len(punctuation_lines) == 6
    assert all(r"\an5\pos(100,200)" in line for line in punctuation_lines)
    assert all(
        not any(tag in line for tag in (r"\fad", r"\fscx", r"\bord", r"\blur", r"\t("))
        for line in punctuation_lines
    )
    templated_fx = [
        line for line in document.splitlines()
        if line.startswith("Dialogue:") and line not in punctuation_lines
    ]
    assert len(templated_fx) == 52
    assert all(r"\alpha&HFF&" not in line for line in templated_fx)


def _case_missing_word_level_is_explicit_error():
    project = SubtitleProject(sentences=[
        _sentence("你好世界"),
        Sentence(text="无字级", start_time=2.0, end_time=3.0),
    ])
    with pytest.raises(ValueError, match="缺失句号：2"):
        apply_template_to_project(project, _form_template())


def test_karaoke_templater_pack():
    _case_split_syllables_from_k_tag_truth()
    _case_syl_matches_aegisub_shape()
    _case_line_and_char_semantics()
    _case_modifiers_and_safe_expressions()
    _case_punctuation_stays_in_place_without_template_effects()
    _case_project_document_matches_apply_structure()
    _case_qss_golden_shape_without_reference_asset()
    _case_missing_word_level_is_explicit_error()
