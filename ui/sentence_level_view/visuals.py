"""ui.sentence_level_view.visuals — 行号脏/锁图标与行底色。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPixmap
from qfluentwidgets import TableWidget

def _make_dirty_dot_icon() -> QIcon:
    size = 12
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    from PySide6.QtGui import QPainter
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor("#E8A33D"))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(2, 2, size - 4, size - 4)
    painter.end()
    return QIcon(pm)


_DIRTY_DOT_ICON: Optional[QIcon] = None
_LOCKED_ICON: Optional[QIcon] = None


def _dirty_dot_icon() -> QIcon:
    """QPixmap 必须在 QApplication 创建后构造，因此按需缓存，避免仅 import 模块就崩溃。"""
    global _DIRTY_DOT_ICON
    if _DIRTY_DOT_ICON is None:
        _DIRTY_DOT_ICON = _make_dirty_dot_icon()
    return _DIRTY_DOT_ICON


def _make_locked_icon() -> QIcon:
    size = 14
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    from PySide6.QtGui import QPainter, QPen
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    # 锁身
    painter.setBrush(QColor("#378ADD"))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(2, 6, 10, 7, 2, 2)
    # 锁梁
    pen = QPen(QColor("#378ADD"), 1.8)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(3, 1, 8, 8, 0, 180 * 16)
    painter.end()
    return QIcon(pm)


def _locked_icon() -> QIcon:
    global _LOCKED_ICON
    if _LOCKED_ICON is None:
        _LOCKED_ICON = _make_locked_icon()
    return _LOCKED_ICON


def _dirty_bg_brush() -> QBrush:
    from qfluentwidgets import isDarkTheme
    # 深色模式使用半透明沉稳暖琥珀色，浅色模式使用温和乳黄琥珀色，柔和优雅不刺眼
    c = QColor(245, 180, 60, 38) if isDarkTheme() else QColor(251, 191, 36, 45)
    return QBrush(c)


def _apply_sentence_visual_state(table: TableWidget, row: int, dirty: bool, locked: bool) -> None:
    """给整行套用视觉状态：
    - 锁定（locked）：行号列小锁图标 🔒，保护态
    - 待对齐（dirty 且未锁定）：整行柔和暖琥珀底色 + 行号列橙色圆点图标
    - 干净（clean）：正常底色，无特殊图标
    """
    for col in range(table.columnCount()):
        it = table.item(row, col)
        if it is None:
            continue
        if dirty and not locked:
            it.setBackground(_dirty_bg_brush())
        else:
            it.setData(Qt.BackgroundRole, None)

    num_it = table.item(row, 0)
    if num_it is not None:
        if locked:
            num_it.setIcon(_locked_icon())
            num_it.setToolTip("已锁定保护（任何自动对齐均不会覆写）")
        elif dirty:
            num_it.setIcon(_dirty_dot_icon())
            num_it.setToolTip("待对齐（内容或时间已被手动修改）")
        else:
            num_it.setIcon(QIcon())
            num_it.setToolTip("")


def _apply_dirty_row(table: TableWidget, row: int, dirty: bool) -> None:
    """兼容旧接口（无 lock 传入时保持兼容）。"""
    _apply_sentence_visual_state(table, row, dirty, False)


__all__ = [
    "_make_dirty_dot_icon",
    "_dirty_dot_icon",
    "_make_locked_icon",
    "_locked_icon",
    "_dirty_bg_brush",
    "_apply_sentence_visual_state",
    "_apply_dirty_row",
]
