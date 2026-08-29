"""tests/test_undo_commands.py — ui.commands Undo/Redo 命令契约（压缩：9+2 → 5）

覆盖 Undo/Redo 全命令 + 拆分撤销钉样。
覆盖：
1. EditWordTimeCommand / WordBoundaryDragCommand redo / undo（合并）
2. Confirm/Dirty/ToggleLock/ChangeLanguage redo / undo（合并）
3. 干净句编辑后 undo 精准恢复 is_dirty=False
4. sort 重排后 undo 按 sid 复原（句级 / 字级 / 词序漂移，3 项合并）
5. 拆分撤销：纯文本兜底可撤回 + sort 夹句不误删
"""

from __future__ import annotations


from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

pytest.importorskip("PySide6", exc_type=ImportError)   # 无 Qt 环境跳过整个模块，保证 -m logic 可 collection

from PySide6.QtGui import QUndoStack

from subs.models import Sentence, SubtitleProject, WordTimestamp
from ui.commands import (
    ChangeSentenceLanguageCommand,
    ConfirmSentencesCommand,
    EditWordTimeCommand,
    SetSentencesDirtyCommand,
    SplitSentenceByCharCommand,
    SplitSentenceCommand,
    ToggleLockSentencesCommand,
    WordBoundaryDragCommand,
)

# 本文件测 QUndoCommand / QUndoStack 行为，顶层依赖 PySide6 → 归 ui 标记，
# 使 `pytest -m logic` 在无 Qt 环境真正可收集/运行（logic=零 Qt 合同）。
pytestmark = pytest.mark.ui


def _make_test_project() -> SubtitleProject:
    s1 = Sentence(
        text="你好世界", start_time=1.0, end_time=3.0,
        words=[
            WordTimestamp(text="你", start_time=1.0, end_time=1.5),
            WordTimestamp(text="好", start_time=1.5, end_time=2.0),
            WordTimestamp(text="世", start_time=2.0, end_time=2.5),
            WordTimestamp(text="界", start_time=2.5, end_time=3.0),
        ],
        language="zh", is_dirty=True, is_locked=False,
    )
    s2 = Sentence(
        text="Hello world", start_time=3.5, end_time=5.0,
        words=[
            WordTimestamp(text="Hello", start_time=3.5, end_time=4.2),
            WordTimestamp(text="world", start_time=4.2, end_time=5.0),
        ],
        language="en", is_dirty=False, is_locked=False,
    )
    return SubtitleProject(sentences=[s1, s2], media_duration=10.0)


def _case_edit_and_drag_commands():
    # EditWordTimeCommand redo/undo + 通知
    p = _make_test_project()
    notified: list = []
    cmd = EditWordTimeCommand(p, 0, 1, 1.6, 2.2, lambda: notified.append(True))
    cmd.redo()
    assert len(notified) == 1
    assert abs(p.sentences[0].words[1].start_time - 1.6) < 1e-4
    assert abs(p.sentences[0].words[1].end_time - 2.2) < 1e-4
    assert p.sentences[0].is_dirty is True
    cmd.undo()
    assert len(notified) == 2
    assert abs(p.sentences[0].words[1].start_time - 1.5) < 1e-4
    assert abs(p.sentences[0].words[1].end_time - 2.0) < 1e-4

    # WordBoundaryDragCommand redo/undo：句界绑定首/尾 word
    sent0_start, sent0_end = p.sentences[0].start_time, p.sentences[0].end_time
    old_words = [
        WordTimestamp(text="你", start_time=1.0, end_time=1.5),
        WordTimestamp(text="好", start_time=1.5, end_time=2.0),
        WordTimestamp(text="世", start_time=2.0, end_time=2.5),
        WordTimestamp(text="界", start_time=2.5, end_time=3.0),
    ]
    new_words = [
        WordTimestamp(text="你", start_time=1.0, end_time=1.4),
        WordTimestamp(text="好", start_time=1.4, end_time=1.9),
        WordTimestamp(text="世", start_time=1.9, end_time=2.4),
        WordTimestamp(text="界", start_time=2.4, end_time=3.2),
    ]
    cmd2 = WordBoundaryDragCommand(p, 0, old_words, new_words, lambda: notified.append(True))
    cmd2.redo()
    assert abs(p.sentences[0].start_time - sent0_start) < 1e-4
    assert abs(p.sentences[0].end_time - 3.2) < 1e-4  # 句尾随最外层 word
    assert abs(p.sentences[0].words[0].end_time - 1.4) < 1e-4
    assert abs(p.sentences[0].words[-1].end_time - 3.2) < 1e-4
    cmd2.undo()
    assert abs(p.sentences[0].end_time - sent0_end) < 1e-4
    assert abs(p.sentences[0].words[0].end_time - 1.5) < 1e-4


