"""subs.exporters — 对外导出函数总入口（5 类 / 6 种产物）

API 一览：
    mode: Literal["per_sentence", "per_word"]
        - per_sentence: 1 句 = 1 条 cue（句级，纯文本 / 无逐字效果）
        - per_word:     1 字 = 1 条 cue（SRT/VTT/ASS_plain_split）或
                        1 句 = 1 条 cue 内嵌 \t 动画（ASS_plain_t）

    WordHighlightStyle 控制「per_word 时，当前字长什么样」（高亮色 + 4 个字形附加项 + ASS 扩展）
    所有 exporter 对「没有 word_level 的句子」自动降级为 per_sentence 单条，
    不抛异常（k-tag ASS 除外：k-tag 必须字级，缺了直接抛 ValueError）。

函数（按对外约定顺序）：
    1. to_srt(project, style, mode="per_word") -> str
    2. to_vtt(project, style, mode="per_word") -> str
    3. to_ass(project, style, mode="per_word", strategy="split") -> str   # 非 k-tag；strategy="split"|"t"|"per_sentence"
    4. to_lrc(project, enhanced=True) -> str
    5. to_ass_karaoke(project, k_mode="kf", ...) -> str

函数都是"返回多行长字符串"；调用方负责写盘或挂到 QFileDialog。
"""

from __future__ import annotations

import html
from typing import List, Literal, Optional

from .ass_karaoke import (
    KMode,
    AssKaraokeHeader,
    build_ass_document as _build_k_ass,
)
from .ass_style import AssStylePrefs
from .ass_utils import (
    escape_ass_text,
    sanitize_ass_field,
    sanitize_ass_tag_payload,
)
from .converter import (
    AssTSegment,
    WordHighlightStyle,
    _filter_animation_words,
    iter_ass_t_segments,
    iter_sentence_word_cues,
)
from .models import Sentence, SubtitleProject
from .time_fmt import ass_time, lrc_time, srt_time, vtt_time


# ─────────────────────────────────────────────────────────────
# 公共辅助：收集所有要遍历的 sentences（统一 sort）
# ─────────────────────────────────────────────────────────────
def _iter_sentences(project: SubtitleProject) -> List[Sentence]:
    out = sorted(project.sentences, key=lambda s: s.start_time)
    return out


# ─────────────────────────────────────────────────────────────
# 1. SRT
# ─────────────────────────────────────────────────────────────
def _render_srt_per_word_cue(
    cue, idx: int, style: WordHighlightStyle
) -> str:
    open_ = style.html_tags_open()
    close = style.html_tags_close()
    seg = cue.segments
    body = (
        html.escape(seg.before, quote=False)
        + f"{open_}{html.escape(seg.current, quote=False)}{close}"
        + html.escape(seg.after, quote=False)
    )
    return (
        f"{idx}\n"
        f"{srt_time(cue.start_time)} --> {srt_time(cue.end_time)}\n"
        f"{body}\n"
    )


def _render_srt_per_sentence(s: Sentence, idx: int) -> str:
    return (
        f"{idx}\n"
        f"{srt_time(s.start_time)} --> {srt_time(s.end_time)}\n"
        f"{html.escape(s.text, quote=False)}\n"
    )


def to_srt(
    project: SubtitleProject,
    style: WordHighlightStyle | None = None,
    mode: Literal["per_sentence", "per_word"] = "per_word",
) -> str:
    style = style or WordHighlightStyle()
    sentences = _iter_sentences(project)
    cues: list[str] = []
    cue_index = 1

    for s in sentences:
        if mode == "per_word" and s.has_word_level():
            for cue in iter_sentence_word_cues(s, style):
                cues.append(_render_srt_per_word_cue(cue, cue_index, style))
                cue_index += 1
        else:
            cues.append(_render_srt_per_sentence(s, cue_index))
            cue_index += 1
    # SRT 没有 NOTE header；降级提示属于 UI，不得写成伪 cue 污染文件。
    return "\n".join(cues).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────
