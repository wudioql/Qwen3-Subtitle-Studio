"""ui.settings — 偏好设置弹窗的子页面包。

对外：``from ui.settings import CloudASRPage``

把体量大的分区从 settings_dialog 拆进来（见 AGENTS.md §6.1 的单文件上限约定）；
设置对话框本身只负责 Pivot 挂载、预览模式与保存。
"""
from __future__ import annotations

from .cloud_asr_tab import CloudASRPage

__all__ = ["CloudASRPage"]
