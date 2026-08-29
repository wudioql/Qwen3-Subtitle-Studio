"""ui.ass_style_dialog.preview — ASS 字幕 QPainter 预览画布。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget
from qfluentwidgets import isDarkTheme

class SubtitlePreviewWidget(QWidget):
    """ASS 字幕实时渲染预览画布（原生 QPainter + QPainterPath，真实字号按比放大，自动跟随明暗主题）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("assSubtitlePreviewCanvas")
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.sample_text = "青瓷风拂过雕花窗"

        self.font_name = "Source Han Sans SC"
        self.font_size = 48.0
        self.bold = True
        self.italic = False
        self.underline = False
        self.strikeout = False
        self.primary_color = QColor("#FFFFFF")
        self.outline_color = QColor("#000000")
        self.outline_width = 3.0
        self.shadow_color = QColor(0, 0, 0, 180)
        self.shadow_offset = 2.5
        self.alignment = 2
        # 高级参数（此前预览未消费，导致「改了没反应」）
        self.scale_x = 100.0            # 水平缩放 %
        self.scale_y = 100.0            # 垂直缩放 %
        self.spacing = 0.0              # 字间距 px
        self.angle = 0.0                # 旋转角度（ASS 逆时针为正）
        self.border_style = 1           # 1=描边+阴影 3=不透明底框

    def preview_font_px(self) -> int:
        """预览画布实际使用的像素字号：随 font_size 连续等比放大（0.65 缩放，11px 下限）。

        paintEvent 与测试共用此方法——预览字号契约的唯一实现点。
        """
        return max(11, int(float(self.font_size) * 0.65))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        rect = self.rect()

        # 1. 模拟视频画框背景（自动跟随当前 Fluent 主题，深色为深邃影视画框，浅色为清爽视窗）
        if isDarkTheme():
            bg_color = QColor("#14171F")
            border_color = QColor("#2E3544")
        else:
            bg_color = QColor("#F1F5F9")
            border_color = QColor("#CBD5E1")

        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(border_color, 1.2))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)

        # 2. 真实字号映射：字号随输入连续显著放大，直接真实反映用户调整
        px = self.preview_font_px()
        font = QFont(self.font_name, px)
        font.setBold(self.bold)
        font.setItalic(self.italic)
        font.setUnderline(self.underline)
        font.setStrikeOut(self.strikeout)
        # 字间距：按预览缩放比例（0.65）折算，正负皆有效
        if abs(float(self.spacing)) > 1e-6:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(self.spacing) * 0.65)
        painter.setFont(font)
        fm = QFontMetrics(font)

        sx = max(0.05, float(self.scale_x) / 100.0)
        sy = max(0.05, float(self.scale_y) / 100.0)

        lines = self.sample_text.split("\n")
        line_h = fm.height() * 1.12 * sy
        total_h = len(lines) * line_h

        # 垂直对齐：1-3 底部 / 4-6 中部 / 7-9 顶部（此前恒居中，看不出区别）
        if self.alignment in (7, 8, 9):
            base_top = 10.0
        elif self.alignment in (4, 5, 6):
            base_top = (rect.height() - total_h) / 2.0
        else:
            base_top = rect.height() - total_h - 10.0
        start_y = base_top + fm.ascent() * sy

        # 开启画布裁切，防止极大字号画到画框外部
        painter.setClipRect(rect.adjusted(2, 2, -2, -2))

        for idx, line in enumerate(lines):
            line_w = fm.horizontalAdvance(line) * sx
            if self.alignment in (1, 4, 7):
                x = 24.0
            elif self.alignment in (3, 6, 9):
                x = rect.width() - 24.0 - line_w
            else:
                x = (rect.width() - line_w) / 2.0
            y = start_y + idx * line_h

            # 缩放/旋转以该行文字中心为基准，在局部坐标系里绘制
            cx = x + line_w / 2.0
            cy = y - fm.ascent() * sy / 2.0 + fm.height() * sy / 2.0
            painter.save()
            painter.translate(cx, cy)
            if abs(float(self.angle)) > 1e-6:
                painter.rotate(-float(self.angle))   # ASS 角度逆时针为正，Qt 顺时针为正
            painter.scale(sx, sy)
            lx = -fm.horizontalAdvance(line) / 2.0
            ly = fm.ascent() - fm.height() / 2.0

            path = QPainterPath()
            path.addText(lx, ly, font, line)

            if int(self.border_style) == 3:
                # 边框样式 3：不透明底框（矩形垫底），描边不再单独勾勒
                box = path.boundingRect().adjusted(-8, -5, 8, 5)
                painter.fillRect(box, QBrush(self.shadow_color if self.shadow_color.alpha() > 0
                                             else QColor(0, 0, 0, 200)))
            else:
                # 边框样式 1：描边 + 阴影
                if self.shadow_offset > 0 and self.shadow_color.alpha() > 0:
                    shadow_path = QPainterPath()
                    shadow_path.addText(lx + self.shadow_offset, ly + self.shadow_offset, font, line)
                    painter.fillPath(shadow_path, QBrush(self.shadow_color))
                if self.outline_width > 0 and self.outline_color.alpha() > 0:
                    pen = QPen(self.outline_color, self.outline_width * 1.4)
                    pen.setJoinStyle(Qt.RoundJoin)
                    pen.setCapStyle(Qt.RoundCap)
                    painter.strokePath(path, pen)

            # 主体文字层
            painter.fillPath(path, QBrush(self.primary_color))
            painter.restore()

        painter.end()


__all__ = ["SubtitlePreviewWidget"]
