"""Fluent 主题桥接与高品质视觉规范（遵循 make-interfaces-feel-better 设计工程原则）。

职责：
1. 协调 qfluentwidgets.setTheme / setThemeColor；
2. 深度适配深/浅色模式下的弹窗（QMessageBox / QProgressDialog / QDialog）、表格选中态、悬浮态、待对齐与锁定状态色；
3. 保证主工具栏、导出侧栏、滚动区、卡片（SimpleCardWidget）与对话框具有统一的视觉层级与高对比度文字；
4. 保证时间戳数字在等宽表格排版下的整齐性与光学对齐。
"""

from __future__ import annotations

import logging
from PySide6.QtGui import QColor, QPalette
from qfluentwidgets import Theme, isDarkTheme, setTheme

from core.app_config import load_preferences, save_preferences

logger = logging.getLogger(__name__)

THEME_KEY = "ui_theme"
_DEFAULT_THEME = "dark"
_ACCENT = "#4F7DFF"

_DARK_SHELL_QSS = """
/* ───────────────────────── 基础容器与窗口 ───────────────────────── */
QMainWindow#main_window, QWidget#central_root {
    background: #1C1C1E;
    color: #F5F5F7;
}

/* ───────────────────────── 弹窗与确认框 (QMessageBox / QProgressDialog / QDialog) ───────────────────────── */
QDialog, QMessageBox, QProgressDialog {
    background-color: #202022;
    color: #F5F5F7;
}
QDialog QLabel, QMessageBox QLabel, QProgressDialog QLabel {
    color: #F5F5F7;
    font-size: 13px;
    background: transparent;
}
QMessageBox QTextEdit, QMessageBox QPlainTextEdit {
    background-color: #28282B;
    color: #F5F5F7;
    border: 1px solid #38383A;
    border-radius: 6px;
}
QMessageBox QPushButton, QDialog QPushButton {
    background-color: #2C2C2E;
    color: #F5F5F7;
    border: 1px solid #38383A;
    border-radius: 6px;
    padding: 6px 18px;
    font-size: 13px;
    min-width: 65px;
    min-height: 22px;
}
QMessageBox QPushButton:hover, QDialog QPushButton:hover {
    background-color: #3A3A3C;
    border-color: #4F7DFF;
}
QMessageBox QPushButton:pressed, QDialog QPushButton:pressed {
    background-color: #1F1F21;
}
QMessageBox QPushButton:default {
    background-color: #4F7DFF;
    color: #FFFFFF;
    border-color: #4F7DFF;
}
QMessageBox QPushButton:default:hover {
    background-color: #638DFF;
}

/* ───────────────────────── 菜单栏与上下文菜单 ───────────────────────── */
QMenuBar {
    background: #1C1C1E;
    color: #E5E5EA;
    padding: 3px 6px;
    border-bottom: 1px solid #2C2C2E;
}
QMenuBar::item {
    padding: 6px 10px;
    border-radius: 5px;
}
QMenuBar::item:selected {
    background: #2C2C2E;
    color: #FFFFFF;
}
QMenu {
    background: #252528;
    color: #F5F5F7;
    border: 1px solid #38383A;
    border-radius: 8px;
    padding: 5px;
}
QMenu::item {
    padding: 7px 24px 7px 14px;
    border-radius: 4px;
}
QMenu::item:selected {
    background: #3A3A3C;
    color: #FFFFFF;
}
QMenu::separator {
    height: 1px;
    background: #38383A;
    margin: 4px 6px;
}

/* ───────────────────────── 主工具栏 ───────────────────────── */
QToolBar#main_command_toolbar {
    background: #1C1C1E;
    border: 0;
    border-bottom: 1px solid #2C2C2E;
    padding: 6px 10px;
    spacing: 6px;
}
QToolBar#main_command_toolbar::separator {
    width: 1px;
    background: #38383A;
    margin: 6px 4px;
}
QToolBar#main_command_toolbar QLabel {
    color: #F5F5F7;
    font-size: 13px;
    font-weight: 500;
    padding: 0 4px;
}

/* ───────────────────────── 底栏与状态栏 ───────────────────────── */
QStatusBar {
    background: #1C1C1E;
    color: #A1A1A6;
    border-top: 1px solid #2C2C2E;
    padding: 2px 4px;
}
QStatusBar QLabel {
    color: #A1A1A6;
    padding: 0 8px;
    font-size: 12px;
}
/* 分割条可见性：#2C2C2E 打 #1C1C1E 底色上几乎不可见；
   提亮到边框同族 #3C3C40 并给 6px 可抓取宽度，悬停/按下用主题色反馈。
   注：主窗三条分割条实际由 GripSplitter 自绘（见文件尾），此规则是其它 QSplitter 的兜底 */
QSplitter::handle {
    background: #3C3C40;
}
QSplitter::handle:horizontal {
    width: 6px;
}
QSplitter::handle:vertical {
    height: 6px;
}
QSplitter::handle:hover {
    background: #4F7DFF;
}
QSplitter::handle:pressed {
    background: #3B63D9;
}
QToolTip {
    background: #2C2C2E;
    color: #FFFFFF;
    border: 1px solid #3A3A3C;
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 12px;
}

/* ───────────────────────── 表格现代质感与选中态 ───────────────────────── */
QTableView {
    background-color: #202022;
    alternate-background-color: #252528;
    gridline-color: #2C2C2E;
    border: 1px solid #333336;
    border-radius: 8px;
    outline: none;
    selection-background-color: #2A3C5E;
    selection-color: #FFFFFF;
}
QTableView::item {
    padding: 6px 10px;
    border: none;
    color: #E5E5EA;
}
QTableView::item:hover {
    background-color: rgba(255, 255, 255, 0.05);
}
QTableView::item:selected {
    background-color: #2A3C5E;
    color: #FFFFFF;
}
QHeaderView::section {
    background-color: #1C1C1E;
    color: #8E8E93;
    font-weight: 600;
    font-size: 12px;
    padding: 7px 10px;
    border: none;
    border-bottom: 1px solid #333336;
    border-right: 1px solid #28282A;
}

/* ───────────────────────── 导出侧栏与卡片容器 ───────────────────────── */
QWidget#export_panel, QWidget#export_scroll_content {
    background: transparent;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
SimpleCardWidget {
    background: #242426;
    border: 1px solid #333336;
    border-radius: 8px;
}
"""

