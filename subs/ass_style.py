"""subs.ass_style — ASS 样式数据模型与渲染

历史问题：
    旧代码里 ASS 导出的 [V4+ Styles] 是硬编码在 exporters.py / ass_karaoke.py 里的
    常量字符串，用户无法设置字体、字号、颜色、描边、阴影、对齐、边距——这正是
    "ASS 文字样式设定缺失"。

本模块提供：
    - AssStylePrefs：完整的 ASS v4+ 样式数据类（可 JSON 序列化，镜像到偏好设置）
    - build_v4_styles_block()：渲染 [V4+ Styles] 段
    - apply_ass_style_to_header()：把样式注入到非 k-tag ASS 的默认 header
    - default_ass_style_prefs()：默认值

颜色统一用 "RRGGBB"（最常用、最直观），内部转成 ASS 的 &HAABBGGRR（ABGR）。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from .ass_utils import sanitize_ass_field


# ASS 对齐编号（小键盘布局）：1-3 底部，4-6 中部，7-9 顶部；2=底部居中，8=顶部居中
ASS_ALIGNMENTS: Dict[int, str] = {
    1: "左下",
    2: "底部居中",
    3: "右下",
    4: "左中",
    5: "正中",
    6: "右中",
    7: "左上",
    8: "顶部居中",
    9: "右上",
}

# ASS 字体常见预设（用户可在下拉里选，也可手填）
FONT_PRESETS: List[str] = [
    "Source Han Sans SC",
    "Source Han Sans SC Bold",
    "Source Han Serif SC",
    "Noto Sans CJK SC",
    "Microsoft YaHei",
    "SimHei",
    "PingFang SC",
    "Arial",
    "Helvetica",
    "Roboto",
]


def _rgb_to_ass(rgb: str, alpha: str = "00") -> str:
    """把 '#RRGGBB' / 'RRGGBB' 转成 ASS 颜色 &HAABBGGRR。

    Args:
        rgb: '#RRGGBB' 或 'RRGGBB'
        alpha: 透明度 '00'(不透明)~'FF'(全透明)
    """
    s = (rgb or "").lstrip("#")
    if len(s) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in s):
        s = "FFFFFF"
    r, g, b = s[0:2], s[2:4], s[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


@dataclass
class AssStylePrefs:
    """ASS v4+ 样式设置（用户可调）。

    字段与 [V4+ Styles] 行一一对应。颜色用 #RRGGBB（UI 友好），
    渲染时由 _rgb_to_ass 转成 ASS 的 &HAABBGGRR。
    """

    name: str = "Default"
    font_name: str = "Source Han Sans SC Bold"
    font_size: int = 64

    # 颜色（#RRGGBB）
    primary_color: str = "#FFFFFF"   # 主填充色（字本身）
    secondary_color: str = "#0000FF"  # 次色（k-tag karaoke 已唱过的字；ASS 约定常用蓝/红）
    outline_color: str = "#000000"   # 描边色
    back_color: str = "#000000"      # 阴影色

    # 透明度 0~255（0=不透明，255=全透明）
    primary_alpha: int = 0
    outline_alpha: int = 0
    back_alpha: int = 128            # 阴影默认半透明（与旧默认 &H80 一致）

    # 字形
    bold: bool = True
    italic: bool = False
    underline: bool = False
    strikeout: bool = False

    # 缩放/间距/角度
    scale_x: int = 100
    scale_y: int = 100
    spacing: int = 0
    angle: float = 0.0

    # 边框
    border_style: int = 1            # 1=描边+阴影, 3=不透明底框, 4=整段底框(libass 扩展)
    outline: float = 3.0
    shadow: float = 2.0

    # 对齐与边距
    alignment: int = 2               # 2=底部居中
    margin_l: int = 120
    margin_r: int = 120
    margin_v: int = 60
    encoding: int = 1                # 1=默认

    # 分辨率（Script Info）
    play_res_x: int = 1920
    play_res_y: int = 1080
    wrap_style: int = 2              # 2=不换行（按 \\n）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "AssStylePrefs":
        if not d or not isinstance(d, dict):
            return cls()
        # 宽松构造：只取已知字段，多余忽略
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        try:
            return cls(**kwargs)
        except TypeError:
            return cls()

    # ── 渲染 ──────────────────────────────────────────
    def _alpha_hex(self, a: int) -> str:
        a = max(0, min(255, int(a)))
        return f"{a:02X}"

    def render_style_line(self) -> str:
        """渲染一条 'Style: ...' 行（不含换行）。"""
        primary = _rgb_to_ass(self.primary_color, self._alpha_hex(self.primary_alpha))
        # Secondary 固定不透明（k-tag 用）
        secondary = _rgb_to_ass(self.secondary_color, "00")
        outline = _rgb_to_ass(self.outline_color, self._alpha_hex(self.outline_alpha))
        back = _rgb_to_ass(self.back_color, self._alpha_hex(self.back_alpha))

        bold_flag = -1 if self.bold else 0
        italic_flag = -1 if self.italic else 0
        underline_flag = -1 if self.underline else 0
        strikeout_flag = -1 if self.strikeout else 0

        return (
            f"Style: {sanitize_ass_field(self.name, fallback='Default')},"
            f"{sanitize_ass_field(self.font_name, fallback='Arial')},{self.font_size},"
            f"{primary},{secondary},{outline},{back},"
            f"{bold_flag},{italic_flag},{underline_flag},{strikeout_flag},"
            f"{self.scale_x},{self.scale_y},{self.spacing},{self.angle:g},"
            f"{self.border_style},{self.outline:g},{self.shadow:g},"
            f"{self.alignment},{self.margin_l},{self.margin_r},{self.margin_v},"
            f"{self.encoding}"
        )

    def render_styles_block(self) -> str:
        """渲染完整 [V4+ Styles] 段。"""
        fmt = (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        )
        return "\n".join(["[V4+ Styles]", fmt, self.render_style_line(), ""])

