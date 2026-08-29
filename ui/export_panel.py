"""响应式 Fluent 导出侧栏。

侧栏只放高频导出入口与逐字高亮选项；参数很多的 ASS v4+ 文字样式
改由「ASS 文字样式…」按钮弹出 ui.ass_style_dialog.AssStyleDialog。

内部 kind 保持兼容：ass_split 仍是原导出分支，但界面文案改为更直观的
「ASS 一字一行」。每个 ASS 产物已有独立按钮，因此不再显示重复且无效的
「ASS 逐字策略」下拉框。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel, CheckBox, ColorPickerButton, ComboBox, LineEdit,
    PrimaryPushButton, PushButton, ScrollArea, SimpleCardWidget, SubtitleLabel,
)

from core.app_config import load_preferences, save_preferences
from subs import WordHighlightStyle
from subs.ass_style import AssStylePrefs
from .ass_style_dialog import AssStyleDialog
from .karaoke_template_dialog import KaraokeTemplateDialog


# kind 值属于已有导出 API，批量替换时保持稳定。
SENTENCE_EXPORTS = [
    ("srt_sentence", "SRT", "通用播放器 / 平台"),
    ("vtt_sentence", "WebVTT", "网页与流媒体"),
    ("ass_sentence", "ASS", "带统一文字样式"),
    ("lrc_sentence", "标准 LRC", "每句一个时间标签"),
]
WORD_EXPORTS = [
    ("srt", "逐字 SRT", "当前字高亮 · 多条 cue"),
    ("vtt", "逐字 WebVTT", "当前字高亮 · 多条 cue"),
    ("ass_split", "ASS 一字一行", "一字一个事件 · 兼容性最好"),
    ("ass_t", r"ASS 颜色动画", "一句一个事件 · 用颜色变换实现逐字高亮"),
    ("lrc", "增强 LRC", "兼容格式：每字一个 <开始时间>；不写双时间戳"),
    ("karaoke", "Aegisub k-tag ASS", "标准计时源 · 自带可运行的示例模板"),
    ("karaoke_applied", "应用模板后 ASS", "Aegisub Apply 结构 · 播放器直接渲染 fx"),
]


def _card_size_policy(widget: QWidget) -> None:
    """卡片垂直策略：Preferred + 按宽算高声明。

    此前为 Maximum——它把 sizeHint()（按宽敞宽度计算的高度）当成上限，
    侧栏压窄后 wordWrap 说明文字换行增多、卡片需要更高，布局却拒绝分配
    （高度钉死在旧 sizeHint）→ 文字上下截断 + 相邻卡片间冒出空白条。
    Preferred 允许按需增高；heightForWidth 让外层布局按当前宽度重算高度；
    面板末尾的 addStretch 吸收宽敞时的多余空间，卡片不会被无意义拉伸。
    """
    sp = widget.sizePolicy()
    sp.setHorizontalPolicy(QSizePolicy.Expanding)
    sp.setVerticalPolicy(QSizePolicy.Preferred)
    sp.setHeightForWidth(True)
    widget.setSizePolicy(sp)


def _refresh_player_preview(card: QWidget) -> None:
    """样式变更后刷新播放器字幕预览（六档预览立即反映新样式）。

    通过主窗的 PlayerPanel.refresh_subtitle_styles() 统一刷新；
    非主窗上下文（无 player）静默跳过。
    """
    try:
        window = card.window()
        if window is not None and hasattr(window, "player"):
            window.player.refresh_subtitle_styles()
    except Exception:  # noqa: BLE001 — 预览刷新失败不影响样式保存
        pass


class _ExportGroup(SimpleCardWidget):
    """一组可随侧栏宽度收缩的导出按钮。"""

    def __init__(self, title: str, subtitle: str, items, on_export, parent=None):
        super().__init__(parent)
        _card_size_policy(self)
        self._buttons: list[PushButton] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 13, 14, 14)
        lay.setSpacing(8)
        heading = SubtitleLabel(title, self)
        lay.addWidget(heading)
        cap = CaptionLabel(subtitle, self)
        cap.setWordWrap(True)
        lay.addWidget(cap)

        for kind, label, hint in items:
            btn = PushButton(self)
            btn.setText(label)
            btn.setToolTip(hint)
            btn.setMinimumWidth(0)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _checked=False, k=kind: on_export(k))
            lay.addWidget(btn)
            self._buttons.append(btn)

    def set_export_enabled(self, enabled: bool) -> None:
        for button in self._buttons:
            button.setEnabled(enabled)


class WordStyleCard(SimpleCardWidget):
    """逐字 cue 的高亮外观与 Aegisub k-tag 类型。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        p = load_preferences()
        st = p.style
        _card_size_policy(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 13, 14, 14)
        lay.setSpacing(9)
        lay.addWidget(SubtitleLabel("逐字高亮", self))
        desc = CaptionLabel("字形用于逐字 SRT / VTT / ASS；颜色只用于两种非 k-tag ASS。", self)
        desc.setWordWrap(True)
        lay.addWidget(desc)

        # 窄侧栏使用单列，避免两列布局把文字压缩/截断；纵向空间由滚动区承担。
        self._cb_bold = CheckBox("加粗当前字", self)
        self._cb_italic = CheckBox("斜体当前字", self)
        self._cb_underline = CheckBox("当前字加下划线", self)
        self._cb_strike = CheckBox("当前字加删除线", self)
        self._cb_bold.setChecked(st.bold)
        self._cb_italic.setChecked(st.italic)
        self._cb_underline.setChecked(st.underline)
        self._cb_strike.setChecked(st.strike)
        for checkbox in (self._cb_bold, self._cb_italic, self._cb_underline, self._cb_strike):
            checkbox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            lay.addWidget(checkbox)

        color_label = CaptionLabel("非 k-tag ASS 当前字颜色", self)
        lay.addWidget(color_label)
        color_row = QHBoxLayout()
        color_row.setSpacing(8)
        self._highlight_color = ColorPickerButton(
            QColor(getattr(st, "ass_highlight_color", "#FFD54F")), "非 k-tag ASS 当前字颜色", self
        )
        self._highlight_color.setToolTip("用于 ASS 一字一行与 ASS 颜色动画；不依赖上面的字形开关")
        color_row.addWidget(self._highlight_color)
        self._highlight_color_text = CaptionLabel(self._highlight_color.color.name(QColor.HexRgb).upper(), self)
        self._highlight_color.colorChanged.connect(
            lambda color: self._highlight_color_text.setText(color.name(QColor.HexRgb).upper())
        )
        color_row.addWidget(self._highlight_color_text)
        color_row.addStretch(1)
        lay.addLayout(color_row)

        punct_hint = CaptionLabel("标点始终正常显示，但不会变色、加字形或生成独立 k-tag。", self)
        punct_hint.setWordWrap(True)
        punct_hint.setMinimumWidth(1)   # wordWrap 标签保持 Preferred：按宽算高不截断，minWidth=1 仍可压窄
        lay.addWidget(punct_hint)

        lay.addWidget(CaptionLabel("Aegisub k-tag 效果", self))
        self._k_mode = ComboBox(self)
        self._k_mode.addItem(r"\kf · 标准扫过填充（Aegisub/libass）", userData="kf")
        self._k_mode.addItem(r"\K · 扫过填充别名（部分 VSFilter 更兼容）", userData="K")
        self._k_mode.addItem(r"\k · 到点切换颜色", userData="k")
        self._k_mode.addItem(r"\ko · 到点移除描边", userData="ko")
        legacy_mode = p.export.k_tag_mode
        if legacy_mode == "km":  # 旧版误用了非标准 \km，迁移为标准 \k
            legacy_mode = "k"
        i = self._k_mode.findData(legacy_mode)
        self._k_mode.setCurrentIndex(i if i >= 0 else 0)
        self._k_mode.setToolTip(
            "只影响 Aegisub k-tag ASS。\\kf 与大写 \\K 语义相同，播放器兼容性可能不同；"
            "\\k/\\ko 本来就是到点瞬变。"
        )
        self._k_mode.setMinimumWidth(0)
        self._k_mode.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._k_mode.currentIndexChanged.connect(self._on_k_mode_changed)
        lay.addWidget(self._k_mode)
        k_color_hint = CaptionLabel(
            "基础 k-tag 颜色取自 ASS 文字样式：未唱=次色，已唱/扫过=主色；"
            "上方黄色‘当前字颜色’不作用于 k-tag。",
            self,
        )
        k_color_hint.setWordWrap(True)
        k_color_hint.setMinimumWidth(1)
        lay.addWidget(k_color_hint)

        workflow = CaptionLabel(
            "“Aegisub k-tag ASS”保留可编辑计时源；“应用模板后 ASS”直接生成 Apply 后的 "
            "karaoke Comment + fx Dialogue，播放器只渲染 fx。",
            self,
        )
        workflow.setWordWrap(True)
        workflow.setMinimumWidth(1)     # 同上：定高截断与上下空洞的根因即 Ignored 丢失按宽算高
        lay.addWidget(workflow)

        extra_label = CaptionLabel("ASS 一字一行的额外 override 标签（高级，可留空）", self)
        extra_label.setWordWrap(True)
        extra_label.setMinimumWidth(1)
        lay.addWidget(extra_label)
        self._ass_extra = LineEdit(self)
        self._ass_extra.setText(st.ass_extra_tags)
        self._ass_extra.setClearButtonEnabled(True)
        self._ass_extra.setPlaceholderText(r"例如 \fad(200,200)\blur0.6（不含外层大括号）")
        self._ass_extra.setMinimumWidth(0)
        self._ass_extra.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        lay.addWidget(self._ass_extra)

        # 逐字高亮样式变更 → 立即写偏好 + 刷新字幕预览（第三种「逐字高亮」等预览实时反映）
        for checkbox in (self._cb_bold, self._cb_italic, self._cb_underline, self._cb_strike):
            checkbox.stateChanged.connect(self._on_word_style_changed)
        self._highlight_color.colorChanged.connect(self._on_word_style_changed)

    def _on_word_style_changed(self, *_args) -> None:
        """勾选字形 / 改高亮颜色 → 写偏好并刷新播放器字幕预览。"""
        self.save_to_prefs()
        _refresh_player_preview(self)

    def word_highlight_style(self) -> WordHighlightStyle:
        return WordHighlightStyle(
            bold=self._cb_bold.isChecked(),
            italic=self._cb_italic.isChecked(),
            underline=self._cb_underline.isChecked(),
            strike=self._cb_strike.isChecked(),
            ass_extra=self._normalized_ass_extra(),
            ass_highlight_color=self._highlight_color.color.name(QColor.HexRgb).upper(),
        )

    def _normalized_ass_extra(self) -> str:
        """允许用户误粘贴 {…}，保存/导出前去掉一层，避免生成 {{…}}。"""
        text = self._ass_extra.text().strip()
        if text.startswith("{") and text.endswith("}"):
            text = text[1:-1].strip()
        return text

    def k_mode(self) -> str:
        return self._k_mode.currentData() or "kf"

    def _on_k_mode_changed(self, _index: int) -> None:
        prefs = load_preferences()
        prefs.export.k_tag_mode = self.k_mode()
        save_preferences(prefs)
        _refresh_player_preview(self)

    def save_to_prefs(self) -> None:
        p = load_preferences()
        p.style.bold = self._cb_bold.isChecked()
        p.style.italic = self._cb_italic.isChecked()
        p.style.underline = self._cb_underline.isChecked()
        p.style.strike = self._cb_strike.isChecked()
        p.style.ass_extra_tags = self._normalized_ass_extra()
        p.style.ass_highlight_color = self._highlight_color.color.name(QColor.HexRgb).upper()
        p.export.k_tag_mode = self.k_mode()
        save_preferences(p)

    def apply_prefs(self, style, k_tag_mode: str) -> None:
        """从偏好对象刷新卡片控件（打开工程应用工程级导出设置时调用）。

        blockSignals：此处是「外部已定值 → 回填控件」，不应触发
        _on_word_style_changed/_on_k_mode_changed（否则会再次写偏好并刷新预览）。
        """
        self._cb_bold.blockSignals(True)
        self._cb_italic.blockSignals(True)
        self._cb_underline.blockSignals(True)
        self._cb_strike.blockSignals(True)
        self._highlight_color.blockSignals(True)
        try:
            self._cb_bold.setChecked(bool(style.bold))
            self._cb_italic.setChecked(bool(style.italic))
            self._cb_underline.setChecked(bool(style.underline))
            self._cb_strike.setChecked(bool(style.strike))
            self._ass_extra.setText(getattr(style, "ass_extra_tags", "") or "")
            color = QColor(getattr(style, "ass_highlight_color", "#FFD54F") or "#FFD54F")
            self._highlight_color.setColor(color)
            self._highlight_color_text.setText(color.name(QColor.HexRgb).upper())
            mode = "k" if k_tag_mode == "km" else (k_tag_mode or "kf")
            i = self._k_mode.findData(mode)
            self._k_mode.blockSignals(True)
            self._k_mode.setCurrentIndex(i if i >= 0 else 0)
            self._k_mode.blockSignals(False)
        finally:
            self._cb_bold.blockSignals(False)
            self._cb_italic.blockSignals(False)
            self._cb_underline.blockSignals(False)
            self._cb_strike.blockSignals(False)
            self._highlight_color.blockSignals(False)


