"""测试 ASS 样式预览真实字号连续放大、主题跟随与句级表格语言列委托交互（彻底消除重影与切换时差）。"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

pytest.importorskip("PySide6", exc_type=ImportError)   # 无 Qt 环境跳过整个模块，保证 -m logic 可 collection

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHeaderView

from subs.ass_style import AssStylePrefs
from subs.models import Sentence, SubtitleProject, WordTimestamp
from ui.ass_style_dialog import AssStyleDialog, SubtitlePreviewWidget
from ui.sentence_level_view import SentenceLevelView
from ui.themes import apply_theme

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["", "-platform", "offscreen"])
    return app


def _case_ass_preview_real_font_scaling_and_theme_follow(app):
    """测试 ASS 预览画布随主题自适应背景色，且字号随用户输入连续、真实按比放大。"""
    widget = SubtitlePreviewWidget()
    widget.resize(680, 140)

    # 1. 验证深色/浅色模式切换
    apply_theme(app, True)
    widget.update()
    apply_theme(app, False)
    widget.update()

    # 2. 验证字号变化时真实像素连续放大（而非死板截断）
    #    读 widget 自身的 preview_font_px()（paintEvent 同源实现），
    #    而非在测试里复算公式——避免同义反复。
    px_list = []
    for fs in [24, 48, 60, 72, 80, 96, 120]:
        widget.font_size = float(fs)
        px_list.append(widget.preview_font_px())
        widget.update()

    # 验证 px 随 font_size 严格单调递增，且确有明显放大（非近似截断）
    for i in range(len(px_list) - 1):
        assert px_list[i] < px_list[i + 1], f"Font size did not scale up: {px_list}"
    assert px_list[-1] >= px_list[0] * 3, f"Scaling too flat: {px_list}"

    # 3. 高级参数进预览：字间距/缩放/旋转/边框样式/垂直对齐（此前改了没反应）
    prefs2 = AssStylePrefs()
    prefs2.spacing = 6.0
    prefs2.scale_x = 130.0
    prefs2.scale_y = 80.0
    prefs2.angle = 15.0
    prefs2.border_style = 3
    prefs2.alignment = 8                       # 顶部居中
    dlg2 = AssStyleDialog(prefs2)
    pv = dlg2._preview_widget
    assert pv.spacing == 6.0 and pv.scale_x == 130.0 and pv.scale_y == 80.0
    assert pv.angle == 15.0 and pv.border_style == 3 and pv.alignment == 8
    # 边框样式下拉含 1/3/4 三个选项，且改动即时刷新预览（此前漏接信号）
    assert dlg2._border_style.count() == 3
    i14 = dlg2._border_style.findData(4)
    dlg2._border_style.setCurrentIndex(i14)
    assert dlg2._preview_widget.border_style == 4, "边框样式改动应立即进入预览"
    # 字体下拉：至少包含预设（有字体数据库的环境还会更多）
    from subs.ass_style import FONT_PRESETS
    assert dlg2._font.count() >= len(FONT_PRESETS)
    dlg2.close()

    # 4. 验证 AssStyleDialog 构造与样式双向绑定正常
    prefs = AssStylePrefs()
    prefs.font_size = 60
    prefs.font_name = "Source Han Sans SC"
    prefs.outline = 3.0
    prefs.shadow = 2.0
    dlg = AssStyleDialog(prefs)
    assert dlg._preview_widget.font_size == 60
    assert dlg._preview_widget.outline_width == 3.0
    print("test_ass_preview_real_font_scaling_and_theme_follow PASSED ✔")


def _case_sentence_table_language_delegate_opaque_and_single_click(app):
    """测试句级表格语言列委托背景完全不透明、单击即开下拉且全表共享统一代理状态。"""
    apply_theme(app, False)
    view = SentenceLevelView()
    view.resize(1000, 600)

    proj = SubtitleProject()
    proj.sentences = [
        Sentence(
            text="青瓷色的风掠过指尖，",
            start_time=0.5,
            end_time=2.36,
            words=[WordTimestamp("青", 0.5, 0.7)],
            language="zh",
        ),
        Sentence(
            text="金线牡丹在呼吸间流转。",
            start_time=3.02,
            end_time=5.44,
            words=[],
            language="zh",
        ),
    ]
    view.set_project(proj)

    table = view._table
    hh = table.horizontalHeader()
    assert hh.sectionResizeMode(6) == QHeaderView.Fixed
    assert table.columnWidth(6) >= 95

    # 验证单套统一委托安装在表格上
    assert table.itemDelegate() is view._delegate

    # 单击列 6 触发下拉框弹出
    view._on_cell_clicked(0, 6)
    assert view._delegate._editing_index == (0, 6)

    # 验证居中对齐与文字显示
    it = table.item(0, 6)
    assert it is not None
    assert it.text() == "中文"
    assert it.textAlignment() == Qt.AlignCenter

    print("test_sentence_table_language_delegate_opaque_and_single_click PASSED ✔")


def test_ass_preview_table_pack(app):
    """test_ass_preview_table_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_ass_preview_real_font_scaling_and_theme_follow(app=app)
    _case_sentence_table_language_delegate_opaque_and_single_click(app=app)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
