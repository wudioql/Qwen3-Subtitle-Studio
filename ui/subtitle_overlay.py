"""ui.subtitle_overlay — 播放器字幕预览叠层（QPainter 兼容渲染）

叠在视频画面（或纯音频的深色底）上方的透明控件，按播放位置实时绘制当前句
字幕。六档预览模式覆盖十种导出观感，并增加所选模板效果兼容预览：

    sentence      句级·通用   —— SRT / WebVTT / 标准 LRC 的观感（白字细黑边）
    sentence_ass  句级·ASS    —— 句级 ASS（套用「ASS 文字样式」设置）
    word          逐字·通用   —— 逐字 SRT / WebVTT / 增强 LRC（当前字按逐字高亮设置加字形）
    word_ass      逐字·ASS    —— ASS 一字一行 / 颜色动画（ASS 样式 + 当前字高亮色）
    karaoke       卡拉OK扫过  —— k-tag ASS（当前字从次色向主色横向扫过填充）
    karaoke_template  所选模板 —— 参数化模板的 QPainter 兼容预览（raw 模式降级）

说明：这是 QPainter 自绘的**兼容预览**。参数模板可近似预览淡入、颜色、
弹跳缩放、描边发光与锚点；模板效果模式只画模板 fx，**不得叠加基础 k-tag
扫过**，标点固定使用基础样式与原排版位置。raw/extra_tags/Lua 表达式无法由
本路径完整执行时降级为无额外动画的
ASS 样式字幕；最终效果以应用模板后的 ASS/libass 渲染为准。

`compute_overlay_segments` 为纯函数（不依赖 Qt），供测试直接钉行为。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .subtitle_render_policy import (
    segment_fill_role,
    should_clip_karaoke_sweep,
    should_hide_upcoming_outline,
)

# 预览模式（key → 下拉显示名）
PREVIEW_MODES: dict[str, str] = {
    "sentence": "句级字幕（SRT / WebVTT / 标准 LRC）",
    "sentence_ass": "句级 ASS（带文字样式）",
    "word": "逐字高亮（逐字 SRT / WebVTT / 增强 LRC）",
    "word_ass": "逐字 ASS（一字一行 / 颜色动画）",
    "karaoke": "基础 k-tag（跟随 \\kf / \\K / \\k / \\ko）",
    "karaoke_template": "所选卡拉OK模板效果",
}


@dataclass
class OverlaySegment:
    """一段待绘制文字及其状态。

    state: "plain"（句级整句）/ "sung"（已唱）/ "current"（正在唱）/ "upcoming"（未唱）
    progress: karaoke 中表示扫过比例；karaoke_template 中只用于驱动模板动画，均为 0~1。
    """
    text: str
    state: str = "plain"
    progress: float = 1.0
    is_punct: bool = False


def _split_punctuation_runs(text: str) -> list[tuple[str, bool]]:
    """把可见文字切为标点/非标点段；Unicode 各语言标点均覆盖。"""
    runs: list[tuple[str, bool]] = []
    for char in text:
        is_punct = unicodedata.category(char).startswith("P")
        if runs and runs[-1][1] == is_punct:
            runs[-1] = (runs[-1][0] + char, is_punct)
        else:
            runs.append((char, is_punct))
    return runs


def _append_segment_runs(
    result: list[OverlaySegment],
    text: str,
    *,
    state: str,
    progress: float,
    force_punct: bool = False,
) -> None:
    for run, is_punct in _split_punctuation_runs(text):
        result.append(
            OverlaySegment(
                text=run,
                state=state,
                progress=progress,
                is_punct=force_punct or is_punct,
            )
        )


def compute_overlay_segments(project, t: float, mode: str) -> List[OverlaySegment]:
    """按播放位置 t（秒）与预览模式计算当前应显示的字幕分段（纯逻辑）。

    - 无项目 / t 不在任何句内 → 空列表（不显示字幕）；
    - 句级两档：整句一个 plain 段；
    - 逐字/卡拉OK三档：按字级时间戳分段标注 已唱/正在唱/未唱；
      标点保留相邻时间状态，但显式标记为不参与卡拉OK模板动画；
    - 句内无字级数据时降级为整句 plain（与导出层「无字级自动降级」一致）。
    """
    if project is None or not getattr(project, "sentences", None):
        return []
    # 严格命中：t 必须落在句内。不用 find_sentence_at——它带「最近句兜底」
    # 语义（为表格高亮设计），字幕预览在句间空隙不应显示任何字幕。
    sent = None
    for s in project.sentences:
        if s.start_time <= t <= s.end_time:
            sent = s
            break
    if sent is None:
        return []
    text = (sent.text or "").strip()
    if not text:
        return []

    if mode in ("sentence", "sentence_ass"):
        return [OverlaySegment(text=text)]
    if not sent.words:
        if mode != "karaoke_template":
            return [OverlaySegment(text=text)]
        result: list[OverlaySegment] = []
        _append_segment_runs(result, text, state="plain", progress=1.0)
        return result

    timed: list[tuple[object, OverlaySegment]] = []
    last_state = "upcoming"
    for word in sent.words:
        if getattr(word, "is_punct", False):
            segment = OverlaySegment(
                text=word.text,
                state=last_state,
                progress=1.0 if last_state == "sung" else 0.0,
                is_punct=True,
            )
            timed.append((word, segment))
            continue
        if t >= word.end_time:
            state, progress = "sung", 1.0
        elif word.start_time <= t < word.end_time:
            state = "current"
            duration = max(1e-6, word.end_time - word.start_time)
            progress = (
                (t - word.start_time) / duration
                if mode in ("karaoke", "karaoke_template") else 1.0
            )
        else:
            state, progress = "upcoming", 0.0
        segment = OverlaySegment(text=word.text, state=state, progress=progress)
        timed.append((word, segment))
        last_state = state

    # 与导出器同源：按顺序把 words 定位回 Sentence.text，未进入 words 的 ♪/♫/空白
    # 等装饰正文作为无时间片段插回。这些装饰字符不应继承前字的卡拉OK状态
    # 和进度（与标点行为一致：可见但无逐字动效、不参与扫过）。
    result: List[OverlaySegment] = []
    cursor = 0
    for word, segment in timed:
        position = text.find(str(word.text), cursor)
        if position < 0:
            position = cursor
        if position > cursor:
            # 装饰字符（♪ ♫ ♬ ♩ # 等特殊符号）不应继承前字的卡拉OK状态/进度
            decoration_text = text[cursor:position]
            decoration_is_special = any(
                ch in ("♪", "♫", "♬", "♩", "#") for ch in decoration_text
            )
            if decoration_is_special:
                # 特殊装饰字符：保留可见但无逐字动效（与标点行为对齐）
                # 使用前字的基础状态但固定进度（已唱状态 1.0，未唱状态 0.0），
                # 绝不继承 current 的扫过进度，避免“跟随前字拥有卡拉OK效果”
                inherited = result[-1] if result else segment
                inherited_state = inherited.state
                # 装饰字符不参与扫过：如果前字已唱完则显示为 sung，不否则为 upcoming
                # 绝不显示为 current（因为没有独立时间戳和逐字动效）
                decoration_state = "sung" if inherited_state == "sung" else "upcoming"
                decoration_progress = 1.0 if decoration_state == "sung" else 0.0
                _append_segment_runs(
                    result,
                    decoration_text,
                    state=decoration_state,
                    progress=decoration_progress,
                    force_punct=True,
                )
            else:
                inherited = result[-1] if result else segment
                _append_segment_runs(
                    result,
                    decoration_text,
                    state=inherited.state,
                    progress=inherited.progress,
                )
        end = min(len(text), position + len(str(word.text)))
        visible = text[position:end] or str(word.text)
        _append_segment_runs(
            result,
            visible,
            state=segment.state,
            progress=segment.progress,
            force_punct=segment.is_punct,
        )
        cursor = end
    if cursor < len(text):
        remaining = text[cursor:]
        remaining_is_special = any(
            ch in ("♪", "♫", "♬", "♩", "#") for ch in remaining
        )
        if remaining_is_special:
            # 句尾装饰字符：同样不继承卡拉OK进度
            inherited = result[-1] if result else OverlaySegment(text="")
            decoration_state = "sung" if inherited.state == "sung" else "upcoming"
            decoration_progress = 1.0 if decoration_state == "sung" else 0.0
            _append_segment_runs(
                result,
                remaining,
                state=decoration_state,
                progress=decoration_progress,
                force_punct=True,
            )
        else:
            inherited = result[-1] if result else OverlaySegment(text="")
            _append_segment_runs(
                result,
                remaining,
                state=inherited.state,
                progress=inherited.progress,
            )
    return result


class SubtitleOverlay(QWidget):
    """透明字幕叠层：套用样式设置自绘当前句（见模块 docstring）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._project = None
        self._time = 0.0
        self._mode = "sentence"
        # 样式（由 PlayerPanel 在打开弹窗保存后刷新）
        self._ass_style = None          # AssStylePrefs
        self._word_style = None         # WordHighlightStyle
        self._karaoke_template = None   # KaraokeTemplate（最多一条）
        self._k_mode = "kf"

    # ── 外部接口 ─────────────────────────────────────────────
    def set_project(self, project) -> None:
        self._project = project
        self.update()

    def set_time(self, t: float) -> None:
        self._time = float(t)
        self.update()

    def set_mode(self, mode: str) -> None:
        if mode in PREVIEW_MODES:
            self._mode = mode
            self.update()

    def mode(self) -> str:
        return self._mode

    def refresh_styles(self) -> None:
        """从偏好重读 ASS 样式与逐字高亮样式（弹窗保存后调用）。"""
        try:
            from core.app_config import load_preferences
            prefs = load_preferences()
            self._ass_style = prefs.ass_style.to_style()
            st = prefs.style
            self._word_style = st
            active = prefs.karaoke_template.to_prefs().effective().templates
            self._karaoke_template = active[0] if active else None
            self._k_mode = prefs.export.k_tag_mode or "kf"
        except Exception:  # noqa: BLE001 — 偏好不可读时用默认观感
            self._ass_style = None
            self._word_style = None
            self._karaoke_template = None
            self._k_mode = "kf"
        self.update()

    # ── 绘制 ─────────────────────────────────────────────────
    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        paint_subtitle_overlay(
            painter,
            self.width(),
            self.height(),
            self._project,
            self._time,
            self._mode,
            self._ass_style,
            self._word_style,
            self._karaoke_template,
            self._k_mode,
        )
        painter.end()


