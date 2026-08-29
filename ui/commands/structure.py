"""ui.commands.structure — 拆分 / 合并 / 句边界拖拽。"""
from __future__ import annotations

import copy
import logging
from typing import Callable, List, Optional, Tuple


from subs.models import Sentence, SubtitleProject

from .base import _BaseCmd
from .helpers import (
    _bind_sentence_and_word_edges,
    _find_by_sid,
    _merge_sentences,
    _resolve_row,
    _split_sentence_at,
    _split_sentence_at_char,
)

logger = logging.getLogger("ui.commands")

class SplitSentenceCommand(_BaseCmd):
    """按 cut_time 拆单句为 2 句。"""

    def __init__(
        self,
        project: SubtitleProject,
        row: int,
        cut_time: float,
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"拆分 S{row+1}")
        self._row = row
        self._cut_time = cut_time
        self._left: Optional[Sentence] = None
        self._right: Optional[Sentence] = None
        self._original: Optional[Sentence] = None
        if 0 <= row < len(project.sentences):
            self._original = copy.deepcopy(project.sentences[row])

    def redo(self) -> None:
        if not (0 <= self._row < len(self._project.sentences)):
            return
        original = self._project.sentences[self._row]
        if self._original is None:
            self._original = copy.deepcopy(original)
        left, right = _split_sentence_at(original, self._cut_time)
        self._left = left
        self._right = right
        del self._project.sentences[self._row]
        self._project.sentences.insert(self._row, left)
        self._project.sentences.insert(self._row + 1, right)
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        if self._original is None or self._left is None or self._right is None:
            return
        # 两半各自按 sid 定位——不能假定「右半 = 左半 + 1」，
        # sort 之后可能有其他句落入两半的时间区间而夹在中间，+1 删除会误伤它
        idxs = []
        for half in (self._left, self._right):
            cur = _find_by_sid(self._project, half.sid)
            if cur is None:
                return
            idxs.append(cur)
        insert_at = min(idxs)
        for i in sorted(idxs, reverse=True):
            del self._project.sentences[i]
        self._project.sentences.insert(insert_at, copy.deepcopy(self._original))
        self._project.sort()
        self._notify()


class SplitSentenceByCharCommand(_BaseCmd):
    """按「文本光标」位置拆单句为 2 句（非按播放时间）。"""

    def __init__(
        self,
        project: SubtitleProject,
        row: int,
        char_index: int,
        on_change: Callable[[], None],
        new_text: Optional[str] = None,
    ):
        super().__init__(project, on_change, f"光标拆分 S{row+1}")
        self._row = row
        self._char_index = int(char_index)
        self._new_text = new_text
        self._left: Optional[Sentence] = None
        self._right: Optional[Sentence] = None
        self._original: Optional[Sentence] = None
        # 光标落在首尾时拆不动，redo 退化为「纯文本编辑」；
        # 必须记标记让 undo 能撤回（此前该分支 undo 直接 return，文本改动永久落栈底）
        self._text_only_edit = False
        if 0 <= row < len(project.sentences):
            self._original = copy.deepcopy(project.sentences[row])

    def redo(self) -> None:
        if not (0 <= self._row < len(self._project.sentences)):
            return
        original = self._project.sentences[self._row]
        if self._original is None:
            self._original = copy.deepcopy(original)
        base = copy.deepcopy(original)
        if self._new_text is not None:
            base.text = self._new_text
            base.is_dirty = True
        pair = _split_sentence_at_char(base, self._char_index)
        if pair is None:
            if self._new_text is not None and self._new_text != original.text:
                self._text_only_edit = True
                original.text = self._new_text
                original.is_dirty = True
                self._project.sort()
                self._notify()
            return
        self._left, self._right = pair
        del self._project.sentences[self._row]
        self._project.sentences.insert(self._row, self._left)
        self._project.sentences.insert(self._row + 1, self._right)
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        if self._original is None:
            return
        # 纯文本编辑退化分支（未发生拆分）——按原文本/脏状态恢复
        if self._text_only_edit:
            idx = _find_by_sid(self._project, self._original.sid)
            if idx is None:
                idx = self._row if 0 <= self._row < len(self._project.sentences) else None
            if idx is None:
                return
            sent = self._project.sentences[idx]
            sent.text = self._original.text
            sent.is_dirty = self._original.is_dirty
            self._project.sort()
            self._notify()
            return
        if self._left is None or self._right is None:
            return
        # 同 SplitSentenceCommand——两半各按 sid 定位，不假定相邻
        idxs = []
        for half in (self._left, self._right):
            cur = _find_by_sid(self._project, half.sid)
            if cur is None:
                return
            idxs.append(cur)
        insert_at = min(idxs)
        for i in sorted(idxs, reverse=True):
            del self._project.sentences[i]
        self._project.sentences.insert(insert_at, copy.deepcopy(self._original))
        self._project.sort()
        self._notify()


