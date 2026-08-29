"""ui.main_window.chrome — 菜单 / 工具栏 / 状态栏 / 主题与动作代理。"""
from __future__ import annotations

import logging

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QLabel, QMessageBox, QSizePolicy, QStatusBar, QToolBar, QWidget,
)
from qfluentwidgets import (
    ComboBox, FluentIcon as FIF, ProgressBar, PushButton, Theme, isDarkTheme,
)

from ui.languages import GLOBAL_LANGUAGES


logger = logging.getLogger("ui.main_window")


class ChromeMixin:
    """装配壳层 UI，不依赖编辑细节。"""

    def _build_menubar(self) -> None:
        mb = self.menuBar()

        # 文件
        m_file = mb.addMenu("文件(&F)")
        self._act_open = QAction("打开媒体…", self)
        self._act_open.setShortcut("Ctrl+O")
        self._act_open.triggered.connect(self._on_act_open_media)
        m_file.addAction(self._act_open)

        self._act_relink_media = QAction("重新关联媒体…", self)
        self._act_relink_media.setToolTip("只替换当前工程的媒体路径，保留全部字幕、字级、脏标记与锁定状态")
        self._act_relink_media.setEnabled(False)
        self._act_relink_media.triggered.connect(self._on_act_relink_media)
        m_file.addAction(self._act_relink_media)

        m_file.addSeparator()
        self._act_import_subtitle = QAction("导入字幕 / 纯文本…", self)
        self._act_import_subtitle.setShortcut("Ctrl+Shift+F")
        self._act_import_subtitle.triggered.connect(self._on_act_import_subtitle)
        m_file.addAction(self._act_import_subtitle)

        m_file.addSeparator()
        self._act_open_project = QAction("打开工程…", self)
        self._act_open_project.setShortcut("Ctrl+Shift+O")
        self._act_open_project.setToolTip("打开本工具导出的 .json 工程（媒体路径 + 句/字级字幕 + 脏/锁/语言）")
        self._act_open_project.triggered.connect(self._on_act_open_project)
        m_file.addAction(self._act_open_project)

        self._act_save_project = QAction("保存工程…", self)
        self._act_save_project.setShortcut("Ctrl+S")
        self._act_save_project.setToolTip("将当前媒体与字幕保存为 .json 工程，便于下次继续编辑")
        self._act_save_project.setEnabled(False)
        self._act_save_project.triggered.connect(self._on_act_save_project)
        m_file.addAction(self._act_save_project)

        m_file.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        # 编辑
        m_edit = mb.addMenu("编辑(&E)")
        self._act_undo = self._undo_stack.createUndoAction(self, "撤销")
        self._act_undo.setShortcut(QKeySequence.Undo)
        self._act_redo = self._undo_stack.createRedoAction(self, "重做")
        self._act_redo.setShortcut(QKeySequence.Redo)
        m_edit.addAction(self._act_undo)
        m_edit.addAction(self._act_redo)
        m_edit.addSeparator()

        self._act_confirm_sent = QAction("确认选中句 (清除待对齐标记)", self)
        self._act_confirm_sent.setShortcut("Ctrl+K")
        self._act_confirm_sent.triggered.connect(self._on_shortcut_confirm)
        m_edit.addAction(self._act_confirm_sent)

        self._act_lock_sent = QAction("切换锁定保护 (禁止自动覆写)", self)
        self._act_lock_sent.setShortcut("Ctrl+L")
        self._act_lock_sent.triggered.connect(self._on_shortcut_toggle_lock)
        m_edit.addAction(self._act_lock_sent)

        # 工具（识别 + 三种对齐）
        m_tools = mb.addMenu("工具(&T)")
        self._act_transcribe = QAction("识别生成字幕 (ASR + 对齐)", self)
        self._act_transcribe.setShortcut("Ctrl+G")
        self._act_transcribe.triggered.connect(self._on_act_transcribe)
        self._act_transcribe.setEnabled(False)
        m_tools.addAction(self._act_transcribe)

        self._act_cancel_task = QAction("取消当前任务", self)
        self._act_cancel_task.setShortcut("Esc")
        self._act_cancel_task.setToolTip("请求在当前模型前向/句/块结束后的安全点取消任务")
        self._act_cancel_task.setEnabled(False)
        self._act_cancel_task.triggered.connect(lambda: self.workflow.request_cancel())
        m_tools.addAction(self._act_cancel_task)

        m_tools.addSeparator()
        self._act_align_full = QAction("全文重对齐", self)
        self._act_align_full.setShortcut("Ctrl+Shift+R")
        self._act_align_full.setToolTip("拼成一段文本整段跑 Aligner；超长媒体自动切块，锁定句严格保护。")
        self._act_align_full.triggered.connect(self._on_act_align_full)
        self._act_align_full.setEnabled(False)
        m_tools.addAction(self._act_align_full)

        self._act_align_sel = QAction("选中句重对齐", self)
        self._act_align_sel.setShortcut("Ctrl+Alt+R")
        self._act_align_sel.triggered.connect(self._on_act_align_sel)
        self._act_align_sel.setEnabled(False)
        m_tools.addAction(self._act_align_sel)

        self._act_align_dirty = QAction("修改句重对齐", self)
        self._act_align_dirty.setShortcut("Ctrl+R")
        self._act_align_dirty.setToolTip("只重对齐手动改过（标脏）且未锁定的句子，未改动句保留。")
        self._act_align_dirty.triggered.connect(self._on_act_align_dirty)
        self._act_align_dirty.setEnabled(False)
        m_tools.addAction(self._act_align_dirty)

        # 设置
        m_sett = mb.addMenu("设置(&S)")
        self._act_theme = QAction("切换浅色/深色模式", self)
        self._act_theme.setShortcut("Ctrl+Shift+T")
        self._act_theme.triggered.connect(self._on_toggle_theme)
        m_sett.addAction(self._act_theme)
        self._act_settings = QAction("偏好设置…", self)
        self._act_settings.triggered.connect(self._on_open_settings)
        m_sett.addAction(self._act_settings)

        # 帮助
        m_help = mb.addMenu("帮助(&H)")
        act_about = QAction("关于…", self)
        act_about.triggered.connect(self._on_act_about)
        m_help.addAction(act_about)


    def _build_toolbar(self) -> None:
        tb = QToolBar("主工具栏", self)
        tb.setObjectName("main_command_toolbar")
        tb.setMovable(False)
        self.addToolBar(tb)
        self._main_toolbar = tb

        self._toolbar_action_items: list[tuple[PushButton, QAction, FIF]] = []

        def add_action_button(action: QAction, label: str, fluent_icon: FIF) -> PushButton:
            button = PushButton(tb)
            button.setText(label)
            button.setToolTip(action.toolTip() or action.text())
            button.setEnabled(action.isEnabled())
            button.clicked.connect(lambda _checked=False, a=action: a.trigger())
            action.changed.connect(lambda b=button, a=action: b.setEnabled(a.isEnabled()))
            tb.addWidget(button)
            self._toolbar_action_items.append((button, action, fluent_icon))
            return button

        add_action_button(self._act_open, "打开媒体", FIF.FOLDER)
        add_action_button(self._act_import_subtitle, "导入字幕", FIF.DOCUMENT)
        tb.addSeparator()
        add_action_button(self._act_transcribe, "识别生成字幕", FIF.ROBOT)
        tb.addSeparator()
        add_action_button(self._act_align_full, "全文重对齐", FIF.SYNC)
        add_action_button(self._act_align_sel, "选中句重对齐", FIF.STOP_WATCH)
        add_action_button(self._act_align_dirty, "修改句重对齐", FIF.UPDATE)
        tb.addSeparator()

        tb.addWidget(QLabel("识别语言", self))
        self._global_lang = ComboBox(self)
        for code, name in GLOBAL_LANGUAGES:
            self._global_lang.addItem(name, userData=code)
        self._global_lang.setToolTip(
            "ASR 识别语言（仅作用于转写；重对齐按各句语言逐句执行，未设置的句回落项目语言）"
        )
        self._global_lang.currentIndexChanged.connect(self._on_global_lang_changed)
        self._global_lang.setMinimumWidth(112)
        self._global_lang.setMaximumWidth(150)
        tb.addWidget(self._global_lang)

        tb.addSeparator()
        tb.addWidget(QLabel("对齐模式", self))
        self._align_backend = ComboBox(self)
        self._align_backend.addItem("🗣️ 口语 / 播客 (Qwen3)", userData="qwen")
        self._align_backend.addItem("🎵 歌曲 / 歌词 (MMS-FA)", userData="mms")
        self._align_backend.setToolTip("选择强制对齐引擎：Qwen3-Aligner 适合口语对话；MMS-FA 适合歌曲长拖音及多语言混杂歌词")
        self._align_backend.currentIndexChanged.connect(self._on_align_backend_changed)
        self._align_backend.setMinimumWidth(168)
        tb.addWidget(self._align_backend)

        spacer = QWidget(tb)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self._btn_theme = PushButton(self)
        self._btn_theme.setText("主题")
        self._btn_theme.setToolTip("切换浅色/深色 (Ctrl+Shift+T)")
        self._btn_theme.clicked.connect(self._on_toggle_theme)
        tb.addWidget(self._btn_theme)
        self._btn_settings = PushButton(self)
        self._btn_settings.setText("设置")
        self._btn_settings.setToolTip("打开偏好设置")
        self._btn_settings.clicked.connect(self._on_open_settings)
        tb.addWidget(self._btn_settings)

        self._update_toolbar_icons(isDarkTheme())


    def _update_toolbar_icons(self, dark: bool) -> None:
        t = Theme.DARK if dark else Theme.LIGHT
        for button, action, fluent_icon in getattr(self, "_toolbar_action_items", []):
            icon = fluent_icon.icon(t)
            action.setIcon(icon)
            button.setIcon(icon)
        if hasattr(self, "_btn_theme"):
            self._btn_theme.setIcon(FIF.BRUSH.icon(t))
        if hasattr(self, "_btn_settings"):
            self._btn_settings.setIcon(FIF.SETTING.icon(t))


    def _build_statusbar(self) -> None:
        sb: QStatusBar = self.statusBar()
        self._sb_path = QLabel("未打开媒体", self)
        self._sb_mode = QLabel("模式：空闲", self)
        self._sb_vram = QLabel("模型：未加载", self)
        self._sb_progress = ProgressBar(self)
        self._sb_progress.setMaximumWidth(260)
        self._sb_progress.setRange(0, 100)
        self._sb_progress.setValue(0)
        self._sb_progress.hide()
        for w in (self._sb_path, self._sb_mode, self._sb_vram):
            w.setMinimumWidth(180)
            sb.addWidget(w, 1)
        sb.addPermanentWidget(self._sb_progress)


    def _load_toolbar_prefs(self) -> None:
        """启动时恢复工具栏下拉（识别语言 / 对齐后端）的上次选择。

        注意：Preferences 没有 ``ui`` 字段；语言选择持久化在
        ``prefs.asr.source_language``（ASRPreferences 的既有字段）。
        """
        try:
            from core.app_config import load_preferences
            prefs = load_preferences()
            code = (prefs.asr.source_language or "auto")
            idx = self._global_lang.findData(code)
            if idx >= 0:
                self._global_lang.setCurrentIndex(idx)

            backend = (prefs.align.align_backend or "qwen")
            b_idx = self._align_backend.findData(backend)
            if b_idx >= 0:
                self._align_backend.setCurrentIndex(b_idx)
        except Exception:
            logger.debug("[偏好] 加载工具栏偏好失败")


    def _on_align_backend_changed(self, _idx: int) -> None:
        backend = self._align_backend.currentData() or "qwen"
        try:
            from core.app_config import load_preferences, save_preferences
            prefs = load_preferences()
            prefs.align.align_backend = backend
            save_preferences(prefs)
        except Exception:
            logger.debug("[设置] 保存对齐后端偏好失败")
        self._model_manager.active_aligner = backend
        if hasattr(self, "_sb_vram"):
            self._sb_vram.setText(self._model_manager.status_text())


    def _on_global_lang_changed(self, _idx: int) -> None:
        code = self._global_lang.currentData() or "auto"
        # 识别语言 = 仅 ASR；不就地改写既有项目的 source_language
        # （项目语言由 ASR 检出结果/「应用到全部」维护；新建项目仍以工具栏选择为初始默认）
        try:
            from core.app_config import load_preferences, save_preferences
            prefs = load_preferences()
            # Preferences 无 ui 字段；识别语言持久化到 prefs.asr.source_language
            prefs.asr.source_language = code
            save_preferences(prefs)
        except Exception:
            logger.debug("[偏好] 保存识别语言失败")


    def _on_toggle_theme(self) -> None:
        from ui.themes import toggle_theme, is_dark, save_theme
        toggle_theme(self)
        dark = is_dark()
        self._update_toolbar_icons(dark)
        if hasattr(self, "waveform"):
            self.waveform.set_theme(dark)
        save_theme(dark)


    def _on_open_settings(self) -> None:
        from ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self)
        dlg.exec()


    def _on_act_about(self) -> None:
        QMessageBox.information(
            self, "关于",
            "Qwen3 Subtitle Studio\n"
            "本地字幕生成与编辑工具\n\n"
            "ASR: Qwen3-ASR-1.7B\n"
            "Aligner: Qwen3-ForcedAligner-0.6B\n"
            "PySide6 + PyQtGraph\n"
            "纯本地离线推理 · 显存预算 8GB",
        )


    def _on_act_open_media(self) -> None:
        self.project_ctrl.open_media_dialog()


    def _on_act_relink_media(self) -> None:
        self.project_ctrl.relink_media_dialog()

    def _on_act_import_subtitle(self) -> None:
        self.project_ctrl.import_subtitle_dialog()


    def _on_act_open_project(self) -> None:
        self.project_ctrl.open_project_dialog()


    def _on_act_save_project(self) -> None:
        self.project_ctrl.save_project_dialog()


    def _on_act_transcribe(self) -> None:
        self.workflow.start_transcribe()


    def _on_act_align_dirty(self) -> None:
        self.workflow.start_align_dirty()


    def _on_act_align_full(self) -> None:
        self.workflow.start_align_full()


    def _on_act_align_sel(self) -> None:
        self.workflow.start_align_selected()


    def _on_realign_single_sentence(self, idx: int) -> None:
        self.workflow.realign_single_sentence(idx)


