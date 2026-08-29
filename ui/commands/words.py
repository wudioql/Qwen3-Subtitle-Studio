"""ui.commands.words — 字级时间与波形字边界。

句级 start/end 与最外层 WordTimestamp（含首/尾标点）保持绑定；内部字界编辑
不会改变句长，首/尾字时间改变时句界随之更新。句级手柄反向同步最外层 word。
"""
from __future__ import annotations

import copy
import logging
from typing import Callable, List, Optional

from subs.models import SubtitleProject, WordTimestamp

from .base import _BaseCmd
from .helpers import _resolve_row

logger = logging.getLogger("ui.commands")


class EditWordTimeCommand(_BaseCmd):
    """编辑单字/词时间戳（通过表格单元格修改）。

    字级路径注意：不能按构造时的 word_idx 直接取词——
        redo 末尾 project.sort() 会按 start_time 重排句子，所以按 sid 定位；修改后
        以首/尾 word 重算句界，整表 words + 句界快照保证 undo/redo 精确。
    """

    def __init__(
        self,
        project: SubtitleProject,
        sent_idx: int,
        word_idx: int,
        new_start: float,
        new_end: float,
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"编辑字时间 S{sent_idx+1}-W{word_idx+1}")
        self._sent_idx = sent_idx
        self._sid: int = project.sentences[sent_idx].sid if 0 <= sent_idx < len(project.sentences) else -1
        self._word_idx = word_idx
        self._new_start = float(new_start)
        self._new_end = float(new_end)
        self._old_dirty: bool = False
        self._old_start: float = 0.0
        self._old_end: float = 0.0
        self._new_sentence_start: float = 0.0
        self._new_sentence_end: float = 0.0
        self._old_words: Optional[List[WordTimestamp]] = None
        self._new_words: Optional[List[WordTimestamp]] = None
        if 0 <= sent_idx < len(project.sentences):
            self._old_dirty = project.sentences[sent_idx].is_dirty

    def redo(self) -> None:
        idx = _resolve_row(self._project, self._sid, self._sent_idx)
        if idx is None:
            return
        sent = self._project.sentences[idx]
        self._old_dirty = sent.is_dirty
        if self._new_words is None:
            self._old_start = sent.start_time
            self._old_end = sent.end_time
            if not (0 <= self._word_idx < len(sent.words)):
                return
            self._old_words = [copy.deepcopy(w) for w in sent.words]
            w = sent.words[self._word_idx]
            w.start_time = self._new_start
            w.end_time = self._new_end
            sent.words.sort(key=lambda x: x.start_time)
            sent.fix_times_from_words()
            self._new_sentence_start = sent.start_time
            self._new_sentence_end = sent.end_time
            self._new_words = [copy.deepcopy(w) for w in sent.words]
        else:
            sent.words = [copy.deepcopy(w) for w in self._new_words]
            sent.start_time = self._new_sentence_start
            sent.end_time = self._new_sentence_end
        sent.is_dirty = True
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        if self._old_words is None:
            return
        idx = _resolve_row(self._project, self._sid, self._sent_idx)
        if idx is None:
            return
        sent = self._project.sentences[idx]
        sent.words = [copy.deepcopy(w) for w in self._old_words]
        sent.start_time = self._old_start
        sent.end_time = self._old_end
        sent.is_dirty = self._old_dirty
        self._project.sort()
        self._notify()


class WordBoundaryDragCommand(_BaseCmd):
    """波形拖动内部字界；句界始终绑定 words 的最外侧时间。"""

    def __init__(
        self,
        project: SubtitleProject,
        sent_idx: int,
        old_words: List[WordTimestamp],
        new_words: List[WordTimestamp],
        on_change: Callable[[], None],
        description: str = "拖动字边界",
    ):
        super().__init__(project, on_change, f"{description} S{sent_idx+1}")
        self._sent_idx = sent_idx
        self._sid: int = project.sentences[sent_idx].sid if 0 <= sent_idx < len(project.sentences) else -1
        self._old_words = [copy.deepcopy(w) for w in old_words]
        self._new_words = [copy.deepcopy(w) for w in new_words]
        self._old_dirty: bool = False
        self._old_start: float = 0.0
        self._old_end: float = 0.0
        if 0 <= sent_idx < len(project.sentences):
            sentence = project.sentences[sent_idx]
            self._old_dirty = sentence.is_dirty
            self._old_start = sentence.start_time
            self._old_end = sentence.end_time

    def redo(self) -> None:
        idx = _resolve_row(self._project, self._sid, self._sent_idx)
        if idx is None:
            return
        sent = self._project.sentences[idx]
        self._old_dirty = sent.is_dirty
        sent.words = [copy.deepcopy(w) for w in self._new_words]
        sent.fix_times_from_words()
        sent.is_dirty = True
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        idx = _resolve_row(self._project, self._sid, self._sent_idx)
        if idx is None:
            return
        sent = self._project.sentences[idx]
        sent.words = [copy.deepcopy(w) for w in self._old_words]
        sent.start_time = self._old_start
        sent.end_time = self._old_end
        sent.is_dirty = self._old_dirty
        self._project.sort()
        self._notify()
