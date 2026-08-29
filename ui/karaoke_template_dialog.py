"""ui.karaoke_template_dialog — Aegisub 卡拉OK效果编辑弹窗

操作逻辑对齐 AssStyleDialog：左侧效果列表（复选框，最多单选，也可全不选）+
右侧当前效果的参数表单 + 生成结果实时预览；只有「保存效果」才向外提交，
持久化到 prefs.karaoke_template。

内置常用效果库（弹跳放大/高亮变色/描边发光/逐字浮现/上浮入场/整句淡入），
均可修改参数或删除，「恢复内置效果库」可整体还原；每条效果可切换
「参数编辑 / 代码编辑」，参数生成的代码可一键导入代码编辑继续手改。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ColorPickerButton,
    ComboBox,
    DoubleSpinBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    SubtitleLabel,
    TitleLabel,
)

from subs.karaoke_template import (
    KaraokeTemplate,
    KaraokeTemplatePrefs,
    TEMPLATE_CLASSES,
    TEMPLATE_MODIFIERS,
    default_karaoke_templates,
)


class KaraokeTemplateDialog(QDialog):
    """编辑 k-tag ASS 的单一卡拉OK模板效果（也允许全不选）。"""

    def __init__(self, prefs: KaraokeTemplatePrefs, parent=None):
        super().__init__(parent)
        self.setObjectName("karaoke_template_dialog")
        self.setWindowTitle("Aegisub 卡拉OK效果")
        self.resize(880, 720)
        self.setMinimumSize(780, 620)

        # 深拷贝：取消不影响调用方
        src = (
            prefs.to_dict() if prefs and prefs.templates else default_karaoke_templates().to_dict()
        )
        self._templates: list[KaraokeTemplate] = KaraokeTemplatePrefs.from_dict(src).templates
        self._current: int = 0
        self._loading = False  # 防止载入表单时触发回写

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(10)
        root.addWidget(TitleLabel("Aegisub 卡拉OK效果", self))
        desc = CaptionLabel(
            "左侧最多勾选一个效果；勾选其它项会自动取消先前选择，也可全部不选。"
            "点击名称可在右侧修改该效果的参数。播放器会忽略这些效果定义行，"
            "需在 Aegisub 中运行「自动化 → 应用卡拉OK模板」后生效。",
            self,
        )
        desc.setWordWrap(True)
        root.addWidget(desc)

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        # ── 左：效果列表（复选框 = 启用；当前行 = 编辑对象） ──────
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(CaptionLabel("效果列表（单选；再次取消可全不选）", self))
        self._list = QListWidget(self)
        self._list.setMaximumWidth(220)
        self._list.currentRowChanged.connect(self._on_select)
        self._list.itemChanged.connect(self._on_item_check_changed)
        left.addWidget(self._list, 1)

        row1 = QHBoxLayout()
        self._btn_add = PushButton(self)
        self._btn_add.setText("新增效果")
        self._btn_del = PushButton(self)
        self._btn_del.setText("删除所选")
        self._btn_add.clicked.connect(self._on_add)
        self._btn_del.clicked.connect(self._on_del)
        row1.addWidget(self._btn_add)
        row1.addWidget(self._btn_del)
        left.addLayout(row1)
        row2 = QHBoxLayout()
        self._btn_up = PushButton(self)
        self._btn_up.setText("上移")
        self._btn_down = PushButton(self)
        self._btn_down.setText("下移")
        self._btn_up.clicked.connect(lambda: self._move(-1))
        self._btn_down.clicked.connect(lambda: self._move(1))
        row2.addWidget(self._btn_up)
        row2.addWidget(self._btn_down)
        left.addLayout(row2)
        body.addLayout(left)

        # ── 右：当前效果编辑区 ─────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(8)
        body.addLayout(right, 1)

        form_host = QWidget(self)
        f = QFormLayout(form_host)
        f.setContentsMargins(0, 0, 0, 0)
        f.setHorizontalSpacing(16)
        f.setVerticalSpacing(9)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._name_edit = LineEdit(self)
        f.addRow("效果名称", self._name_edit)

        self._mode_combo = ComboBox(self)
        self._mode_combo.addItem("参数编辑（表单生成代码）", userData="form")
        self._mode_combo.addItem("代码编辑（直接写效果定义行）", userData="raw")
        f.addRow("编辑方式", self._mode_combo)

        self._class_combo = ComboBox(self)
        for key, zh in TEMPLATE_CLASSES.items():
            self._class_combo.addItem(zh, userData=key)
        f.addRow("作用范围", self._class_combo)

        mod_row = QHBoxLayout()
        self._mod_cbs: dict[str, CheckBox] = {}
        _MOD_SHORT = {
            "all": "全部样式",
            "noblank": "跳过空拍",
            "keeptags": "保留原标签",
            "notext": "隐藏原文字",
        }
        for key in TEMPLATE_MODIFIERS:
            cb = CheckBox(self)
            cb.setText(_MOD_SHORT.get(key, key))
            cb.setToolTip(TEMPLATE_MODIFIERS[key])
            self._mod_cbs[key] = cb
            mod_row.addWidget(cb)
        mod_row.addStretch(1)
        f.addRow("附加选项", mod_row)

        self._layer_spin = SpinBox(self)
        self._layer_spin.setRange(0, 99)
        self._layer_spin.setToolTip(
            "Aegisub 应用模板后生成行的绘制层级；数字越大越靠上"
        )
        f.addRow("绘制层级", self._layer_spin)

        self._anchor_combo = ComboBox(self)
        for n, zh in (
            (7, "左上"),
            (8, "顶部居中"),
            (9, "右上"),
            (4, "左中"),
            (5, "正中"),
            (6, "右中"),
            (1, "左下"),
            (2, "底部居中"),
            (3, "右下"),
        ):
            self._anchor_combo.addItem(zh, userData=n)
        self._anchor_combo.setToolTip("动画的基准点：正中可让放大等动画围绕字的中心对称进行")
        f.addRow("动画基准点", self._anchor_combo)

        self._pos_cb = CheckBox(self)
        self._pos_cb.setText("锁定原位（推荐；关闭后各字会跑到样式默认位置叠在一起）")
        f.addRow("", self._pos_cb)

        fade_row = QHBoxLayout()
        self._fad_in = SpinBox(self)
        self._fad_in.setRange(0, 2000)
        self._fad_in.setSuffix(" 毫秒")
        self._fad_out = SpinBox(self)
        self._fad_out.setRange(0, 2000)
        self._fad_out.setSuffix(" 毫秒")
        fade_row.addWidget(BodyLabel("淡入", self))
        fade_row.addWidget(self._fad_in)
        fade_row.addWidget(BodyLabel("淡出", self))
        fade_row.addWidget(self._fad_out)
        fade_row.addStretch(1)
        f.addRow("渐显渐隐", fade_row)

        scale_row = QHBoxLayout()
        self._scale_cb = CheckBox(self)
        self._scale_cb.setText("弹跳放大")
        self._scale_spin = SpinBox(self)
        self._scale_spin.setRange(100, 300)
        self._scale_spin.setSuffix(" %")
        self._scale_spin.setToolTip("唱到该字时放大到此百分比，随后回落到 100%")
        scale_row.addWidget(self._scale_cb)
        scale_row.addWidget(self._scale_spin)
        scale_row.addStretch(1)
        f.addRow("效果开关", scale_row)

        color_row = QHBoxLayout()
        self._color_cb = CheckBox(self)
        self._color_cb.setText("高亮变色（原色 → 高亮色）")
        self._color_original = ColorPickerButton(QColor("#FFFFFF"), "变化前的原色", self)
        self._color_highlight = ColorPickerButton(QColor("#FFD54F"), "唱到时的目标高亮色", self)
        color_row.addWidget(self._color_cb)
        color_row.addWidget(BodyLabel("原色", self))
        color_row.addWidget(self._color_original)
        color_row.addWidget(BodyLabel("高亮色", self))
        color_row.addWidget(self._color_highlight)
        color_row.addStretch(1)
        f.addRow("", color_row)

        glow_row = QHBoxLayout()
        self._glow_cb = CheckBox(self)
        self._glow_cb.setText("描边发光")
        self._glow_bord = DoubleSpinBox(self)
        self._glow_bord.setRange(0, 20)
        self._glow_bord.setDecimals(1)
        self._glow_blur = DoubleSpinBox(self)
        self._glow_blur.setRange(0, 10)
        self._glow_blur.setDecimals(1)
        glow_row.addWidget(self._glow_cb)
        glow_row.addWidget(BodyLabel("描边宽度", self))
        glow_row.addWidget(self._glow_bord)
        glow_row.addWidget(BodyLabel("模糊强度", self))
        glow_row.addWidget(self._glow_blur)
        glow_row.addStretch(1)
        f.addRow("", glow_row)

        self._extra_edit = LineEdit(self)
        self._extra_edit.setPlaceholderText("附加特效标签（进阶，可留空），例如 \\frz10\\blur0.4")
        self._extra_edit.setToolTip(
            "原样追加到效果定义中的 ASS 特效标签；可使用 $start、$end 等时间变量与 !…! 表达式"
        )
        self._extra_edit.setClearButtonEnabled(True)
        f.addRow("附加标签", self._extra_edit)

        right.addWidget(form_host)

        # 代码编辑框（代码模式）
        self._raw_edit = QPlainTextEdit(self)
        self._raw_edit.setPlaceholderText(
            "直接编辑整条效果定义行（行首的 Comment: 前缀可省略，保存时自动补齐）。\n"
            "可用变量：$start/$end/$mid（该字起止/中点时间）、$dur（时长）、\n"
            "$scenter/$smiddle（该字中心坐标）等；!…! 内可写计算表达式。"
        )
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._raw_edit.setFont(mono)
        self._raw_edit.setMaximumHeight(96)
        right.addWidget(self._raw_edit)

        self._btn_to_raw = PushButton(self)
        self._btn_to_raw.setText("把当前参数生成的代码导入代码编辑继续修改")
        self._btn_to_raw.clicked.connect(self._form_to_raw)
        right.addWidget(self._btn_to_raw)

        # 生成结果预览
        right.addWidget(SubtitleLabel("生成结果预览（全部已勾选效果，随参数实时更新）", self))
        self._preview = QPlainTextEdit(self)
        self._preview.setReadOnly(True)
        self._preview.setFont(mono)
        self._preview.setMaximumHeight(88)
        right.addWidget(self._preview)

        # 底部按钮
        buttons = QHBoxLayout()
        self._btn_reset = PushButton(self)
        self._btn_reset.setText("恢复内置效果库")
        self._btn_reset.clicked.connect(self._on_reset)
        buttons.addWidget(self._btn_reset)
        buttons.addStretch(1)
        cancel = PushButton(self)
        cancel.setText("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = PrimaryPushButton(self)
        save.setText("保存效果")
        save.clicked.connect(self.accept)
        buttons.addWidget(save)
        root.addLayout(buttons)

        # 信号 → 回写 + 预览（统一走 _sync_back）
        self._name_edit.textChanged.connect(self._sync_back)
        self._mode_combo.currentIndexChanged.connect(self._sync_back)
        self._class_combo.currentIndexChanged.connect(self._sync_back)
        for cb in self._mod_cbs.values():
            cb.stateChanged.connect(self._sync_back)
        self._layer_spin.valueChanged.connect(self._sync_back)
        self._anchor_combo.currentIndexChanged.connect(self._sync_back)
        self._pos_cb.stateChanged.connect(self._sync_back)
        self._fad_in.valueChanged.connect(self._sync_back)
        self._fad_out.valueChanged.connect(self._sync_back)
        self._scale_cb.stateChanged.connect(self._sync_back)
        self._scale_spin.valueChanged.connect(self._sync_back)
        self._color_cb.stateChanged.connect(self._sync_back)
        self._color_original.colorChanged.connect(self._sync_back)
        self._color_highlight.colorChanged.connect(self._sync_back)
        self._glow_cb.stateChanged.connect(self._sync_back)
        self._glow_bord.valueChanged.connect(self._sync_back)
        self._glow_blur.valueChanged.connect(self._sync_back)
        self._extra_edit.textChanged.connect(self._sync_back)
        self._raw_edit.textChanged.connect(self._sync_back)

        self._reload_list()
        enabled_row = next(
            (index for index, template in enumerate(self._templates) if template.enabled),
            0,
        )
        self._list.setCurrentRow(enabled_row)

    # ── 结果 ───────────────────────────────────────────────────
    @property
    def current_prefs(self) -> KaraokeTemplatePrefs:
        return KaraokeTemplatePrefs(templates=self._templates)

    # ── 列表操作（复选框 = 启用开关） ──────────────────────────
    def _reload_list(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for t in self._templates:
            item = QListWidgetItem(t.name or "(未命名)")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if t.enabled else Qt.CheckState.Unchecked)
            self._list.addItem(item)
        self._list.blockSignals(False)

    def _on_item_check_changed(self, item: QListWidgetItem) -> None:
        row = self._list.row(item)
        if not (0 <= row < len(self._templates)):
            return
        checked = item.checkState() == Qt.CheckState.Checked
        if checked:
            self._list.blockSignals(True)
            try:
                for index, template in enumerate(self._templates):
                    template.enabled = index == row
                    other = self._list.item(index)
                    if other is not None:
                        other.setCheckState(
                            Qt.CheckState.Checked if index == row else Qt.CheckState.Unchecked
                        )
            finally:
                self._list.blockSignals(False)
        else:
            self._templates[row].enabled = False
        self._update_preview()

    def _on_select(self, row: int) -> None:
        if 0 <= row < len(self._templates):
            self._current = row
            self._load_form(self._templates[row])

    def _on_add(self) -> None:
        """新增 = 复制当前选中的效果（参数原样带走），命名为「原名 副本」并去重。"""
        if 0 <= self._current < len(self._templates):
            src = self._templates[self._current]
            clone = KaraokeTemplate.from_dict(src.to_dict())
            clone.name = self._duplicate_name(src.name or "未命名效果")
        else:
            clone = KaraokeTemplate(name=self._duplicate_name("新效果"))
        for template in self._templates:
            template.enabled = False
        clone.enabled = True
        # 新增项成为唯一选择，并插到复制项下方便于继续编辑
        insert_at = (
            self._current + 1 if 0 <= self._current < len(self._templates) else len(self._templates)
        )
        self._templates.insert(insert_at, clone)
        self._reload_list()
        self._list.setCurrentRow(insert_at)

    def _duplicate_name(self, base: str) -> str:
        """「原名 副本」；重名时递增为「原名 副本2」「原名 副本3」…"""
        base = base.strip() or "未命名效果"
        # 复制副本时不叠加「副本 副本」：剥掉已有的副本后缀再拼
        import re

        m = re.match(r"^(.*?)(?: 副本(\d*)?)?$", base)
        stem = (m.group(1) or base).strip() if m else base
        existing = {t.name for t in self._templates}
        candidate = f"{stem} 副本"
        n = 2
        while candidate in existing:
            candidate = f"{stem} 副本{n}"
            n += 1
        return candidate

    def _on_del(self) -> None:
        if len(self._templates) <= 1:
            return  # 至少保留一条效果定义；是否启用可全部取消
        del self._templates[self._current]
        self._current = max(0, self._current - 1)
        self._reload_list()
        self._list.setCurrentRow(self._current)

    def _move(self, delta: int) -> None:
        i, j = self._current, self._current + delta
        if 0 <= j < len(self._templates):
            self._templates[i], self._templates[j] = self._templates[j], self._templates[i]
            self._current = j
            self._reload_list()
            self._list.setCurrentRow(j)

    def _on_reset(self) -> None:
        self._templates = default_karaoke_templates().templates
        self._current = 0
        self._reload_list()
        self._list.setCurrentRow(0)

    # ── 表单载入 / 回写 ────────────────────────────────────────
    def _load_form(self, t: KaraokeTemplate) -> None:
        self._loading = True
        try:
            self._name_edit.setText(t.name)
            i = self._mode_combo.findData(t.mode)
            self._mode_combo.setCurrentIndex(max(0, i))
            i = self._class_combo.findData(t.template_class)
            self._class_combo.setCurrentIndex(max(0, i))
            for key, cb in self._mod_cbs.items():
                cb.setChecked(key in (t.modifiers or []))
            self._layer_spin.setValue(int(t.layer))
            i = self._anchor_combo.findData(int(t.anchor))
            self._anchor_combo.setCurrentIndex(max(0, i))
            self._pos_cb.setChecked(t.use_pos)
            self._fad_in.setValue(int(t.fad_in_ms))
            self._fad_out.setValue(int(t.fad_out_ms))
            self._scale_cb.setChecked(t.scale_enabled)
            self._scale_spin.setValue(int(t.scale_percent))
            self._color_cb.setChecked(t.color_enabled)
            self._color_original.setColor(QColor(t.color_original))
            self._color_highlight.setColor(QColor(t.color_highlight))
            self._glow_cb.setChecked(t.glow_enabled)
            self._glow_bord.setValue(float(t.glow_bord))
            self._glow_blur.setValue(float(t.glow_blur))
            self._extra_edit.setText(t.extra_tags)
            self._raw_edit.setPlainText(t.raw_text)
        finally:
            self._loading = False
        self._apply_mode_visibility()
        self._update_preview()

    def _sync_back(self, *args) -> None:
        if self._loading or not (0 <= self._current < len(self._templates)):
            return
        t = self._templates[self._current]
        t.name = self._name_edit.text().strip() or t.name
        t.mode = self._mode_combo.currentData() or "form"
        t.template_class = self._class_combo.currentData() or "syl"
        t.modifiers = [k for k, cb in self._mod_cbs.items() if cb.isChecked()]
        t.layer = int(self._layer_spin.value())
        t.anchor = int(self._anchor_combo.currentData() or 5)
        t.use_pos = self._pos_cb.isChecked()
        t.fad_in_ms = int(self._fad_in.value())
        t.fad_out_ms = int(self._fad_out.value())
        t.scale_enabled = self._scale_cb.isChecked()
        t.scale_percent = int(self._scale_spin.value())
        t.color_enabled = self._color_cb.isChecked()
        t.color_original = self._color_original.color.name(QColor.HexRgb).upper()
        t.color_highlight = self._color_highlight.color.name(QColor.HexRgb).upper()
        t.glow_enabled = self._glow_cb.isChecked()
        t.glow_bord = float(self._glow_bord.value())
        t.glow_blur = float(self._glow_blur.value())
        t.extra_tags = self._extra_edit.text().strip()
        t.raw_text = self._raw_edit.toPlainText().strip()
        # 列表名即时刷新（不重建列表防抖动）
        item = self._list.item(self._current)
        if item is not None and item.text() != (t.name or "(未命名)"):
            self._list.blockSignals(True)
            item.setText(t.name or "(未命名)")
            self._list.blockSignals(False)
        self._apply_mode_visibility()
        self._update_preview()

    def _apply_mode_visibility(self) -> None:
        is_raw = self._mode_combo.currentData() == "raw"
        self._raw_edit.setVisible(is_raw)
        self._btn_to_raw.setVisible(not is_raw)
        for w in (
            self._class_combo,
            self._layer_spin,
            self._anchor_combo,
            self._pos_cb,
            self._fad_in,
            self._fad_out,
            self._scale_cb,
            self._scale_spin,
            self._color_cb,
            self._color_original,
            self._color_highlight,
            self._glow_cb,
            self._glow_bord,
            self._glow_blur,
            self._extra_edit,
            *self._mod_cbs.values(),
        ):
            w.setEnabled(not is_raw)

    def _form_to_raw(self) -> None:
        """把当前参数渲染结果灌进代码编辑，切换后继续手改。"""
        if not (0 <= self._current < len(self._templates)):
            return
        t = self._templates[self._current]
        rendered = t.render_comment()
        t.mode = "raw"
        t.raw_text = rendered
        self._load_form(t)

    def _update_preview(self) -> None:
        prefs = KaraokeTemplatePrefs(templates=self._templates)
        lines = prefs.render_comments("Default")
        active_n = sum(
            1
            for t in self._templates
            if t.enabled and not (t.mode == "raw" and not (t.raw_text or "").strip())
        )
        if active_n == 0:
            self._preview.setPlainText("（未选择模板效果：只导出基础 k-tag，不附加 Automation 模板）")
        else:
            self._preview.setPlainText("\n".join(lines))


__all__ = ["KaraokeTemplateDialog"]
