"""ui.commands.flags — 确认 / 标脏 / 锁定 / 语言。"""
from __future__ import annotations

import logging
from typing import Callable, List, Tuple


from subs.models import SubtitleProject

from .base import _BaseCmd

logger = logging.getLogger("ui.commands")

class ConfirmSentencesCommand(_BaseCmd):
    """手动确认句子编辑（将标脏句设为干净句，固定效果）。"""

    def __init__(
        self,
        project: SubtitleProject,
        rows: List[int],
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"确认 {len(rows)} 句")
        self._rows = list(rows)
        self._old_states: List[Tuple[int, bool]] = []
        for r in self._rows:
            if 0 <= r < len(project.sentences):
                self._old_states.append((r, project.sentences[r].is_dirty))

    def redo(self) -> None:
        for r, _ in self._old_states:
            if 0 <= r < len(self._project.sentences):
                self._project.sentences[r].is_dirty = False
        self._notify()

    def undo(self) -> None:
        for r, old_dirty in self._old_states:
            if 0 <= r < len(self._project.sentences):
                self._project.sentences[r].is_dirty = old_dirty
        self._notify()


class SetSentencesDirtyCommand(_BaseCmd):
    """手动将句子标脏（待 AI 重对齐）。"""

    def __init__(
        self,
        project: SubtitleProject,
        rows: List[int],
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"标脏 {len(rows)} 句")
        self._rows = list(rows)
        self._old_states: List[Tuple[int, bool]] = []
        for r in self._rows:
            if 0 <= r < len(project.sentences):
                self._old_states.append((r, project.sentences[r].is_dirty))

    def redo(self) -> None:
        for r, _ in self._old_states:
            if 0 <= r < len(self._project.sentences):
                self._project.sentences[r].is_dirty = True
        self._notify()

    def undo(self) -> None:
        for r, old_dirty in self._old_states:
            if 0 <= r < len(self._project.sentences):
                self._project.sentences[r].is_dirty = old_dirty
        self._notify()


class ToggleLockSentencesCommand(_BaseCmd):
    """切换句子锁定状态（加锁保护，防止一切重对齐覆写）。"""

    def __init__(
        self,
        project: SubtitleProject,
        rows: List[int],
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"切换锁定 {len(rows)} 句")
        self._rows = list(rows)
        self._old_states: List[Tuple[int, bool]] = []
        for r in self._rows:
            if 0 <= r < len(project.sentences):
                self._old_states.append((r, project.sentences[r].is_locked))

    def redo(self) -> None:
        for r, old_locked in self._old_states:
            if 0 <= r < len(self._project.sentences):
                self._project.sentences[r].is_locked = not old_locked
        self._notify()

    def undo(self) -> None:
        for r, old_locked in self._old_states:
            if 0 <= r < len(self._project.sentences):
                self._project.sentences[r].is_locked = old_locked
        self._notify()


class ChangeSentenceLanguageCommand(_BaseCmd):
    """修改单句或多句的语言标记。"""

    def __init__(
        self,
        project: SubtitleProject,
        rows: List[int],
        new_lang: str,
        on_change: Callable[[], None],
    ):
        super().__init__(project, on_change, f"修改语言 {len(rows)} 句 -> {new_lang}")
        self._rows = list(rows)
        self._new_lang = new_lang
        self._old_langs: List[Tuple[int, str, bool]] = []
        for r in self._rows:
            if 0 <= r < len(project.sentences):
                s = project.sentences[r]
                self._old_langs.append((r, s.language, s.is_dirty))

    def redo(self) -> None:
        for r, _, _ in self._old_langs:
            if 0 <= r < len(self._project.sentences):
                s = self._project.sentences[r]
                s.language = self._new_lang
                s.is_dirty = True
        self._notify()

    def undo(self) -> None:
        for r, old_lang, old_dirty in self._old_langs:
            if 0 <= r < len(self._project.sentences):
                s = self._project.sentences[r]
                s.language = old_lang
                s.is_dirty = old_dirty
        self._notify()
