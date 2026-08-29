"""ui.main_window — 主窗口包。

对外：``from ui.main_window import MainWindow``（与单文件时代相同）。
子模块：window / chrome / navigation / editing。
"""
from __future__ import annotations

from .window import MainWindow

__all__ = ["MainWindow"]
