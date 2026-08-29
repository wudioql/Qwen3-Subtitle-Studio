"""字幕兼容预览的纯状态策略（零 Qt 依赖）。

把“某模式应使用哪种填充/是否裁剪扫过/是否隐藏未唱描边”与 QColor/QPainter
分离，避免 ``karaoke_template`` 再误入基础 k-tag 的绘制分支。模板模式仍保留
syllable progress 供缩放、颜色、发光等 fx 动画使用，但该 progress 绝不能被
解释成 ``\\kf/\\K`` 横向扫过。
"""

from __future__ import annotations

from typing import Literal

FillRole = Literal["primary", "secondary", "highlight"]


def segment_fill_role(
    mode: str,
    state: str,
    *,
    use_ass: bool,
    k_mode: str,
) -> FillRole:
    """返回分段默认填充角色；模板显式颜色动画由调用方覆盖。"""
    if state == "plain":
        return "primary"
    if mode == "karaoke":
        if state == "sung" or (state == "current" and k_mode in ("k", "ko")):
            return "primary"
        return "secondary"
    if mode == "karaoke_template":
        # Apply 后的 k-tag 源行是 Comment；播放器只渲染模板 fx Dialogue。
        return "primary"
    if state == "current":
        return "highlight"
    if state == "upcoming" and not use_ass:
        return "secondary"
    return "primary"


def should_clip_karaoke_sweep(
    mode: str,
    state: str,
    progress: float,
    *,
    k_mode: str,
    template_color: bool,
) -> bool:
    """仅基础 karaoke 的 ``\\kf/\\K`` 当前段允许横向裁剪扫过。"""
    return (
        mode == "karaoke"
        and k_mode in ("kf", "K")
        and not template_color
        and state == "current"
        and 0.0 < float(progress) < 1.0
    )


def should_hide_upcoming_outline(mode: str, state: str, *, k_mode: str) -> bool:
    """``\\ko`` 只影响基础 k-tag；模板 fx 不继承其未唱字描边规则。"""
    return mode == "karaoke" and k_mode == "ko" and state == "upcoming"


__all__ = [
    "FillRole",
    "segment_fill_role",
    "should_clip_karaoke_sweep",
    "should_hide_upcoming_outline",
]