# 2. WebVTT
# ─────────────────────────────────────────────────────────────
def _render_vtt_per_word_cue(cue, style: WordHighlightStyle) -> str:
    open_ = style.html_tags_open()
    close = style.html_tags_close()
    seg = cue.segments
    body = (
        html.escape(seg.before, quote=False)
        + f"{open_}{html.escape(seg.current, quote=False)}{close}"
        + html.escape(seg.after, quote=False)
    )
    return (
        f"{vtt_time(cue.start_time, with_hours=False)} --> "
        f"{vtt_time(cue.end_time, with_hours=False)}\n"
        f"{body}\n"
    )


def _render_vtt_per_sentence(s: Sentence) -> str:
    return (
        f"{vtt_time(s.start_time, with_hours=False)} --> "
        f"{vtt_time(s.end_time, with_hours=False)}\n"
        f"{html.escape(s.text, quote=False)}\n"
    )


def to_vtt(
    project: SubtitleProject,
    style: WordHighlightStyle | None = None,
    mode: Literal["per_sentence", "per_word"] = "per_word",
) -> str:
    style = style or WordHighlightStyle()
    sentences = _iter_sentences(project)
    cues: list[str] = []
    notes: list[str] = []
    has_any_word = project.has_word_level()
    if not has_any_word and mode == "per_word":
        notes.append(
            "NOTE "
            + f"{len(sentences)} sentences have no word-level timestamps; "
            + "auto-falling back to per-sentence mode."
        )

    for s in sentences:
        if mode == "per_word" and s.has_word_level():
            for cue in iter_sentence_word_cues(s, style):
                cues.append(_render_vtt_per_word_cue(cue, style))
        else:
            cues.append(_render_vtt_per_sentence(s))

    head_lines = ["WEBVTT FILE", ""]
    if notes:
        head_lines.extend(notes)
        head_lines.append("")
    return "\n".join(head_lines + cues).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────
# 3. 非 k-tag ASS（三种 strategy：per_sentence / split / t）
# ─────────────────────────────────────────────────────────────
AssStrategy = Literal["per_sentence", "split", "t"]


