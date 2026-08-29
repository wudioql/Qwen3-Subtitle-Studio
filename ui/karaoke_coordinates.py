"""Karaoke Templater 坐标提供器（Qt 字体度量近似 Aegisub karaskel）。"""

from __future__ import annotations


def make_karaoke_coord_provider(ass_style):
    """返回 ``(scenter, smiddle, lcenter, lmiddle)`` provider。

    使用 ASS play-res、alignment、margin、pixel font size、ScaleX 与 Spacing。
    QFontMetrics 和 Aegisub/libass 的字体栅格仍可能有少量平台差异。
    """
    from PySide6.QtGui import QFont, QFontMetrics

    font = QFont(ass_style.font_name)
    font.setPixelSize(max(1, int(round(float(ass_style.font_size)))))
    font.setBold(bool(ass_style.bold))
    font.setItalic(bool(ass_style.italic))
    metrics = QFontMetrics(font)
    play_width = max(1, int(getattr(ass_style, "play_res_x", 1920) or 1920))
    play_height = max(1, int(getattr(ass_style, "play_res_y", 1080) or 1080))
    alignment = int(getattr(ass_style, "alignment", 2) or 2)
    margin_left = int(getattr(ass_style, "margin_l", 120) or 0)
    margin_right = int(getattr(ass_style, "margin_r", 120) or 0)
    margin_vertical = int(getattr(ass_style, "margin_v", 60) or 0)
    scale_x = max(0.01, float(getattr(ass_style, "scale_x", 100) or 100) / 100.0)
    spacing = float(getattr(ass_style, "spacing", 0) or 0)

    def text_width(text: str) -> float:
        if not text:
            return 0.0
        return metrics.horizontalAdvance(text) * scale_x + spacing * max(0, len(text) - 1)

    if alignment in (7, 8, 9):
        line_y = margin_vertical + metrics.height() / 2
    elif alignment in (4, 5, 6):
        line_y = play_height / 2
    else:
        line_y = play_height - margin_vertical - metrics.height() / 2

    state = {"line": None, "cursor": 0, "last_index": -1}

    def provider(syllable_index, syllable_text, line_text):
        line_width = text_width(line_text)
        if alignment in (1, 4, 7):
            line_left = float(margin_left)
        elif alignment in (3, 6, 9):
            line_left = float(play_width - margin_right) - line_width
        else:
            line_left = (play_width - line_width) / 2.0
        line_center = line_left + line_width / 2.0

        if (
            state["line"] != line_text
            or int(syllable_index) <= int(state["last_index"])
        ):
            state["line"] = line_text
            state["cursor"] = 0
        state["last_index"] = int(syllable_index)

        if not syllable_text:
            position = 0
        else:
            position = line_text.find(syllable_text, int(state["cursor"]))
            if position < 0:
                position = int(state["cursor"])
            state["cursor"] = position + len(syllable_text)
        left_extent = text_width(line_text[:position])
        right_extent = text_width(line_text[:position + len(syllable_text)])
        syllable_center = line_left + (left_extent + right_extent) / 2.0
        return (
            int(round(syllable_center)),
            int(round(line_y)),
            int(round(line_center)),
            int(round(line_y)),
        )

    return provider


__all__ = ["make_karaoke_coord_provider"]
