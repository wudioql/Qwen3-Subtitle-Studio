"""ui.waveform_view.widget — WaveformView 主组件。"""
from __future__ import annotations

import copy
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QVBoxLayout, QWidget

from core.constants import ALIGN_SEAM_SNAP_MAX
from subs.models import SubtitleProject, WordTimestamp

from .theme import (
    WaveformTheme,
    _DARK_THEME,
    _LIGHT_THEME,
    _LOD_MAX_POINTS,
    _MAX_DOWNSAMPLE,
)
from .viewbox import _WaveViewBox

logger = logging.getLogger("ui.waveform_view")

class WaveformView(QWidget):
    """音频波形组件：支持句级/字级双层视图、手柄拖动、动态 LOD 与深浅色主题。"""

    playhead_seeked = Signal(float)                        # 用户拖动播放头
    click_seeked = Signal(float)                           # 用户单击空白处
    boundary_dragged = Signal(int, str, float)             # 句级手柄拖动中
    boundary_drag_finished = Signal(int, str, float)       # 句级手柄拖动完成
    word_boundary_dragged = Signal(int, list)              # sent_idx, preview_words（不写模型）
    word_boundary_drag_finished = Signal(int, list, list)  # sent_idx, old_words, new_words
    region_changed = Signal(float, float)                  # 选段变更
    view_range_changed = Signal(float, float)              # 视野范围改变 (lo, hi)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("waveform_view")
        self._project: Optional[SubtitleProject] = None
        self._raw_audio: Optional[np.ndarray] = None
        self._sr: int = 16000
        self._duration_s: float = 0.0

        # 主题管理
        self._is_dark: bool = True
        self._theme: WaveformTheme = _DARK_THEME

        # 句级渲染缓存
        self._blocks: Dict[int, pg.LinearRegionItem] = {}
        self._labels: Dict[int, pg.TextItem] = {}
        self._handles: Dict[int, Tuple[pg.InfiniteLine, pg.InfiniteLine]] = {}
        # 近重合句界仍是两条独立边界；拖动时按方向决定真正目标。
        self._boundary_drag_source: Optional[Tuple[int, str]] = None
        self._boundary_drag_target: Optional[Tuple[int, str, float]] = None

        # 活跃句与字级渲染缓存
        self._active_idx: int = -1
        self._word_blocks: List[pg.LinearRegionItem] = []
        self._word_labels: List[pg.TextItem] = []
        self._word_handles: List[pg.InfiniteLine] = []
        self._drag_start_words: List[WordTimestamp] = []
        self._preview_words: List[WordTimestamp] = []
        self._is_high_res_lod: bool = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # ── PlotWidget ──
        self._vb = _WaveViewBox()
        plot_item = pg.PlotItem(viewBox=self._vb)
        plot_item.setMenuEnabled(False)
        if getattr(plot_item, "autoBtn", None) is not None:
            try:
                plot_item.autoBtn.clicked.disconnect()
            except Exception:
                pass
            plot_item.autoBtn.clicked.connect(self._on_auto_btn)
        self._plot_item = plot_item
        self.plot = pg.PlotWidget(parent=self, plotItem=plot_item)
        self.plot.setLabel("bottom", "时间", units="s")
        self.plot.setLabel("left", "振幅")
        self._vb.click_seeked.connect(self._on_click_seek)
        self._vb.after_auto_range.connect(self._autoscale_y)
        self._vb.sigXRangeChanged.connect(self._on_x_range_changed)

        # 波形曲线
        self.curve = pg.PlotCurveItem(pen=pg.mkPen(color=self._theme.curve_color, width=1))
        self.plot.addItem(self.curve)

        # 播放游标
        self.playhead = pg.InfiniteLine(
            pos=0.0, angle=90,
            pen=pg.mkPen(color=self._theme.playhead_color, width=2),
            movable=True,
        )
        self.playhead.sigPositionChangeFinished.connect(
            lambda _: self.playhead_seeked.emit(float(self.playhead.value())))
        self.plot.addItem(self.playhead)

        # 区域选段
        self.region = pg.LinearRegionItem(
            [0.0, 0.0],
            brush=pg.mkBrush(self._theme.region_color),
            pen=pg.mkPen(color="#D85A30", width=1, style=Qt.PenStyle.DashLine),
        )
        self.region.hide()
        self.region.sigRegionChangeFinished.connect(
            lambda rgn: self.region_changed.emit(*[float(v) for v in rgn.getRegion()]))
        self.plot.addItem(self.region)

        self._vb.setMouseEnabled(x=True, y=True)
        self._vb.setMenuEnabled(False)
        self._vb.setAspectLocked(False)

        root.addWidget(self.plot, 1)
        self._apply_theme_visuals()

    # ── 主题适配 ───────────────────────────────────────
    def set_theme(self, dark: bool) -> None:
        """根据深/浅色模式切换波形画布、网格、曲线与手柄的高品质配色。"""
        self._is_dark = dark
        self._theme = _DARK_THEME if dark else _LIGHT_THEME
        self._apply_theme_visuals()
        # 刷新所有句级色块与标签
        if self._project is not None:
            self.update_blocks_in_place(self._project)

    def _apply_theme_visuals(self) -> None:
        t = self._theme
        self.plot.setBackground(QColor(t.plot_bg))
        self.curve.setPen(pg.mkPen(color=t.curve_color, width=1))
        self.playhead.setPen(pg.mkPen(color=t.playhead_color, width=2))
        self.region.setBrush(pg.mkBrush(t.region_color))

        # 坐标轴与网格线
        axis_b = self.plot.getAxis("bottom")
        axis_l = self.plot.getAxis("left")
        axis_b.setPen(pg.mkPen(color=t.axis_color, width=1))
        axis_b.setTextPen(pg.mkPen(color=t.axis_color))
        axis_l.setPen(pg.mkPen(color=t.axis_color, width=1))
        axis_l.setTextPen(pg.mkPen(color=t.axis_color))
        self.plot.showGrid(x=True, y=True, alpha=t.grid_alpha)

    def _sentence_brush(self, sentence, *, active: bool) -> QColor:
        """句级块按 locked > dirty > confirmed 显示；确认后视觉必须立即变化。"""
        t = self._theme
        if getattr(sentence, "is_locked", False):
            return t.locked_active_block_bg if active else t.locked_block_bg
        if getattr(sentence, "is_dirty", False):
            return t.dirty_active_block_bg if active else t.dirty_block_bg
        return t.active_block_bg if active else t.block_bg

    @staticmethod
    def _sentence_label_text(idx: int, sentence) -> str:
        marker = "🔒" if getattr(sentence, "is_locked", False) else (
            "●" if getattr(sentence, "is_dirty", False) else "✓"
        )
        return f"{marker} S{idx + 1}: {(sentence.text or '')[:8]}"

    def _coincident_boundary_pair(
        self,
        idx: int,
        edge: str,
    ) -> Optional[Tuple[int, int, float]]:
        """返回当前手柄所属的近重合相邻句界，以及该手柄的拖动原点。

        ≤25ms 只用于解决屏幕上两条线难以分别命中的交互歧义；数据层仍保留
        前句 end 与后句 start 两个独立值，绝不把它们合并成共享边界。
        """
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return None
        sentences = self._project.sentences
        if edge == "start" and idx > 0:
            previous_idx, following_idx = idx - 1, idx
        elif edge == "end" and idx + 1 < len(sentences):
            previous_idx, following_idx = idx, idx + 1
        else:
            return None

        previous = sentences[previous_idx]
        following = sentences[following_idx]
        if abs(float(previous.end_time) - float(following.start_time)) > ALIGN_SEAM_SNAP_MAX:
            return None
        origin = float(sentences[idx].start_time if edge == "start" else sentences[idx].end_time)
        return previous_idx, following_idx, origin

    def resolve_boundary_drag_target(
        self,
        idx: int,
        edge: str,
        new_time: float,
    ) -> Tuple[int, str]:
        """把近重合句界的鼠标方向解释为一个独立边界目标。

        无论鼠标实际命中前句 end 还是后句 start：向左拖只改前句尾，向右拖
        只改后句首。两条线一旦拉开，后续就按用户直接命中的普通句界处理。
        """
        pair = self._coincident_boundary_pair(idx, edge)
        if pair is None:
            return idx, edge
        previous_idx, following_idx, origin = pair
        delta = float(new_time) - origin
        if delta < -1e-9:
            return previous_idx, "end"
        if delta > 1e-9:
            return following_idx, "start"
        return idx, edge

    def _set_sentence_handle_value(self, idx: int, edge: str, value: float) -> None:
        if idx not in self._handles:
            return
        handle = self._handles[idx][0 if edge == "start" else 1]
        handle.blockSignals(True)
        handle.setValue(float(value))
        handle.blockSignals(False)

    def _on_sentence_handle_dragged(
        self,
        line: pg.InfiniteLine,
        idx: int,
        edge: str,
    ) -> None:
        raw_time = float(line.value())
        target_idx, target_edge = idx, edge
        target_time = raw_time
        pair = None
        # setValue() 的程序刷新也会发 sigPositionChanged；只有真实鼠标拖动
        # （InfiniteLine.moving=True）才启用方向判定和双线预览。
        if bool(getattr(line, "moving", False)):
            pair = self._coincident_boundary_pair(idx, edge)
            target_idx, target_edge = self.resolve_boundary_drag_target(idx, edge, raw_time)
            if pair is not None and self._project is not None:
                previous_idx, following_idx, origin = pair
                delta = raw_time - origin
                if (target_idx, target_edge) == (previous_idx, "end"):
                    target_time = float(self._project.sentences[previous_idx].end_time) + delta
                elif (target_idx, target_edge) == (following_idx, "start"):
                    target_time = float(self._project.sentences[following_idx].start_time) + delta
            self._boundary_drag_source = (idx, edge)
            self._boundary_drag_target = (target_idx, target_edge, target_time)

        if pair is not None and self._project is not None:
            previous_idx, following_idx, _origin = pair
            previous = self._project.sentences[previous_idx]
            following = self._project.sentences[following_idx]
            # 实时预览也必须呈现最终语义：向左时后句首留在原位，向右时
            # 前句尾留在原位。blockSignals 防止另一条线递归进入本槽。
            previous_end = (
                target_time
                if (target_idx, target_edge) == (previous_idx, "end")
                else float(previous.end_time)
            )
            following_start = (
                target_time
                if (target_idx, target_edge) == (following_idx, "start")
                else float(following.start_time)
            )
            self._set_sentence_handle_value(previous_idx, "end", previous_end)
            self._set_sentence_handle_value(following_idx, "start", following_start)

        self.boundary_dragged.emit(target_idx, target_edge, target_time)

    def _on_sentence_handle_drag_finished(
        self,
        line: pg.InfiniteLine,
        idx: int,
        edge: str,
    ) -> None:
        target = None
        if self._boundary_drag_source == (idx, edge):
            target = self._boundary_drag_target
        self._boundary_drag_source = None
        self._boundary_drag_target = None
        if target is None:
            target = (idx, edge, float(line.value()))
        target_idx, target_edge, target_time = target
        self.boundary_drag_finished.emit(target_idx, target_edge, target_time)

    # ── 交互槽函数 ────────────────────────────────────
    def _on_click_seek(self, t: float) -> None:
        t = max(0.0, min(t, self._duration_s if self._duration_s > 0 else t))
        self.playhead_seeked.emit(float(t))

    def _on_auto_btn(self) -> None:
        if self._duration_s > 0:
            self.plot.setXRange(0.0, self._duration_s, padding=0)
        self._autoscale_y()

    def _on_x_range_changed(self, _, rng) -> None:
        lo, hi = float(rng[0]), float(rng[1])
        self.view_range_changed.emit(lo, hi)
        self._update_lod(lo, hi)

    # ── LOD 动态高精采样 ──────────────────────────────
    def _update_lod(self, lo: float, hi: float) -> None:
        if self._raw_audio is None or self._duration_s <= 0:
            return

        dur = hi - lo
        if dur <= 15.0:
            s_idx = max(0, int(lo * self._sr))
            e_idx = min(len(self._raw_audio), int(hi * self._sr))
            if e_idx <= s_idx:
                return
            n_samples = e_idx - s_idx
            if n_samples <= _LOD_MAX_POINTS:
                sub_y = self._raw_audio[s_idx:e_idx]
                sub_x = np.linspace(s_idx / self._sr, e_idx / self._sr, len(sub_y))
            else:
                step = max(1, n_samples // _LOD_MAX_POINTS)
                sub_y = self._raw_audio[s_idx:e_idx:step]
                sub_x = np.linspace(s_idx / self._sr, e_idx / self._sr, len(sub_y))
            self.curve.setData(sub_x, sub_y)
            self._is_high_res_lod = True
        else:
            if self._is_high_res_lod:
                self._draw_global_waveform()
                self._is_high_res_lod = False

    def _draw_global_waveform(self) -> None:
        if self._raw_audio is None:
            self.curve.setData([], [])
            return
        total_n = len(self._raw_audio)
        if total_n <= _MAX_DOWNSAMPLE:
            y = self._raw_audio
        else:
            step = max(1, total_n // _MAX_DOWNSAMPLE)
            y = self._raw_audio[::step]
        x = np.linspace(0.0, self._duration_s, len(y))
        self.curve.setData(x, y)

    # ── 音频载入 ──────────────────────────────────────
    def set_audio(self, audio_np: Optional[np.ndarray], sr: int = 16000) -> None:
        self._raw_audio = audio_np
        self._sr = int(sr)
        if audio_np is None or len(audio_np) == 0:
            self._duration_s = 0.0
            self.curve.setData([], [])
            return

        self._duration_s = float(len(audio_np) / self._sr)
        self._draw_global_waveform()
        self.plot.setXRange(0.0, self._duration_s, padding=0)
        self._autoscale_y()

    def _autoscale_y(self) -> None:
        if self._raw_audio is None or len(self._raw_audio) == 0:
            self._vb.setYRange(-1.3, 1.3, padding=0)
            return

        vmax = float(np.max(np.abs(self._raw_audio)))
        lo = -vmax * 1.15
        span = vmax * 2.30
        label_pad_px = 28.0
        try:
            _, dy = self._vb.viewPixelSize()
            label_pad_y = label_pad_px * dy
        except Exception:
            label_pad_y = span * 0.35
        hi = max(vmax + span * 0.25, 0.95 + label_pad_y)
        lo = min(lo, -1.3)
        hi = max(hi, 1.3)
        self._vb.setYRange(lo, hi, padding=0)

    # ── 项目与句级色块 ────────────────────────────────
    def set_project(self, project: SubtitleProject) -> None:
        self._project = project
        self._clear_blocks()
        self._clear_word_visuals()
        self._active_idx = -1
        if project is None or not project.sentences:
            return
        if project.media_duration > 0:
            self._duration_s = float(project.media_duration)
        for idx, sent in enumerate(project.sentences):
            self._add_sentence_visuals(idx, sent)

    def update_blocks_in_place(self, project: SubtitleProject) -> None:
        if project is None:
            return
        n = len(project.sentences)
        if n != len(self._blocks):
            self.set_project(project)
            return
        self._project = project
        t = self._theme
        for idx, sent in enumerate(project.sentences):
            if idx in self._blocks:
                self._blocks[idx].setRegion((sent.start_time, sent.end_time))
                brush = self._sentence_brush(sent, active=(idx == self._active_idx))
                self._blocks[idx].setBrush(pg.mkBrush(brush))
            if idx in self._handles:
                h_start, h_end = self._handles[idx]
                h_start.setValue(sent.start_time)
                h_end.setValue(sent.end_time)
                h_start.setPen(pg.mkPen(color=t.handle_color, width=2))
                h_end.setPen(pg.mkPen(color=t.handle_color, width=2))
            if idx in self._labels:
                self._labels[idx].setText(self._sentence_label_text(idx, sent))
                self._labels[idx].setColor(t.sentence_text_color)
                self._labels[idx].setPos(sent.start_time, 0.95)

        if 0 <= self._active_idx < len(project.sentences):
            self._build_word_visuals(self._active_idx, project.sentences[self._active_idx])

    def update_word_overlay(self, sent_idx: Optional[int] = None) -> None:
        target = self._active_idx if sent_idx is None else sent_idx
        if self._project is not None and 0 <= target < len(self._project.sentences):
            self._active_idx = target
            self._build_word_visuals(target, self._project.sentences[target])

    def set_active_sentence(self, idx: int, *, force: bool = False) -> None:
        if not force and self._active_idx == idx and self._word_handles:
            return
        if self._active_idx in self._blocks and self._project is not None:
            previous = self._project.sentences[self._active_idx]
            self._blocks[self._active_idx].setBrush(
                pg.mkBrush(self._sentence_brush(previous, active=False))
            )
        self._active_idx = idx
        self._clear_word_visuals()

        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return

        sent = self._project.sentences[idx]
        if idx in self._blocks:
            self._blocks[idx].setBrush(
                pg.mkBrush(self._sentence_brush(sent, active=True))
            )
        if sent.words:
            self._build_word_visuals(idx, sent)

    @property
    def active_sentence_index(self) -> int:
        """当前波形激活的句子索引（无激活为 -1），供外部做「是否同句」判断。"""
        return self._active_idx

    def refresh_sentence_visuals(self, idx: int, sent) -> None:
        """原地刷新单句的句级图元：色块区间 + 起止手柄 + 标签文本（不动主题笔刷与视野）。

        供外部在「单句轻量编辑后」调用（行数未变的原位更新）；行数变化请走
        update_blocks_in_place() 全量路径。
        """
        if idx in self._blocks:
            self._blocks[idx].setRegion((sent.start_time, sent.end_time))
            self._blocks[idx].setBrush(
                pg.mkBrush(self._sentence_brush(sent, active=(idx == self._active_idx)))
            )
        if idx in self._handles:
            h_start, h_end = self._handles[idx]
            h_start.setValue(sent.start_time)
            h_end.setValue(sent.end_time)
        if idx in self._labels:
            self._labels[idx].setText(self._sentence_label_text(idx, sent))
            self._labels[idx].setPos(sent.start_time, 0.95)

    def get_view_range(self) -> Tuple[float, float]:
        view = self._vb.viewRange()[0]
        return float(view[0]), float(view[1])

    def set_view_range(self, lo: float, hi: float) -> None:
        if lo >= hi:
            return
        self.plot.setXRange(float(lo), float(hi), padding=0)

    def focus_on_sentence(self, idx: int, *, target_ratio: float = 0.80) -> None:
        if self._project is None or not (0 <= idx < len(self._project.sentences)):
            return
        s = self._project.sentences[idx]
        dur = max(0.2, float(s.end_time - s.start_time))
        ratio = max(0.1, min(0.95, float(target_ratio)))
        span = dur / ratio
        margin = (span - dur) / 2.0
        lo = max(0.0, float(s.start_time - margin))
        hi = lo + span
        if self._duration_s > 0 and hi > self._duration_s:
            hi = self._duration_s
            lo = max(0.0, hi - span)
        self.plot.setXRange(lo, hi, padding=0)

    def set_playhead(self, t: float) -> None:
        self.playhead.setValue(float(t))

    def follow_playhead(self, t: float, *, left_ratio: float = 0.20) -> None:
        """光标越出当前视野时平移视窗，使光标落在视野左侧 left_ratio 处。

        视野跨度（用户缩放）保持不变；光标仍在视野内时不动，
        不与用户手动平移/缩放抢方向盘。句级/字级模式通用。
        """
        t = float(t)
        lo, hi = self._vb.viewRange()[0]
        span = hi - lo
        if span <= 0:
            return
        if lo <= t <= hi:
            return
        new_lo = t - span * float(left_ratio)
        if self._duration_s > 0:
            new_lo = min(new_lo, self._duration_s - span)
        new_lo = max(0.0, new_lo)
        self.plot.setXRange(new_lo, new_lo + span, padding=0)

    def set_region(self, start: float, end: float, visible: bool = True) -> None:
        if visible:
            self.region.setRegion((float(start), float(end)))
            self.region.show()
        else:
            self.region.hide()

    # ── 句级图元构建 ────────────────────────────────────
    def _clear_blocks(self) -> None:
        for block in self._blocks.values():
            self.plot.removeItem(block)
        for label in self._labels.values():
            self.plot.removeItem(label)
        for h_start, h_end in self._handles.values():
            self.plot.removeItem(h_start)
            self.plot.removeItem(h_end)
        self._blocks.clear()
        self._labels.clear()
        self._handles.clear()
        self._boundary_drag_source = None
        self._boundary_drag_target = None

    def _add_sentence_visuals(self, idx: int, sent) -> None:
        t = self._theme
        brush = self._sentence_brush(sent, active=(idx == self._active_idx))
        block = pg.LinearRegionItem(
            values=(sent.start_time, sent.end_time),
            brush=pg.mkBrush(brush),
            pen=pg.mkPen(color=(0, 0, 0, 0), width=0),
            movable=False,
        )
        block.setZValue(-10)
        self.plot.addItem(block)
        self._blocks[idx] = block

        label = pg.TextItem(
            text=self._sentence_label_text(idx, sent),
            color=t.sentence_text_color,
            anchor=(0, 1),
        )
        label.setPos(sent.start_time, 0.95)
        label.setZValue(0)
        self.plot.addItem(label)
        self._labels[idx] = label

        h_start = pg.InfiniteLine(
            pos=sent.start_time, angle=90,
            pen=pg.mkPen(color=t.handle_color, width=2),
            movable=True,
        )
        h_end = pg.InfiniteLine(
            pos=sent.end_time, angle=90,
            pen=pg.mkPen(color=t.handle_color, width=2),
            movable=True,
        )
        h_start.sigPositionChanged.connect(
            lambda il, i=idx: self._on_sentence_handle_dragged(il, i, "start"))
        h_start.sigPositionChangeFinished.connect(
            lambda il, i=idx: self._on_sentence_handle_drag_finished(il, i, "start"))
        h_end.sigPositionChanged.connect(
            lambda il, i=idx: self._on_sentence_handle_dragged(il, i, "end"))
        h_end.sigPositionChangeFinished.connect(
            lambda il, i=idx: self._on_sentence_handle_drag_finished(il, i, "end"))
        # 句级手柄同时代表首/尾 word（或标点）的外边界，层级高于内部字界。
        h_start.setZValue(30)
        h_end.setZValue(30)
        self.plot.addItem(h_start)
        self.plot.addItem(h_end)
        self._handles[idx] = (h_start, h_end)

    # ── 字级图元构建 ────────────────────────────────────
    def _clear_word_visuals(self) -> None:
        for b in self._word_blocks:
            self.plot.removeItem(b)
        for lbl in self._word_labels:
            self.plot.removeItem(lbl)
        for h in self._word_handles:
            self.plot.removeItem(h)
        self._word_blocks.clear()
        self._word_labels.clear()
        self._word_handles.clear()
        self._drag_start_words = []
        self._preview_words = []

    def _build_word_visuals(self, sent_idx: int, sent) -> None:
        self._clear_word_visuals()
        if not sent or not sent.words:
            return

        self._drag_start_words = [copy.deepcopy(w) for w in sent.words]
        self._preview_words = [copy.deepcopy(w) for w in sent.words]
        words = self._preview_words
        n_words = len(words)
        t = self._theme

        for i, w in enumerate(words):
            bg_color = t.word_bg_a if i % 2 == 0 else t.word_bg_b
            block = pg.LinearRegionItem(
                values=(w.start_time, w.end_time),
                brush=pg.mkBrush(bg_color),
                pen=pg.mkPen(color=(0, 0, 0, 0), width=0),
                movable=False,
            )
            block.setZValue(-5)
            self.plot.addItem(block)
            self._word_blocks.append(block)

            mid_t = (w.start_time + w.end_time) / 2.0
            lbl = pg.TextItem(
                text=w.text,
                color=t.word_text_color if not w.is_punct else t.punct_text_color,
                anchor=(0.5, 0.5),
            )
            lbl.setPos(mid_t, 0.65)
            lbl.setZValue(5)
            self.plot.addItem(lbl)
            self._word_labels.append(lbl)

        # 句级 start/end 手柄就是首/尾 word（或标点）的外侧边界；这里只创建
        # n-1 个内部接缝，避免同一位置叠两条可拖线造成语义和鼠标命中冲突。
        for boundary_idx in range(n_words - 1):
            split_t = words[boundary_idx].end_time
            pen = pg.mkPen(
                color=t.word_handle_color,
                width=2,
                style=Qt.PenStyle.DashLine,
            )
            handle = pg.InfiniteLine(pos=split_t, angle=90, pen=pen, movable=True)
            handle.setZValue(15)
            handle.sigPositionChanged.connect(
                lambda line, bi=boundary_idx: self._on_word_handle_dragged(
                    bi, float(line.value())
                )
            )
            handle.sigPositionChangeFinished.connect(
                lambda line, bi=boundary_idx: self._on_word_handle_drag_finished(
                    bi, float(line.value())
                )
            )
            self.plot.addItem(handle)
            self._word_handles.append(handle)

    def _set_word_handle_value(self, index: int, value: float) -> None:
        if not (0 <= index < len(self._word_handles)):
            return
        handle = self._word_handles[index]
        handle.blockSignals(True)
        handle.setValue(float(value))
        handle.blockSignals(False)

    def _refresh_word_preview_visuals(self) -> None:
        words = self._preview_words
        for index, word in enumerate(words):
            if index < len(self._word_blocks):
                self._word_blocks[index].setRegion((word.start_time, word.end_time))
            if index < len(self._word_labels):
                self._word_labels[index].setPos(
                    (word.start_time + word.end_time) / 2.0,
                    0.65,
                )
        for boundary in range(len(words) - 1):
            self._set_word_handle_value(boundary, words[boundary].end_time)

    def _on_word_handle_dragged(self, boundary_idx: int, new_t: float) -> None:
        """内部字界只改预览副本；句级手柄负责绑定首/尾 word 外边界。"""
        if self._project is None or not (0 <= self._active_idx < len(self._project.sentences)):
            return
        words = self._preview_words
        if not (0 <= boundary_idx < len(words) - 1):
            return

        left = words[boundary_idx]
        right = words[boundary_idx + 1]
        minimum = float(left.start_time) + 0.030
        maximum = float(right.end_time) - 0.030
        clamped = round(max(minimum, min(maximum, new_t)), 3)
        left.end_time = clamped
        right.start_time = clamped

        self._refresh_word_preview_visuals()
        self.word_boundary_dragged.emit(
            self._active_idx,
            [copy.deepcopy(word) for word in words],
        )

    def _on_word_handle_drag_finished(self, boundary_idx: int, new_t: float) -> None:
        if self._project is None or not (0 <= self._active_idx < len(self._project.sentences)):
            return
        self._on_word_handle_dragged(boundary_idx, new_t)
        old_words = [copy.deepcopy(word) for word in self._drag_start_words]
        new_words = [copy.deepcopy(word) for word in self._preview_words]
        if old_words == new_words:
            return
        self.word_boundary_drag_finished.emit(self._active_idx, old_words, new_words)
        self._drag_start_words = [copy.deepcopy(word) for word in new_words]



__all__ = ["WaveformView"]
