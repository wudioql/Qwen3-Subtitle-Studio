"""PlayerPanel 的字幕生成与 mpv/libass 预览回调子域。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SubtitlePreviewMixin:
    # ── mpv 真渲染：时间/时长/状态回调 + 字幕生成 ──────────────
    def _on_mpv_time(self, t: float) -> None:
        # 由 _MpvSignalBridge queued 到 GUI 线程；非 active 的迟到回调直接忽略。
        if self._active_backend != "mpv":
            return
        self._stage.set_time(t)
        self.position_changed.emit(t)

    def _on_mpv_duration(self, d: float) -> None:
        if self._active_backend == "mpv":
            self.duration_changed.emit(d)

    def _on_mpv_playing(self, playing: bool) -> None:
        if self._active_backend != "mpv":
            return
        if playing:
            self.state_playing.emit()
        else:
            self.state_paused.emit()

    # 预览模式 → 导出 kind（按导出语义真生成，预览与导出一致）
    _MPV_PREVIEW_KIND = {
        "sentence": "srt_sentence",
        "sentence_ass": "ass_sentence",
        "word": "srt",
        "word_ass": "ass_split",
        "karaoke": "karaoke",
        # karaoke_template：走 Karaoke Templater 应用器真展开，见 _rebuild_mpv_subtitle
    }

    def _rebuild_mpv_subtitle(self) -> None:
        """按当前档位/样式/工程重新生成字幕并交给 mpv（libass 真渲染）。"""
        if (
            self._mpv is None
            or not self._mpv_ready
            or self._active_backend != "mpv"
            or self._project is None
        ):
            return
        mode = self._stage.mode()
        try:
            if mode == "karaoke_template":
                text = self._render_karaoke_template_applied()
                is_ass = True
            else:
                kind = self._MPV_PREVIEW_KIND.get(mode, "srt_sentence")
                text = self._render_preview_subtitle(kind)
                is_ass = mode not in ("sentence", "word")
        except ValueError:
            # karaoke 无字级 / 模板含 Lua 表达式 → 预览降级为句级 ASS
            try:
                text = self._render_preview_subtitle("ass_sentence")
                is_ass = True
            except Exception as e:  # noqa: BLE001
                logger.debug("[mpv] 字幕降级生成失败: %s", e)
                return
        except Exception as e:  # noqa: BLE001
            logger.debug("[mpv] 字幕生成失败(%s): %s", mode, e)
            return
        if not self._mpv.set_subtitle(text, is_ass=is_ass):
            logger.warning("[mpv] 字幕命令队列不可用；保留当前画面，等待后端回退通知")

    def _render_preview_subtitle(self, kind: str) -> str:
        """按导出语义生成预览字幕文本（复用导出唯一真源 render_export）。"""
        from core.app_config import load_preferences
        from subs import WordHighlightStyle
        from ui.export_controller import render_export

        prefs = load_preferences()
        st = prefs.style
        word_style = WordHighlightStyle(
            bold=st.bold, italic=st.italic, underline=st.underline,
            strike=st.strike, ass_extra=st.ass_extra_tags,
            ass_highlight_color=getattr(st, "ass_highlight_color", "#FFD54F"),
        )
        return render_export(
            kind,
            self._project,
            word_style=word_style,
            ass_style=prefs.ass_style.to_style(),
            k_mode=prefs.export.k_tag_mode or "kf",
            include_aegisub_template=False,   # libass 不执行 Automation 模板，纯 k-tag
            template_prefs=prefs.karaoke_template.to_prefs(),
        )

    def _render_karaoke_template_applied(self) -> str:
        """「所选模板」档：用 Karaoke Templater 应用器把模板真正展开成成品 ASS。

        全不选模板 → 句级 ASS（不伪装成模板、更不出现 k-tag 扫过）；模板含不支持的
        Lua 表达式 → 抛 LuaExpressionError，由 _rebuild_mpv_subtitle 明确降级。
        """
        from core.app_config import load_preferences
        from subs import WordHighlightStyle
        from subs.karaoke_templater import apply_template_to_project

        prefs = load_preferences()
        st = prefs.style
        word_style = WordHighlightStyle(
            bold=st.bold, italic=st.italic, underline=st.underline,
            strike=st.strike, ass_extra=st.ass_extra_tags,
            ass_highlight_color=getattr(st, "ass_highlight_color", "#FFD54F"),
        )
        ass_style = prefs.ass_style.to_style()
        k_mode = prefs.export.k_tag_mode or "kf"
        active = prefs.karaoke_template.to_prefs().effective().templates
        if not active:
            # 这是“所选模板”预览，不是 k-tag 档；无模板时只显示句级 ASS。
            return self._render_preview_subtitle("ass_sentence")
        return apply_template_to_project(
            self._project,
            active[0],
            k_mode=k_mode,
            style=word_style,
            ass_style=ass_style,
            coord_provider=self._make_coord_provider(ass_style),
        )

    @staticmethod
    def _make_coord_provider(ass_style):
        """兼容旧调用；预览与应用后导出共用同一坐标 provider。"""
        from .karaoke_coordinates import make_karaoke_coord_provider

        return make_karaoke_coord_provider(ass_style)


__all__ = ["SubtitlePreviewMixin"]
