"""ui.export_controller — 导出执行逻辑

负责：
    - 根据 kind 调用 subs 层导出函数
    - 统一应用 WordHighlightStyle / AssStylePrefs（从导出面板或偏好设置读取）
    - 弹保存对话框并写盘
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from subs import (
    WordHighlightStyle,
    to_srt, to_vtt, to_ass, to_lrc, to_ass_karaoke, to_ass_karaoke_applied,
)
from subs.ass_style import AssStylePrefs

logger = logging.getLogger(__name__)


def word_level_mismatch_rows(project) -> list:
    """导出预检（纯函数，供测试直调）：返回字级内容与句文本不一致的句行号（0 基）。

    「逐字类」导出（k-tag / 逐字 SRT/VTT/ASS / Enhanced LRC）的时间与高亮都吃 words，
    而 words 可能与当前句文本脱节（改了文本未重对齐、attach 比例回退漂移等）——
    这类错位在句级表与波形上完全不可见，导出前是最后的守门点。
    """
    if project is None:
        return []
    from core.text_utils import words_content_match
    return [i for i, s in enumerate(project.sentences) if s.words and not words_content_match(s)]


# kind → (扩展名, 文件过滤器描述, 是句级)
KIND_META = {
    "srt":          (".srt", "SubRip (*.srt)", False),
    "vtt":          (".vtt", "WebVTT (*.vtt)", False),
    "ass_split":    (".ass", "ASS (*.ass)", False),
    "ass_t":        (".ass", "ASS (*.ass)", False),
    "lrc":          (".lrc", "LRC (*.lrc)", False),
    "karaoke":      (".ass", "ASS (*.ass)", False),
    "karaoke_applied": (".ass", "ASS (*.ass)", False),
    "srt_sentence": (".srt", "SubRip (*.srt)", True),
    "vtt_sentence": (".vtt", "WebVTT (*.vtt)", True),
    "ass_sentence": (".ass", "ASS (*.ass)", True),
    "lrc_sentence": (".lrc", "LRC (*.lrc)", True),
}


def render_export(
    kind: str,
    project,
    word_style: Optional[WordHighlightStyle] = None,
    ass_style: Optional[AssStylePrefs] = None,
    *,
    k_mode: str = "kf",
    include_aegisub_template: bool = True,
    template_prefs=None,
    coord_provider=None,
) -> str:
    """根据 kind 生成导出文本；Aegisub k-tag 默认附带可直接应用的示例模板。"""
    word_style = word_style or WordHighlightStyle()
    ass_style = ass_style or AssStylePrefs()

    if kind == "srt":
        return to_srt(project, style=word_style, mode="per_word")
    if kind == "vtt":
        return to_vtt(project, style=word_style, mode="per_word")
    if kind == "ass_split":
        return to_ass(project, style=word_style, mode="per_word",
                      strategy="split", ass_style=ass_style)
    if kind == "ass_t":
        return to_ass(project, style=word_style, mode="per_word",
                      strategy="t", ass_style=ass_style)
    if kind == "lrc":
        return to_lrc(project, enhanced=True)
    if kind == "karaoke":
        return to_ass_karaoke(
            project,
            k_mode=k_mode,
            style=word_style,
            ass_style=ass_style,
            include_automation_template=include_aegisub_template,
            template_prefs=template_prefs,
        )
    if kind == "karaoke_applied":
        if coord_provider is None:
            from ui.karaoke_coordinates import make_karaoke_coord_provider

            coord_provider = make_karaoke_coord_provider(ass_style)
        return to_ass_karaoke_applied(
            project,
            k_mode=k_mode,
            style=word_style,
            ass_style=ass_style,
            template_prefs=template_prefs,
            coord_provider=coord_provider,
        )
    # 句级
    if kind == "srt_sentence":
        return to_srt(project, style=word_style, mode="per_sentence")
    if kind == "vtt_sentence":
        return to_vtt(project, style=word_style, mode="per_sentence")
    if kind == "ass_sentence":
        return to_ass(project, style=word_style, mode="per_sentence",
                      ass_style=ass_style)
    if kind == "lrc_sentence":
        return to_lrc(project, enhanced=False)
    raise ValueError(f"未知导出类型: {kind}")


def prompt_save_path(
    parent: Optional[QWidget],
    kind: str,
    project,
    default_dir: str = "",
    default_stem: str = "",
) -> Optional[str]:
    """弹保存对话框，返回选中路径或 None（用户取消）。"""
    ext, filt, _is_sentence = KIND_META[kind]
    base = default_stem
    if not base:
        if project is not None and project.source_media_path:
            base = Path(project.source_media_path).stem
        else:
            base = "subtitle"
    if not default_dir:
        if project is not None and project.source_media_path:
            default_dir = str(Path(project.source_media_path).parent)
        else:
            # 绝对路径兜底：相对 ".temp" 会随 cwd 漂移（cwd 非项目根时落错位置）
            from core.constants import TEMP_DIR
            default_dir = str(TEMP_DIR)
    suggested_stem = base
    if kind == "karaoke_applied" and not suggested_stem.endswith(".applied"):
        suggested_stem += ".applied"
    out, _ = QFileDialog.getSaveFileName(
        parent, "导出字幕", str(Path(default_dir) / f"{suggested_stem}{ext}"), filt,
    )
    return out or None


def do_export(
    project=None,
    kind: str = "",
    *,
    parent: Optional[QWidget] = None,
    word_style: Optional[WordHighlightStyle] = None,
    ass_style: Optional[AssStylePrefs] = None,
    k_mode: str = "kf",
    include_aegisub_template: bool = True,
    template_prefs=None,
    coord_provider=None,
    default_dir: str = "",
    default_stem: str = "",
    prefs=None,
) -> Optional[str]:
    """完整导出流程：生成文本 → 选路径 → 写盘。返回写入路径或 None。

    支持灵活的调用签名（兼顾按关键字参数与旧式位置参数调用）。
    """
    # 兼容位置参数反转: do_export(parent, kind, project, ...)
    if isinstance(project, QWidget) and not isinstance(parent, QWidget):
        parent, project = project, parent

    # 从偏好设置补齐缺省样式
    if prefs is not None:
        if word_style is None and hasattr(prefs, "style"):
            st = prefs.style
            word_style = WordHighlightStyle(
                bold=st.bold, italic=st.italic, underline=st.underline,
                strike=st.strike, ass_extra=st.ass_extra_tags,
                ass_highlight_color=getattr(st, "ass_highlight_color", "#FFD54F"),
            )
        if ass_style is None and hasattr(prefs, "ass_style"):
            ass_style = prefs.ass_style.to_style()
        if hasattr(prefs, "export") and hasattr(prefs.export, "k_tag_mode"):
            k_mode = prefs.export.k_tag_mode or k_mode
        if template_prefs is None and hasattr(prefs, "karaoke_template"):
            template_prefs = prefs.karaoke_template.to_prefs()

    word_style = word_style or WordHighlightStyle()
    ass_style = ass_style or AssStylePrefs()

    # 逐字类导出前的字级内容一致性守门（最后守门员）
    if kind in KIND_META and not KIND_META[kind][2] and parent is not None and project is not None:
        rows = word_level_mismatch_rows(project)
        if rows:
            preview = "、".join(f"S{r+1}" for r in rows[:8]) + (" …" if len(rows) > 8 else "")
            dlg = QMessageBox(parent)
            dlg.setWindowTitle("逐字导出预检")
            dlg.setIcon(QMessageBox.Icon.Warning)
            dlg.setText(
                f"有 {len(rows)} 句的字级内容与句文本不一致（{preview}）。\n"
                "继续导出将沿用这些句的旧词级时间与内容——逐字高亮可能对不上显示的文本。\n"
                "建议先「修改句重对齐」（Ctrl+R）消除漂移后再导出。"
            )
            btn_go = dlg.addButton("仍要导出", QMessageBox.ButtonRole.AcceptRole)
            dlg.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            dlg.exec()
            if dlg.clickedButton() is not btn_go:
                logger.info("[Export] 用户因字级漂移预检取消导出（%d 句不一致）", len(rows))
                return None

    try:
        text = render_export(
            kind, project, word_style, ass_style,
            k_mode=k_mode,
            include_aegisub_template=include_aegisub_template,
            template_prefs=template_prefs,
            coord_provider=coord_provider,
        )
    except Exception as e:  # noqa: BLE001
        if parent is not None:
            QMessageBox.critical(parent, "导出失败", f"{type(e).__name__}: {e}")
        logger.exception("[Export] 渲染失败 kind=%s: %s", kind, e)
        return None

    out = prompt_save_path(parent, kind, project, default_dir=default_dir, default_stem=default_stem)
    if not out:
        return None
    try:
        from subs.atomic_io import atomic_write_text
        atomic_write_text(out, text)
    except Exception as e:  # noqa: BLE001
        if parent is not None:
            QMessageBox.critical(
                parent, "导出失败",
                f"写入文件失败：{type(e).__name__}: {e}\n\n请检查磁盘空间与目录权限。",
            )
        logger.exception("[Export] 写盘失败 kind=%s: %s", kind, e)
        return None
    logger.info("[Export] 已导出 %s → %s", kind, out)
    return out


__all__ = ["do_export", "render_export", "prompt_save_path", "KIND_META"]
