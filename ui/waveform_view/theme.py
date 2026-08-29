"""ui.waveform_view.theme — 波形深/浅色调色板与 LOD 常量。"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor


@dataclass
class WaveformTheme:
    """波形视图深/浅色主题调色板（遵循 make-interfaces-feel-better 规范）。"""
    plot_bg: str
    curve_color: str
    block_bg: QColor
    active_block_bg: QColor
    dirty_block_bg: QColor
    dirty_active_block_bg: QColor
    locked_block_bg: QColor
    locked_active_block_bg: QColor
    handle_color: str
    word_handle_color: str
    word_bg_a: QColor
    word_bg_b: QColor
    sentence_text_color: str
    word_text_color: str
    punct_text_color: str
    playhead_color: str
    region_color: QColor
    axis_color: str
    grid_alpha: float


_DARK_THEME = WaveformTheme(
    plot_bg="#1A1A24",
    curve_color="#4F9DFF",
    block_bg=QColor(130, 140, 165, 50),
    active_block_bg=QColor(55, 138, 221, 85),
    dirty_block_bg=QColor(245, 158, 11, 55),
    dirty_active_block_bg=QColor(245, 158, 11, 95),
    locked_block_bg=QColor(16, 185, 129, 55),
    locked_active_block_bg=QColor(16, 185, 129, 95),
    handle_color="#38BDF8",
    word_handle_color="#FBBF24",
    word_bg_a=QColor(78, 160, 235, 55),
    word_bg_b=QColor(168, 85, 247, 55),
    sentence_text_color="#E2E8F0",
    word_text_color="#FDE047",
    punct_text_color="#94A3B8",
    playhead_color="#FF5252",
    region_color=QColor(239, 68, 68, 50),
    axis_color="#9E9EAE",
    grid_alpha=0.20,
)

_LIGHT_THEME = WaveformTheme(
    plot_bg="#FFFFFF",
    curve_color="#1D4ED8",
    block_bg=QColor(148, 163, 184, 45),
    active_block_bg=QColor(37, 99, 235, 55),
    dirty_block_bg=QColor(245, 158, 11, 45),
    dirty_active_block_bg=QColor(217, 119, 6, 75),
    locked_block_bg=QColor(16, 185, 129, 40),
    locked_active_block_bg=QColor(5, 150, 105, 70),
    handle_color="#0284C7",
    word_handle_color="#D97706",
    word_bg_a=QColor(59, 130, 246, 35),
    word_bg_b=QColor(147, 51, 234, 35),
    sentence_text_color="#0F172A",
    word_text_color="#92400E",
    punct_text_color="#64748B",
    playhead_color="#DC2626",
    region_color=QColor(220, 38, 38, 40),
    axis_color="#64748B",
    grid_alpha=0.15,
)

_MAX_DOWNSAMPLE = 50000
_LOD_MAX_POINTS = 8000

__all__ = [
    "WaveformTheme",
    "_DARK_THEME",
    "_LIGHT_THEME",
    "_MAX_DOWNSAMPLE",
    "_LOD_MAX_POINTS",
]
