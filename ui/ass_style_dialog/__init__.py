"""ui.ass_style_dialog — ASS 文字样式弹窗包。

对外：``from ui.ass_style_dialog import AssStyleDialog, SubtitlePreviewWidget``
"""
from __future__ import annotations

from .dialog import AssStyleDialog
from .preview import SubtitlePreviewWidget

__all__ = ["AssStyleDialog", "SubtitlePreviewWidget"]
