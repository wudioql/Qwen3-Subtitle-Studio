"""ui.widgets — 通用自绘/增强控件

当前内容：自绘分割条（GripSplitter / GripSplitterHandle）。

为什么自绘：仅靠 QSS「提亮把手颜色 + 6px 尺寸」在深色模式下仍几乎不可见——
QSS 的 QSplitter::handle 规则在部分平台样式引擎下会被忽略，且纯色把手与
深色面板底色对比先天不足。QPainter 自绘握把（常驻三段式指示 + hover/pressed
高亮）完全不依赖 QSS 级联；themes.py 中的 QSplitter::handle QSS 规则保留，
作为其它默认 QSplitter 的兜底。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSplitter, QSplitterHandle
from qfluentwidgets import isDarkTheme

# 主题强调色与 themes._ACCENT 同值；不反向 import themes（避免 themes ↔ widgets 循环）
_ACCENT = "#4F7DFF"


def _grip_colors(dark: bool, hover: bool, pressed: bool) -> tuple[QColor, QColor]:
    """返回 (把手底色, 握把指示色)。纯函数，便于测试钉样。"""
    if pressed:
        return QColor("#3B63D9") if dark else QColor("#3B6BFF"), QColor("#FFFFFF")
    if hover:
        base = QColor("#343438") if dark else QColor("#C6C6CE")
        return base, QColor(_ACCENT) if dark else QColor("#3B6BFF")
    return (
        QColor("#2E2E31") if dark else QColor("#D5D5DA"),
        QColor("#9A9AA0") if dark else QColor("#6E6E73"),
    )


class GripSplitterHandle(QSplitterHandle):
    """自绘分割条把手：常驻握把指示 + hover/pressed 反馈。"""

    _DASH = 12    # 单段握把长度（px）
    _GAP = 5      # 段间距（px）
    _THICK = 2    # 握把厚度（px）

    def __init__(self, orientation: Qt.Orientation, parent: QSplitter) -> None:
        super().__init__(orientation, parent)
        self._pressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._pressed = True
        self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ARG002 — Qt 覆写签名
        p = QPainter(self)
        _paint_grip(p, self.rect(), self.orientation(),
                    isDarkTheme(), self.underMouse(), self._pressed)
        p.end()


def _paint_grip(p: QPainter, rect, orientation: Qt.Orientation,
                dark: bool, hover: bool, pressed: bool) -> None:
    """把手的纯绘制逻辑。独立成函数以便测试直接往 QImage 上画——
    离屏测试里「控件从未 show 就 render」的绘制默认被 Qt 跳过，像素断言不可靠。"""
    dash, gap, thick = (GripSplitterHandle._DASH,
                        GripSplitterHandle._GAP, GripSplitterHandle._THICK)
    base, grip = _grip_colors(dark, hover, pressed)
    p.fillRect(rect, base)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grip)
    total = dash * 3 + gap * 2
    if orientation == Qt.Orientation.Horizontal:   # 竖向把手：三条竖杠
        x = rect.center().x() - thick / 2
        y0 = rect.center().y() - total / 2
        for i in range(3):
            p.drawRoundedRect(QRectF(x, y0 + i * (dash + gap), thick, dash), 1.0, 1.0)
    else:                                          # 横向把手：三条横杠
        y = rect.center().y() - thick / 2
        x0 = rect.center().x() - total / 2
        for i in range(3):
            p.drawRoundedRect(QRectF(x0 + i * (dash + gap), y, dash, thick), 1.0, 1.0)


class GripSplitter(QSplitter):
    """createHandle 换为自绘握把把手 + 6px 可抓取宽度。"""

    _HANDLE_PX = 6

    def __init__(self, orientation: Qt.Orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self.setHandleWidth(self._HANDLE_PX)

    def createHandle(self) -> QSplitterHandle:  # Qt 覆写命名
        return GripSplitterHandle(self.orientation(), self)


__all__ = ["GripSplitter", "GripSplitterHandle"]
