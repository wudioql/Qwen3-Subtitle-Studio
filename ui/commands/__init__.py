"""ui.commands — QUndoCommand 集合（包入口）

设计原则：
- 所有 command 都不弹任何 QMessageBox（手动改 silent）
- redo() 设 is_dirty=True；undo() 精准恢复修改前 is_dirty
- 拆分/合并纯函数在 helpers，可单独测

子模块：helpers / base / edit / structure / words / flags
对外：``from ui.commands import EditTextCommand, ...`` 不变。
"""
from __future__ import annotations

from .helpers import (
    _find_by_sid,
    _merge_sentences,
    _resolve_row,
    _split_sentence_at,
    _split_sentence_at_char,
)
from .base import _BaseCmd
from .edit import (
    AddSentenceCommand,
    DeleteSentencesCommand,
    EditTextCommand,
    EditTimeCommand,
)
from .structure import (
    BoundaryDragCommand,
    MergeSentencesCommand,
    SplitSentenceByCharCommand,
    SplitSentenceCommand,
    StripTrailingPunctCommand,
)
from .words import EditWordTimeCommand, WordBoundaryDragCommand
from .flags import (
    ChangeSentenceLanguageCommand,
    ConfirmSentencesCommand,
    SetSentencesDirtyCommand,
    ToggleLockSentencesCommand,
)

__all__ = [
    "EditTextCommand",
    "EditTimeCommand",
    "AddSentenceCommand",
    "DeleteSentencesCommand",
    "SplitSentenceCommand",
    "SplitSentenceByCharCommand",
    "MergeSentencesCommand",
    "StripTrailingPunctCommand",
    "BoundaryDragCommand",
    "EditWordTimeCommand",
    "WordBoundaryDragCommand",
    "ConfirmSentencesCommand",
    "SetSentencesDirtyCommand",
    "ToggleLockSentencesCommand",
    "ChangeSentenceLanguageCommand",
    "_split_sentence_at",
    "_merge_sentences",
    "_find_by_sid",
    "_resolve_row",
    "_BaseCmd",
]
