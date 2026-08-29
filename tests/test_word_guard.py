# -*- coding: utf-8 -*-
"""tests/test_word_guard.py — 字级内容一致性守门合并套件

每条契约留 1 个钉样：
1. L0：words_content_match 判定矩阵（标点豁免 / mora 词 / 漂移 / 空态豁免）；
2. L1c：导出预检行号收集（word_level_mismatch_rows，纯函数）；
3. L2a：attach 比例回退必留 WARNING（不再静默）+ 守门闭环可检出；
4. GUI 闭环：字级页漂移警示条 + 单元格编辑只发信号不写模型（撤销链正确性）
   + EditWordTimeCommand undo/redo 恢复词时间并绑定句级外边界。
"""

import logging

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest  # noqa: E402

pytestmark = pytest.mark.ui

from core.text_utils import (  # noqa: E402
    attach_words_to_sentences,
    merge_punct_into_words,
    words_content_match,
)
from subs.models import Sentence, SubtitleProject, WordTimestamp  # noqa: E402


def _words_of(text: str):
    """按 extract 语义构造与 text 一致的 words（模拟健康对齐产物，含标点回填）。"""
    from core.text_utils import extract_pure_words
    raw = [
        WordTimestamp(text=w, start_time=i * 0.2, end_time=i * 0.2 + 0.15)
        for i, w in enumerate(extract_pure_words(text))
    ]
    return merge_punct_into_words(text, raw)


def _case_words_content_match_matrix():
    # 健康：中文含标点（标点词豁免比对）
    s = Sentence(text="风掠过指尖。", start_time=0.0, end_time=2.0)
    s.words = _words_of(s.text)
    assert words_content_match(s) is True

    # 健康：日语 mora 词（ヒョ 是整拍一词）+ 新标点符号豁免
    s2 = Sentence(text="キム・テヒョン〜", start_time=0.0, end_time=2.0)
    s2.words = _words_of(s2.text)
    assert words_content_match(s2) is True

    # 漂移：改了文本没重对齐（旧 words 保留）
    s3 = Sentence(text="风掠过指尖。", start_time=0.0, end_time=2.0)
    s3.words = _words_of(s3.text)
    s3.text = "月光落进湖里。"
    assert words_content_match(s3) is False

    # 漂移：词序列内部错序/缺词
    s4 = Sentence(text="风掠过指尖。", start_time=0.0, end_time=2.0)
    s4.words = _words_of(s4.text)
    s4.words = [w for w in s4.words if w.text != "掠"]
    assert words_content_match(s4) is False

    # 无字级：已声明的空态，不算漂移
    s5 = Sentence(text="暂无字级。", start_time=0.0, end_time=1.0)
    assert words_content_match(s5) is True
    print("test_words_content_match_matrix OK ✔")


def _case_export_preflight_mismatch_rows():
    from ui.export_controller import word_level_mismatch_rows

    ok = Sentence(text="第一句。", start_time=0.0, end_time=1.0)
    ok.words = _words_of(ok.text)
    drifted = Sentence(text="第二句改了。", start_time=2.0, end_time=3.0)
    drifted.words = _words_of("第二句原文。")
    no_words = Sentence(text="第三句。", start_time=4.0, end_time=5.0)

    proj = SubtitleProject(sentences=[ok, drifted, no_words])
    assert word_level_mismatch_rows(proj) == [1]
    assert word_level_mismatch_rows(None) == []
    # 全部健康 → 空（不打扰导出）
    proj_ok = SubtitleProject(sentences=[ok, no_words])
    assert word_level_mismatch_rows(proj_ok) == []


def _case_proportional_fallback_logs_warning():
    # 构造计数必然失配：句需 6+3=9 纯词，却只给 4 词
    sents = [
        Sentence(text="风掠过指尖的甲。", start_time=0.0, end_time=3.0),
        Sentence(text="月光落在湖面。", start_time=4.0, end_time=6.0),
    ]
    ghost = [
        WordTimestamp(text=f"w{i}", start_time=i * 1.0, end_time=i * 0.5 + i * 1.0)
        for i in range(4)
    ]

    records = []
    handler = logging.Handler()
    handler.emit = lambda rec: records.append(rec)  # noqa: E731
    tu_logger = logging.getLogger("core.text_utils")
    old_level = tu_logger.level
    tu_logger.addHandler(handler)
    tu_logger.setLevel(logging.WARNING)
    try:
        attach_words_to_sentences(sents, ghost)
    finally:
        tu_logger.removeHandler(handler)
        tu_logger.setLevel(old_level)

    assert any("比例回退" in rec.getMessage() for rec in records), (
        f"未捕获比例回退 WARNING: {[r.getMessage() for r in records]}"
    )
    # 回退后 drift 可被 L0 检出（守门闭环）
    assert not words_content_match(sents[1])  # 末句被摊到 w3，与文本不符