class MergeSentencesCommand(_BaseCmd):
    """合并多选 N 句为 1 句。"""

    def __init__(
        self,
        project: SubtitleProject,
        rows: List[int],
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"合并 {len(rows)} 句")
        self._rows = sorted(set(rows))
        self._snapshots: List[Tuple[int, Sentence]] = []
        for r in self._rows:
            if 0 <= r < len(project.sentences):
                self._snapshots.append((r, copy.deepcopy(project.sentences[r])))
        self._merged: Optional[Sentence] = None

    def redo(self) -> None:
        if len(self._snapshots) < 2:
            return
        current = []
        for r_orig, snap in self._snapshots:
            idx = self._find_current(snap)
            if idx is not None:
                current.append((idx, snap))
        current.sort(key=lambda x: x[0])
        if not current:
            return
        merged = _merge_sentences([snap for _, snap in current])
        self._merged = merged
        rows_del = [idx for idx, _ in current]
        rows_del.sort(reverse=True)
        first_idx = rows_del[-1]
        for i in rows_del:
            del self._project.sentences[i]
        self._project.sentences.insert(first_idx, merged)
        self._project.mark_dirty(first_idx)
        self._project.sort()
        self._notify()

    def undo(self) -> None:
        if self._merged is None:
            return
        cur_idx = _find_by_sid(self._project, self._merged.sid)
        if cur_idx is None:
            return
        del self._project.sentences[cur_idx]
        for r_orig, snap in sorted(self._snapshots, key=lambda x: x[0]):
            insert_at = max(0, min(r_orig, len(self._project.sentences)))
            self._project.sentences.insert(insert_at, copy.deepcopy(snap))
        self._project.sort()
        self._notify()

    def _find_current(self, snap: Sentence) -> Optional[int]:
        return _find_by_sid(self._project, snap.sid)


class BoundaryDragCommand(_BaseCmd):
    """波形拖单句边界（start / end）。"""

    def __init__(
        self,
        project: SubtitleProject,
        idx: int,
        new_start: Optional[float],
        new_end: Optional[float],
        on_change: Callable[[], None],
    ):
        edge = []
        if new_start is not None:
            edge.append("start")
        if new_end is not None:
            edge.append("end")
        super().__init__(project, on_change, f"拖动 {'/'.join(edge)} S{idx+1}")
        self._idx = idx
        # 同 EditTimeCommand，sid 锁定目标句
        self._sid: int = project.sentences[idx].sid if 0 <= idx < len(project.sentences) else -1
        self._new_start = new_start
        self._new_end = new_end
        self._old_start: float = 0.0
        self._old_end: float = 0.0
        self._old_dirty: bool = False
        self._old_words = None
        self._new_words = None
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


class StripTrailingPunctCommand(_BaseCmd):
    """批量删除句尾标点（字符 + 对应时间）。锁定句跳过（手动精修保护）。"""

    def __init__(
        self,
        project: SubtitleProject,
        rows: List[int],
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"删除 {len(rows)} 句句尾标点")
        self._rows = list(rows)
        # 按 sid 记录快照（删除会改 end_time 触发 sort 重排，不能靠行号定位）
        self._snapshots: List[Tuple[int, Sentence]] = []
        for r in self._rows:
            if 0 <= r < len(project.sentences):
                s = project.sentences[r]
                if not s.is_locked:
                    self._snapshots.append((s.sid, copy.deepcopy(s)))
        self._changed = 0

    def redo(self) -> None:
        from core.text_utils import strip_trailing_punct
        self._changed = 0
        for sid, _snap in self._snapshots:
            idx = _find_by_sid(self._project, sid)
            if idx is None:
                continue
            if strip_trailing_punct(self._project.sentences[idx]):
                self._changed += 1
        if self._changed:
            self._project.sort()
        self._notify()

    def undo(self) -> None:
        for sid, snap in self._snapshots:
            idx = _find_by_sid(self._project, sid)
            if idx is not None:
                self._project.sentences[idx] = copy.deepcopy(snap)
        self._project.sort()
        self._notify()
