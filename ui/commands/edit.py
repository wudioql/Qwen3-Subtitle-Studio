"""ui.commands.edit — 文本 / 时间编辑与增删句。"""
from __future__ import annotations

import copy
import logging
from typing import Callable, List, Optional, Tuple


from subs.models import Sentence, SubtitleProject

from .base import _BaseCmd, _SentenceEdgeCommand
from .helpers import (
    _find_by_sid,
    _resolve_row,
)

logger = logging.getLogger("ui.commands")

class EditTextCommand(_BaseCmd):
    """编辑单句文本。"""

    def __init__(
        self,
        project: SubtitleProject,
        idx: int,
        new_text: str,
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"编辑文本 S{idx+1}")
        self._idx = idx
        # 构造时锁定目标句的稳定 sid，redo/undo 不裸用行号
        self._sid: int = project.sentences[idx].sid if 0 <= idx < len(project.sentences) else -1
        self._new_text = new_text
        self._old_text: str = ""
        self._old_dirty: bool = False
        if 0 <= idx < len(project.sentences):
            self._old_dirty = project.sentences[idx].is_dirty

    def redo(self) -> None:
        idx = _resolve_row(self._project, self._sid, self._idx)
        if idx is None:
            return
        sent = self._project.sentences[idx]
        self._old_text = sent.text
        self._old_dirty = sent.is_dirty
        sent.text = self._new_text
        sent.is_dirty = True
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        idx = _resolve_row(self._project, self._sid, self._idx)
        if idx is None:
            return
        sent = self._project.sentences[idx]
        sent.text = self._old_text
        sent.is_dirty = self._old_dirty
        self._project.sort()
        self._notify()


class EditTimeCommand(_SentenceEdgeCommand):
    """编辑单句 start / end（redo/undo 共用 ``_SentenceEdgeCommand`` 状态机）。"""

    def __init__(
        self,
        project: SubtitleProject,
        idx: int,
        new_start: float,
        new_end: float,
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"编辑时间 S{idx+1}")
        self._init_edge_state(project, idx, new_start, new_end)


class AddSentenceCommand(_BaseCmd):
    """在 row 位置插入新句。"""

    def __init__(
        self,
        project: SubtitleProject,
        row: int,
        new_sentence: Sentence,
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"插入 S{row+1}")
        self._row = row
        self._sentence = copy.deepcopy(new_sentence)

    def redo(self) -> None:
        row = max(0, min(self._row, len(self._project.sentences)))
        self._project.sentences.insert(row, copy.deepcopy(self._sentence))
        self._project.mark_dirty(row)
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        idx = _find_by_sid(self._project, self._sentence.sid)
        if idx is not None:
            del self._project.sentences[idx]
        self._project.sort()
        self._notify()


class DeleteSentencesCommand(_BaseCmd):
    """删除多句（rows 升序）。"""

    def __init__(
        self,
        project: SubtitleProject,
        rows: List[int],
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"删除 {len(rows)} 句")
        self._rows = sorted(set(rows))
        self._snapshots: List[Tuple[int, Sentence]] = []
        for r in self._rows:
            if 0 <= r < len(project.sentences):
                self._snapshots.append((r, copy.deepcopy(project.sentences[r])))

    def redo(self) -> None:
        if not self._snapshots:
            return
        rows_now = []
        for r_orig, snap in self._snapshots:
            cur_idx = self._find_current(snap)
            if cur_idx is not None:
                rows_now.append(cur_idx)
        rows_now.sort(reverse=True)
        for i in rows_now:
            if 0 <= i < len(self._project.sentences):
                del self._project.sentences[i]
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        for r_orig, snap in sorted(self._snapshots, key=lambda x: x[0]):
            insert_at = max(0, min(r_orig, len(self._project.sentences)))
            self._project.sentences.insert(insert_at, copy.deepcopy(snap))
        self._project.sort()
        self._notify()

    def _find_current(self, snap: Sentence) -> Optional[int]:
        return _find_by_sid(self._project, snap.sid)