def _mini_proj() -> SubtitleProject:
    words = [
        WordTimestamp(text="青", start_time=0.0, end_time=0.2, language="zh"),
        WordTimestamp(text="春", start_time=0.2, end_time=0.4, language="zh"),
    ]
    return SubtitleProject(
        audio_path="x.wav", source_language="zh",
        sentences=[Sentence(text="青春", start_time=0.0, end_time=0.4,
                            words=words, language="zh")],
    )


def _case_view_banner_edit_emit_only_and_undo_chain():
    """GUI 闭环：警示条 + emit-only（R1）+ EditWordTimeCommand 撤销链。"""
    from PySide6.QtGui import QUndoStack
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    from ui.commands import EditWordTimeCommand
    from ui.word_level_view import WordLevelView

    view = WordLevelView()
    healthy = Sentence(text="风掠过指尖。", start_time=0.0, end_time=2.0)
    healthy.words = _words_of(healthy.text)
    drifted = Sentence(text="月光落进湖里。", start_time=3.0, end_time=5.0)
    drifted.words = _words_of("旧句子文本。")
    proj = SubtitleProject(sentences=[healthy, drifted])
    view.set_project(proj)

    # ── L1a：漂移警示条 ─────────────────────────────────────────
    view.show_sentence(0)
    assert "⚠" not in view._lbl_status.text()
    view.show_sentence(1)
    assert "⚠" in view._lbl_status.text() and "不一致" in view._lbl_status.text()
    assert "#d4380d" in view._lbl_status.styleSheet()
    view.show_sentence(0)
    assert "⚠" not in view._lbl_status.text()

    # ── R1：单元格编辑 **只发信号、绝不写模型** ──────────────────
    view2 = WordLevelView()
    project = _mini_proj()
    view2.set_project(project)
    view2.show_sentence(0)
    sent = project.sentences[0]
    before = (sent.words[0].start_time, sent.words[0].end_time)
    emitted: list = []
    view2.word_time_edited.connect(lambda *a: emitted.append(a))
    item = view2._table.item(0, 2)                     # 「起始」列
    item.setText("00:00.150")                          # 手动改起始 0.000 → 0.150
    assert emitted and abs(emitted[-1][2] - 0.150) < 1e-6
    # 关键钉样：模型未被视图染指（旧实现在这里已经把 start_time 改成 0.150）
    assert (sent.words[0].start_time, sent.words[0].end_time) == before
    assert not sent.is_dirty

    # ── 命令侧：undo/redo 闭环 ───────────────────────────────────
    stack = QUndoStack()
    stack.push(EditWordTimeCommand(project, 0, 1, 0.25, 0.45, lambda: None))
    assert (sent.words[1].start_time, sent.words[1].end_time) == (0.25, 0.45)
    # 现役合同：句界与最外层 word 绑定；末字 end 改动同步句尾。
    assert sent.end_time == 0.45 and sent.is_dirty
    stack.undo()
    assert (sent.words[1].start_time, sent.words[1].end_time) == (0.2, 0.4)
    assert sent.end_time == 0.4 and not sent.is_dirty
    stack.redo()
    assert (sent.words[1].start_time, sent.words[1].end_time) == (0.25, 0.45)
    assert sent.end_time == 0.45 and sent.is_dirty

    view.close()
    view.deleteLater()
    view2.close()
    view2.deleteLater()
    print("test_view_banner_edit_emit_only_and_undo_chain OK ✔")


# ── 聚合入口 ──────────────────────────────────────────────────────

def _case_word_guard_logic_pack():
    """守门纯逻辑 3 合 1：内容一致性矩阵 / 导出预检行号 / 比例回退 WARNING。"""
    _case_words_content_match_matrix()
    _case_export_preflight_mismatch_rows()
    _case_proportional_fallback_logs_warning()


def _case_word_guard_view_pack():
    """GUI 闭环：字级页警示条 + 单元格 emit-only 撤销链。"""
    _case_view_banner_edit_emit_only_and_undo_chain()


def test_word_guard_pack():
    """test_word_guard_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_word_guard_logic_pack()
    _case_word_guard_view_pack()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
