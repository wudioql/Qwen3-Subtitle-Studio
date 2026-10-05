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

        # 人声提取（工具栏亦有按钮，见 _build_toolbar）
        self._act_extract_vocals = QAction("提取人声…", self)
        self._act_extract_vocals.setToolTip(
            "对当前媒体提取纯人声（Kim_Vocal_2 剥离伴奏），字幕数据保留。\n"
            "提取后可重新识别，音轨即换成去伴奏版本。"
        )
        self._act_extract_vocals.setEnabled(False)
        self._act_extract_vocals.triggered.connect(self._on_act_extract_vocals)
        m_tools.addAction(self._act_extract_vocals)

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
        """主工具栏 = **两行** QToolBar（2026-10-04）。

        为什么拆两行：单行放不下——7 个操作按钮 + 3 组「标签 + 下拉」+
        主题/设置，实测在常见窗口宽度下右边被挤出可视区。QToolBar 本身不会
        自动换行，且**不换行会直接裁掉右侧控件**（不是出现省略号）。

        分行原则（按用户指定）：
        - 第 1 行：纯**动作**按钮，按工作流顺序，识别/对齐各自成组；
        - 第 2 行：从「识别语言」开始的全部**设置类**下拉 + 主题/设置。
          设置类天然是「调一次就一直生效」的参数，与动作按钮混排时最容易
          被误当成下一步操作，放第二行也更符合使用节奏。

        第 2 行放在 Qt.TopToolBarArea 的下一行——注意 ``addToolBar`` 是
        **顺序追加**语义：连续调用两次会把两个工具栏**并排放进同一行**
        （实测 y 均为 30，看起来就是一个长工具栏），**不会**自动堆叠。
        必须显式 ``addToolBarBreak()`` 才开始新行（2026-10-04 真机复测发现：
        控件归属测试全绿，但真机只有 1 行）。
        """
        tb = QToolBar("主工具栏", self)
        tb.setObjectName("main_command_toolbar")
        tb.setMovable(False)
        self.addToolBar(tb)
        self._main_toolbar = tb

        tb2 = QToolBar("主工具栏·设置", self)
        tb2.setObjectName("main_command_toolbar_settings")
        tb2.setMovable(False)
        # 换行必须显式声明，否则第2 个工具栏会被并到第 1 行右侧。
        self.addToolBarBreak()
        self.addToolBar(tb2)
        self._main_toolbar_settings = tb2

        self._toolbar_action_items: list[tuple[PushButton, QAction, FIF]] = []

        def add_action_button(action: QAction, label: str, fluent_icon: FIF,
                             target: QToolBar | None = None) -> PushButton:
            bar = target if target is not None else tb
            button = PushButton(bar)
            button.setText(label)
            button.setToolTip(action.toolTip() or action.text())
            button.setEnabled(action.isEnabled())
            button.clicked.connect(lambda _checked=False, a=action: a.trigger())
            action.changed.connect(lambda b=button, a=action: b.setEnabled(a.isEnabled()))
            bar.addWidget(button)
            self._toolbar_action_items.append((button, action, fluent_icon))
            return button

        # ── 第 1 行：动作按钮 ──
        add_action_button(self._act_open, "打开媒体", FIF.FOLDER)
        add_action_button(self._act_import_subtitle, "导入字幕", FIF.DOCUMENT)
        tb.addSeparator()
        # 人声提取：独立成组，夹在「导入字幕」右、「识别生成字幕」左。
        # 独立是因为它既不是导入也不是识别，而是一条**可重复**的后处理：
        # 媒体打开后随时可以提取人声、保留字幕、换音轨重识别。
        add_action_button(self._act_extract_vocals, "人声提取", FIF.MUSIC)
        tb.addSeparator()
        add_action_button(self._act_transcribe, "识别生成字幕", FIF.ROBOT)
        tb.addSeparator()
        add_action_button(self._act_align_full, "全文重对齐", FIF.SYNC)
        add_action_button(self._act_align_sel, "选中句重对齐", FIF.STOP_WATCH)
        add_action_button(self._act_align_dirty, "修改句重对齐", FIF.UPDATE)

        # ── 第 2 行：设置类下拉（从「识别语言」开始）──
        # 整行**靠右对齐**（用户指定）：弹性 spacer 放在最前面占掉左侧空白，
        # 后面跟一串设置类控件整体贴右。原来 spacer 放在中间，只有末尾的
        # 「主题/设置」靠右，前面的下拉仍然左对齐。
        # 三个下拉的宽度由 _fit_combo 按字体度量算出（不截断文字），
        # 它们独占一整行，因此不再有撑爆工具栏的风险。
        spacer = QWidget(tb2)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb2.addWidget(spacer)                      # 弹性占位：把后续控件整体推向右侧
        tb2.addWidget(QLabel("识别语言", self))
        self._global_lang = ComboBox(self)
        for code, name in GLOBAL_LANGUAGES:
            self._global_lang.addItem(name, userData=code)
        self._global_lang.setToolTip(
            "ASR 识别语言（仅作用于转写；重对齐按各句语言逐句执行，未设置的句回落项目语言）"
        )
        self._global_lang.currentIndexChanged.connect(self._on_global_lang_changed)
        self._fit_combo(self._global_lang, 96, 130)
        tb2.addWidget(self._global_lang)

        tb2.addSeparator()
        tb2.addWidget(QLabel("识别后端", self))
        self._asr_backend = ComboBox(self)
        self._asr_backend.addItem("🖥️ 本地 · Qwen3-ASR", userData="local")
        self._asr_backend.addItem("☁️ 云端 · SiliconFlow", userData="cloud")
        self._asr_backend.setToolTip(
            "语音识别在哪里跑：\n"
            "本地：本机显卡加载 Qwen3-ASR-1.7B（默认，需要显存）\n"
            "云端：音频上传到 SiliconFlow，本地只跑 0.6B 对齐器（省显存，需要 API Key）\n"
            "两种模式的字级时间戳都由本地对齐器产出，精度一致。"
        )
        self._asr_backend.currentIndexChanged.connect(self._on_asr_backend_changed)
        self._fit_combo(self._asr_backend, 120, 150)
        tb2.addWidget(self._asr_backend)

        tb2.addSeparator()
        tb2.addWidget(QLabel("对齐模式", self))
        self._align_backend = ComboBox(self)
        self._align_backend.addItem("🗣️ 口语 / 播客 (Qwen3)", userData="qwen")
        self._align_backend.addItem("🎵 歌曲 / 歌词 (MMS-FA)", userData="mms")
        self._align_backend.setToolTip("选择强制对齐引擎：Qwen3-Aligner 适合口语对话；MMS-FA 适合歌曲长拖音及多语言混杂歌词")
        self._align_backend.currentIndexChanged.connect(self._on_align_backend_changed)
        self._fit_combo(self._align_backend, 120, 160)
        tb2.addWidget(self._align_backend)

        tb2.addSeparator()
        self._btn_theme = PushButton(self)
        self._btn_theme.setText("主题")
        self._btn_theme.setToolTip("切换浅色/深色 (Ctrl+Shift+T)")
        self._btn_theme.clicked.connect(self._on_toggle_theme)
        tb2.addWidget(self._btn_theme)
        self._btn_settings = PushButton(self)
        self._btn_settings.setText("设置")
        self._btn_settings.setToolTip("打开偏好设置")
        self._btn_settings.clicked.connect(self._on_open_settings)
        tb2.addWidget(self._btn_settings)

        self._update_toolbar_icons(isDarkTheme())

    # qfluentwidgets ``ComboBox`` 的**实测**布局常量（headless 下用 ``grab()``
    # 逐列扫描暗像素标定，2026-10-04）：
    #
    #   * 文字墨迹**左对齐**，起点恒为 ``_COMBO_TEXT_LEFT``（12px），且**不随控件
    #     宽度变化**——所以文字不会被「居中」到箭头底下。
    #   * 文字可用区右边界恒为 ``width - _COMBO_TEXT_RIGHT``（34px），而箭头
    #     画在 ``width-22``（见 combo_box.py::paintEvent），二者天然留 12px
    #     间隙——**箭头本就不会压住文字**。
    #   * 因此完整显示一项文字需要 ``advance + 12 + 34``。差这几像素不会
    #     「被箭头遮挡」，而是 Qt **静默裁掉末字**——这才是用户看到的
    #     「显示不全」：「自动检测」advance=56、需 102px，原来只给 96px。
    _COMBO_TEXT_LEFT = 12
    _COMBO_TEXT_RIGHT = 34
    _COMBO_CHROME = _COMBO_TEXT_LEFT + _COMBO_TEXT_RIGHT   # 46

    @classmethod
    def _fit_combo(cls, combo: ComboBox, min_w: int, max_w: int) -> None:
        """按**实测绘制几何**给下拉定宽，保证最长一项完整显示。

        这里踩过两次坑，结论都写在下面，别再改回去：

        1. **不能拍脑袋定死宽**（更早一版夹 120~150px）：实测「☁️ 云端 ·
           SiliconFlow」``advance=252``，加 46px 装饰共需 298px，150px
           必然裁掉末字。
        2. **不要试图给箭头「让位」**：``ComboBox.paintEvent`` 只画箭头，文字
           由 ``QPushButton.paintEvent`` 绘制且**左对齐**、起点恒为 12px，
           文字区右边界恒为 ``width-34``，比箭头起点 ``width-22`` 还靠左
           12px——**箭头压不到文字**。真实症状是「宽度不够 → 末字被静默
           裁掉」，加padding 只会让文字更早被裁。

        ⚠️ 曾尝试 ``setTextMargins`` 避让箭头，**无效**：
        ``QWidget.setTextMargins`` 在 **PySide6 里未导出**（与
        ``QWidget.moveCenter`` 同一类问题）；改用 QSS padding 实测墨迹仅从
        ``12..63`` 移到 ``13..64``，因为原始 QSS 本身已有内边距。

        宽度公式：``最长项 advance + 46``，定点（min==max）。第 2 行独占
        整行，实测最宽处仍有余量。
        """
        fm = combo.fontMetrics()
        need_text = 0
        for i in range(combo.count()):
            need_text = max(need_text, fm.horizontalAdvance(combo.itemText(i)))
        want = need_text + cls._COMBO_CHROME
        # 上限仅在「文字本身就超长」时放弃——此时截断文字比撑宽更糟。
        width = want if want > max_w else max(min_w, want)
        combo.setMinimumWidth(width)
        combo.setMaximumWidth(width)
        combo.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )

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

            # 识别后端（本地 / 云端）持久化在 prefs.asr.asr_backend
            asr_backend = (getattr(prefs.asr, "asr_backend", "local") or "local")
            c_idx = self._asr_backend.findData(asr_backend)
            if c_idx >= 0:
                self._asr_backend.setCurrentIndex(c_idx)

            backend = (prefs.align.align_backend or "qwen")
            b_idx = self._align_backend.findData(backend)
            if b_idx >= 0:
                self._align_backend.setCurrentIndex(b_idx)
        except Exception:
            logger.debug("[偏好] 加载工具栏偏好失败")


    def _on_asr_backend_changed(self, _idx: int) -> None:
        """识别后端切换（本地 / 云端）。持久化到 prefs.asr.asr_backend。

        这里**不**做任何 Key 校验：工具栏只负责选择，缺 Key 会在真正开始识别前
        由 workflow_controller 拦截并给出引导（那时用户才知道去哪里填）。
        """
        backend = self._asr_backend.currentData() or "local"
        try:
            from core.app_config import load_preferences, save_preferences
            prefs = load_preferences()
            prefs.asr.asr_backend = backend
            save_preferences(prefs)
        except Exception:
            logger.debug("[偏好] 保存识别后端失败")

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
        # 与父窗口中心对齐（2026-10-04）。不居中时对话框跟随父窗口几何，
        # 父窗口非最大化且偏上时对话框会跑出屏幕上边缘，标题栏够不到 → 整个窗口拖不动，
        # 只能关掉、把主窗口最大化再重开。
        #
        # 注意：PySide6 的 QWidget/QDialog **没有** moveCenter()（那是 C++ QWidget 的方法，
        # PySide6 未导出；上一版照C++ 写法直接调会抛 AttributeError，对话框根本弹不出来）。
        # 正确做法是拿 frameGeometry() 调 QRect.moveCenter()，再把结果topLeft 交给 move()——
        # move() 收的是frame 左上角，正好和 frameGeometry 配套（直接用 geometry() 会差一个标题栏高度）。
        frame = dlg.frameGeometry()
        frame.moveCenter(self.frameGeometry().center())
        dlg.move(frame.topLeft())
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


    def _on_act_extract_vocals(self) -> None:
        """工具栏/菜单「人声提取」的动作代理（真正的活在 ProjectController）。"""
        self.project_ctrl.extract_vocals_now()


    def _on_act_align_dirty(self) -> None:
        self.workflow.start_align_dirty()


    def _on_act_align_full(self) -> None:
        self.workflow.start_align_full()


    def _on_act_align_sel(self) -> None:
        self.workflow.start_align_selected()


    def _on_realign_single_sentence(self, idx: int) -> None:
        self.workflow.realign_single_sentence(idx)


