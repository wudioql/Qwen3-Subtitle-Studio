"""subs.karaoke_template — Aegisub Karaoke Templater 模板的数据模型与渲染（纯逻辑）

背景：k-tag ASS 导出自带的 Automation 模板行此前是 ass_karaoke.py 里的硬编码
常量，用户无法定制效果——与 AssStylePrefs 出现前的 [V4+ Styles] 同病。

本模块提供：
    - KaraokeTemplate：单条模板（参数化字段 + 自由文本两种模式）
    - KaraokeTemplatePrefs：模板列表容器（效果库；最多一条启用，也允许全不选）
    - render_template_comment()：单条模板 → Comment 事件行
    - default_karaoke_templates()：默认模板（与历史硬编码效果一致）

参数化字段渲染的标签体结构（每段可独立开关）：
    {\\an<锚> \\pos($scenter,$smiddle) \\fad(in,out)
     [高亮变色 \\1c&H..&\\t($start,$end,\\1c&H..&)]
     [弹跳缩放 \\t($start,$mid,\\fscx..\\fscy..)\\t($mid,$end,\\fscx100\\fscy100)]
     [上浮位移 \\t($start,$mid,\\fscy..)…（简化为缩放竖向）]
     [额外标签原样附加]}

自由模式（raw）直接持有整行 Comment 文本，完全交给用户（多层/Lua 表达式等
进阶用法）；表单生成的行可导出为 raw 起点。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List

# 作用范围（Karaoke Templater 的 template <class>）——UI 显示中文名
TEMPLATE_CLASSES: Dict[str, str] = {
    "syl": "按字（每个字独立动画，逐字特效的基础）",
    "line": "整句（整句一条行，做整行渐入/移动）",
    "char": "按字符（比按字更细的拆分粒度）",
}

# 常用附加选项（多选，输出顺序按此表）——UI 显示中文名
TEMPLATE_MODIFIERS: Dict[str, str] = {
    "all": "对所有字幕样式生效（推荐保持勾选）",
    "noblank": "跳过空白音节（空拍不生成行）",
    "keeptags": "保留原字幕行已有的特效标签",
    "notext": "不显示原文字（配合绘图代码使用）",
}


def _rgb_to_ass_tag(rgb: str) -> str:
    """'#RRGGBB' → ASS 内联颜色 '&HBBGGRR&'（override 标签用，无 alpha 字节）。"""
    s = (rgb or "").lstrip("#")
    if len(s) != 6:
        s = "FFFFFF"
    r, g, b = s[0:2], s[2:4], s[4:6]
    return f"&H{b}{g}{r}&".upper()


@dataclass
class KaraokeTemplate:
    """单条 Karaoke Templater 模板。

    mode="form"：由参数化字段渲染标签体；mode="raw"：raw_text 即整行 Comment
    （不含行首 "Comment: " 也可，render 时自动补齐规范前缀）。
    """

    enabled: bool = True
    name: str = "模板"                      # 仅 UI 显示用
    mode: str = "form"                      # "form" | "raw"

    # ── 声明部分 ─────────────────────────────────────────────
    template_class: str = "syl"             # syl / line / char
    modifiers: List[str] = field(default_factory=lambda: ["all"])
    layer: int = 0                          # 多层叠加时用（辉光层 0，本体层 1…）
    style_name: str = "Default"             # 模板行 Style 字段（含 all 时仅占位）

    # ── 定位与出入场 ─────────────────────────────────────────
    anchor: int = 5                         # \an：1-9，5=正中（缩放动画对称）
    use_pos: bool = True                    # \pos($scenter,$smiddle) 锁原位
    fad_in_ms: int = 80                     # \fad 淡入（0=关）
    fad_out_ms: int = 80                    # \fad 淡出（0=关）

    # ── 弹跳缩放（唱到时放大→回落） ──────────────────────────
    scale_enabled: bool = True
    scale_percent: int = 116                # 峰值缩放（100=关）

    # ── 高亮变色（唱到期间从原色渐变为高亮色） ──────────────────
    color_enabled: bool = False
    color_highlight: str = "#FFD54F"        # 目标高亮色
    # 旧工程字段名保留以兼容 JSON/API；现役语义是“原色”，不再表示回落色。
    color_restore: str = "#FFFFFF"

    @property
    def color_original(self) -> str:
        return self.color_restore

    @color_original.setter
    def color_original(self, value: str) -> None:
        self.color_restore = value

    # ── 描边/模糊脉冲（唱到时描边加粗+发光） ──────────────────
    glow_enabled: bool = False
    glow_bord: float = 4.0                  # 峰值描边宽
    glow_blur: float = 2.0                  # 峰值模糊

    # ── 额外标签（原样附加进标签体，进阶自由度） ──────────────
    extra_tags: str = ""

    # ── 自由模式 ─────────────────────────────────────────────
    raw_text: str = ""

    # —— 序列化（宽松：缺字段用默认值，多余忽略）——————————————
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "KaraokeTemplate":
        if not isinstance(d, dict):
            return cls()
        known = {f_.name for f_ in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in d.items() if k in known}
        try:
            return cls(**kwargs)
        except TypeError:
            return cls()

    # —— 渲染 ————————————————————————————————————————————————
    def effect_field(self) -> str:
        """Effect 字段：template <class> [modifiers...]。"""
        parts = ["template", self.template_class if self.template_class in TEMPLATE_CLASSES else "syl"]
        for m in TEMPLATE_MODIFIERS:
            if m in (self.modifiers or []):
                parts.append(m)
        return " ".join(parts)

    def tag_body(self) -> str:
        """渲染 {…} 内的标签体（form 模式）。"""
        tags: List[str] = [f"\\an{self.anchor if 1 <= self.anchor <= 9 else 5}"]
        if self.use_pos:
            # line 模板整行定位用行级变量；syl/char 用音节级
            if self.template_class == "line":
                tags.append("\\pos($lcenter,$lmiddle)")
            else:
                tags.append("\\pos($scenter,$smiddle)")
        if self.fad_in_ms > 0 or self.fad_out_ms > 0:
            tags.append(f"\\fad({max(0, self.fad_in_ms)},{max(0, self.fad_out_ms)})")
        if self.color_enabled:
            original = _rgb_to_ass_tag(self.color_original)
            highlight = _rgb_to_ass_tag(self.color_highlight)
            tags.append(f"\\1c{original}\\t($start,$end,\\1c{highlight})")
        if self.scale_enabled and int(self.scale_percent) != 100:
            p = int(self.scale_percent)
            tags.append(f"\\t($start,$mid,\\fscx{p}\\fscy{p})")
            tags.append("\\t($mid,$end,\\fscx100\\fscy100)")
        if self.glow_enabled:
            tags.append(f"\\t($start,$mid,\\bord{self.glow_bord:g}\\blur{self.glow_blur:g})")
            tags.append("\\t($mid,$end,\\bord2\\blur0)")
        extra = (self.extra_tags or "").strip()
        if extra.startswith("{") and extra.endswith("}"):
            extra = extra[1:-1].strip()
        if extra:
            tags.append(extra)
        return "{" + "".join(tags) + "}"

    def render_comment(self, style_name: str | None = None) -> str:
        """渲染完整 Comment 事件行。raw 模式原样输出（补齐 Comment 前缀）。"""
        if self.mode == "raw":
            text = (self.raw_text or "").strip()
            if not text:
                return ""
            if not text.startswith(("Comment:", "Dialogue:")):
                text = "Comment: " + text
            return text
        st = style_name or self.style_name or "Default"
        return (
            f"Comment: {max(0, int(self.layer))},0:00:00.00,0:00:05.00,{st},,0000,0000,0000,"
            f"{self.effect_field()},{self.tag_body()}"
        )


@dataclass
class KaraokeTemplatePrefs:
    """模板效果库（偏好持久化镜像；最多一条启用，允许全部禁用）。"""

    templates: List[KaraokeTemplate] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"templates": [t.to_dict() for t in self.templates]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "KaraokeTemplatePrefs":
        if not isinstance(d, dict):
            return cls()
        raw = d.get("templates")
        if not isinstance(raw, list) or not raw:
            return cls()
        templates = [KaraokeTemplate.from_dict(item) for item in raw]
        # 旧偏好可能多选：按列表顺序保留第一条，后续全部关闭。
        selected = False
        for template in templates:
            if not template.enabled:
                continue
            if selected:
                template.enabled = False
            else:
                selected = True
        return cls(templates=templates)

    def effective(self) -> "KaraokeTemplatePrefs":
        """返回至多一条启用且可产出的模板；全不选就返回空。

        首次无偏好仍由 ``KaraokeTemplatePreferences.to_prefs`` 提供默认首选；用户
        明确取消全部效果后必须保持为空，不能偷偷回退“弹跳放大”。
        """
        usable = [
            template
            for template in self.templates
            if template.enabled
            and not (
                template.mode == "raw"
                and not (template.raw_text or "").strip()
            )
        ]
        return KaraokeTemplatePrefs(templates=usable[:1])

    def render_comments(self, style_name: str = "Default") -> List[str]:
        lines = [t.render_comment(style_name) for t in self.effective().templates]
        return [ln for ln in lines if ln]


def builtin_effects() -> List[KaraokeTemplate]:
    """内置常用效果库（单选或全不选；仅「弹跳放大」默认启用）。

    每条都是普通 KaraokeTemplate：用户可改参数、可删除，「恢复内置效果库」
    可整体还原。首条与历史硬编码模板逐字节等价（升级零行为变化）。
    """
    return [
        KaraokeTemplate(
            name="弹跳放大", enabled=True,
            fad_in_ms=80, fad_out_ms=80,
            scale_enabled=True, scale_percent=116,
        ),
        KaraokeTemplate(
            name="高亮变色", enabled=False,
            fad_in_ms=80, fad_out_ms=80, scale_enabled=False,
            color_enabled=True, color_highlight="#FFD54F", color_restore="#FFFFFF",
        ),
        KaraokeTemplate(
            name="描边发光", enabled=False,
            fad_in_ms=80, fad_out_ms=80, scale_enabled=False,
            glow_enabled=True, glow_bord=4.0, glow_blur=2.0,
        ),
        KaraokeTemplate(
            name="逐字浮现", enabled=False,
            fad_in_ms=0, fad_out_ms=0, scale_enabled=False,
            extra_tags="\\alpha&HFF&\\t($start,!$start+120!,\\alpha&H00&)",
        ),
        KaraokeTemplate(
            name="上浮入场", enabled=False,
            fad_in_ms=0, fad_out_ms=0, scale_enabled=False, use_pos=False,
            extra_tags="\\move($scenter,!$smiddle+14!,$scenter,$smiddle,$start,$mid)",
        ),
        KaraokeTemplate(
            name="整句淡入", enabled=False,
            template_class="line",
            fad_in_ms=200, fad_out_ms=200, scale_enabled=False,
        ),
    ]


def default_karaoke_templates() -> KaraokeTemplatePrefs:
    """默认效果集：完整内置库（仅「弹跳放大」启用）。

    render_comments 只输出启用项，因此默认导出与历史硬编码模板逐字节等价。
    """
    return KaraokeTemplatePrefs(templates=builtin_effects())


__all__ = [
    "KaraokeTemplate", "KaraokeTemplatePrefs",
    "builtin_effects", "default_karaoke_templates",
    "TEMPLATE_CLASSES", "TEMPLATE_MODIFIERS",
]
