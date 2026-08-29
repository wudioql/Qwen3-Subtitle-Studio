"""ui.ass_style_dialog.dialog — ASS 文字样式设置弹窗。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CaptionLabel, CheckBox, ColorPickerButton, ComboBox,
    DoubleSpinBox, EditableComboBox, Pivot, PrimaryPushButton,
    PushButton, SpinBox, SubtitleLabel, TitleLabel,
)

from subs.ass_style import ASS_ALIGNMENTS, FONT_PRESETS, AssStylePrefs

from .preview import SubtitlePreviewWidget


# 本机字体列表进程内缓存：QFontDatabase.families() 枚举在 Windows 上约 0.1~0.5s，
# 且结果进程内不变——只在首次打开样式弹窗时枚举一次，后续复用。
_FONT_FAMILIES_CACHE: list[str] | None = None


def _system_font_families() -> list[str]:
    """返回本机全部字体家族名（不含 `.` 开头系统内部字体），进程内缓存一次。"""
    global _FONT_FAMILIES_CACHE
    if _FONT_FAMILIES_CACHE is None:
        try:
            from PySide6.QtGui import QFontDatabase
            _FONT_FAMILIES_CACHE = [
                fam for fam in QFontDatabase.families()
                if not fam.startswith(".")
            ]
        except Exception:  # noqa: BLE001 — 无字体数据库环境（离屏测试等）退回预设
            _FONT_FAMILIES_CACHE = []
    return _FONT_FAMILIES_CACHE


class AssStyleDialog(QDialog):
    """分组编辑 ASS v4+ 样式；只有按「保存样式」才向外提交。"""

    style_saved = Signal(object)

    def __init__(self, style: AssStylePrefs, parent=None):
        super().__init__(parent)
        self.setObjectName("ass_style_dialog")
        self.setWindowTitle("ASS 文字样式")
        self.resize(720, 680)
        self.setMinimumSize(640, 580)
        # 防止取消时修改调用方持有的对象
        self._initial = AssStylePrefs.from_dict(style.to_dict())
        self._result = AssStylePrefs.from_dict(style.to_dict())

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        root.addWidget(TitleLabel("ASS 文字样式", self))
        desc = CaptionLabel(
            "句级 ASS、逐字 ASS 与 Aegisub k-tag ASS 共用此样式。颜色使用常见的 RGB 表示，导出时自动转换为 ASS 的 ABGR。",
            self,
        )
        desc.setWordWrap(True)
        root.addWidget(desc)

        # 实时渲染预览（跟随明暗主题，真实字号按比放大）
        root.addWidget(SubtitleLabel("实时渲染预览", self))
        self._preview_widget = SubtitlePreviewWidget(self)
        root.addWidget(self._preview_widget)
        self._preview_hint = CaptionLabel("", self)
        self._preview_hint.setAlignment(Qt.AlignCenter)
        root.addWidget(self._preview_hint)

        self._pivot = Pivot(self)
        self._stack = QStackedWidget(self)
        root.addWidget(self._pivot)
        root.addWidget(self._stack, 1)

        self._build_basic_page()
        self._build_color_page()
        self._build_layout_page()
        self._build_advanced_page()
        self._stack.currentChanged.connect(self._sync_pivot)
        self._pivot.setCurrentItem("basic")

        buttons = QHBoxLayout()
        self._btn_reset = PushButton(self)
        self._btn_reset.setText("恢复默认")
        self._btn_reset.clicked.connect(self._reset)
        buttons.addWidget(self._btn_reset)
        buttons.addStretch(1)
        self._btn_cancel = PushButton(self)
        self._btn_cancel.setText("取消")
        self._btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self._btn_cancel)
        self._btn_save = PrimaryPushButton(self)
        self._btn_save.setText("保存样式")
        self._btn_save.clicked.connect(self._save_and_accept)
        buttons.addWidget(self._btn_save)
        root.addLayout(buttons)

        self._load_style(self._result)
        self._connect_preview_signals()
        self._update_preview()

    @property
    def current_style(self) -> AssStylePrefs:
        """返回弹窗当前结果的副本。取消时仍是初始值。"""
        return AssStylePrefs.from_dict(self._result.to_dict())

    # ── 页面 ──────────────────────────────────────────
    def _add_page(self, key: str, title: str, page: QWidget) -> None:
        page.setObjectName(f"ass_style_{key}_page")
        self._stack.addWidget(page)
        self._pivot.addItem(
            routeKey=key,
            text=title,
            onClick=lambda _checked=False, p=page: self._stack.setCurrentWidget(p),
        )

    def _form_page(self) -> tuple[QWidget, QFormLayout]:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(12, 12, 12, 12)
        f.setSpacing(10)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return w, f

    def _build_basic_page(self) -> None:
        w, f = self._form_page()
        self._font_preset = ComboBox(w)
        self._font_preset.addItem("常用字体预设…", userData="")
        for family in FONT_PRESETS:
            self._font_preset.addItem(family, userData=family)
        self._font_preset.currentIndexChanged.connect(self._apply_font_preset)
        f.addRow("字体预设", self._font_preset)

        self._font = EditableComboBox(w)
        self._font.setPlaceholderText("输入或选择字体名称（已列出本机全部字体）")
        _sys_families = _system_font_families()   # 进程内缓存，首次枚举后复用
        seen: set[str] = set()
        for family in list(FONT_PRESETS) + _sys_families:
            if family not in seen:
                seen.add(family)
                self._font.addItem(family)
        f.addRow("字体名称", self._font)

        self._font_size = SpinBox(w)
        self._font_size.setRange(8, 144)
        self._font_size.setValue(48)
        f.addRow("字体字号", self._font_size)

        style_row = QHBoxLayout()
        self._bold = CheckBox("粗体", w)
        self._italic = CheckBox("斜体", w)
        self._underline = CheckBox("下划线", w)
        self._strikeout = CheckBox("删除线", w)
        for cb in (self._bold, self._italic, self._underline, self._strikeout):
            style_row.addWidget(cb)
        style_row.addStretch(1)
        f.addRow("字形效果", style_row)
        self._add_page("basic", "基础", w)

    def _build_color_page(self) -> None:
        w, f = self._form_page()
        self._primary = ColorPickerButton(QColor("#FFFFFF"), "主颜色", w)
        self._secondary = ColorPickerButton(QColor("#00FFFF"), "卡拉OK高亮色", w)
        self._outline_color = ColorPickerButton(QColor("#000000"), "描边颜色", w)
        self._back_color = ColorPickerButton(QColor("#000000"), "阴影/背景色", w)

        f.addRow("主体颜色", self._color_with_alpha_row(self._primary, "主文字色彩"))
        f.addRow("卡拉OK色", self._color_with_alpha_row(self._secondary, "k-tag 高亮颜色"))
        f.addRow("描边颜色", self._color_with_alpha_row(self._outline_color, "文字边缘描边色彩"))
        f.addRow("阴影颜色", self._color_with_alpha_row(self._back_color, "文字投影色彩"))

        self._primary_alpha = SpinBox(w)
        self._primary_alpha.setRange(0, 255)
        self._primary_alpha.setValue(0)
        self._outline_alpha = SpinBox(w)
        self._outline_alpha.setRange(0, 255)
        self._outline_alpha.setValue(0)
        self._back_alpha = SpinBox(w)
        self._back_alpha.setRange(0, 255)
        self._back_alpha.setValue(80)

        alpha_grid = QGridLayout()
        alpha_grid.setHorizontalSpacing(10)
        alpha_grid.setVerticalSpacing(6)
        alpha_grid.addWidget(CaptionLabel("主体透明度", w), 0, 0)
        alpha_grid.addWidget(self._primary_alpha, 0, 1)
        alpha_grid.addWidget(CaptionLabel("描边透明度", w), 1, 0)
        alpha_grid.addWidget(self._outline_alpha, 1, 1)
        alpha_grid.addWidget(CaptionLabel("阴影透明度", w), 1, 2)
        alpha_grid.addWidget(self._back_alpha, 1, 3)
        f.addRow("透明度分量", alpha_grid)
        f.addRow(_hint("0 = 完全不透明，255 = 完全透明。ASS 内部用 00-FF 表示透明度。"))
        self._add_page("colors", "颜色与透明度", w)

    def _color_with_alpha_row(self, picker: ColorPickerButton, tip: str) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        picker.setToolTip(tip)
        lay.addWidget(picker)
        label = CaptionLabel(picker.color.name(QColor.HexRgb).upper(), row)
        picker.colorChanged.connect(
            lambda color, target=label: target.setText(color.name(QColor.HexRgb).upper())
        )
        lay.addWidget(label)
        lay.addStretch(1)
        return row

    def _build_layout_page(self) -> None:
        w, f = self._form_page()
        self._alignment = ComboBox(w)
        for val, name in ASS_ALIGNMENTS.items():
            self._alignment.addItem(name, userData=val)
        f.addRow("对齐方式", self._alignment)

        self._margin_l = SpinBox(w)
        self._margin_r = SpinBox(w)
        self._margin_v = SpinBox(w)
        for sp in (self._margin_l, self._margin_r, self._margin_v):
            sp.setRange(0, 500)
        self._margin_l.setValue(30)
        self._margin_r.setValue(30)
        self._margin_v.setValue(30)

        m_row = QHBoxLayout()
        m_row.addWidget(CaptionLabel("左边距", w))
        m_row.addWidget(self._margin_l)
        m_row.addWidget(CaptionLabel("右边距", w))
        m_row.addWidget(self._margin_r)
        m_row.addWidget(CaptionLabel("垂直边距", w))
        m_row.addWidget(self._margin_v)
        m_row.addStretch(1)
        f.addRow("边距 (px)", m_row)

        self._resolution_preset = ComboBox(w)
        self._resolution_preset.addItem("常用分辨率…", userData=None)
        self._resolution_preset.addItem("1080p (1920 × 1080)", userData=(1920, 1080))
        self._resolution_preset.addItem("4K (3840 × 2160)", userData=(3840, 2160))
        self._resolution_preset.addItem("720p (1280 × 720)", userData=(1280, 720))
        self._resolution_preset.currentIndexChanged.connect(self._apply_resolution_preset)

        res_row = QHBoxLayout()
        self._res_x = SpinBox(w)
        self._res_y = SpinBox(w)
        self._res_x.setRange(320, 7680)
        self._res_y.setRange(240, 4320)
        self._res_x.setValue(1920)
        self._res_y.setValue(1080)
        res_row.addWidget(self._res_x)
        res_row.addWidget(CaptionLabel("×", w))
        res_row.addWidget(self._res_y)
        res_row.addWidget(self._resolution_preset)
        res_row.addStretch(1)
        f.addRow("渲染基准分辨率", res_row)
        f.addRow(_hint("PlayResX / PlayResY：ASS 坐标系统的基准分辨率，决定字号与边距的物理比例。"))
        self._add_page("layout", "排版与边距", w)

    def _build_advanced_page(self) -> None:
        w, f = self._form_page()
        self._border_style = ComboBox(w)
        self._border_style.addItem("描边 + 阴影（标准，最常用）", userData=1)
        self._border_style.addItem("不透明底框（每行文字垫一块底色矩形）", userData=3)
        self._border_style.addItem("整段底框（libass 扩展；部分播放器不支持）", userData=4)
        self._border_style.setToolTip("ASS 标准只有 1 和 3 两种；4 是 libass 播放器的扩展样式")
        f.addRow("边框样式", self._border_style)

        self._outline = DoubleSpinBox(w)
        self._outline.setRange(0.0, 10.0)
        self._outline.setSingleStep(0.5)
        self._outline.setValue(2.0)
        f.addRow("描边宽度", self._outline)

        self._shadow = DoubleSpinBox(w)
        self._shadow.setRange(0.0, 10.0)
        self._shadow.setSingleStep(0.5)
        self._shadow.setValue(2.0)
        f.addRow("阴影距离", self._shadow)

        self._scale_x = SpinBox(w)
        self._scale_y = SpinBox(w)
        self._scale_x.setRange(10, 400)
        self._scale_y.setRange(10, 400)
        self._scale_x.setValue(100)
        self._scale_y.setValue(100)
        scale_row = QHBoxLayout()
        scale_row.addWidget(CaptionLabel("水平缩放 %", w))
        scale_row.addWidget(self._scale_x)
        scale_row.addWidget(CaptionLabel("垂直缩放 %", w))
        scale_row.addWidget(self._scale_y)
        scale_row.addStretch(1)
        f.addRow("字形缩放", scale_row)

        self._spacing = DoubleSpinBox(w)
        self._spacing.setRange(-20.0, 50.0)
        self._spacing.setValue(0.0)
        f.addRow("字间距 (px)", self._spacing)

        self._angle = DoubleSpinBox(w)
        self._angle.setRange(-180.0, 180.0)
        self._angle.setValue(0.0)
        f.addRow("旋转角度 (°)", self._angle)

        self._encoding = ComboBox(w)
        self._encoding.addItem("1 · 默认 / UTF-8", userData=1)
        self._encoding.addItem("134 · 简体中文 (GB2312)", userData=134)
        self._encoding.addItem("128 · 日文 (Shift-JIS)", userData=128)
        self._encoding.addItem("129 · 韩文 (Hangul)", userData=129)
        f.addRow("字符编码", self._encoding)
        self._add_page("advanced", "高级特效", w)

    # ── 数据读写 ──────────────────────────────────────
    def _load_style(self, st: AssStylePrefs) -> None:
        self._font.setText(st.font_name)
        self._font_size.setValue(int(st.font_size))
        self._bold.setChecked(st.bold)
        self._italic.setChecked(st.italic)
        self._underline.setChecked(st.underline)
        self._strikeout.setChecked(st.strikeout)

        self._primary.setColor(QColor(st.primary_color))
        self._secondary.setColor(QColor(st.secondary_color))
        self._outline_color.setColor(QColor(st.outline_color))
        self._back_color.setColor(QColor(st.back_color))

        self._primary_alpha.setValue(int(getattr(st, "primary_alpha", 0)))
        self._outline_alpha.setValue(int(getattr(st, "outline_alpha", 0)))
        self._back_alpha.setValue(int(getattr(st, "back_alpha", 80)))

        idx = self._alignment.findData(st.alignment)
        if idx >= 0:
            self._alignment.setCurrentIndex(idx)
        self._margin_l.setValue(int(st.margin_l))
        self._margin_r.setValue(int(st.margin_r))
        self._margin_v.setValue(int(st.margin_v))
        self._res_x.setValue(int(st.play_res_x))
        self._res_y.setValue(int(st.play_res_y))

        idx = self._border_style.findData(st.border_style)
        if idx >= 0:
            self._border_style.setCurrentIndex(idx)
        self._outline.setValue(float(st.outline))
        self._shadow.setValue(float(st.shadow))
        self._scale_x.setValue(int(st.scale_x))
        self._scale_y.setValue(int(st.scale_y))
        self._spacing.setValue(float(st.spacing))
        self._angle.setValue(float(st.angle))
        idx = self._encoding.findData(st.encoding)
        if idx >= 0:
            self._encoding.setCurrentIndex(idx)

    def _read_style(self) -> AssStylePrefs:
        st = AssStylePrefs()
        st.font_name = self._font.text().strip() or "Source Han Sans SC"
        st.font_size = float(self._font_size.value())
        st.bold = self._bold.isChecked()
        st.italic = self._italic.isChecked()
        st.underline = self._underline.isChecked()
        st.strikeout = self._strikeout.isChecked()

        st.primary_color = self._primary.color.name(QColor.HexRgb).upper()
        st.secondary_color = self._secondary.color.name(QColor.HexRgb).upper()
        st.outline_color = self._outline_color.color.name(QColor.HexRgb).upper()
        st.back_color = self._back_color.color.name(QColor.HexRgb).upper()

        st.primary_alpha = self._primary_alpha.value()
        st.outline_alpha = self._outline_alpha.value()
        st.back_alpha = self._back_alpha.value()

        st.alignment = int(self._alignment.currentData() or 2)
        st.margin_l = int(self._margin_l.value())
        st.margin_r = int(self._margin_r.value())
        st.margin_v = int(self._margin_v.value())
        st.play_res_x = int(self._res_x.value())
        st.play_res_y = int(self._res_y.value())

        st.border_style = int(self._border_style.currentData() or 1)
        st.outline = float(self._outline.value())
        st.shadow = float(self._shadow.value())
        st.scale_x = float(self._scale_x.value())
        st.scale_y = float(self._scale_y.value())
        st.spacing = float(self._spacing.value())
        st.angle = float(self._angle.value())
        st.encoding = int(self._encoding.currentData() or 1)
        return st

    def _apply_font_preset(self, _index: int) -> None:
        val = self._font_preset.currentData()
        if val:
            self._font.setText(val)

    def _apply_resolution_preset(self, _index: int) -> None:
        value = self._resolution_preset.currentData()
        if value:
            self._res_x.setValue(int(value[0]))
            self._res_y.setValue(int(value[1]))

    # ── 预览 / 按钮 ───────────────────────────────────
    def _connect_preview_signals(self) -> None:
        for w in (
            self._font, self._font_size, self._bold, self._italic,
            self._underline, self._strikeout, self._primary,
            self._secondary, self._outline_color, self._back_color,
            self._primary_alpha, self._outline_alpha, self._back_alpha,
            self._alignment, self._outline, self._shadow, self._scale_x,
            self._scale_y, self._spacing, self._angle,
            self._border_style,   # 边框样式此前漏接：改后预览不刷新，须等其他参数触发
        ):
            if isinstance(w, (ComboBox, EditableComboBox)):
                signal = w.currentIndexChanged
            elif isinstance(w, (SpinBox, DoubleSpinBox)):
                signal = w.valueChanged
            elif isinstance(w, CheckBox):
                signal = w.toggled
            elif isinstance(w, ColorPickerButton):
                signal = w.colorChanged
            else:
                continue
            signal.connect(self._update_preview)
        if hasattr(self._font, "textChanged"):
            self._font.textChanged.connect(self._update_preview)

    def _update_preview(self, *_args) -> None:
        st = self._read_style()
        self._preview_widget.font_name = st.font_name
        self._preview_widget.font_size = st.font_size
        self._preview_widget.bold = st.bold
        self._preview_widget.italic = st.italic
        self._preview_widget.underline = st.underline
        self._preview_widget.strikeout = st.strikeout
        self._preview_widget.alignment = st.alignment

        # 主颜色与透明度
        prim_c = QColor(st.primary_color)
        prim_c.setAlpha(max(0, 255 - int(st.primary_alpha)))
        self._preview_widget.primary_color = prim_c

        # 描边
        out_c = QColor(st.outline_color)
        out_c.setAlpha(max(0, 255 - int(st.outline_alpha)))
        self._preview_widget.outline_color = out_c
        self._preview_widget.outline_width = float(st.outline)

        # 阴影
        shad_c = QColor(st.back_color)
        shad_c.setAlpha(max(0, 255 - int(st.back_alpha)))
        self._preview_widget.shadow_color = shad_c
        self._preview_widget.shadow_offset = float(st.shadow)

        # 高级参数：缩放/字间距/旋转/边框样式（此前未传入预览，改了没反应）
        self._preview_widget.scale_x = float(st.scale_x)
        self._preview_widget.scale_y = float(st.scale_y)
        self._preview_widget.spacing = float(st.spacing)
        self._preview_widget.angle = float(st.angle)
        self._preview_widget.border_style = int(st.border_style)

        self._preview_widget.update()

        self._preview_hint.setText(
            f"{st.font_name} · {st.font_size:g}px · 描边 {st.outline:g} · 阴影 {st.shadow:g} · {ASS_ALIGNMENTS.get(st.alignment, st.alignment)}"
        )

    def _sync_pivot(self, index: int) -> None:
        if 0 <= index < self._stack.count():
            key = self._stack.widget(index).objectName().removeprefix("ass_style_").removesuffix("_page")
            self._pivot.setCurrentItem(key)

    def _reset(self) -> None:
        self._load_style(AssStylePrefs())
        self._update_preview()

    def _save_and_accept(self) -> None:
        self._result = self._read_style()
        self.style_saved.emit(self.current_style)
        self.accept()


def _hint(text: str) -> QLabel:
    lbl = CaptionLabel(text)
    lbl.setWordWrap(True)
    return lbl


__all__ = ["AssStyleDialog"]
