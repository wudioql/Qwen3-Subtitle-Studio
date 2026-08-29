"""subs.time_fmt — 字幕「文件格式态」时间码格式化 / 解析工具

分工：本模块只服务文件读写的规范时间码；UI 表格的「显示态」（宽容手改输入）
在 ui/time_utils.py，勿在此重复实现。

时间基准：
    - 所有内部时间统一为 **秒，float**；小数点后保留足够精度。
    - 各格式只在「写入文件」时才转成对应字符串。

四种主流格式：
    1) SRT        : HH:MM:SS,mmm     (逗号 ms)
    2) VTT        : HH:MM:SS.mmm 或 MM:SS.mmm (点 ms)
    3) ASS        : H:MM:SS.cc        (厘秒 centisecond, 1cs=10ms)
    4) LRC        : MM:SS.xx          (百分秒或厘秒；写 LRC 时统一厘秒读兼容任意)
"""

from __future__ import annotations

import re
from typing import Tuple

# ─────────────────────────────────────────────────────────────
# 格式化
# ─────────────────────────────────────────────────────────────


def _split_hmsms(t_s: float) -> Tuple[int, int, int, int]:
    t_s = max(0.0, float(t_s))
    h = int(t_s // 3600)
    m = int((t_s % 3600) // 60)
    s = int(t_s % 60)
    ms = int(round((t_s - int(t_s)) * 1000))
    if ms >= 1000:
        s += 1
        ms = 0
        if s >= 60:
            m += 1
            s = 0
            if m >= 60:
                h += 1
                m = 0
    return h, m, s, ms


def srt_time(t_s: float) -> str:
    """SRT cue 时间码: HH:MM:SS,mmm"""
    h, m, s, ms = _split_hmsms(t_s)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def vtt_time(t_s: float, *, with_hours: bool = True) -> str:
    """WebVTT cue 时间码：HH:MM:SS.mmm（或省略小时 MM:SS.mmm）"""
    h, m, s, ms = _split_hmsms(t_s)
    if with_hours or h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{m:02d}:{s:02d}.{ms:03d}"


def ass_time(t_s: float) -> str:
    """ASS Dialogue 时间码: H:MM:SS.cc (厘秒)"""
    t_s = max(0.0, float(t_s))
    h = int(t_s // 3600)
    m = int((t_s % 3600) // 60)
    s = int(t_s % 60)
    cs = int(round((t_s - int(t_s)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
        if s >= 60:
            m += 1
            s = 0
            if m >= 60:
                h += 1
                m = 0
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def lrc_time(t_s: float) -> str:
    """LRC 行首时间码: [mm:ss.xx]（百分秒 / 厘秒二选一，我们写厘秒，读时兼容）"""
    h, m, s, ms = _split_hmsms(t_s)
    m += h * 60
    cs = ms // 10
    return f"[{m:02d}:{s:02d}.{cs:02d}]"


# ─────────────────────────────────────────────────────────────
# 解析（用于测试 / 读回 LRC 句级）
# ─────────────────────────────────────────────────────────────

_RE_SRT = re.compile(r"(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})")
_RE_VTT = re.compile(
    r"(?:(?P<h>\d+):)?(?P<m>\d{2}):(?P<s>\d{2})[\.,](?P<ms>\d{1,3})"
)
_RE_ASS = re.compile(r"(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})[\.,](?P<cs>\d{1,2})")
_RE_LRC = re.compile(
    r"\[(?P<m>\d{1,3}):(?P<s>\d{2})(?:[\.:](?P<x>\d{1,3}))?\]"
)


def _to_int(m) -> int:
    return int(m) if m is not None else 0


def parse_srt_time(s: str) -> float:
    s = s.strip()
    m = _RE_SRT.match(s)
    if not m:
        raise ValueError(f"invalid srt time: {s!r}")
    return (
        _to_int(m["h"]) * 3600
        + int(m["m"]) * 60
        + int(m["s"])
        + _to_int(m["ms"]) / 1000.0
    )


def parse_vtt_time(s: str) -> float:
    s = s.strip()
    m = _RE_VTT.match(s)
    if not m:
        raise ValueError(f"invalid vtt time: {s!r}")
    ms = int(m["ms"].ljust(3, "0"))
    return (
        _to_int(m.group("h")) * 3600
        + int(m["m"]) * 60
        + int(m["s"])
        + ms / 1000.0
    )


def parse_ass_time(s: str) -> float:
    s = s.strip()
    m = _RE_ASS.match(s)
    if not m:
        raise ValueError(f"invalid ass time: {s!r}")
    cs = int(m["cs"].ljust(2, "0"))
    return (
        _to_int(m["h"]) * 3600
        + int(m["m"]) * 60
        + int(m["s"])
        + cs / 100.0
    )


def parse_lrc_time(s: str) -> float:
    """解析 `[mm:ss.xx]` 标签，返回秒；xx 可 2/3 位（厘秒/毫秒都兼容）。"""
    s = s.strip()
    m = _RE_LRC.match(s)
    if not m:
        raise ValueError(f"invalid lrc time: {s!r}")
    xx = m.group("x") or "00"
    if len(xx) >= 3:
        ms = int(xx[:3])
        frac = ms / 1000.0
    else:
        cs = int(xx.ljust(2, "0"))
        frac = cs / 100.0
    return int(m["m"]) * 60 + int(m["s"]) + frac


def lrc_centisecond_to_ms(s_xx: str) -> int:
    """`xx` 两位/三位 → 毫秒（给测试用）。"""
    s_xx = s_xx or "00"
    if len(s_xx) >= 3:
        return int(s_xx[:3])
    return int(s_xx.ljust(2, "0")) * 10
