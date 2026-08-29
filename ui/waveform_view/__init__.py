"""ui.waveform_view — 音频波形组件包。

对外：``from ui.waveform_view import WaveformView``（与单文件时代相同）。
"""
from __future__ import annotations

from .widget import WaveformView
from .theme import WaveformTheme

__all__ = ["WaveformView", "WaveformTheme"]