def _case_confirm_dirty_lock_language_commands():
    p = _make_test_project()
    assert p.sentences[0].is_dirty is True and p.sentences[1].is_dirty is False

    cmd_confirm = ConfirmSentencesCommand(p, [0], lambda: None)
    cmd_confirm.redo()
    assert p.sentences[0].is_dirty is False
    cmd_confirm.undo()
    assert p.sentences[0].is_dirty is True

    cmd_dirty = SetSentencesDirtyCommand(p, [1], lambda: None)
    cmd_dirty.redo()
    assert p.sentences[1].is_dirty is True
    cmd_dirty.undo()
    assert p.sentences[1].is_dirty is False

    cmd_lock = ToggleLockSentencesCommand(p, [0], lambda: None)
    cmd_lock.redo()
    assert p.sentences[0].is_locked is True
    cmd_lock.undo()
    assert p.sentences[0].is_locked is False

    cmd_lang = ChangeSentenceLanguageCommand(p, [0], "ja", lambda: None)
    cmd_lang.redo()
    assert p.sentences[0].language == "ja" and p.sentences[0].is_dirty is True
    cmd_lang.undo()
    assert p.sentences[0].language == "zh"


def _case_undo_redo_restores_dirty_state():
    """对干净句（is_dirty=False）编辑后，undo 准确恢复 is_dirty=False。"""
    from ui.commands import EditTextCommand, EditTimeCommand, BoundaryDragCommand

    p = _make_test_project()
    assert p.sentences[1].is_dirty is False

    cmd_text = EditTextCommand(p, 1, "Modified text", lambda: None)
    cmd_text.redo()
    assert p.sentences[1].is_dirty is True and p.sentences[1].text == "Modified text"
    cmd_text.undo()
    assert p.sentences[1].is_dirty is False and p.sentences[1].text == "Hello world"

    cmd_time = EditTimeCommand(p, 1, 3.6, 5.2, lambda: None)
    cmd_time.redo()
    assert p.sentences[1].is_dirty is True
    assert p.sentences[1].words[0].start_time == 3.6
    assert p.sentences[1].words[-1].end_time == 5.2
    cmd_time.undo()
    assert p.sentences[1].is_dirty is False and p.sentences[1].start_time == 3.5

    cmd_drag = BoundaryDragCommand(p, 1, 3.2, None, lambda: None)
    cmd_drag.redo()
    assert p.sentences[1].is_dirty is True
    assert p.sentences[1].words[0].start_time == 3.2
    cmd_drag.undo()
    assert p.sentences[1].is_dirty is False and p.sentences[1].start_time == 3.5

    cmd_word = EditWordTimeCommand(p, 1, 0, 3.2, 4.0, lambda: None)
    cmd_word.redo()
    assert p.sentences[1].is_dirty is True
    cmd_word.undo()
    assert p.sentences[1].is_dirty is False