class AssStyleCard(SimpleCardWidget):
    """窄侧栏中的 ASS 样式摘要与弹窗入口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        _card_size_policy(self)
        self._prefs = load_preferences()
        self._style = self._prefs.ass_style.to_style()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 13, 14, 14)
        lay.setSpacing(8)
        lay.addWidget(SubtitleLabel("ASS 文字样式", self))
        self._summary = CaptionLabel("", self)
        self._summary.setWordWrap(True)
        lay.addWidget(self._summary)
        self._button = PrimaryPushButton(self)
        self._button.setText("打开文字样式设置…")
        self._button.setMinimumWidth(0)
        self._button.clicked.connect(self._open_dialog)
        lay.addWidget(self._button)
        self._refresh_summary()

    def current_style(self) -> AssStylePrefs:
        return AssStylePrefs.from_dict(self._style.to_dict())

    def apply_prefs(self, style: AssStylePrefs) -> None:
        """从外部样式对象刷新卡片（打开工程应用工程级 ASS 样式时调用）。"""
        self._style = AssStylePrefs.from_dict(style.to_dict())
        self._refresh_summary()

    def _open_dialog(self) -> None:
        dlg = AssStyleDialog(self._style, self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._style = dlg.current_style
        # 每次弹窗前重新读取，可保留其他模块刚写入的偏好。
        self._prefs = load_preferences()
        self._prefs.ass_style.apply(self._style)
        save_preferences(self._prefs)
        self._refresh_summary()
        _refresh_player_preview(self)   # ASS 样式变更 → 所有含 ASS 样式的预览实时反映

    def _refresh_summary(self) -> None:
        st = self._style
        self._summary.setText(
            f"{st.font_name} · {st.font_size}px\n"
            f"字色 {st.primary_color} · 描边 {st.outline:g} · 阴影 {st.shadow:g}"
        )


class KaraokeTemplateCard(SimpleCardWidget):
    """Aegisub 卡拉OK效果摘要与弹窗入口（操作逻辑同 AssStyleCard）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        _card_size_policy(self)
        self._prefs = load_preferences()
        self._tpl = self._prefs.karaoke_template.to_prefs()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 13, 14, 14)
        lay.setSpacing(8)
        lay.addWidget(SubtitleLabel("Aegisub 卡拉OK效果", self))
        self._summary = CaptionLabel("", self)
        self._summary.setWordWrap(True)
        lay.addWidget(self._summary)
        self._button = PrimaryPushButton(self)
        self._button.setText("打开效果编辑…")
        self._button.setMinimumWidth(0)
        self._button.clicked.connect(self._open_dialog)
        lay.addWidget(self._button)
        self._refresh_summary()

    def current_template_prefs(self):
        from subs.karaoke_template import KaraokeTemplatePrefs
        return KaraokeTemplatePrefs.from_dict(self._tpl.to_dict())

    def apply_prefs(self, tpl) -> None:
        """从外部模板对象刷新卡片（打开工程应用工程级模板时调用）。"""
        from subs.karaoke_template import KaraokeTemplatePrefs
        self._tpl = KaraokeTemplatePrefs.from_dict(tpl.to_dict())
        self._refresh_summary()

    def _open_dialog(self) -> None:
        dlg = KaraokeTemplateDialog(self._tpl, self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._tpl = dlg.current_prefs
        # 每次弹窗前重新读取，可保留其他模块刚写入的偏好。
        self._prefs = load_preferences()
        self._prefs.karaoke_template.apply(self._tpl)
        save_preferences(self._prefs)
        self._refresh_summary()
        _refresh_player_preview(self)   # 卡拉OK模板变更 → 卡拉OK档预览实时反映

    def _refresh_summary(self) -> None:
        active = [t for t in self._tpl.templates if t.enabled]
        if not active:
            self._summary.setText("未选择模板效果 · 成品导出将提示先选择")
            return
        name = active[0].name or "(未命名)"
        self._summary.setText(f"已选择：{name} · 用于模板预览、k-tag 源与成品 ASS")


class ExportPanel(QWidget):
    """句级 / 字级导出侧栏；设计宽度 240–340 px，无水平滚动。"""

    def __init__(self, on_export: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.setObjectName("export_panel")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._on_export = on_export

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(0)

        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(ScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.enableTransparentBackground()

        inner = QWidget(scroll)
        inner.setObjectName("export_scroll_content")
        inner.setMinimumWidth(0)
        inner.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(5, 5, 5, 8)
        lay.setSpacing(10)

        self._grp_sentence = _ExportGroup(
            "句级导出", "每句一个字幕事件", SENTENCE_EXPORTS, on_export, inner
        )
        self._grp_word = _ExportGroup(
            "字级导出", "逐字高亮、动画与卡拉OK", WORD_EXPORTS, on_export, inner
        )
        self.word_style = WordStyleCard(inner)
        self.ass_style = AssStyleCard(inner)
        self.karaoke_template = KaraokeTemplateCard(inner)
        for widget in (self._grp_sentence, self._grp_word, self.word_style,
                       self.ass_style, self.karaoke_template):
            widget.setMinimumWidth(0)
            lay.addWidget(widget)
        lay.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

    def sizeHint(self) -> QSize:
        return QSize(286, 620)

    def minimumSizeHint(self) -> QSize:
        return QSize(220, 320)

    def refresh_states(self, has_sentences: bool) -> None:
        self._grp_sentence.set_export_enabled(has_sentences)
        self._grp_word.set_export_enabled(has_sentences)

    def save_options(self) -> None:
        self.word_style.save_to_prefs()

    def apply_prefs_from(self, prefs) -> None:
        """把偏好对象应用到三个卡片（打开工程应用工程级三件套时调用）。"""
        self.word_style.apply_prefs(prefs.style, prefs.export.k_tag_mode)
        self.ass_style.apply_prefs(prefs.ass_style.to_style())
        self.karaoke_template.apply_prefs(prefs.karaoke_template.to_prefs())


__all__ = [
    "ExportPanel", "SENTENCE_EXPORTS", "WORD_EXPORTS",
    "WordStyleCard", "AssStyleCard", "KaraokeTemplateCard",
]
