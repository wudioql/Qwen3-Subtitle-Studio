"""Aegisub Karaoke Templater 的安全 Python 子集。

把模板 Comment + timed k-tag 源行展开为 Aegisub Apply 后的 ASS 结构：
- 模板 Comment 保留；
- 原 k-tag Dialogue 攴为 ``Comment/effect=karaoke``（播放器不渲染）；
- 模板生成的可见内容为 ``Dialogue/effect=fx``；
- ``template syl`` 每个 syllable 只生成自身，不复制整句/透明其它字；
- 标点从模板 fx 正文剥离，并以基础样式独立定位；无坐标时才回退透明遮罩；
- 模板 start/end 使用真实字时间，不吞掉后续停顿或标点时间；
- 未设 noblank 时保留 Aegisub 的零号空 syllable；
- 时间从 ``build_karaoke_line_text`` 的厘秒真源反解，避免预览与导出各算一套。

不执行任意 Lua。支持普通变量和 ``!$var±number!`` 安全算术；其它 Lua 表达式抛
``LuaExpressionError``，由调用方明确降级。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Callable, List, Tuple

from .ass_karaoke import (
    _aegisub_garbage_block,
    _normalize_k_mode,
    _script_info_from_ass_style,
    build_karaoke_line_text,
)
from .ass_style import AssStylePrefs
from .ass_utils import escape_ass_text, sanitize_ass_field, unescape_ass_text
from .converter import WordHighlightStyle, _filter_animation_words
from .models import Sentence, SubtitleProject
from .time_fmt import ass_time

# coord_provider(syl_index, syl_text, line_text) -> (sx, sy, lx, ly)
CoordProvider = Callable[[int, str, str], Tuple[int, int, int, int]]

_K_TAG_RE = re.compile(r"\{\\(?:[kK][fFoO]?)(\d+)\}")
_SIMPLE_EXPR_RE = re.compile(
    r"^\$?([A-Za-z_]+)\s*([+-])\s*(\d+(?:\.\d+)?)$"
)
_LUA_HINTS = (
    "(", ")", "=", "..", "math", "string", "function", "return", "[", "]", "*", "/", "%", "^",
)
_SPACE_CHARS = " \t\r\n\u3000"


class LuaExpressionError(ValueError):
    """模板含不在安全子集内的 Lua 表达式。"""


@dataclass
class Syllable:
    """k-tag 音节；时间相对原 Dialogue 行起点，单位毫秒。"""

    text: str
    start_ms: int
    end_ms: int
    index: int = 0

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def _strip_syllable_text(text: str) -> str:
    """近似 Aegisub ``syl.text_stripped``：去两侧普通/全角空白。"""
    return str(text or "").strip(_SPACE_CHARS)


# 特殊装饰符号（音乐装饰符号 ♪ ♫ ♬ ♩ 及相关特殊字符 #）
# 这些字符在模板应用时应被视为标点（不生成可见动画音节，但保留在基础样式中）
_DECORATION_SYMBOLS = set("♪♫♬♩#")


def _is_punctuation(char: str) -> bool:
    """覆盖中西文及其它 Unicode 标点类别（P*），并包含特殊装饰符号。"""
    if char in _DECORATION_SYMBOLS:
        return True
    return bool(char) and unicodedata.category(char).startswith("P")


def _without_punctuation(text: str) -> str:
    return "".join(char for char in text if not _is_punctuation(char))


def _punctuation_mask(text: str) -> str:
    """无坐标 provider 时的保底：只显示标点，其余文字透明但仍占位。"""
    if not any(_is_punctuation(char) for char in text):
        return ""
    parts: list[str] = []
    visible: bool | None = None
    for char in text:
        char_visible = _is_punctuation(char)
        if char_visible != visible:
            alpha = "00" if char_visible else "FF"
            parts.append(fr"{{\alpha&H{alpha}&}}")
            visible = char_visible
        parts.append(escape_ass_text(char))
    return "".join(parts)


def _split_punctuation_runs(text: str) -> list[tuple[str, bool]]:
    runs: list[tuple[str, bool]] = []
    for char in text:
        is_punct = _is_punctuation(char)
        if runs and runs[-1][1] == is_punct:
            runs[-1] = (runs[-1][0] + char, is_punct)
        else:
            runs.append((char, is_punct))
    return runs


def _clamp_effect_times_to_words(
    sentence: Sentence,
    syllables: list[Syllable],
) -> list[Syllable]:
    """模板动画只使用真实字的 start/end，不吞掉后续停顿或标点时长。"""
    words = _filter_animation_words(list(sentence.words or []))
    if not words:
        return syllables
    total_ms = max(0, int(round(sentence.duration * 100)) * 10)
    word_cursor = 0
    output: list[Syllable] = []
    for syllable in syllables:
        core = _without_punctuation(_strip_syllable_text(syllable.text))
        matched = None
        if core:
            for index in range(word_cursor, len(words)):
                candidate = _without_punctuation(_strip_syllable_text(words[index].text))
                if candidate == core:
                    matched = words[index]
                    word_cursor = index + 1
                    break
        if matched is None:
            output.append(syllable)
            continue
        start_ms = int(round((matched.start_time - sentence.start_time) * 100)) * 10
        end_ms = int(round((matched.end_time - sentence.start_time) * 100)) * 10
        start_ms = max(0, min(total_ms, start_ms))
        end_ms = max(start_ms, min(total_ms, end_ms))
        output.append(replace(syllable, start_ms=start_ms, end_ms=end_ms))
    return output


def _parse_karaoke_syllables(source_text: str) -> List[Syllable]:
    """从标准 k-tag Text 反解音节；首项恒为 Aegisub kara[0] 空音节。"""
    matches = list(_K_TAG_RE.finditer(source_text))
    syllables = [Syllable(text="", start_ms=0, end_ms=0, index=0)]
    elapsed_cs = 0
    for index, match in enumerate(matches, start=1):
        duration_cs = max(0, int(match.group(1)))
        next_start = matches[index].start() if index < len(matches) else len(source_text)
        encoded_text = source_text[match.end():next_start]
        text = unescape_ass_text(encoded_text)
        start_ms = elapsed_cs * 10
        elapsed_cs += duration_cs
        syllables.append(
            Syllable(text=text, start_ms=start_ms, end_ms=elapsed_cs * 10, index=index)
        )
    return syllables


def split_syllables(
    sentence: Sentence,
    *,
    style: WordHighlightStyle | None = None,
    k_mode: str = "kf",
) -> List[Syllable]:
    """按实际 k-tag 导出真源切分，包含索引 0 的空 syllable。"""
    source = build_karaoke_line_text(
        sentence,
        k_mode=_normalize_k_mode(str(k_mode)),
        style=style,
    )
    return _parse_karaoke_syllables(source)


def _split_chars(syllables: List[Syllable]) -> List[Syllable]:
    """Aegisub ``template char``：逐字符生成，但沿用所属 syllable 的完整时间。"""
    out: List[Syllable] = []
    next_index = 0
    for syllable in syllables:
        text = _strip_syllable_text(syllable.text)
        for char in text:
            out.append(
                Syllable(
                    text=char,
                    start_ms=syllable.start_ms,
                    end_ms=syllable.end_ms,
                    index=next_index,
                )
            )
            next_index += 1
    return out


def _template_parts(template) -> Tuple[str, List[str], str]:
    """解析模板 → (class, modifiers, code)。form 用字段；raw 解析 Comment。"""
    if template.mode == "raw":
        body = template.render_comment().strip()
        if body.startswith(("Comment:", "Dialogue:")):
            body = body.split(":", 1)[1].lstrip()
        parts = body.split(",", 9)
        effect = parts[8].strip() if len(parts) > 8 else ""
        code = parts[9] if len(parts) > 9 else ""
        tokens = effect.split()
        cls = tokens[1] if len(tokens) > 1 and tokens[0].lower() == "template" else "syl"
        mods = [
            token.lower() for token in tokens[2:]
            if token.lower() in ("all", "noblank", "keeptags", "notext")
        ]
        return cls.lower(), mods, code.strip()
    return (
        template.template_class if template.template_class in ("syl", "line", "char") else "syl",
        [
            modifier for modifier in (template.modifiers or [])
            if modifier in ("all", "noblank", "keeptags", "notext")
        ],
        template.tag_body(),
    )


def _number_text(value: float | int) -> str:
    value = float(value)
    return str(int(round(value))) if value.is_integer() else f"{value:g}"


def _expand(
    text: str,
    *,
    syl: Syllable,
    line: Syllable,
    coord: CoordProvider | None,
) -> str:
    """展开 Aegisub 常用变量和安全 ``!$var±number!`` 表达式。"""
    start, end = syl.start_ms, syl.end_ms
    duration = max(0, end - start)
    middle = start + duration / 2
    line_duration = max(0, line.end_ms - line.start_ms)
    values: dict[str, float | int] = {
        "start": start,
        "end": end,
        "mid": middle,
        "dur": duration,
        "kdur": duration / 10,  # Aegisub kdur/skdur 单位是厘秒
        "sstart": start,
        "send": end,
        "smid": middle,
        "sdur": duration,
        "skdur": duration / 10,
        "lstart": line.start_ms,
        "lend": line.end_ms,
        "lmid": line.start_ms + line_duration / 2,
        "ldur": line_duration,
        "i": syl.index,
        "si": syl.index,
    }
    center_x = center_y = line_x = line_y = 0
    if coord is not None:
        try:
            center_x, center_y, line_x, line_y = coord(syl.index, syl.text, line.text)
        except Exception:  # noqa: BLE001
            pass
    values.update({
        "scenter": center_x,
        "smiddle": center_y,
        "lcenter": line_x,
        "lmiddle": line_y,
    })

    def resolve(name: str) -> str | None:
        key = name.lstrip("$").strip()
        if key in values:
            return _number_text(values[key])
        return None

    def replace_expression(match: "re.Match[str]") -> str:
        expression = match.group(1).strip()
        direct = resolve(expression)
        if direct is not None:
            return direct
        simple = _SIMPLE_EXPR_RE.fullmatch(expression)
        if simple and simple.group(1) in values:
            base = float(values[simple.group(1)])
            delta = float(simple.group(3))
            result = base + delta if simple.group(2) == "+" else base - delta
            return _number_text(result)
        if any(hint in expression for hint in _LUA_HINTS) or re.search(r"[+\-]", expression):
            raise LuaExpressionError(f"模板含不支持的 Lua 表达式 !{expression}!")
        return match.group(0)

    expanded = re.sub(r"!([^!]+)!", replace_expression, text)

    def replace_variable(match: "re.Match[str]") -> str:
        name = match.group(1)
        return _number_text(values[name]) if name in values else match.group(0)

    return re.sub(r"\$([A-Za-z_]+)", replace_variable, expanded)


def _event(
    prefix: str,
    layer: int,
    sentence: Sentence,
    style_name: str,
    effect: str,
    text: str,
) -> str:
    return (
        f"{prefix}: {max(0, int(layer))},{ass_time(sentence.start_time)},"
        f"{ass_time(sentence.end_time)},{sanitize_ass_field(style_name, fallback='Default')},"
        f"{sanitize_ass_field(sentence.speaker)},0,0,0,{effect},{text}"
    )


def _template_matches_style(template, modifiers: List[str], style_name: str) -> bool:
    if "all" in modifiers:
        return True
    wanted = sanitize_ass_field(getattr(template, "style_name", "Default"), fallback="Default")
    return wanted == sanitize_ass_field(style_name, fallback="Default")


def _is_blank_syllable(syllable: Syllable) -> bool:
    return syllable.duration_ms <= 0 or not _strip_syllable_text(syllable.text)


def apply_template(
    sentence: Sentence,
    template,
    *,
    k_mode: str = "kf",
    style: WordHighlightStyle | None = None,
    coord_provider: CoordProvider | None = None,
    style_name: str = "Default",
) -> List[str]:
    """对一句应用模板，返回 Aegisub 形态的 ``Dialogue/effect=fx`` 行。

    模板行不携带可见标点。坐标可用时，每个标点段以基础样式 + 独立 ``pos``
    绘制，和模板字共用同一字体度量坐标系；无坐标 provider 才回退整句透明
    遮罩。模板变量的 start/end 取真实字时间，不包含后续停顿或标点时长。
    """
    mode = _normalize_k_mode(str(k_mode))
    cls, modifiers, code = _template_parts(template)
    if not _template_matches_style(template, modifiers, style_name):
        return []

    source_syllables = split_syllables(sentence, style=style, k_mode=mode)
    syllables = _clamp_effect_times_to_words(sentence, source_syllables)
    line = Syllable(
        text=sentence.text,
        start_ms=0,
        end_ms=max((item.end_ms for item in source_syllables), default=0),
        index=0,
    )
    notext = "notext" in modifiers
    layer = max(0, int(getattr(template, "layer", 0) or 0))
    coordinate_index = 0
    coordinate_failed = False
    punctuation_positions: list[tuple[str, Tuple[int, int, int, int] | None]] = []

    def next_coordinate(text: str) -> Tuple[int, int, int, int] | None:
        nonlocal coordinate_index, coordinate_failed
        if coord_provider is None:
            return None
        index = coordinate_index
        coordinate_index += 1
        try:
            values = coord_provider(index, text, line.text)
            return tuple(int(round(value)) for value in values)  # type: ignore[return-value]
        except Exception:  # noqa: BLE001
            coordinate_failed = True
            return None

    def fixed_provider(values: Tuple[int, int, int, int] | None) -> CoordProvider | None:
        if values is None:
            return None
        return lambda _index, _text, _line: values

    def expanded(syllable: Syllable, run: str, coord) -> str:
        effect_syllable = replace(syllable, text=run)
        return _expand(
            code,
            syl=effect_syllable,
            line=line,
            coord=fixed_provider(coord),
        )

    def collect_runs(syllable: Syllable, destination: list[str]) -> None:
        stripped = _strip_syllable_text(syllable.text)
        skip_effect = "noblank" in modifiers and _is_blank_syllable(syllable)
        if not stripped:
            if not skip_effect:
                coord = next_coordinate("")
                destination.append(expanded(syllable, "", coord))
            return
        for run, is_punct in _split_punctuation_runs(stripped):
            coord = next_coordinate(run)
            if is_punct:
                punctuation_positions.append((run, coord))
                continue
            if skip_effect:
                continue
            tags = expanded(syllable, run, coord)
            visible = "" if notext else escape_ass_text(run)
            destination.append(tags + visible)

    if cls == "line":
        # kara[0] 空音节不参与 line 循环；其它文字仍拼成一条模板 Dialogue。
        parts: List[str] = []
        for syllable in syllables[1:]:
            collect_runs(syllable, parts)
        output = [_event("Dialogue", layer, sentence, style_name, "fx", "".join(parts))]
    else:
        targets = _split_chars(syllables) if cls == "char" else syllables
        output = []
        for syllable in targets:
            parts: list[str] = []
            collect_runs(syllable, parts)
            output.extend(
                _event("Dialogue", layer, sentence, style_name, "fx", part)
                for part in parts
            )

    if not notext and punctuation_positions:
        if coord_provider is not None and not coordinate_failed and all(
            coord is not None for _run, coord in punctuation_positions
        ):
            for run, coord in punctuation_positions:
                assert coord is not None
                x, y, _line_x, _line_y = coord
                text = fr"{{\an5\pos({x},{y})}}" + escape_ass_text(run)
                output.append(_event("Dialogue", layer, sentence, style_name, "fx", text))
        else:
            punctuation = _punctuation_mask(sentence.text)
            if punctuation:
                output.append(_event("Dialogue", layer, sentence, style_name, "fx", punctuation))
    return output


def _styles_block_with_furigana(style: AssStylePrefs) -> str:
    """复现 karaskel collect_head(..., true) 生成的 ``<Style>-furigana``。"""
    furigana = replace(
        style,
        name=f"{style.name}-furigana",
        font_size=max(1, int(round(style.font_size / 2))),
        outline=max(0.0, style.outline / 2),
        shadow=max(0.0, style.shadow / 2),
    )
    fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    return "\n".join([
        "[V4+ Styles]",
        fmt,
        furigana.render_style_line(),
        style.render_style_line(),
        "",
    ])


def apply_template_to_project(
    project: SubtitleProject,
    template,
    *,
    k_mode: str = "kf",
    style: WordHighlightStyle | None = None,
    ass_style: AssStylePrefs | None = None,
    coord_provider: CoordProvider | None = None,
) -> str:
    """返回 Aegisub Apply 后结构的完整 ASS；可直接交给 libass 或导出。"""
    missing = [index + 1 for index, sentence in enumerate(project.sentences) if not sentence.has_word_level()]
    if missing:
        preview = ", ".join(map(str, missing[:12]))
        suffix = "…" if len(missing) > 12 else ""
        raise ValueError(f"应用卡拉OK模板要求每句都有字级时间；缺失句号：{preview}{suffix}")

    effective_style = ass_style or AssStylePrefs()
    parts = [
        _script_info_from_ass_style(effective_style).replace(
            "(Aegisub karaoke source)", "(karaoke template applied)"
        ),
        _aegisub_garbage_block(
            source_media_path=project.source_media_path,
            audio_path=project.audio_path,
            video_path=project.video_path,
        ),
        _styles_block_with_furigana(effective_style),
    ]
    events: List[str] = [
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    template_comment = template.render_comment(effective_style.name)
    if template_comment:
        events.append(template_comment)

    source_events: List[str] = []
    generated_fx: List[str] = []
    mode = _normalize_k_mode(str(k_mode))
    for sentence in project.sentences:
        style_name = sanitize_ass_field(sentence.ass_style, fallback=effective_style.name)
        source_text = build_karaoke_line_text(sentence, k_mode=mode, style=style)
        sentence_fx = apply_template(
            sentence,
            template,
            k_mode=mode,
            style=style,
            coord_provider=coord_provider,
            style_name=style_name,
        )
        if sentence_fx:
            source_events.append(
                _event("Comment", 0, sentence, style_name, "karaoke", source_text)
            )
            generated_fx.extend(sentence_fx)
        else:
            # 模板样式不匹配时 Aegisub 不会把源行注释掉；保留可见 k-tag Dialogue。
            source_events.append(
                _event("Dialogue", 0, sentence, style_name, "", source_text)
            )

    # Aegisub 保留原行位置并把生成 fx 追加到 Events 尾部。
    events.extend(source_events)
    events.extend(generated_fx)
    parts.append("\n".join(events).rstrip())
    return "\n".join(parts) + "\n"


__all__ = [
    "LuaExpressionError",
    "Syllable",
    "split_syllables",
    "apply_template",
    "apply_template_to_project",
]