def build_plain_ass_header(ass_style: AssStylePrefs | None = None) -> str:
    """非 k-tag ASS 统一 header（mpv / PotPlayer 都认识）。

    用 AssStylePrefs 渲染，未传则用默认（保持与历史一致的外观）。
    """
    st = ass_style or AssStylePrefs()
    script_info = "\n".join([
        "[Script Info]",
        "; Non-karaoke plain ASS (preview). Exported by Qwen3-Subtitle-Studio.",
        "ScriptType: v4.00+",
        "Collisions: Normal",
        f"PlayResX: {st.play_res_x}",
        f"PlayResY: {st.play_res_y}",
        f"WrapStyle: {st.wrap_style}",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
    ])
    return (
        script_info
        + st.render_styles_block()
        + "[Events]\n"
        + "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


# 向后兼容：模块级默认 header（旧代码/测试可能引用）
_DEFAULT_ASS_HEADER = build_plain_ass_header()


def _ass_default_style_for(s: Sentence) -> str:
    return sanitize_ass_field(s.ass_style, fallback="Default")


def _render_ass_dialogue(
    s: Sentence,
    text: str,
    *,
    override_start: Optional[str] = None,
    text_is_markup: bool = False,
) -> str:
    st = _ass_default_style_for(s)
    nm = sanitize_ass_field(s.speaker)
    extra = sanitize_ass_tag_payload(s.ass_extra_tags)
    t = text if text_is_markup else escape_ass_text(text)
    if override_start:
        t = override_start + t
    if extra:
        t = "{" + extra + "}" + t
    return (
        f"Dialogue: 0,{ass_time(s.start_time)},{ass_time(s.end_time)},"
        f"{st},{nm},0000,0000,0000,,{t}"
    )


def _render_ass_per_sentence(project: SubtitleProject) -> list[str]:
    sentences = _iter_sentences(project)
    return [_render_ass_dialogue(s, s.text) for s in sentences]


def _render_ass_split(
    project: SubtitleProject, style: WordHighlightStyle
) -> tuple[list[str], list[str]]:
    """Strategy 'split': 1 字 1 Dialogue（和 SRT/VTT 逐字同源）"""
    sentences = _iter_sentences(project)
    comments: list[str] = []
    dialogues: list[str] = []
    if not project.has_word_level():
        comments.append(
            "Comment: 0,0:00:00.00,0:00:00.01,Default,,0000,0000,0000,,"
            "no word-level timestamps; falling back to per-sentence mode"
        )
        dialogues.extend(_render_ass_per_sentence(project))
        return comments, dialogues

    for s in sentences:
        if not s.has_word_level():
            dialogues.append(_render_ass_dialogue(s, s.text))
            continue
        open_ov = style.ass_override_open()
        close_ov = style.ass_override_close()
        cues = list(iter_sentence_word_cues(s, style))
        for cue_index, cue in enumerate(cues):
            seg = cue.segments
            # 前后恢复 Style，当前字使用明确高亮色；事件延续到下一字开始，
            # 避免 forced alignment 的字间空隙让整行字幕短暂消失。
            text = (
                close_ov + escape_ass_text(seg.before)
                + open_ov + escape_ass_text(seg.current)
                + close_ov + escape_ass_text(seg.after)
            )
            next_start = cues[cue_index + 1].start_time if cue_index + 1 < len(cues) else s.end_time
            cue_end = max(cue.end_time, next_start)
            d = (
                f"Dialogue: 0,{ass_time(cue.start_time)},{ass_time(cue_end)},"
                f"{_ass_default_style_for(s)},{sanitize_ass_field(s.speaker)},0000,0000,0000,,"
            )
            extra = sanitize_ass_tag_payload(s.ass_extra_tags)
            if extra:
                text = "{" + extra + "}" + text
            dialogues.append(d + text)
    return comments, dialogues


def _ass_override_color(rgb: str) -> str:
    raw = (rgb or "#FFFFFF").strip().lstrip("#")
    if len(raw) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in raw):
        raw = "FFFFFF"
    r, g, b = raw[0:2], raw[2:4], raw[4:6]
    return rf"\1c&H{b}{g}{r}&".upper().replace("\\1C", "\\1c")


def _render_ass_t_line(
    s: Sentence,
    style: WordHighlightStyle,
    ass_style: AssStylePrefs,
) -> str:
    """一句一个事件，用可动画的 ``\\1c`` 实现当前字变色。

    旧实现把 ``\\b/\\i/\\u/\\s`` 和 ``\\r`` 塞进 ``\\t``；这些标签不是
    跨播放器可靠的 transform 属性，PotPlayer 中通常看不到效果。现在每个字有
    独立 override 作用域，使用 ASS 规范明确支持动画的主颜色 ``\\1c``，并在
    字结束时恢复文字样式的主色。
    """
    segs: list[AssTSegment] = iter_ass_t_segments(s, style)
    if not segs:
        return _render_ass_dialogue(s, s.text)

    highlight = style.ass_color_override()
    base = _ass_override_color(ass_style.primary_color)
    parts: list[str] = []
    cursor = 0
    line_duration_ms = max(1, int(round(s.duration * 1000)))
    for seg in segs:
        if seg.text_start > cursor:
            parts.append(escape_ass_text(s.text[cursor:seg.text_start]))
        t1 = min(line_duration_ms, max(0, int(seg.relative_t1_ms)))
        t2 = min(line_duration_ms, max(t1 + 1, int(seg.relative_t2_ms)))
        # 1ms 过渡近似瞬时切换；\r 放在每字块开头，限定 transform 只作用于该字。
        transforms = f"\\t({t1},{min(line_duration_ms, t1 + 1)},{highlight})"
        if t2 < line_duration_ms:
            transforms += f"\\t({t2},{t2 + 1},{base})"
        block = "{\\r" + transforms + "}"
        # 字后立即恢复 Style，确保紧随其后的标点不继承当前字颜色动画。
        parts.append(block + escape_ass_text(seg.text) + r"{\r}")
        cursor = seg.text_end
    if cursor < len(s.text):
        parts.append(escape_ass_text(s.text[cursor:]))
    return _render_ass_dialogue(s, "".join(parts), text_is_markup=True)


