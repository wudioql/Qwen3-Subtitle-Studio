"""Fluent 偏好设置弹窗。

高频的语言、逐字效果与 ASS 文字样式分别留在主工具栏、导出侧栏和
ASS 样式弹窗；这里只保留全局且低频的识别、分句、路径与外观设置。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QHBoxLayout, QStackedWidget,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CaptionLabel, CheckBox, ComboBox, DoubleSpinBox, LineEdit, Pivot,
    PrimaryPushButton, PushButton, SpinBox, TitleLabel,
)

from core.app_config import Preferences, load_preferences, save_preferences
from .themes import _DEFAULT_THEME


class SettingsDialog(QDialog):
    def __init__(self, parent=None, prefs: Optional[Preferences] = None):
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.setWindowTitle("偏好设置")
        self.resize(620, 570)
        self.setMinimumSize(560, 500)
        self._prefs = prefs or load_preferences()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        root.addWidget(TitleLabel("偏好设置", self))

        self._pivot = Pivot(self)
        self._stack = QStackedWidget(self)
        root.addWidget(self._pivot)
        root.addWidget(self._stack, 1)

        self._build_asr_tab()
        self._build_segmentation_tab()
        self._build_paths_tab()
        self._build_advanced_tab()
        self._build_appearance_tab()
        self._stack.currentChanged.connect(self._on_page_changed)
        self._pivot.setCurrentItem("asr")

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = PushButton(self)
        cancel.setText("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = PrimaryPushButton(self)
        save.setText("保存设置")
        save.clicked.connect(self._on_save)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def _add_page(self, key: str, title: str, page: QWidget) -> None:
        page.setObjectName(f"settings_{key}_page")
        self._stack.addWidget(page)
        self._pivot.addItem(
            routeKey=key,
            text=title,
            onClick=lambda _checked=False, p=page: self._stack.setCurrentWidget(p),
        )

    def _on_page_changed(self, index: int) -> None:
        if 0 <= index < self._stack.count():
            key = self._stack.widget(index).objectName().removeprefix("settings_").removesuffix("_page")
            self._pivot.setCurrentItem(key)

    @staticmethod
    def _form_page() -> tuple[QWidget, QFormLayout]:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(8, 14, 8, 8)
        f.setHorizontalSpacing(18)
        f.setVerticalSpacing(13)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return w, f

    def _build_asr_tab(self) -> None:
        w, f = self._form_page()
        asr = self._prefs.asr

        self._context_edit = LineEdit(w)
        self._context_edit.setText(asr.context)
        self._context_edit.setClearButtonEnabled(True)
        self._context_edit.setPlaceholderText("专有名词、热词、背景提示（可为空）")
        f.addRow("识别上下文 / 热词", self._context_edit)

        self._max_new_tokens = SpinBox(w)
        self._max_new_tokens.setRange(64, 8192)
        self._max_new_tokens.setValue(asr.max_new_tokens)
        f.addRow("最大生成 token 数", self._max_new_tokens)

        self._extract_vocals_cb = CheckBox(w)
        self._extract_vocals_cb.setText("默认启用人声提取 (Kim_Vocal_2 自动剥离伴奏)")
        self._extract_vocals_cb.setChecked(getattr(asr, "extract_vocals", False))
        f.addRow("", self._extract_vocals_cb)

        self._return_words = CheckBox(w)
        self._return_words.setText("保留字级时间戳（逐字导出必须）")
        self._return_words.setChecked(asr.return_word_timestamps)
        f.addRow("", self._return_words)

        self._use_cache = CheckBox(w)
        self._use_cache.setText("启用 KV cache（通常保持开启）")
        self._use_cache.setChecked(asr.use_cache)
        f.addRow("", self._use_cache)
        f.addRow(_hint("归属指引：识别语言 / 对齐后端 / 人声提取在主工具栏即时切换（本页仅默认项）；逐字高亮样式与 k-tag 在右侧导出侧栏；ASS 文字样式在其专属弹窗——各处修改都会持久化到同一份 preferences.json。"))
        self._add_page("asr", "识别与对齐", w)

    def _build_segmentation_tab(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 14, 8, 8)
        lay.setSpacing(12)

        seg = self._prefs.segmentation
        asr = self._prefs.asr

        # ── 全局默认上限（asr.max_sentence_chars / max_sentence_sec）────────
        # 引擎语义：无论下方按语言开关是否启用，这两条全局上限始终生效
        # （按语言条目仅在启用且 >0 时覆盖对应语言）。此前该组参数只能改
        # json，且页内文案谎称「关闭=保留原始切分」——现与真实行为对齐。
        g_form = QFormLayout()
        g_form.setVerticalSpacing(12)
        self._g_max_chars = SpinBox(w)
        self._g_max_chars.setRange(0, 200)
        self._g_max_chars.setSpecialValueText("不限制")
        self._g_max_chars.setValue(int(asr.max_sentence_chars))
        g_form.addRow("全局单句最大字数", self._g_max_chars)
        self._g_max_sec = DoubleSpinBox(w)
        self._g_max_sec.setRange(0, 60)
        self._g_max_sec.setDecimals(1)
        self._g_max_sec.setSingleStep(0.5)
        self._g_max_sec.setSpecialValueText("不限制")
        self._g_max_sec.setSuffix(" 秒")
        self._g_max_sec.setValue(float(asr.max_sentence_sec))
        g_form.addRow("全局单句最大时长", self._g_max_sec)
        lay.addLayout(g_form)
        lay.addWidget(_hint("全局上限对所有语言始终生效（0 = 不限制）；下方按语言条目启用后可覆盖对应语言。"))

        self._seg_enabled = CheckBox(w)
        self._seg_enabled.setText("启用按语言分句上限（覆盖全局上限）")
        self._seg_enabled.setChecked(seg.enabled)
        lay.addWidget(self._seg_enabled)

        from .languages import SENTENCE_LANGUAGES
        self._lang_combo = ComboBox(w)
        for code, name in SENTENCE_LANGUAGES:
            if code:
                self._lang_combo.addItem(name, userData=code)
        self._lang_combo.currentIndexChanged.connect(self._on_seg_lang_changed)
        lay.addWidget(self._lang_combo)

        form = QFormLayout()
        form.setVerticalSpacing(12)
        self._max_chars = SpinBox(w)
        self._max_chars.setRange(0, 200)
        self._max_chars.setSpecialValueText("不限制")
        form.addRow("单句最大字数", self._max_chars)
        self._max_sec = DoubleSpinBox(w)
        self._max_sec.setRange(0, 60)
        self._max_sec.setDecimals(1)
        self._max_sec.setSingleStep(0.5)
        self._max_sec.setSpecialValueText("不限制")
        self._max_sec.setSuffix(" 秒")
        form.addRow("单句最大时长", self._max_sec)
        lay.addLayout(form)

        self._seg_lang_cache: dict[str, tuple[int, float]] = {
            k: (int(v.get("max_chars", 0) or 0), float(v.get("max_duration_sec", 0) or 0))
            for k, v in seg.per_lang.items()
        }
        if self._lang_combo.count() > 0:
            self._on_seg_lang_changed(0)
        lay.addWidget(_hint("0 表示不限制。硬切只处理 ASR 给出的超长句，不会从一个词中间切开；短句合并与无标点回退的门槛在「高级」页。"))
        lay.addStretch(1)
        self._add_page("segmentation", "分句", w)

    def _on_seg_lang_changed(self, _idx: int) -> None:
        # 先保存离开语言前的当前值，再载入目标语言值。
        old_code = getattr(self, "_active_seg_lang", None)
        if old_code:
            self._seg_lang_cache[old_code] = (
                int(self._max_chars.value()), float(self._max_sec.value())
            )
        code = self._lang_combo.currentData()
        self._active_seg_lang = code
        c, s = self._seg_lang_cache.get(code, (0, 0.0))
        self._max_chars.blockSignals(True)
        self._max_sec.blockSignals(True)
        self._max_chars.setValue(c)
        self._max_sec.setValue(s)
        self._max_chars.blockSignals(False)
        self._max_sec.blockSignals(False)

    def _build_paths_tab(self) -> None:
        w, f = self._form_page()
        pt = self._prefs.paths
        self._ffmpeg_edit = LineEdit(w)
        self._ffmpeg_edit.setText(pt.ffmpeg_path)
        self._asr_model_edit = LineEdit(w)
        self._asr_model_edit.setText(pt.asr_model_path)
        self._aligner_model_edit = LineEdit(w)
        self._aligner_model_edit.setText(pt.aligner_model_path)
        self._vocal_model_edit = LineEdit(w)
        self._vocal_model_edit.setText(getattr(pt, "vocal_model_path", ""))
        self._mms_model_edit = LineEdit(w)
        self._mms_model_edit.setText(getattr(pt, "mms_aligner_model_path", ""))

        f.addRow("FFmpeg 路径", self._file_picker(self._ffmpeg_edit, "选择 ffmpeg 可执行文件"))
        f.addRow("ASR 模型目录", self._dir_picker(self._asr_model_edit))
        f.addRow("Qwen3 对齐器目录", self._dir_picker(self._aligner_model_edit))
        f.addRow("Kim_Vocal_2 模型文件", self._file_picker(self._vocal_model_edit, "选择 Kim_Vocal_2.onnx 文件 (*.onnx)"))
        f.addRow("MMS-FA ONNX 目录", self._dir_picker(self._mms_model_edit))
        f.addRow(_hint("留空时自动检测并使用 constants.py 中的默认模型路径。"))
        self._add_page("paths", "路径", w)

    def _build_advanced_tab(self) -> None:
        """高级参数：此前只能手改 preferences.json 的推理细节，全部纳入 UI。

        设置弹窗从此覆盖 json 的全部可调项（除工具栏/导出侧栏各自维护的
        高频项），「偏好设置 ↔ preferences.json」一一对应。
        """
        w, f = self._form_page()
        asr = self._prefs.asr
        align = self._prefs.align

        f.addRow(_hint("以下为推理细节参数，默认值经过调校，通常无需修改；改坏了可删 .config/preferences.json 恢复默认。"))

        # ── 无标点回退分句（ASR 未给标点时按理想时长切）────────────────
        self._fb_min_sec = DoubleSpinBox(w)
        self._fb_min_sec.setRange(0.5, 30.0)
        self._fb_min_sec.setDecimals(1)
        self._fb_min_sec.setSingleStep(0.5)
        self._fb_min_sec.setSuffix(" 秒")
        self._fb_min_sec.setValue(float(asr.fallback_min_sentence_sec))
        f.addRow("无标点回退·最短句时长", self._fb_min_sec)
        self._fb_max_sec = DoubleSpinBox(w)
        self._fb_max_sec.setRange(0, 60.0)
        self._fb_max_sec.setDecimals(1)
        self._fb_max_sec.setSingleStep(0.5)
        self._fb_max_sec.setSpecialValueText("不限制")
        self._fb_max_sec.setSuffix(" 秒")
        self._fb_max_sec.setValue(float(asr.fallback_max_sentence_sec))
        f.addRow("无标点回退·最长句时长", self._fb_max_sec)

        # ── 短句合并门槛（低于其一则并入下一句）────────────────────────
        self._min_chars = SpinBox(w)
        self._min_chars.setRange(0, 20)
        self._min_chars.setSpecialValueText("不合并")
        self._min_chars.setValue(int(asr.min_sentence_chars))
        f.addRow("短句合并·最小字数", self._min_chars)
        self._min_sec = DoubleSpinBox(w)
        self._min_sec.setRange(0, 5.0)
        self._min_sec.setDecimals(2)
        self._min_sec.setSingleStep(0.1)
        self._min_sec.setSpecialValueText("不合并")
        self._min_sec.setSuffix(" 秒")
        self._min_sec.setValue(float(asr.min_sentence_sec))
        f.addRow("短句合并·最小时长", self._min_sec)

        # ── 对齐裁剪窗留白（声学上下文；ASR 直通与重对齐共用语义）──────
        self._pad_before = DoubleSpinBox(w)
        self._pad_before.setRange(0, 1.0)
        self._pad_before.setDecimals(2)
        self._pad_before.setSingleStep(0.02)
        self._pad_before.setSuffix(" 秒")
        self._pad_before.setValue(float(align.pad_before))
        f.addRow("对齐窗·句首留白", self._pad_before)
        self._pad_after = DoubleSpinBox(w)
        self._pad_after.setRange(0, 1.0)
        self._pad_after.setDecimals(2)
        self._pad_after.setSingleStep(0.02)
        self._pad_after.setSuffix(" 秒")
        self._pad_after.setValue(float(align.pad_after))
        f.addRow("对齐窗·句尾留白", self._pad_after)

        # ── 超长句子切分（>5 分钟单句的兜底切分粒度）────────────────────
        self._subchunk_min = SpinBox(w)
        self._subchunk_min.setRange(1, 50)
        self._subchunk_min.setValue(int(align.subchunk_min_chars))
        f.addRow("超长句切分·最小字符数", self._subchunk_min)

        f.addRow(_hint("留白同时用于 ASR 识别后的直通对齐与三种重对齐；重对齐窗口另有邻句锚与拖音前瞻逻辑（constants.py）。"))
        self._add_page("advanced", "高级", w)

    def _build_appearance_tab(self) -> None:
        w, f = self._form_page()
        self._theme_combo = ComboBox(w)
        self._theme_combo.addItem("深色", userData="dark")
        self._theme_combo.addItem("浅色", userData="light")
        cur = str(getattr(self._prefs, "ui_theme", "") or "dark")
        i = self._theme_combo.findData(cur)
        self._theme_combo.setCurrentIndex(i if i >= 0 else 0)
        f.addRow("主题", self._theme_combo)
        f.addRow(_hint("界面由 PySide6-Fluent-Widgets 原生控件绘制；也可按 Ctrl+Shift+T 快速切换。"))
        self._add_page("appearance", "外观", w)

    def _file_picker(self, edit: LineEdit, caption: str) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(edit, 1)
        btn = PushButton(row)
        btn.setText("浏览…")
        btn.clicked.connect(lambda: self._pick_file(edit, caption))
        lay.addWidget(btn)
        return row

    def _dir_picker(self, edit: LineEdit) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(edit, 1)
        btn = PushButton(row)
        btn.setText("浏览…")
        btn.clicked.connect(lambda: self._pick_dir(edit))
        lay.addWidget(btn)
        return row

    def _pick_file(self, edit: LineEdit, caption: str) -> None:
        p, _ = QFileDialog.getOpenFileName(self, caption, edit.text() or "", "可执行文件 (*)")
        if p:
            edit.setText(p)

    def _pick_dir(self, edit: LineEdit) -> None:
        p = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or "")
        if p:
            edit.setText(p)

    def _collect_from_widgets(self, prefs: Preferences) -> None:
        """把控件值写入给定 prefs——只触碰本对话框拥有的字段组
        （asr 推理与分句参数 / segmentation / 路径 / 高级 / 主题），
        其余字段（工具栏维护的 asr.source_language 与 align.align_backend、
        导出侧栏维护的 style/export/ass_style、导出路径记忆等）一律不碰。"""
        asr = prefs.asr
        asr.context = self._context_edit.text().strip()
        asr.max_new_tokens = int(self._max_new_tokens.value())
        asr.return_word_timestamps = self._return_words.isChecked()
        asr.use_cache = self._use_cache.isChecked()
        asr.extract_vocals = self._extract_vocals_cb.isChecked()
        # 分句页·全局上限
        asr.max_sentence_chars = int(self._g_max_chars.value())
        asr.max_sentence_sec = float(self._g_max_sec.value())
        # 高级页
        asr.fallback_min_sentence_sec = float(self._fb_min_sec.value())
        asr.fallback_max_sentence_sec = float(self._fb_max_sec.value())
        asr.min_sentence_chars = int(self._min_chars.value())
        asr.min_sentence_sec = float(self._min_sec.value())
        # 对齐窗留白：ASR 直通与重对齐共用同一对语义值，成对同步
        prefs.align.pad_before = float(self._pad_before.value())
        prefs.align.pad_after = float(self._pad_after.value())
        asr.align_pad_before = prefs.align.pad_before
        asr.align_pad_after = prefs.align.pad_after
        prefs.align.subchunk_min_chars = int(self._subchunk_min.value())

        seg = prefs.segmentation
        seg.enabled = self._seg_enabled.isChecked()
        seg.per_lang = {
            k: {"max_chars": c, "max_duration_sec": s}
            for k, (c, s) in self._seg_lang_cache.items()
        }

        paths = prefs.paths
        paths.ffmpeg_path = self._ffmpeg_edit.text().strip()
        paths.asr_model_path = self._asr_model_edit.text().strip()
        paths.aligner_model_path = self._aligner_model_edit.text().strip()
        paths.vocal_model_path = self._vocal_model_edit.text().strip()
        paths.mms_aligner_model_path = self._mms_model_edit.text().strip()
        prefs.ui_theme = self._theme_combo.currentData() or _DEFAULT_THEME

    def _on_save(self) -> None:
        # 分句页：把当前语言行的最新编辑先并入缓存（per_lang 由缓存整体重建）
        code = self._lang_combo.currentData()
        if code:
            self._seg_lang_cache[code] = (
                int(self._max_chars.value()), float(self._max_sec.value())
            )
        # 保持注入对象的旧契约：构造时传入的 prefs 随保存同步更新（调用方/测试可读回）
        self._collect_from_widgets(self._prefs)
        # 防字段漂移：以**磁盘最新偏好**为基底，只覆写本对话框自有字段后保存——
        # 不再把构造时的整份快照写回，避免抹掉打开对话框期间别处（如工具栏）的改动。
        disk = load_preferences()
        self._collect_from_widgets(disk)
        save_preferences(disk)
        self.accept()


def _hint(text: str) -> CaptionLabel:
    label = CaptionLabel(text)
    label.setWordWrap(True)
    return label


__all__ = ["SettingsDialog"]