def _case_undo_after_reorder_pack() -> None:
    """redo 末尾 sort 重排后，undo 必须按 sid 精准复原（句级/字级/词序）。"""
    from ui.commands import EditTimeCommand

    # A. 句级：编辑时间使句子越过邻句
    p = SubtitleProject(sentences=[
        Sentence(text="第一句", start_time=1.0, end_time=2.0),
        Sentence(text="第二句", start_time=3.0, end_time=4.0),
        Sentence(text="第三句", start_time=5.0, end_time=6.0),
    ])
    cmd = EditTimeCommand(p, 0, 5.5, 6.5, lambda: None)
    cmd.redo()
    assert [s.text for s in p.sentences] == ["第二句", "第三句", "第一句"]
    assert p.sentences[-1].start_time == 5.5 and p.sentences[-1].is_dirty is True
    cmd.undo()
    assert [s.text for s in p.sentences] == ["第一句", "第二句", "第三句"]
    assert p.sentences[0].start_time == 1.0 and p.sentences[0].end_time == 2.0
    assert p.sentences[1].start_time == 3.0 and p.sentences[1].is_dirty is False
    cmd.redo()
    cmd.undo()   # 可重复执行
    assert p.sentences[1].start_time == 3.0

    # B. 字级：唯一 word 移动时句界绑定并按 sid 支持跨句 sort/undo
    p2 = SubtitleProject(sentences=[
        Sentence(text="你好", start_time=1.0, end_time=1.5,
                 words=[WordTimestamp(text="你好", start_time=1.0, end_time=1.5)]),
        Sentence(text="第二句", start_time=3.0, end_time=4.0),
        Sentence(text="第三句", start_time=5.0, end_time=6.0),
    ])
    cmd2 = EditWordTimeCommand(p2, 0, 0, 5.2, 5.7, lambda: None)
    cmd2.redo()
    assert [s.text for s in p2.sentences] == ["第二句", "第三句", "你好"]
    moved = p2.sentences[-1]
    assert abs(moved.start_time - 5.2) < 1e-4
    assert abs(moved.end_time - 5.7) < 1e-4
    assert abs(moved.words[0].start_time - 5.2) < 1e-4
    assert abs(moved.words[0].end_time - 5.7) < 1e-4
    cmd2.undo()
    assert [s.text for s in p2.sentences] == ["你好", "第二句", "第三句"]
    assert p2.sentences[0].start_time == 1.0 and abs(p2.sentences[0].end_time - 1.5) < 1e-4
    assert p2.sentences[0].words[0].start_time == 1.0

    # C. 词序漂移：词被拖过相邻词后句界绑定重排后的外词；undo 全量还原
    p3 = SubtitleProject(sentences=[
        Sentence(text="你好", start_time=1.0, end_time=2.0,
                 words=[
                     WordTimestamp(text="你", start_time=1.0, end_time=1.5),
                     WordTimestamp(text="好", start_time=1.5, end_time=2.0),
                 ]),
    ])
    cmd3 = EditWordTimeCommand(p3, 0, 0, 1.6, 1.9, lambda: None)
    cmd3.redo()
    assert [w.text for w in p3.sentences[0].words] == ["好", "你"]
    assert p3.sentences[0].start_time == 1.5 and p3.sentences[0].end_time == 1.9
    cmd3.undo()
    assert [w.text for w in p3.sentences[0].words] == ["你", "好"]
    assert p3.sentences[0].words[0].start_time == 1.0
    assert p3.sentences[0].end_time == 2.0


def _case_split_undo_pack():
    """拆分撤销链钉样：纯文本兜底可撤回 + sort 夹句不误删。"""
    # R2：光标在边界时拆分退化为纯文本编辑，必须也能撤回
    proj = SubtitleProject(audio_path="x.wav", source_language="zh",
                           sentences=[Sentence(text="你好世界", start_time=0.0, end_time=2.0,
                                               language="zh")])
    stack = QUndoStack()
    cmd = SplitSentenceByCharCommand(proj, 0, 0, lambda: None, new_text="你好，世界")
    stack.push(cmd)
    assert proj.sentences[0].text == "你好，世界" and proj.sentences[0].is_dirty
    stack.undo()
    assert proj.sentences[0].text == "你好世界" and proj.sentences[0].is_dirty is False
    stack.redo()
    assert proj.sentences[0].text == "你好，世界"

    # R3：sort 夹句场景——两半按 sid 各自定位，undo 不得误删夹缝句
    proj2 = SubtitleProject(audio_path="x.wav", source_language="zh", sentences=[
        Sentence(text="长句子A", start_time=0.0, end_time=10.0, language="zh"),
        Sentence(text="夹缝句B", start_time=2.0, end_time=3.0, language="zh"),
    ])
    stack2 = QUndoStack()
    stack2.push(SplitSentenceCommand(proj2, 0, 5.0, lambda: None))
    assert [s.text for s in proj2.sentences] == ["长句", "夹缝句B", "子A"]
    stack2.undo()
    assert sorted(s.text for s in proj2.sentences) == ["夹缝句B", "长句子A"]
    assert any(s.text == "夹缝句B" and s.start_time == 2.0 for s in proj2.sentences)


