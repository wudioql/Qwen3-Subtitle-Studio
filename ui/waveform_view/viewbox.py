"""ui.waveform_view.viewbox — 自定义 ViewBox（滚轮缩放/平移、点击 seek）。"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal

class _WaveViewBox(pg.ViewBox):
    """自定义 ViewBox：滚轮/点击交互。"""

    click_seeked = Signal(float)   # 点击位置（秒）
    after_auto_range = Signal()

    def autoRange(self, padding=None, items=None, item=None):
        super().autoRange(padding=padding, items=items, item=item)
        self.after_auto_range.emit()

    def wheelEvent(self, ev, axis=None):
        if hasattr(ev, "delta"):
            delta = ev.delta()
        elif hasattr(ev, "angleDelta"):
            ad = ev.angleDelta()
            delta = ad.y() or ad.x()
        else:
            delta = 0

        if delta == 0:
            ev.accept()
            return

        if hasattr(ev, "modifiers"):
            mods = ev.modifiers()
        else:
            from PySide6.QtWidgets import QApplication
            mods = QApplication.keyboardModifiers()

        if hasattr(ev, "pos"):
            pos = ev.pos()
        elif hasattr(ev, "position"):
            pos = ev.position()
        else:
            pos = None

        scale_factor = 0.85 if delta > 0 else 1.18
        pan_step_ratio = 0.08
        cur_view = self.viewRange()
        x_span = cur_view[0][1] - cur_view[0][0]
        y_span = cur_view[1][1] - cur_view[1][0]

        has_ctrl = bool(mods & Qt.ControlModifier)
        has_shift = bool(mods & Qt.ShiftModifier)
        has_alt = bool(mods & Qt.AltModifier)

        if has_ctrl and has_shift:
            center_x = self.mapToView(pos).x() if pos is not None else (cur_view[0][0] + cur_view[0][1]) / 2.0
            new_lo = center_x - (center_x - cur_view[0][0]) * scale_factor
            new_hi = center_x + (cur_view[0][1] - center_x) * scale_factor
            if new_hi - new_lo > 0.05:
                self.setXRange(new_lo, new_hi, padding=0)
            ev.accept()
            return

        if has_ctrl and not has_shift and not has_alt:
            mouse_pt = self.mapToView(pos) if pos is not None else None
            if mouse_pt is not None:
                cx, cy = mouse_pt.x(), mouse_pt.y()
            else:
                cx = (cur_view[0][0] + cur_view[0][1]) / 2.0
                cy = (cur_view[1][0] + cur_view[1][1]) / 2.0
            new_x_lo = cx - (cx - cur_view[0][0]) * scale_factor
            new_x_hi = cx + (cur_view[0][1] - cx) * scale_factor
            new_y_lo = cy - (cy - cur_view[1][0]) * scale_factor
            new_y_hi = cy + (cur_view[1][1] - cy) * scale_factor
            if new_x_hi - new_x_lo > 0.05:
                self.setXRange(new_x_lo, new_x_hi, padding=0)
            if new_y_hi - new_y_lo > 0.05:
                self.setYRange(new_y_lo, new_y_hi, padding=0)
            ev.accept()
            return

        if has_alt:
            cy = self.mapToView(pos).y() if pos is not None else (cur_view[1][0] + cur_view[1][1]) / 2.0
            new_lo = cy - (cy - cur_view[1][0]) * scale_factor
            new_hi = cy + (cur_view[1][1] - cy) * scale_factor
            if new_hi - new_lo > 0.05:
                self.setYRange(new_lo, new_hi, padding=0)
            ev.accept()
            return

        if has_shift:
            direction = -1 if delta > 0 else 1
            dx = x_span * pan_step_ratio * direction
            self.setXRange(cur_view[0][0] + dx, cur_view[0][1] + dx, padding=0)
            ev.accept()
            return

        # 普通滚轮遵循画布方向：向上滚查看更高振幅区域，向下滚查看更低区域。
        # 旧符号与图坐标方向相反，表现为滚轮向上、画面反而向下。
        direction = 1 if delta > 0 else -1
        dy = y_span * pan_step_ratio * direction
        self.setYRange(cur_view[1][0] + dy, cur_view[1][1] + dy, padding=0)
        ev.accept()

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            pt = self.mapToView(ev.pos())
            t = float(pt.x())
            self.click_seeked.emit(t)
            ev.accept()
            return
        super().mouseClickEvent(ev)


__all__ = ["_WaveViewBox"]