def _render_ass_t(
    project: SubtitleProject,
    style: WordHighlightStyle,
    ass_style: AssStylePrefs,
) -> tuple[list[str], list[str]]:
    """Strategy 't': 1 句 1 Dialogue，每字用 ``\\t + \\1c`` 触发颜色高亮。"""
    sentences = _iter_sentences(project)
    comments: list[str] = []
    dialogues: list[str] = []
    if not project.has_word_level():
        comments.append(
            "Comment: 0,0:00:00.00,0:00:00.01,Default,,0000,0000,0000,,"
            "no word-level timestamps; falling back to per-sentence mode"
        )
        dialogues.extend(_render_ass_per_sentence(project))
        return comments, dialogues
    for s in sentences:
        dialogues.append(_render_ass_t_line(s, style, ass_style))
    return comments, dialogues


def to_ass(
    project: SubtitleProject,
    style: WordHighlightStyle | None = None,
    mode: Literal["per_sentence", "per_word"] = "per_word",
    strategy: AssStrategy = "split",
    header: Optional[str] = None,
    ass_style: "AssStylePrefs | None" = None,
) -> str:
    """非 k-tag ASS（逐字预览用）。

    strategy 仅当 mode="per_word" 时生效：
        - "split"   : 1 字 1 Dialogue，当前字用明确颜色高亮（mpv/PotPlayer 最稳）
        - "t"       : 1 句 1 Dialogue，逐字用 ``\\t + \\1c`` 做颜色切换（文件小）
        - "per_sentence": 显式走句级（与 mode="per_sentence" 等价）

    ass_style: ASS 文字样式（字体/颜色/描边等）；None 用默认。仅当 header 未显式传入时生效。
    """
    style = style or WordHighlightStyle()
    effective_ass_style = ass_style or AssStylePrefs()
    if header is not None:
        header_text = header
    elif ass_style is not None:
        header_text = build_plain_ass_header(ass_style)
    else:
        header_text = _DEFAULT_ASS_HEADER

    eff_strategy: AssStrategy = strategy
    if mode == "per_sentence":
        eff_strategy = "per_sentence"
    if not project.has_word_level() and mode == "per_word":
        # 自动降级提示放在 strategy 各自的 comment 里
        pass

    if eff_strategy == "per_sentence":
        dialogues = _render_ass_per_sentence(project)
        comments: list[str] = []
    elif eff_strategy == "t":
        comments, dialogues = _render_ass_t(project, style, effective_ass_style)
    else:  # split
        comments, dialogues = _render_ass_split(project, style)

    body = header_text.rstrip() + "\n"
    if comments:
        body += "\n".join(comments) + "\n"
    if dialogues:
        body += "\n".join(dialogues) + "\n"
    return body


