"""ui.main_window.window — MainWindow 主体（布局 + 生命周期）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QMainWindow, QMessageBox, QVBoxLayout, QWidget

from subs.models import SubtitleProject
from core.model_manager import ModelManager
from ui.player_panel import PlayerPanel
from ui.widgets import GripSplitter
from ui.waveform_view import WaveformView
from ui.subs_editor import SubsEditor
from ui.export_panel import ExportPanel
from ui.workflow_controller import WorkflowController
from ui.project_controller import ProjectController

from .chrome import ChromeMixin
from .navigation import NavigationMixin
from .editing import EditingMixin
from .player_focus import PlayerFocusMixin

logger = logging.getLogger("ui.main_window")


class MainWindow(
    ChromeMixin,
    NavigationMixin,
    EditingMixin,
    PlayerFocusMixin,
    QMainWindow,
):
    """主窗口：壳层 + 导航 + 编辑 mixin 组合。"""

    def __init__(self, *, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("main_window")
        self.setWindowTitle("Qwen3 Subtitle Studio — 本地字幕生成与编辑")
        self.resize(1500, 920)
        self.setMinimumSize(1360, 760)
        self.setAcceptDrops(True)

        self._model_manager = ModelManager()
        self._project: Optional[SubtitleProject] = None
        self._sentence_mode_x_range: Optional[tuple[float, float]] = None
        # 导出路径记忆：目录跨会话持久化（prefs.export.default_dir 仅作无媒体时兜底；
        # 载入媒体后 _apply_project 会以媒体目录覆盖）；文件名仅会话内记忆。
        self._last_export_dir: Optional[str] = None
        self._last_export_stem: Optional[str] = None
        try:
            from core.app_config import load_preferences
            self._last_export_dir = load_preferences().export.default_dir or None
        except Exception:
            logger.debug("[偏好] 读取导出目录记忆失败")

        self._undo_stack = QUndoStack(self)
        self._undo_stack.setUndoLimit(100)
        self._project_path: Optional[Path] = None
        self._project_modified: bool = False  # 非 Undo 操作（ASR/对齐/导入/重关联）
        self._closing_accepted: bool = False
        self._undo_stack.cleanChanged.connect(self._on_undo_clean_changed)

        # 辅助控制器
        self.workflow = WorkflowController(self)
        self.project_ctrl = ProjectController(self)

        self._build_central()
        self._build_menubar()
        self._build_statusbar()
        self._build_toolbar()
        self._setup_player_focus_mode()
        self._wire_panel_interconnects()
        self._setup_space_shortcut()
        # 恢复工具栏「识别语言 / 对齐后端」上次选择
        self._load_toolbar_prefs()

        from ui.themes import apply_theme, is_dark
        if hasattr(self, "waveform"):
            self.waveform.set_theme(is_dark())

        # 全部控件构建完成后重新应用一次当前主题：应用级 QSS 若早于控件创建装上，
        # 首次 polish 的几何（工具栏高度/按钮 padding）与后续 re-polish 不一致，
        # 表现为「第一次切换主题时布局跳一下」。此处主动补一次 polish，
        # 首帧即进入稳定几何，之后任意次主题切换布局零变化。
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance()
        if _app is not None:
            apply_theme(_app, is_dark())


    @property
    def _running_worker(self):
        return self.workflow.running_worker


    def _build_central(self) -> None:
        root = QWidget(self)
        root.setObjectName("central_root")
        self.setCentralWidget(root)
        root_lay = QVBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # 最外层水平分割：左侧内容区 | 右侧导出面板（常驻）
        # GripSplitter 自绘握把把手（常驻三段式指示 + hover/pressed 高亮）——
        # 纯 QSS 提亮方案在部分平台样式引擎下把手颜色/尺寸会被忽略，
        # 深色模式下分界几乎不可见（用户二次反馈）。6px 宽度在 GripSplitter 构造器内设定。
        main_h_split = GripSplitter(Qt.Horizontal, self)
        main_h_split.setChildrenCollapsible(False)
        self._main_h_split = main_h_split

        # 左侧：垂直分割（上=播放器+编辑器，下=波形）
        left_v_split = GripSplitter(Qt.Vertical, main_h_split)
        left_v_split.setChildrenCollapsible(False)
        self._left_v_split = left_v_split

        top_split = GripSplitter(Qt.Horizontal, left_v_split)
        top_split.setChildrenCollapsible(False)
        self._top_split = top_split

        self.player = PlayerPanel(top_split)
        self.editor = SubsEditor(top_split)
        self.player.setMinimumWidth(470)
        # 编辑器最小宽度以句级工具条自动计算值为准（低于它按钮文字会被裁），
        # 620 仅作无子约束时的下限兜底
        self.editor.setMinimumWidth(max(620, self.editor.minimumSizeHint().width()))
        top_split.addWidget(self.player)
        top_split.addWidget(self.editor)
        top_split.setStretchFactor(0, 3)
        top_split.setStretchFactor(1, 5)
        top_split.setSizes([540, 800])

        self.waveform = WaveformView(left_v_split)
        left_v_split.addWidget(top_split)
        left_v_split.addWidget(self.waveform)
        left_v_split.setStretchFactor(0, 5)
        left_v_split.setStretchFactor(1, 2)
        left_v_split.setSizes([680, 240])

        # 右侧：导出面板（常驻，不可拆卸/隐藏）
        self._export_panel = ExportPanel(self._on_export_requested, self)
        self._export_panel.setMinimumWidth(220)

        main_h_split.addWidget(left_v_split)
        main_h_split.addWidget(self._export_panel)
        main_h_split.setStretchFactor(0, 1)   # 左侧内容区占主要宽度
        main_h_split.setStretchFactor(1, 0)   # 导出面板保持自身 sizeHint
        main_h_split.setSizes([1100, 286])

        root_lay.addWidget(main_h_split, 1)


    @property
    def has_unsaved_changes(self) -> bool:
        if self._project is None or self._closing_accepted:
            return False
        try:
            undo_dirty = not self._undo_stack.isClean()
        except RuntimeError:
            # Qt 子对象析构期间 QUndoStack C++ 实例可能已先被删除；此阶段不得
            # 再把析构信号当成真实工程变化（Windows/PySide6 实测回归）。
            undo_dirty = False
        return self._project_modified or undo_dirty

    def _on_undo_clean_changed(self, clean: bool) -> None:
        """使用信号自带的 clean 值，避免析构期重新访问已删除 QUndoStack。"""
        if self._closing_accepted:
            return
        self._update_window_title(undo_clean=bool(clean))

    def _mark_project_modified(self) -> None:
        self._project_modified = True
        self._update_window_title()

    def _mark_project_saved(self, path: Path | None = None) -> None:
        if path is not None:
            self._project_path = Path(path)
        self._project_modified = False
        self._undo_stack.setClean()
        self._update_window_title(undo_clean=True)

    def _reset_project_file_state(self, *, path: Path | None = None, modified: bool = False) -> None:
        self._project_path = Path(path) if path is not None else None
        self._project_modified = bool(modified)
        self._undo_stack.clear()
        self._undo_stack.setClean()
        self._update_window_title(undo_clean=True)

    def _update_window_title(self, *, undo_clean: bool | None = None) -> None:
        if self._closing_accepted:
            return
        label = "Qwen3 Subtitle Studio — 本地字幕生成与编辑"
        if self._project_path is not None:
            label += f" — {self._project_path.name}"
        elif self._project and self._project.source_media_path:
            label += f" — {Path(self._project.source_media_path).name}"
        if undo_clean is None:
            try:
                undo_clean = self._undo_stack.isClean()
            except RuntimeError:
                undo_clean = True
        if self._project is not None and (self._project_modified or not undo_clean):
            label += " *"
        self.setWindowTitle(label)

    def _create_unsaved_dialog(self, reason: str):
        """创建中文未保存对话框；不用平台默认英文 StandardButton 文案。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("工程尚未保存")
        box.setText(f"当前工程有尚未保存的改动。\n\n{reason}\n\n请选择如何处理：")
        save_button = box.addButton("保存工程", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("不保存", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        return box, save_button, discard_button, cancel_button

    def _ask_unsaved_changes(self, reason: str) -> str:
        box, save_button, discard_button, _cancel_button = self._create_unsaved_dialog(reason)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return "save"
        if clicked is discard_button:
            return "discard"
        return "cancel"

    def _maybe_save_changes(self, reason: str) -> bool:
        """破坏性操作前统一中文“保存工程 / 不保存 / 取消”门禁。"""
        if not self.has_unsaved_changes:
            return True
        decision = self._ask_unsaved_changes(reason)
        if decision == "save":
            return bool(self.project_ctrl.save_project_dialog())
        if decision == "discard":
            return True
        return False

    @property
    def model_manager(self) -> ModelManager:
        return self._model_manager


    @property
    def current_project(self) -> Optional[SubtitleProject]:
        return self._project


    def _ask_cancel_running_task(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("任务仍在运行")
        box.setText(
            "当前任务仍在运行。是否请求在下一个安全点取消，并在任务收尾后退出？\n\n"
            "模型单次前向不能安全强杀，因此退出可能需要等待当前前向完成。"
        )
        cancel_task = box.addButton("取消任务并退出", QMessageBox.ButtonRole.AcceptRole)
        keep_waiting = box.addButton("继续等待", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_waiting)
        box.exec()
        return box.clickedButton() is cancel_task

    def closeEvent(self, event) -> None:
        """忙碌时延后关闭；空闲时先过未保存门禁，再释放模型和缓存。"""
        if self.workflow.is_busy():
            if self._ask_cancel_running_task():
                self.workflow.shutdown()
            event.ignore()
            return

        if not self._maybe_save_changes("关闭应用将丢弃这些改动。"):
            event.ignore()
            return

        self._closing_accepted = True
        try:
            self._undo_stack.cleanChanged.disconnect(self._on_undo_clean_changed)
        except (RuntimeError, TypeError):
            pass

        try:
            self._model_manager.cleanup()
        except Exception:
            logger.debug("[MainWindow] 模型卸载异常")
        try:
            self.player.shutdown_mpv()
        except Exception:
            logger.debug("[MainWindow] mpv 释放异常")
        try:
            from core.temp_cleanup import shutdown_cleanup
            shutdown_cleanup()
        except Exception:
            pass
        super().closeEvent(event)



__all__ = ["MainWindow"]
