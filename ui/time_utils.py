"""ui.time_utils — 表格时间码「显示态」格式化/解析（句级与字级页面共享）

分工：本模块只服务 UI 表格的可读显示与手改解析（mm:ss.mmm，宽容输入）；
「文件格式态」（SRT/VTT/ASS/LRC 的规范时间码）在 subs/time_fmt.py，勿在此重复实现。
"""

from __future__ import annotations

from typing import Optional


def format_time(t: Optional[float]) -> str:
    """秒 → mm:ss.mmm 或 hh:mm:ss.mmm；None/负数显示占位。"""
    if t is None or t < 0:
        return "--:--.---"
    m, s = divmod(float(t), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{int(h):d}:{int(m):02d}:{s:06.3f}"
    return f"{int(m):02d}:{s:06.3f}"


def parse_time(txt: str, fallback: Optional[float] = None) -> Optional[float]:
    """解析手改的时间字符串。支持 hh:mm:ss.mmm / mm:ss.mmm / 纯秒；失败返回 fallback。"""
    if txt is None:
        return fallback
    t = str(txt).strip()
    if not t or t.startswith("--"):
        return fallback
    try:
        if ":" in t:
            parts = t.split(":")
            parts = [float(p or "0") for p in parts]
            while len(parts) < 3:
                parts.insert(0, 0.0)
            h, m, s = parts[-3], parts[-2], parts[-1]
            return h * 3600 + m * 60 + s
        return float(t)
    except (TypeError, ValueError):
        return fallback


__all__ = ["format_time", "parse_time"]
