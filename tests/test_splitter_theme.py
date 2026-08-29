# -*- coding: utf-8 -*-
"""tests/test_splitter_theme.py — 主题视觉与通用控件合并套件（分割条可见性 + 切换布局稳定性）

背景：深色模式下窗口分界几乎不可见；纯 QSS 提亮会被平台样式引擎吃掉，
现行方案为 QPainter 自绘握把（GripSplitter/GripSplitterHandle），QSS 降级为兜底。
每条契约 1 个钉样：
1. 主窗三层分割条全部 GripSplitter + 自绘把手 + ≥6px；
2. QSS 兜底规则仍在（把手块内提亮色 + hover/pressed + 横竖向尺寸）；
3. 六组握把配色钉样；
4. QImage 像素级「底色 + 三段握把」真实落笔（横/竖两向）。
"""


from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest  # noqa: E402

pytestmark = pytest.mark.ui


def _case_main_window_splitters_are_grip():
    from PySide6.QtWidgets import QApplication, QSplitter
    QApplication.instance() or QApplication(["test"])

    from ui.main_window import MainWindow
    from ui.widgets import GripSplitter, GripSplitterHandle
    win = MainWindow()
    splitters = win.findChildren(QSplitter)
    assert len(splitters) >= 3                      # 主横向 / 左纵向 / 上横向
    for sp in splitters:
        assert isinstance(sp, GripSplitter), f"应为自绘分割条：{type(sp).__name__}"
        assert sp.handleWidth() >= 6
        assert isinstance(sp.handle(1), GripSplitterHandle)
    win.close()
    win.deleteLater()
    # 注意：deleteLater 后不得 processEvents（队列残余事件 → 悬垂指针段错误）
    print("test_main_window_splitters_are_grip OK ✔")


