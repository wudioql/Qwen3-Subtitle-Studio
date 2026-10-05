"""ui.commands.base — QUndoCommand 基类。"""
from __future__ import annotations

import copy
import logging
from typing import Callable, Optional

from PySide6.QtGui import QUndoCommand

from subs.models import SubtitleProject

from .helpers import _bind_sentence_and_word_edges, _resolve_row

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


class _SentenceEdgeCommand(_BaseCmd):
    """拖动/编辑单句 start/end 的**共同状态机**，供下面两个命令共用。

    `EditTimeCommand`（表格里改时间）与 `BoundaryDragCommand`（波形拖边界）行为完全一致，
    只有命令文案、以及 `new_start`/`new_end` 是否允许为 None（拖拽可只动一端）不同，
    所以 redo/undo 与状态初始化收在这里，避免两份逐字相同的复制各自漂移。

    两条不可变的语义（改这里前先读）：
    - 目标句按**构造时锁定的稳定 sid** 定位，不裸用行号：redo 末尾 `sort()` 会把本句挪走。
    - redo 通过 `_bind_sentence_and_word_edges` 同步最外层 word/标点界，**内部字界不动**；
      首次 redo 记录 `_applied_*`/`_new_words`，之后 redo 直接回放，保证幂等。
    """

    def _init_edge_state(
        self,
        project: SubtitleProject,
        idx: int,
        new_start: Optional[float],
        new_end: Optional[float],
    ) -> None:
        self._idx = idx
        self._sid: int = project.sentences[idx].sid if 0 <= idx < len(project.sentences) else -1
        self._new_start = new_start
        self._new_end = new_end
        self._old_start: float = 0.0
        self._old_end: float = 0.0
        self._old_dirty: bool = False
        self._old_words = None
        self._new_words = None
        # 初值只在「首次 redo 之前的 undo」才可能被读到，而 QUndoStack 不会那样调用；
        # 首次 redo 必定重算为真实值，故 0.0 与 float(new_start) 等价。
        self._applied_start: float = 0.0
        self._applied_end: float = 0.0
        if 0 <= idx < len(project.sentences):
            self._old_dirty = project.sentences[idx].is_dirty

    def redo(self) -> None:
        idx = _resolve_row(self._project, self._sid, self._idx)
        if idx is None:
            return
        sent = self._project.sentences[idx]
        self._old_start = sent.start_time
        self._old_end = sent.end_time
        self._old_dirty = sent.is_dirty
        if self._new_words is None:
            self._old_words = copy.deepcopy(sent.words)
            _bind_sentence_and_word_edges(
                sent, new_start=self._new_start, new_end=self._new_end,
            )
            self._applied_start = sent.start_time
            self._applied_end = sent.end_time
            self._new_words = copy.deepcopy(sent.words)
        else:
            sent.start_time = self._applied_start
            sent.end_time = self._applied_end
            sent.words = copy.deepcopy(self._new_words)
        sent.is_dirty = True
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        idx = _resolve_row(self._project, self._sid, self._idx)
        if idx is None:
            return
        sent = self._project.sentences[idx]
        sent.start_time = self._old_start
        sent.end_time = self._old_end
        if self._old_words is not None:
            sent.words = copy.deepcopy(self._old_words)
        sent.is_dirty = self._old_dirty
        self._project.sort()
        self._notify()