_LIGHT_SHELL_QSS = """
/* ───────────────────────── 基础容器与窗口 ───────────────────────── */
QMainWindow#main_window, QWidget#central_root {
    background: #F2F2F7;
    color: #1C1C1E;
}

/* ───────────────────────── 弹窗与确认框 (QMessageBox / QProgressDialog / QDialog) ───────────────────────── */
QDialog, QMessageBox, QProgressDialog {
    background-color: #FFFFFF;
    color: #1C1C1E;
}
QDialog QLabel, QMessageBox QLabel, QProgressDialog QLabel {
    color: #1C1C1E;
    font-size: 13px;
    background: transparent;
}
QMessageBox QTextEdit, QMessageBox QPlainTextEdit {
    background-color: #F9FAFB;
    color: #1C1C1E;
    border: 1px solid #E5E5EA;
    border-radius: 6px;
}
QMessageBox QPushButton, QDialog QPushButton {
    background-color: #F2F2F7;
    color: #1C1C1E;
    border: 1px solid #D1D1D6;
    border-radius: 6px;
    padding: 6px 18px;
    font-size: 13px;
    min-width: 65px;
    min-height: 22px;
}
QMessageBox QPushButton:hover, QDialog QPushButton:hover {
    background-color: #E5E5EA;
    border-color: #4F7DFF;
}
QMessageBox QPushButton:pressed, QDialog QPushButton:pressed {
    background-color: #D1D1D6;
}
QMessageBox QPushButton:default {
    background-color: #4F7DFF;
    color: #FFFFFF;
    border-color: #4F7DFF;
}
QMessageBox QPushButton:default:hover {
    background-color: #3B6BFF;
}

/* ───────────────────────── 菜单栏与上下文菜单 ───────────────────────── */
QMenuBar {
    background: #FFFFFF;
    color: #1C1C1E;
    padding: 3px 6px;
    border-bottom: 1px solid #E5E5EA;
}
QMenuBar::item {
    padding: 6px 10px;
    border-radius: 5px;
}
QMenuBar::item:selected {
    background: #E5E5EA;
}
QMenu {
    background: #FFFFFF;
    color: #1C1C1E;
    border: 1px solid #D1D1D6;
    border-radius: 8px;
    padding: 5px;
}
QMenu::item {
    padding: 7px 24px 7px 14px;
    border-radius: 4px;
}
QMenu::item:selected {
    background: #E5E5EA;
}
QMenu::separator {
    height: 1px;
    background: #E5E5EA;
    margin: 4px 6px;
}

/* ───────────────────────── 主工具栏 ───────────────────────── */
QToolBar#main_command_toolbar {
    background: #FFFFFF;
    border: 0;
    border-bottom: 1px solid #E5E5EA;
    padding: 6px 10px;
    spacing: 6px;
}
QToolBar#main_command_toolbar::separator {
    width: 1px;
    background: #E5E5EA;
    margin: 6px 4px;
}
QToolBar#main_command_toolbar QLabel {
    color: #1C1C1E;
    font-size: 13px;
    font-weight: 500;
    padding: 0 4px;
}

/* ───────────────────────── 底栏与状态栏 ───────────────────────── */
QStatusBar {
    background: #FFFFFF;
    color: #636366;
    border-top: 1px solid #E5E5EA;
    padding: 2px 4px;
}
QStatusBar QLabel {
    color: #636366;
    padding: 0 8px;
    font-size: 12px;
}
/* 同深色侧，浅色 #E5E5EA 打 #F2F2F7 上同样过淡 */
QSplitter::handle {
    background: #C9C9CE;
}
QSplitter::handle:horizontal {
    width: 6px;
}
QSplitter::handle:vertical {
    height: 6px;
}
QSplitter::handle:hover {
    background: #4F7DFF;
}
QSplitter::handle:pressed {
    background: #3B6BFF;
}
QToolTip {
    background: #FFFFFF;
    color: #1C1C1E;
    border: 1px solid #D1D1D6;
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 12px;
}

/* ───────────────────────── 表格现代质感与选中态 ───────────────────────── */
QTableView {
    background-color: #FFFFFF;
    alternate-background-color: #F9FAFB;
    gridline-color: #F2F2F7;
    border: 1px solid #E5E5EA;
    border-radius: 8px;
    outline: none;
    selection-background-color: #E2ECFF;
    selection-color: #0F172A;
}
QTableView::item {
    padding: 6px 10px;
    border: none;
    color: #1E293B;
}
QTableView::item:hover {
    background-color: #F1F5F9;
}
QTableView::item:selected {
    background-color: #E2ECFF;
    color: #0F172A;
}
QHeaderView::section {
    background-color: #F9FAFB;
    color: #64748B;
    font-weight: 600;
    font-size: 12px;
    padding: 7px 10px;
    border: none;
    border-bottom: 1px solid #E5E5EA;
    border-right: 1px solid #F2F2F7;
}

/* ───────────────────────── 导出侧栏与卡片容器 ───────────────────────── */
QWidget#export_panel, QWidget#export_scroll_content {
    background: transparent;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
SimpleCardWidget {
    background: #FFFFFF;
    border: 1px solid #E5E5EA;
    border-radius: 8px;
}
"""