def _case_theme_qss_splitter_fallback_rules():
    """QSS 规则保留为默认 QSplitter 的兜底（主窗可见性由自绘握把承担）。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(["test"])

    from ui import themes
    themes.apply_theme(app, True)                    # 深色
    dark_qss = app.styleSheet()
    assert "QSplitter::handle" in dark_qss
    assert "#3C3C40" in dark_qss
    # 把手规则块内不得再出现旧的隐形深色 #2C2C2E（其它控件合法使用，必须限定块内）
    handle_block = dark_qss.split("QSplitter::handle {", 1)[1].split("}", 1)[0]
    assert "#3C3C40" in handle_block and "#2C2C2E" not in handle_block
    for rule in ("QSplitter::handle:hover", "QSplitter::handle:pressed",
                 "QSplitter::handle:horizontal", "QSplitter::handle:vertical"):
        assert rule in dark_qss

    themes.apply_theme(app, False)                   # 浅色
    light_qss = app.styleSheet()
    assert "#C9C9CE" in light_qss and "QSplitter::handle:pressed" in light_qss
    themes.apply_theme(app, True)                    # 复位深色
    print("test_theme_qss_splitter_fallback_rules OK ✔")


def _case_grip_colors_pins():
    from PySide6.QtGui import QColor
    from ui.widgets import _grip_colors

    d_base, d_grip = _grip_colors(dark=True, hover=False, pressed=False)
    l_base, l_grip = _grip_colors(dark=False, hover=False, pressed=False)
    # 常态握把必须与把手底色形成明确反差（只靠底色提亮不够）
    assert d_grip.name() != d_base.name() and l_grip.name() != l_base.name()
    assert d_grip.name() == QColor("#9A9AA0").name()
    assert l_grip.name() == QColor("#6E6E73").name()
    assert d_base.name() != l_base.name()
    p_base, p_grip = _grip_colors(dark=True, hover=False, pressed=True)
    assert p_grip.name() == QColor("#FFFFFF").name()
    assert p_base.name() == QColor("#3B63D9").name()
    h_base, h_grip = _grip_colors(dark=True, hover=True, pressed=False)
    assert h_grip.name() == QColor("#4F7DFF").name()
    assert h_base.name() == QColor("#343438").name()
    hl_base, hl_grip = _grip_colors(dark=False, hover=True, pressed=False)
    assert hl_grip.name() == QColor("#3B6BFF").name()
    assert hl_base.name() == QColor("#C6C6CE").name()
    print("test_grip_colors_pins OK ✔")


def _count_color(img, want) -> int:
    n = 0
    for x in range(img.width()):
        for y in range(img.height()):
            if img.pixelColor(x, y).rgb() == want.rgb():
                n += 1
    return n


def _case_grip_paint_pixels_both_orientations():
    """像素级：常态深色 把手底色 + 三段握把 真实落笔（横/竖两向）。

    离屏实测：show() 新顶层窗口会段错误、未 show 的控件 render() 绘制被跳过——
    像素断言只能走「QPainter 直接画 QImage」确定性路径。
    """
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    from ui import widgets
    for orientation, w, h in ((Qt.Orientation.Horizontal, 6, 240),
                              (Qt.Orientation.Vertical, 320, 6)):
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.fill(QColor("#1C1C1E"))                # 模拟深色面板底色
        p = QPainter(img)
        widgets._paint_grip(p, QRect(0, 0, w, h), orientation,
                           dark=True, hover=False, pressed=False)
        p.end()
        n_grip = _count_color(img, QColor("#9A9AA0"))
        assert n_grip >= 30, f"{orientation} 握把像素过少（隐形回归）: {n_grip}"
    # 横向竖杠再核底色铺满率
    img = QImage(6, 240, QImage.Format.Format_RGB32)
    img.fill(QColor("#1C1C1E"))
    p = QPainter(img)
    widgets._paint_grip(p, QRect(0, 0, 6, 240), Qt.Orientation.Horizontal,
                       True, False, False)
    p.end()
    assert _count_color(img, QColor("#2E2E31")) > 6 * 240 * 0.8
    print("test_grip_paint_pixels_both_orientations OK ✔")


# ═════════════ 主题切换布局零跳变 + 外壳 QSS 应用级安装 ═════════════

def _case_first_theme_toggle_does_not_change_layout():
    from PySide6.QtWidgets import QApplication, QPushButton, QToolBar
    app = QApplication.instance() or QApplication(["test"])
    # 注意：不调 setStyle / 不额外 processEvents / 不 show()——三者在离屏 + 前序
    # 测试残留控件（deleteLater 待处理）场景下都会触发全量 repolish/事件派发，
    # 踩悬垂指针段错误（见 test_splitter_theme 同款教训）。sizeHint 几何在未显示
    # 时同样经过 QSS polish，足以钉住布局稳定性契约。

    from ui.themes import apply_theme
    apply_theme(app, False)                      # 模拟 main.py：先装应用级 QSS
    from ui.main_window import MainWindow
    win = MainWindow()

    try:
        tb = win.findChildren(QToolBar)[0]
        mb = win.menuBar()

        def geometry():
            return (
                tb.sizeHint().height(),
                mb.sizeHint().height(),
                tuple(b.sizeHint().width() for b in tb.findChildren(QPushButton)),
            )

        baseline = geometry()
        for i in range(1, 4):                    # 连切 3 次（暗→亮→暗）
            win._on_toggle_theme()
            # 不 processEvents：全量套件下它会派发前序测试遗留的 deferred-delete，
            # 踩悬垂指针段错误；QSS repolish 在 setStyleSheet 内同步完成，
            # sizeHint 无需事件泵即已反映新几何。
            assert geometry() == baseline, f"第 {i} 次切换主题后布局发生跳变"
    finally:
        win.close()
    print("test_first_theme_toggle_does_not_change_layout PASSED ✔")


def _case_apply_theme_targets_application_not_widget():
    """apply_theme 误传 QWidget 时外壳 QSS 仍装应用级，不在窗口级叠加。"""
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication(["test"])

    from ui.themes import apply_theme
    w = QWidget()
    try:
        w.setStyleSheet("")
        apply_theme(w, True)                     # 故意传 widget
        assert w.styleSheet() == "", "外壳 QSS 不得装到窗口级"
        assert len(app.styleSheet()) > 100, "外壳 QSS 应装在 QApplication 级"
    finally:
        w.close()
    print("test_apply_theme_targets_application_not_widget PASSED ✔")


def _case_theme_layout_stability_pack():
    """主题切换布局稳定 2 合 1：任意次切换零跳变 / 外壳 QSS 只装应用级。"""
    _case_first_theme_toggle_does_not_change_layout()
    _case_apply_theme_targets_application_not_widget()


def _case_grip_colors_pack():
    """六组握把配色钉样（并入聚合计数）。"""
    _case_grip_colors_pins()


def test_splitter_pack():
    """test_splitter_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_main_window_splitters_are_grip()
    _case_theme_qss_splitter_fallback_rules()
    _case_grip_paint_pixels_both_orientations()


def test_theme_pack():
    """test_theme_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_theme_layout_stability_pack()
    _case_grip_colors_pack()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
