"""ui.commands.base — QUndoCommand 基类。"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtGui import QUndoCommand

from subs.models import SubtitleProject

logger = logging.getLogger("ui.commands")


class _BaseCmd(QUndoCommand):
    """所有 command 的基类：提供 on_change 回调 + project_changed signal 触发。

    on_change 是 MainWindow 注入的回调，签名: () -> None
    每次 redo/undo 后调用，UI 据此刷新（表格行重画 + 脏色块同步 + 状态栏更新）。
    """

    def __init__(self, project: SubtitleProject, on_change: Callable[[], None], text: str):
        super().__init__(text)
        self._project = project
        self._on_change = on_change

    def _notify(self) -> None:
        try:
            if self._on_change is not None:
                self._on_change()
        except Exception:
            logger.exception("[commands] on_change 回调异常")