def _segment_fill(mode: str, seg: OverlaySegment, primary: QColor,
                  secondary: QColor, hi: QColor, use_ass: bool, k_mode: str) -> QColor:
    role = segment_fill_role(mode, seg.state, use_ass=use_ass, k_mode=k_mode)
    return {"primary": primary, "secondary": secondary, "highlight": hi}[role]


def _template_scale_factor(percent: int, progress: float) -> float:
    peak = max(100, int(percent)) / 100.0
    p = max(0.0, min(1.0, float(progress)))
    triangle = 1.0 - abs(2.0 * p - 1.0)
    return 1.0 + (peak - 1.0) * triangle


def _lerp_color(start: QColor, end: QColor, progress: float) -> QColor:
    p = max(0.0, min(1.0, float(progress)))
    return QColor(
        round(start.red() + (end.red() - start.red()) * p),
        round(start.green() + (end.green() - start.green()) * p),
        round(start.blue() + (end.blue() - start.blue()) * p),
        round(start.alpha() + (end.alpha() - start.alpha()) * p),
    )


def paint_subtitle_overlay(
    painter: QPainter,
    width: int,
    height: int,
    project,
    t: float,
    mode: str,
    ass_style,
    word_style,
    karaoke_template=None,
    k_mode: str = "kf",
) -> None:
    """在任意 QPainter 上绘制当前字幕（供叠层控件与播放舞台共用）。"""
    segs = compute_overlay_segments(project, t, mode)
    if not segs or width <= 1 or height <= 1:
        return
    painter.save()

    use_ass = mode in ("sentence_ass", "word_ass", "karaoke", "karaoke_template") and ass_style is not None
    if use_ass:
        st = ass_style
        scale = max(0.2, height / max(1, int(st.play_res_y)))
        px = max(12, int(float(st.font_size) * scale * 1.35))
        font = QFont(st.font_name)
        font.setPixelSize(px)
        font.setBold(bool(st.bold))
        font.setItalic(bool(st.italic))
        primary = QColor(st.primary_color)
        secondary = QColor(st.secondary_color)
        outline_c = QColor(st.outline_color)
        outline_w = max(0.0, float(st.outline)) * scale * 1.35
        shadow_c = QColor(st.back_color)
        shadow_c.setAlpha(max(0, 255 - int(getattr(st, "back_alpha", 128))))
        shadow_off = max(0.0, float(st.shadow)) * scale * 1.35
        alignment = int(st.alignment)
    else:
        scale = max(0.2, height / 1080.0)
        px = max(14, int(height * 0.09))
        font = QFont("Microsoft YaHei")
        font.setPixelSize(px)
        font.setBold(True)
        primary = QColor("#FFFFFF")
        secondary = QColor("#9CA3AF")
        outline_c = QColor("#000000")
        outline_w = max(1.5, px * 0.08)
        shadow_c = QColor(0, 0, 0, 160)
        shadow_off = 2.0
        alignment = 2

    hi_color = QColor("#FFD54F")
    cur_bold = cur_italic = cur_under = cur_strike = False
    if word_style is not None:
        hi_color = QColor(getattr(word_style, "ass_highlight_color", "#FFD54F"))
        cur_bold = bool(getattr(word_style, "bold", False))
        cur_italic = bool(getattr(word_style, "italic", False))
        cur_under = bool(getattr(word_style, "underline", False))
        cur_strike = bool(getattr(word_style, "strike", False))

    template = (
        karaoke_template
        if mode == "karaoke_template"
        and karaoke_template is not None
        and getattr(karaoke_template, "mode", "form") == "form"
        else None
    )
    current_progress = next(
        (
            segment.progress
            for segment in segs
            if segment.state == "current" and not segment.is_punct
        ),
        None,
    )
    template_opacity = 1.0
    if template is not None:
        # 模板的 \an 是缩放/旋转基点；有 \pos 锁位时不应覆盖 ASS Style 的行对齐。
        # 只有关闭 use_pos、让模板使用默认屏幕位置时才采用模板 anchor 布局。
        if not bool(getattr(template, "use_pos", True)):
            alignment = int(getattr(template, "anchor", alignment) or alignment)
        # 近似模板 fad：按当前句总区间调整全局透明度。
        sentence = next(
            (
                item for item in getattr(project, "sentences", [])
                if item.start_time <= t <= item.end_time
            ),
            None,
        )
        if sentence is not None:
            fade_in = max(0, int(getattr(template, "fad_in_ms", 0))) / 1000.0
            fade_out = max(0, int(getattr(template, "fad_out_ms", 0))) / 1000.0
            if fade_in > 0:
                template_opacity = min(
                    template_opacity,
                    max(0.0, (t - sentence.start_time) / fade_in),
                )
            if fade_out > 0:
                template_opacity = min(
                    template_opacity,
                    max(0.0, (sentence.end_time - t) / fade_out),
                )

    segment_fonts: list[QFont] = []
    segment_widths: list[int] = []
    base_widths: list[int] = []
    base_metrics = QFontMetrics(font)
    for segment in segs:
        segment_font = QFont(font)
        if segment.state == "current" and mode == "word":
            segment_font.setBold(font.bold() or cur_bold)
            segment_font.setItalic(font.italic() or cur_italic)
            segment_font.setUnderline(cur_under)
            segment_font.setStrikeOut(cur_strike)
        if template is not None and current_progress is not None and not segment.is_punct:
            applies = (
                segment.state == "current"
                or getattr(template, "template_class", "syl") == "line"
            )
            if applies and bool(getattr(template, "scale_enabled", False)):
                factor = _template_scale_factor(
                    int(getattr(template, "scale_percent", 100)),
                    current_progress,
                )
                segment_font.setPixelSize(max(1, round(px * factor)))
        segment_fonts.append(segment_font)
        segment_widths.append(QFontMetrics(segment_font).horizontalAdvance(segment.text))
        base_widths.append(base_metrics.horizontalAdvance(segment.text))

    fm = base_metrics
    # 模板 fx 的每个音节以原始排版中心为锚点；缩放不能把后续标点挤离原位。
    layout_widths = base_widths if template is not None else segment_widths
    total_w = sum(layout_widths)
    if alignment in (1, 4, 7):
        x = 16.0
    elif alignment in (3, 6, 9):
        x = width - 16.0 - total_w
    else:
        x = (width - total_w) / 2.0
    if alignment in (7, 8, 9):
        y = 10.0 + fm.ascent()
    elif alignment in (4, 5, 6):
        y = (height - fm.height()) / 2.0 + fm.ascent()
    else:
        y = height - 18.0 - fm.descent()

    for index, seg in enumerate(segs):
        seg_w = segment_widths[index]
        base_w = base_widths[index]
        seg_font = segment_fonts[index]
        # 模板缩放围绕该段的基础排版中心，不改变下一段（尤其标点）的 x。
        draw_x = x + (base_w - seg_w) / 2.0 if template is not None else x
        painter.save()
        if template is not None and not seg.is_punct:
            painter.setOpacity(template_opacity)

        path = QPainterPath()
        path.addText(draw_x, y, seg_font, seg.text)

        if shadow_off > 0 and shadow_c.alpha() > 0:
            sp = QPainterPath()
            sp.addText(draw_x + shadow_off, y + shadow_off, seg_font, seg.text)
            painter.fillPath(sp, QBrush(shadow_c))
        effective_outline = outline_w
        if (
            template is not None
            and not seg.is_punct
            and seg.state == "current"
            and bool(getattr(template, "glow_enabled", False))
        ):
            triangle = 1.0 - abs(2.0 * seg.progress - 1.0)
            effective_outline = max(
                effective_outline,
                float(getattr(template, "glow_bord", 0.0)) * scale * (0.5 + triangle),
            )
        if should_hide_upcoming_outline(mode, seg.state, k_mode=k_mode):
            effective_outline = 0.0
        if effective_outline > 0 and outline_c.alpha() > 0:
            pen = QPen(outline_c, effective_outline)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.strokePath(path, pen)

        template_color = (
            template is not None
            and not seg.is_punct
            and seg.state == "current"
            and bool(getattr(template, "color_enabled", False))
        )
        if template_color:
            fill = _lerp_color(
                QColor(
                    getattr(
                        template,
                        "color_original",
                        getattr(template, "color_restore", "#FFFFFF"),
                    )
                ),
                QColor(getattr(template, "color_highlight", "#FFD54F")),
                seg.progress,
            )
        else:
            fill = _segment_fill(
                mode, seg, primary, secondary, hi_color, use_ass, k_mode,
            )
        if should_clip_karaoke_sweep(
            mode,
            seg.state,
            seg.progress,
            k_mode=k_mode,
            template_color=template_color,
        ):
            painter.save()
            painter.setClipRect(int(draw_x), 0, max(1, int(seg_w * seg.progress)), height)
            painter.fillPath(path, QBrush(primary))
            painter.restore()
            painter.save()
            painter.setClipRect(
                int(draw_x + seg_w * seg.progress), 0,
                max(1, int(seg_w * (1 - seg.progress)) + 2), height,
            )
            painter.fillPath(path, QBrush(secondary))
            painter.restore()
        else:
            painter.fillPath(path, QBrush(fill))
        painter.restore()
        x += layout_widths[index]
    painter.restore()


__all__ = [
    "SubtitleOverlay",
    "compute_overlay_segments",
    "paint_subtitle_overlay",
    "OverlaySegment",
    "PREVIEW_MODES",
]
