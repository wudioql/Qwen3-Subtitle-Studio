"""tests/test_subtitle_overlay.py — 播放器字幕预览套件

覆盖：
1.（logic）compute_overlay_segments 纯函数：句级整句 / 逐字三态分段 /
   卡拉OK扫过进度 / 标点跟随 / 无字级降级 / 句外无字幕；
2.（ui）PlayerPanel 重构契约：切换按钮已移除、六档下拉齐全且持久化、
   视频/音频画面层切换、字幕叠层接收时间与项目；
3.（ui）模板档只画 Apply 后的 fx，绝不随 k-tag 模式出现扫过填充。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

from subs.models import Sentence, SubtitleProject, WordTimestamp


def _proj():
    return SubtitleProject(sentences=[
        Sentence(text="你好，世界", start_time=1.0, end_time=3.0, words=[
            WordTimestamp(text="你", start_time=1.0, end_time=1.5),
            WordTimestamp(text="好", start_time=1.5, end_time=2.0),
            WordTimestamp(text="，", start_time=2.0, end_time=2.0, is_punct=True),
            WordTimestamp(text="世", start_time=2.0, end_time=2.5),
            WordTimestamp(text="界", start_time=2.5, end_time=3.0),
        ]),
        Sentence(text="无字级句", start_time=5.0, end_time=6.0),
    ])


# ═════════════ logic ═════════════

def _case_segments_sentence_and_gap():
    from ui.subtitle_overlay import compute_overlay_segments
    p = _proj()
    # 句级：整句一段
    segs = compute_overlay_segments(p, 1.6, "sentence")
    assert len(segs) == 1 and segs[0].text == "你好，世界" and segs[0].state == "plain"
    # 句间空隙：无字幕
    assert compute_overlay_segments(p, 4.0, "sentence") == []
    assert compute_overlay_segments(p, 4.0, "word") == []
    # 无项目
    assert compute_overlay_segments(None, 1.0, "word") == []


def _case_segments_word_states_and_punct():
    from ui.subtitle_overlay import compute_overlay_segments
    p = _proj()
    # t=1.7：「你」已唱、「好」正在唱、标点跟随前段、「世/界」未唱
    segs = compute_overlay_segments(p, 1.7, "word")
    states = [(s.text, s.state) for s in segs]
    assert states == [("你", "sung"), ("好", "current"), ("，", "current"),
                      ("世", "upcoming"), ("界", "upcoming")]
    assert [s.is_punct for s in segs] == [False, False, True, False, False]

    # 即使 ASR 把标点黏在单词尾部，也必须切出独立的免模板段。
    mixed = SubtitleProject(sentences=[Sentence(
        text="好，world!", start_time=0.0, end_time=1.0,
        words=[WordTimestamp("好，", 0.0, 0.5), WordTimestamp("world!", 0.5, 1.0)],
    )])
    mixed_segments = compute_overlay_segments(mixed, 0.25, "karaoke_template")
    assert [(s.text, s.is_punct) for s in mixed_segments] == [
        ("好", False), ("，", True), ("world", False), ("!", True),
    ]
    # 无字级句降级为整句
    segs2 = compute_overlay_segments(p, 5.5, "word")
    assert len(segs2) == 1 and segs2[0].state == "plain"
    no_words_punct = SubtitleProject(sentences=[
        Sentence(text="无字级，句", start_time=0.0, end_time=1.0),
    ])
    fallback_segments = compute_overlay_segments(
        no_words_punct, 0.5, "karaoke_template"
    )
    assert [(s.text, s.is_punct) for s in fallback_segments] == [
        ("无字级", False), ("，", True), ("句", False),
    ]

    music = SubtitleProject(sentences=[Sentence(
        text="♪你好♫", start_time=0.0, end_time=1.0,
        words=[
            WordTimestamp("你", 0.0, 0.5),
            WordTimestamp("好", 0.5, 1.0),
        ],
    )])
    decorated = compute_overlay_segments(music, 0.25, "word")
    assert "".join(segment.text for segment in decorated) == "♪你好♫"
    assert any("♪" in segment.text for segment in decorated)
    assert any("♫" in segment.text for segment in decorated)

    # 特殊装饰字符（♪ ♫）应保留可见文本，但在 karaoke 模式下不应继承 current 状态
    # 和扫过进度（应与标点行为一致：不参与逐字动效）
    segs_decoration = compute_overlay_segments(music, 0.25, "karaoke")
    # 装饰字符（♪ 和 ♫）应存在于结果中，但不应有 progress 值表示扫过
    decoration_segs = [s for s in segs_decoration if s.text in ("♪", "♫")]
    assert len(decoration_segs) == 2, f"装饰字符应保留在预览中，实际得到：{[(s.text, s.state, s.progress) for s in segs_decoration]}"
    # 装饰字符不应继承 current 的扫过进度（应为 0.0 或 1.0，不为中间值）
    for dec in decoration_segs:
        assert dec.progress in (0.0, 1.0), f"装饰字符 {dec.text!r} 不应有中间扫过进度，实际：{dec.progress}"
        # 装饰字符不应被标记为 current（因为没有独立逐字动效）
        assert dec.state != "current", f"装饰字符 {dec.text!r} 不应继承 current 状态"

    # 在 karaoke_template 模式下，装饰字符同样不应获得模板动画（缩放、颜色变化）
    segs_tpl_decoration = compute_overlay_segments(music, 0.25, "karaoke_template")
    decoration_tpl = [s for s in segs_tpl_decoration if s.text in ("♪", "♫")]
    for dec in decoration_tpl:
        assert dec.is_punct is True or dec.state in ("plain", "upcoming", "sung"), \
            f"装饰字符在模板模式下应无动画状态，实际：{dec.state}"


def _case_segments_karaoke_progress():
    from ui.subtitle_overlay import compute_overlay_segments
    p = _proj()
    # t=1.75：「好」(1.5~2.0) 扫过 50%
    segs = compute_overlay_segments(p, 1.75, "karaoke")
    cur = [s for s in segs if s.state == "current" and not s.text == "，"][0]
    assert cur.text == "好" and abs(cur.progress - 0.5) < 1e-6
    # 逐字档 current 不带扫过进度（恒 1.0）
    segs_w = compute_overlay_segments(p, 1.75, "word")
    cur_w = [s for s in segs_w if s.text == "好"][0]
    assert cur_w.progress == 1.0
    # 模板档仍需 syllable 内进度驱动 \t 的缩放/颜色/发光近似；该 progress
    # 不能再被绘制层解释成基础 k-tag 的横向扫过。
    segs_tpl = compute_overlay_segments(p, 1.75, "karaoke_template")
    cur_tpl = [s for s in segs_tpl if s.text == "好"][0]
    assert abs(cur_tpl.progress - 0.5) < 1e-6


def _case_playback_media_choice(tmp_path=None):
    """视频始终播原文件（保画面）；纯音频仅人声分离后播人声轨。

    用户实测回归：视频导入后曾被提取的 .wav 顶掉播放媒体 → 画面永远丢失、
    面板恒显「音频媒体·无画面」。"""
    import tempfile
    from pathlib import Path as _P
    from ui.project_controller import choose_playback_media

    with tempfile.TemporaryDirectory() as td:
        wav = _P(td) / "extracted.wav"
        wav.write_bytes(b"RIFF")
        video = _P(td) / "movie.mp4"
        audio = _P(td) / "song.flac"

        # 视频：无论有无提取件/人声件，都播原文件
        assert choose_playback_media(video, wav, False) == video
        assert choose_playback_media(video, wav, True) == video
        assert choose_playback_media(video, None, False) == video
        # 纯音频：仅人声分离后播人声轨；普通提取件不顶替
        assert choose_playback_media(audio, wav, True) == wav
        assert choose_playback_media(audio, wav, False) == audio
        assert choose_playback_media(audio, None, False) == audio
        # 人声件标记了但文件已被清理 → 回退原文件
        missing = _P(td) / "gone.wav"
        assert choose_playback_media(audio, missing, True) == audio


@pytest.mark.logic
def _case_overlay_segments_pack():
    """字幕分段纯逻辑 4 合 1：句级与空隙 / 逐字三态与标点 / 卡拉OK进度 / 播放媒体选择。"""
    _case_segments_sentence_and_gap()
    _case_segments_word_states_and_punct()
    _case_segments_karaoke_progress()
    _case_playback_media_choice()


# ═════════════ ui ═════════════

def _case_player_panel_contract():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.player_panel import PlayerPanel
    from ui.subtitle_overlay import PREVIEW_MODES

    panel = PlayerPanel()
    try:
        # 切换按钮与双模式已移除（本面板只负责预览）
        assert not hasattr(panel, "_btn_mode")
        assert not hasattr(panel, "_toggle_mode")
        assert not hasattr(PlayerPanel, "karaoke_switched")
        # 六档下拉齐全；模式名描述效果本身，不把 renderer 固定叫“模拟”。
        assert panel._mode_combo.count() == len(PREVIEW_MODES) == 6
        template_index = panel._mode_combo.findData("karaoke_template")
        assert "模拟" not in panel._mode_combo.itemText(template_index)
        # 窄面板必须把播放按钮与预览控件拆成两行，给模式名/后端状态留空间。
        assert panel.layout().count() >= 3
        assert len(panel._backend_label.text()) <= 16
        # 未导入媒体时也能表达 mpv 能力状态，而不是固定写“Qt 兼容预览”。
        from types import SimpleNamespace
        panel._mpv_probe = SimpleNamespace(candidate=True, reason="ready")
        panel._mpv_failed_reason = ""
        panel._mpv_ready = False
        panel._update_backend_label()
        assert "初始化" in panel._backend_label.text()
        panel._mpv_ready = True
        panel._update_backend_label()
        assert panel._backend_label.text() == "libmpv 已就绪"
        panel._mpv_ready = False
        # 切档生效 + 持久化
        i = panel._mode_combo.findData("karaoke")
        panel._mode_combo.setCurrentIndex(i)
        assert panel.preview_mode() == "karaoke"
        from core.app_config import load_preferences
        assert load_preferences().player_preview_mode == "karaoke"
        # 新面板恢复上次选择
        panel2 = PlayerPanel()
        assert panel2.preview_mode() == "karaoke"
        panel2.close()
        template_index = panel._mode_combo.findData("karaoke_template")
        panel._mode_combo.setCurrentIndex(template_index)
        assert panel.preview_mode() == "karaoke_template"
        assert panel._stage._karaoke_template is not None  # 默认单选“弹跳放大”
        # 项目与时间进舞台（视频+字幕同绘，不再用 HWND 叠层）
        p = _proj()
        panel.set_project(p)
        panel.set_preview_time(1.7)
        assert abs(float(panel.subtitle_overlay._time) - 1.7) < 1e-6
        assert panel.subtitle_overlay.project is p or panel.subtitle_overlay._project is p
        # 视频/音频只是舞台内部标志（同一控件）
        panel._show_video_surface(False)
        assert panel._stage._is_video is False
        panel._show_video_surface(True)
        assert panel._stage._is_video is True
        panel.resize(640, 360)
        assert panel._stage.width() >= 100 and panel._stage.height() >= 100
        # 必须走 QVideoSink 路径（有 Multimedia 时），禁止再依赖 QVideoWidget 原生窗
        assert hasattr(panel._stage, "video_sink")
    finally:
        panel.close()


@pytest.mark.ui
def _case_selected_template_preview_changes_rendered_frame():
    import numpy as np
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from subs.ass_style import AssStylePrefs
    from subs.converter import WordHighlightStyle
    from subs.karaoke_template import default_karaoke_templates
    from ui.subtitle_overlay import _template_scale_factor, paint_subtitle_overlay

    def render(mode: str, template=None, k_mode="kf") -> QImage:
        image = QImage(640, 360, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        paint_subtitle_overlay(
            painter, 640, 360, _proj(), 1.75, mode,
            AssStylePrefs(alignment=2), WordHighlightStyle(), template, k_mode,
        )
        painter.end()
        return image

    def bounds(image: QImage) -> tuple[int, int, int, int]:
        raw = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(
            image.height(), image.bytesPerLine()
        )
        alpha = raw[:, 3:image.width() * 4:4]
        ys, xs = np.where(alpha > 0)
        return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())

    template = default_karaoke_templates().effective().templates[0]
    template_image = render("karaoke_template", template)
    base_image = render("karaoke", None)
    tpl_bounds = bounds(template_image)
    base_bounds = bounds(base_image)
    assert template_image.constBits().tobytes() != base_image.constBits().tobytes()
    assert tpl_bounds[2] - tpl_bounds[0] > base_bounds[2] - base_bounds[0]  # 中点确实放大
    assert tpl_bounds[1] > 180  # use_pos=True 时保持 ASS 底部对齐，不被模板 anchor=5 拉到正中
    assert _template_scale_factor(116, 0.0) == 1.0
    assert _template_scale_factor(116, 0.5) > 1.0
    assert _template_scale_factor(116, 1.0) == 1.0

    # 第五档跟随 k-tag：\k 瞬变与 \kf 半程扫过应产生不同像素。
    assert render("karaoke", None, "k").constBits().tobytes() != render(
        "karaoke", None, "kf"
    ).constBits().tobytes()

    # 第六档只画 Apply 后的模板 fx。原 k-tag 在成品文件中属于 Comment，
    # 因此切换 k/kf/K/ko 不得改变模板预览像素，更不能强制叠加半程扫过。
    template_pixels = render("karaoke_template", template, "kf").constBits().tobytes()
    for mode in ("K", "k", "ko"):
        assert render("karaoke_template", template, mode).constBits().tobytes() == template_pixels


@pytest.mark.ui
def _case_player_panel_preview_pack():
    """播放面板契约：六档下拉与持久化 / 画面层切换 / 模板与 k-tag 接线。"""
    _case_player_panel_contract()


def test_subtitle_overlay_pack():
    """test_subtitle_overlay_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_overlay_segments_pack()
    _case_selected_template_preview_changes_rendered_frame()
    _case_player_panel_preview_pack()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