# ═════════════ 合并句文本拼接规则（_merge_sentences：CJK 无空格/拉丁有空格） ═════════════

from ui.commands import _merge_sentences  # noqa: E402


def _s(text, lang):
    return Sentence(text=text, start_time=0.0, end_time=1.0, language=lang)


def _case_merge_sentences_join_rules():
    """合并句子：CJK 无空格 / 拉丁有空格；空语言按内容判定；yue 属 CJK 族。"""
    assert _merge_sentences([_s("第一句", "zh"), _s("第二句", "zh")]).text == "第一句第二句"
    assert _merge_sentences([_s("Hello world", "en"), _s("again now", "en")]).text == "Hello world again now"
    assert _merge_sentences([_s("Hello world", ""), _s("again now", "")]).text == "Hello world again now"
    assert _merge_sentences([_s("第一句", ""), _s("第二句", "")]).text == "第一句第二句"
    assert _merge_sentences([_s("第一句", "yue"), _s("第二句", "yue")]).text == "第一句第二句"


# ═════════════ 拆句沿用拆前字级时间戳（含空白/音乐符号的句不再丢字级） ═════════════

def _case_split_keeps_word_timestamps():
    from ui.commands.helpers import _split_sentence_at, _split_sentence_at_char

    # ① 光标拆：含空格的英文句（words 拼接 != text）必须保留字级时间戳
    en = Sentence(
        text="Hello world", start_time=1.0, end_time=3.0,
        words=[
            WordTimestamp("Hello", 1.0, 1.8),
            WordTimestamp("world", 1.9, 2.8),
        ],
        language="en",
    )
    left, right = _split_sentence_at_char(en, 5)   # 切在空格后
    assert left.text == "Hello" and left.words[0].start_time == 1.0 and left.end_time == 1.8
    assert right.text == " world" and right.words[0].start_time == 1.9 and right.start_time == 1.9
    assert right.words[0].text == "world"

    # ② 光标拆：含音乐符号的歌词句（words 不含 ♪）保留字级
    song = Sentence(
        text="♪你好♫", start_time=1.0, end_time=3.0,
        words=[
            WordTimestamp("你", 1.0, 1.4),
            WordTimestamp("好", 1.4, 1.8),
        ],
        language="zh",
    )
    l2, r2 = _split_sentence_at_char(song, 2)
    assert l2.text == "♪你" and [w.text for w in l2.words] == ["你"]
    assert r2.text == "好♫" and [w.text for w in r2.words] == ["好"]
    assert l2.end_time == 1.4 and r2.start_time == 1.4

    # ③ 按时间拆：text 保留 words 之外的空白（不再 join words 丢空格）
    t3 = Sentence(
        text="Hello world", start_time=1.0, end_time=3.0,
        words=[
            WordTimestamp("Hello", 1.0, 1.8),
            WordTimestamp("world", 1.9, 2.8),
        ],
        language="en",
    )
    l3, r3 = _split_sentence_at(t3, 1.9)
    assert l3.text == "Hello " and [w.text for w in l3.words] == ["Hello"]
    assert r3.text == "world" and [w.text for w in r3.words] == ["world"]
    assert l3.end_time == 1.8 and r3.start_time == 1.9


# ── 聚合入口 ──────────────────────────────────────────────────────

def _case_undo_commands_pack():
    """命令层 5 合 1：编辑/拖界 / 确认脏锁语言 / 标脏回退 / sort 重排 sid 复原 / 拆分撤销。"""
    _case_edit_and_drag_commands()
    _case_confirm_dirty_lock_language_commands()
    _case_undo_redo_restores_dirty_state()
    _case_undo_after_reorder_pack()
    _case_split_undo_pack()


def _case_merge_text_join_pack():
    """合并句文本拼接：CJK 无空格 / 拉丁有空格 / 空语言按内容判定。"""
    _case_merge_sentences_join_rules()


def _case_split_keeps_timestamps_pack():
    """拆句沿用拆前字级时间戳：空白/音乐符号句不再整句丢字级。"""
    _case_split_keeps_word_timestamps()


def test_undo_commands_all_pack():
    """test_undo_commands_all_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_undo_commands_pack()
    _case_merge_text_join_pack()
    _case_split_keeps_timestamps_pack()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
