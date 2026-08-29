"""模板预览与基础 k-tag 绘制策略回归（纯逻辑，零 Qt）。"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401

import pytest

from ui.subtitle_render_policy import (
    segment_fill_role,
    should_clip_karaoke_sweep,
    should_hide_upcoming_outline,
)

pytestmark = pytest.mark.logic


def test_template_preview_never_inherits_k_tag_sweep_or_outline_rules():
    """Apply 后 k-tag 在 Comment 中；模板画面只由 fx Dialogue 决定。"""
    for k_mode in ("kf", "K", "k", "ko"):
        for state in ("sung", "current", "upcoming"):
            assert segment_fill_role(
                "karaoke_template", state, use_ass=True, k_mode=k_mode,
            ) == "primary"
            assert not should_clip_karaoke_sweep(
                "karaoke_template", state, 0.5,
                k_mode=k_mode, template_color=False,
            )
            assert not should_hide_upcoming_outline(
                "karaoke_template", state, k_mode=k_mode,
            )


def test_basic_k_tag_modes_keep_their_own_visual_semantics():
    assert segment_fill_role(
        "karaoke", "current", use_ass=True, k_mode="kf",
    ) == "secondary"
    assert should_clip_karaoke_sweep(
        "karaoke", "current", 0.5, k_mode="kf", template_color=False,
    )
    assert should_clip_karaoke_sweep(
        "karaoke", "current", 0.5, k_mode="K", template_color=False,
    )
    assert not should_clip_karaoke_sweep(
        "karaoke", "current", 0.5, k_mode="k", template_color=False,
    )
    assert segment_fill_role(
        "karaoke", "current", use_ass=True, k_mode="k",
    ) == "primary"
    assert should_hide_upcoming_outline("karaoke", "upcoming", k_mode="ko")