def _apply_palette(app, dark: bool) -> None:
    p = QPalette()
    if dark:
        p.setColor(QPalette.Window, QColor("#1C1C1E"))
        p.setColor(QPalette.WindowText, QColor("#F5F5F7"))
        p.setColor(QPalette.Base, QColor("#202022"))
        p.setColor(QPalette.AlternateBase, QColor("#252528"))
        p.setColor(QPalette.Text, QColor("#F5F5F7"))
        p.setColor(QPalette.Button, QColor("#2C2C2E"))
        p.setColor(QPalette.ButtonText, QColor("#F5F5F7"))
        p.setColor(QPalette.Highlight, QColor(_ACCENT))
        p.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        p.setColor(QPalette.PlaceholderText, QColor("#8E8E93"))
    else:
        p.setColor(QPalette.Window, QColor("#F2F2F7"))
        p.setColor(QPalette.WindowText, QColor("#1C1C1E"))
        p.setColor(QPalette.Base, QColor("#FFFFFF"))
        p.setColor(QPalette.AlternateBase, QColor("#F9FAFB"))
        p.setColor(QPalette.Text, QColor("#1C1C1E"))
        p.setColor(QPalette.Button, QColor("#FFFFFF"))
        p.setColor(QPalette.ButtonText, QColor("#1C1C1E"))
        p.setColor(QPalette.Highlight, QColor(_ACCENT))
        p.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        p.setColor(QPalette.PlaceholderText, QColor("#8E8E93"))
    app.setPalette(p)


def apply_theme(app, dark: bool) -> None:
    """应用 QFluentWidgets 原生主题与标准 Qt 外壳配色（lazy=True 极速模式，零延迟切换）。

    外壳 QSS 必须且只能安装在 QApplication 级：若误传入 QWidget（如 MainWindow），
    QSS 会装到窗口级、与启动时的应用级 QSS 双层叠加——首次切换主题时布局被二次
    重算（工具栏变高、按钮变宽）。此处对传入对象做防御性归一，始终作用于全局应用。
    """
    from PySide6.QtWidgets import QApplication
    target = QApplication.instance() or app
    setTheme(Theme.DARK if dark else Theme.LIGHT, lazy=True)
    _apply_palette(target, dark)
    target.setStyleSheet(_DARK_SHELL_QSS if dark else _LIGHT_SHELL_QSS)


def toggle_theme(app) -> bool:
    new_dark = not isDarkTheme()
    apply_theme(app, new_dark)
    return new_dark


def is_dark(app=None) -> bool:
    return isDarkTheme()


def load_theme() -> str:
    try:
        prefs = load_preferences()
        theme = str(getattr(prefs, "ui_theme", "") or "")
        return theme if theme in ("light", "dark") else _DEFAULT_THEME
    except Exception:
        return _DEFAULT_THEME


def save_theme(dark: bool) -> None:
    try:
        prefs = load_preferences()
        prefs.ui_theme = "dark" if dark else "light"
        save_preferences(prefs)
    except Exception:
        logger.debug("[theme] 保存主题偏好失败")


# 自绘分割条控件已移至 ui/widgets.py；此处再导出以保持既有 import 路径可用
from .widgets import GripSplitter, GripSplitterHandle  # noqa: E402,F401


__all__ = [
    "THEME_KEY", "apply_theme", "toggle_theme", "is_dark", "load_theme", "save_theme",
    "GripSplitter", "GripSplitterHandle",
]