# ─────────────────────────────────────────────────────────────
# 4. LRC 写（读放 lrc_io.py）
# ─────────────────────────────────────────────────────────────
def to_lrc(project: SubtitleProject, *, enhanced: bool = True) -> str:
    """写标准 LRC 或兼容型 Enhanced LRC。

    标准句级：``[mm:ss.xx]整句文本``

    Enhanced LRC 使用通行的“字/词开始时间”单时间戳：
    ``[mm:ss.xx]<mm:ss.xx>字<mm:ss.xx>字``。

    旧版写成 ``<start,end>`` 双时间戳，这不是多数播放器识别的 Enhanced LRC
    形式，PotPlayer 会把它当普通文本或拒绝字级解析。标点保留显示，但不带
    独立 inline timestamp。无 word-level 时自动降级句级。
    """
    sentences = _iter_sentences(project)
    lines: list[str] = []
    any_word = project.has_word_level() and enhanced

    def _fmt_word_start(start_s: float) -> str:
        return f"<{lrc_time(start_s).strip('[]')}>"

    for s in sentences:
        lead = lrc_time(s.start_time)
        if not any_word or not s.has_word_level():
            lines.append(f"{lead}{s.text}")
            continue
        # 所有显式/隐式标点都不参与字级时间标签，但仍从原句 text 中输出。
        words = _filter_animation_words(list(s.words))
        cursor = 0
        buf = lead
        for w in words:
            pos = s.text.find(w.text, cursor)
            if pos < 0:
                pos = cursor
            if pos > cursor:
                buf += s.text[cursor:pos]
            buf += _fmt_word_start(w.start_time) + s.text[pos : pos + len(w.text)]
            cursor = pos + len(w.text)
        if cursor < len(s.text):
            buf += s.text[cursor:]
        lines.append(buf)
    return "\n".join(lines).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────
# 5. k-tag ASS（给 Aegisub）
# ─────────────────────────────────────────────────────────────
def to_ass_karaoke(
    project: SubtitleProject,
    *,
    k_mode: KMode = "kf",
    style: WordHighlightStyle | None = None,
    header: Optional[AssKaraokeHeader] = None,
    include_aegisub_garbage: bool = True,
    include_automation_template: bool = True,
    ass_style: "AssStylePrefs | None" = None,
    template_prefs=None,
) -> str:
    """导出标准 Aegisub k-tag 计时源。必须有 word-level。

    默认附带合法的示例 Karaoke Template，使 Aegisub 打开后可直接应用；
    模板 Comment 不参与 PotPlayer 渲染。源字幕仍保持 Dialogue + 空 Effect。
    """
    if not project.has_word_level():
        raise ValueError(
            "to_ass_karaoke requires word-level timestamps on at least one sentence. "
            "Use to_ass(mode='per_sentence') or run forced align first."
        )
    sentences = _iter_sentences(project)
    return _build_k_ass(
        sentences,
        header=header,
        k_mode=k_mode,
        include_aegisub_garbage=include_aegisub_garbage,
        include_automation_template=include_automation_template,
        style=style,
        ass_style=ass_style,
        template_prefs=template_prefs,
        source_media_path=project.source_media_path,
        audio_path=project.audio_path,
        video_path=project.video_path,
    )


def to_ass_karaoke_applied(
    project: SubtitleProject,
    *,
    k_mode: KMode = "kf",
    style: WordHighlightStyle | None = None,
    ass_style: "AssStylePrefs | None" = None,
    template_prefs=None,
    coord_provider=None,
) -> str:
    """导出已应用所选 Karaoke Template 的成品 ASS。

    结构与 Aegisub Apply 一致：模板/原 k-tag 保留为 Comment，播放器只渲染
    ``effect=fx`` Dialogue。全不选模板时明确失败，不把基础 k-tag 冒充成品。
    """
    from .karaoke_template import default_karaoke_templates
    from .karaoke_templater import apply_template_to_project

    preferences = template_prefs if template_prefs is not None else default_karaoke_templates()
    active = preferences.effective().templates
    if not active:
        raise ValueError("尚未选择卡拉OK模板；无法导出应用模板后的 ASS")
    return apply_template_to_project(
        project,
        active[0],
        k_mode=k_mode,
        style=style,
        ass_style=ass_style,
        coord_provider=coord_provider,
    )
