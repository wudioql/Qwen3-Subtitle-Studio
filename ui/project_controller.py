"""ui.project_controller — 媒体文件与字幕文件 I/O 控制器

职责：
- 打开/加载音视频媒体文件、提取 16kHz 单声道 WAV、计算时长
- 导入 SRT / VTT / LRC / TXT / ASS 字幕文件
- 保存/打开本工具 .json 工程（媒体路径 + 句/字级 + 脏锁语言）
- 媒体、字幕与 JSON 工程拖放嗅探；JSON 拖放复用“打开工程”完整逻辑
- 波形图音频数据加载
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QFileDialog, QMessageBox

from core import audio_io
from subs.models import PROJECT_SCHEMA_VERSION, SubtitleProject

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)

_MEDIA_EXT = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".flv", ".wmv",
}


def is_media_file(path: str) -> bool:
    return Path(path).suffix.lower() in _MEDIA_EXT


def is_project_file(path: str) -> bool:
    """工程拖放与“打开工程”使用同一扩展名合同。"""
    return Path(path).suffix.lower() == ".json"


def sniff_project_file(path: str | Path) -> bool:
    """轻量嗅探：是否为 Subtitle Studio 工程 JSON（含合法 schema_version 的对象）。

    拖放普通 .json 时避免把任意 JSON 都当工程（打开后 load_json 才会报错）。
    无副作用（不分配 sid、不迁移）；读失败/非对象/无 schema_version → False。
    """
    import json

    try:
        p = Path(path)
        if not p.is_file():
            return False
        if p.stat().st_size > 200 * 1024 * 1024:  # 与 subs.models 工程大小上限一致
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        ver = data.get("schema_version")
        return isinstance(ver, int) and ver == PROJECT_SCHEMA_VERSION
    except Exception:  # noqa: BLE001 — 嗅探失败一律视为非工程，交拖放/打开流程兜底
        return False


def probe_duration(media_path: str) -> float:
    try:
        info = audio_io.get_audio_info(media_path)
        return float(info.duration)
    except Exception:
        return 0.0


def choose_playback_media(source: Path, wav_path, vocal_extracted: bool) -> Path:
    """决定喂给播放器的媒体（纯函数，供测试直调）。

    规则：
    - 视频媒体 → **始终播放原文件**（QMediaPlayer 自解码画面+声音）。提取的
      16k wav / 人声分离 wav 只作 ASR/对齐数据源（audio_path），不顶替播放
      媒体——否则视频永远被 .wav 顶掉，画面丢失（用户实测：任何视频都显示
      「音频媒体·无画面」）；
    - 纯音频媒体 → 做过人声分离时播放人声轨（试听分离效果，沿袭原行为），
      否则播放原文件（soundfile 不能直读的音频格式 QMediaPlayer 也能播）。
    """
    from subs.media_types import VIDEO_SUFFIXES
    if source.suffix.lower() in VIDEO_SUFFIXES:
        return source
    if vocal_extracted and wav_path and Path(wav_path).exists():
        return Path(wav_path)
    return source


class ProjectController:
    """项目 I/O 控制器：管理媒体打开、字幕导入、波形音频载入。"""

    def __init__(self, win: MainWindow) -> None:
        self._win = win

    def _ask_vocal_extraction(self, media_name: str) -> bool:
        """使用中文按钮询问是否执行默认启用的人声提取。"""
        box = QMessageBox(self._win)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("人声提取确认")
        box.setText(
            "检测到已开启默认人声提取偏好。\n\n"
            f"是否对媒体「{media_name}」提取纯人声音轨？"
        )
        extract_button = box.addButton("提取人声", QMessageBox.ButtonRole.AcceptRole)
        _original_button = box.addButton("使用原音频", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(extract_button)
        box.exec()
        return box.clickedButton() is extract_button

    def _should_extract_vocals(self, path: Path) -> bool:
        """「是否执行人声分离」决策（UI 线程，在启动媒体准备 Worker 前完成）。

        条件：偏好 extract_vocals 开启 + 人声模型可用 + 用户确认；任一步不满足即跳过。
        """
        try:
            from core.app_config import load_preferences
            from core.vocal_separator import get_vocal_separator

            prefs = load_preferences()
            enabled = bool(getattr(prefs.asr, "extract_vocals", False))
            if not enabled or not get_vocal_separator().is_available():
                return False
            return self._ask_vocal_extraction(path.name)
        except Exception:  # noqa: BLE001
            logger.debug("[ProjectController] 人声提取偏好检查失败，跳过", exc_info=True)
            return False

    def _start_media_prep(self, path: Path, *, do_vocals: bool, done_label: str, on_ready) -> None:
        """启动媒体准备 Worker（探测 / 提取 / 人声分离全部移出 UI 主线程）。"""
        w = self._win
        from workers import MediaPrepWorker
        worker = MediaPrepWorker(path, do_extract_vocals=do_vocals, parent=w)
        worker.prepared.connect(lambda ap, info, ve: on_ready(path, ap, info, ve))
        worker.vocal_fallback.connect(self._on_vocal_fallback)
        w.workflow.bind_and_start_worker(
            worker, mode_label="正在准备媒体…", done_label=done_label,
        )

    def _on_vocal_fallback(self, msg: str) -> None:
        QMessageBox.warning(
            self._win, "人声提取未完成",
            f"人声提取未成功，将继续使用原音频。\n\n{msg}",
        )

    def _finish_media_prep(self, path: Path, audio_path, info, vocal_extracted: bool) -> None:
        """媒体准备完成：载入播放器、构建工程、同步波形（打开媒体路径）。"""
        w = self._win
        media_to_play = choose_playback_media(path, audio_path, vocal_extracted)
        try:
            w.player.load(media_to_play)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(w, "加载失败", f"无法加载媒体：\n{e!r}")
            return

        w._project = SubtitleProject(
            source_media_path=str(path),
            audio_path=str(audio_path) if audio_path else str(path),
        )
        if info is not None:
            w._project.media_duration = float(info.duration)
        w._project.source_language = w._global_lang.currentData() or "auto"
        # 统一走 _apply_project：同步编辑器/波形/播放器字幕叠层（仅 set 编辑器
        # 不 set_project 到 player 时，叠层无数据 → 画面永远无字幕预览）。
        w._apply_project(w._project)
        self.load_waveform_audio(w._project)

        w._sb_path.setText(f"媒体：{path.name}")
        w._reset_project_file_state(path=None, modified=False)
        # 「模式」文案与动作恢复由 WorkflowController._on_worker_done 用 done_label 收尾

    def _finish_relink_prep(self, path: Path, audio_path, info, vocal_extracted: bool) -> None:
        """媒体准备完成：仅替换媒体字段，保留字幕（重新关联媒体路径）。"""
        w = self._win
        project = w._project
        if project is None:
            return
        media_to_play = choose_playback_media(path, audio_path, vocal_extracted)
        try:
            w.player.load(media_to_play)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(w, "关联失败", f"无法加载媒体：\n{type(e).__name__}: {e}")
            return

        from subs.media_types import VIDEO_SUFFIXES
        project.source_media_path = str(path)
        project.audio_path = str(audio_path) if audio_path else str(path)
        project.video_path = str(path) if path.suffix.lower() in VIDEO_SUFFIXES else None
        if info is not None:
            project.media_duration = float(info.duration)
        project.sample_rate = 16000
        w._apply_project(project)
        self.load_waveform_audio(project)
        w._mark_project_modified()
        w._sb_path.setText(f"媒体：{path.name}")

    def open_media_dialog(self) -> None:
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return
        file_filter = (
            "媒体文件 (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus "
            "*.mp4 *.mkv *.mov *.avi *.webm *.ts);;所有文件 (*)"
        )
        path, _ = QFileDialog.getOpenFileName(w, "选择音/视频文件", "", file_filter)
        if not path:
            return
        self.open_media_file(Path(path))

    def open_media_file(self, p: Path) -> None:
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return
        if not w._maybe_save_changes("打开新媒体将替换当前工程。"):
            return

        # 人声分离决策（模态确认）在 UI 线程先完成；探测/提取/分离移出主线程。
        do_vocals = self._should_extract_vocals(p)
        self._start_media_prep(
            p,
            do_vocals=do_vocals,
            done_label="已加载媒体，等待识别",
            on_ready=self._finish_media_prep,
        )

    def import_subtitle_dialog(self) -> None:
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return
        if w._project is None:
            w._project = SubtitleProject(
                audio_path="",
                source_media_path=None,
                media_duration=0.0,
            )
            w._project.source_language = w._global_lang.currentData() or "auto"
            w.editor.set_project(w._project)
            w.waveform.set_project(w._project)
            w._undo_stack.clear()
        path, _ = QFileDialog.getOpenFileName(
            w, "导入字幕 / 纯文本",
            "", "字幕/文本 (*.srt *.vtt *.lrc *.txt *.ass);;所有文件 (*)"
        )
        if not path:
            return
        self.import_subtitle_file(Path(path))

    def import_subtitle_file(self, path: Path) -> None:
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return
        if w._project is not None and w._project.sentences:
            if not w._maybe_save_changes("导入字幕将替换当前工程中的全部字幕行。"):
                return
        if w._project is None:
            w._project = SubtitleProject(
                audio_path="",
                source_media_path=None,
                media_duration=0.0,
            )
            w._project.source_language = w._global_lang.currentData() or "auto"
        from subs import parse_subtitle_or_text
        dur = float(w._project.media_duration or 0.0)
        if dur <= 0 and w._project.source_media_path:
            dur = probe_duration(w._project.source_media_path or "")
            w._project.media_duration = dur
        try:
            sentences = parse_subtitle_or_text(path, media_duration=dur)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(w, "导入失败", f"{type(e).__name__}: {e}")
            return
        if not sentences:
            QMessageBox.information(w, "提示", "文件里没有可用的字幕/文本。")
            return
        w._project.sentences = sentences
        try:
            w._project.source_language = w._global_lang.currentData() or "auto"
        except Exception:
            logger.debug("[导入] 设置项目源语言失败")
        w._apply_project(w._project)
        w._undo_stack.clear()
        w._undo_stack.setClean()
        w._mark_project_modified()
        n_timed = sum(1 for s in sentences if s.timed)
        w._sb_mode.setText(
            f"模式：已导入 {len(sentences)} 句（带时间 {n_timed}，纯文本 {len(sentences)-n_timed}）；"
            "请先在句级设置各句语言，再手动全文/修改句重对齐"
        )

    def relink_media_dialog(self) -> None:
        """为当前工程选择新的媒体，只改媒体字段，不替换字幕。"""
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return
        if w._project is None or not w._project.sentences:
            QMessageBox.information(w, "提示", "当前没有可重新关联媒体的字幕工程。")
            return
        path, _ = QFileDialog.getOpenFileName(
            w, "重新关联媒体", "",
            "媒体文件 (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus "
            "*.mp4 *.mkv *.mov *.avi *.webm *.ts *.flv *.wmv);;所有文件 (*)",
        )
        if path:
            self.relink_media_file(Path(path))

    def relink_media_file(self, path: Path) -> bool:
        """事务式重新关联：同步守卫照旧；重活（探测/提取/人声分离）移出主线程。

        返回 True 表示已启动准备流程（实际提交在 prepared 信号回调
        ``_finish_relink_prep`` 中完成，成功才替换媒体字段）。
        """
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return False
        project = w._project
        if project is None or not project.sentences:
            return False
        if not path.is_file():
            QMessageBox.critical(w, "关联失败", f"媒体文件不存在：\n{path}")
            return False

        do_vocals = self._should_extract_vocals(path)
        self._start_media_prep(
            path,
            do_vocals=do_vocals,
            done_label="媒体已重新关联，字幕数据保持不变",
            on_ready=self._finish_relink_prep,
        )
        return True

    # ── 工程文件（.json = SubtitleProject.save_json / load_json）────────

    def save_project_dialog(self) -> bool:
        """保存当前工程；已有工程路径时直接覆盖，新工程才询问文件名。"""
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return False
        proj = w._project
        if proj is None or (not proj.sentences and not proj.source_media_path and not proj.audio_path):
            QMessageBox.information(w, "提示", "当前没有可保存的工程内容。")
            return False

        out = w._project_path
        if out is None:
            default_name = "project.json"
            start_dir = ""
            if proj.source_media_path:
                mp = Path(proj.source_media_path)
                default_name = f"{mp.stem}.qss.json"
                start_dir = str(mp.parent)
            elif w._last_export_dir:
                start_dir = w._last_export_dir
                if w._last_export_stem:
                    default_name = f"{w._last_export_stem}.qss.json"

            path, _ = QFileDialog.getSaveFileName(
                w, "保存工程",
                str(Path(start_dir) / default_name) if start_dir else default_name,
                "Subtitle Studio 工程 (*.json *.qss.json);;所有文件 (*)",
            )
            if not path:
                return False
            out = Path(path)
            if out.suffix.lower() != ".json":
                out = out.with_suffix(out.suffix + ".json") if out.suffix else out.with_suffix(".json")

        try:
            # 跨机复现：把当前导出三件套（ASS 样式/卡拉OK模板/导出设置）随工程保存
            self._embed_export_settings(proj)
            proj.save_json(out)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(w, "保存失败", f"{type(e).__name__}: {e}")
            return False
        w._last_export_dir = str(out.parent)
        w._mark_project_saved(out)
        w._sb_mode.setText(f"模式：工程已保存 → {out.name}")
        logger.info("[ProjectController] 工程已保存：%s", out)
        return True

    def _embed_export_settings(self, proj) -> None:
        """保存工程前把导出三件套写入工程（跨机复现：打开后渲染结果一致）。

        三件套 = ASS 文字样式 + 卡拉OK模板 + 导出设置（k_tag_mode + 逐字高亮）。
        预览模式/主题属 UI 偏好，不入工程。
        """
        try:
            panel = self._win._export_panel
            ws = panel.word_style.word_highlight_style()
            proj.ass_style_data = panel.ass_style.current_style().to_dict()
            proj.karaoke_template_data = panel.karaoke_template.current_template_prefs().to_dict()
            proj.export_settings = {
                "k_tag_mode": panel.word_style.k_mode(),
                "word_style": {
                    "bold": ws.bold, "italic": ws.italic, "underline": ws.underline,
                    "strike": ws.strike, "ass_extra": ws.ass_extra,
                    "ass_highlight_color": ws.ass_highlight_color,
                },
            }
        except Exception:  # noqa: BLE001 — 嵌入失败不阻断保存
            logger.debug("[ProjectController] 导出三件套入工程失败", exc_info=True)

    def _apply_project_export_settings(self, proj) -> None:
        """打开工程后把工程级三件套应用到全局偏好并刷新导出面板/播放器字幕预览。

        工程未携带三件套（旧工程/纯字幕工程）→ 不覆盖，沿用全局偏好。
        """
        if not (proj.ass_style_data or proj.karaoke_template_data or proj.export_settings):
            return
        try:
            from core.app_config import load_preferences, save_preferences
            from subs.ass_style import AssStylePrefs
            from subs.karaoke_template import KaraokeTemplatePrefs

            prefs = load_preferences()
            if proj.ass_style_data:
                prefs.ass_style.apply(AssStylePrefs.from_dict(proj.ass_style_data))
            if proj.karaoke_template_data:
                prefs.karaoke_template.apply(KaraokeTemplatePrefs.from_dict(proj.karaoke_template_data))
            if proj.export_settings:
                es = proj.export_settings or {}
                if es.get("k_tag_mode"):
                    prefs.export.k_tag_mode = es["k_tag_mode"]
                ws = es.get("word_style") or {}
                st = prefs.style
                st.bold = bool(ws.get("bold", False))
                st.italic = bool(ws.get("italic", False))
                st.underline = bool(ws.get("underline", True))
                st.strike = bool(ws.get("strike", False))
                st.ass_extra_tags = str(ws.get("ass_extra", "") or "")
                st.ass_highlight_color = str(ws.get("ass_highlight_color", "#FFD54F") or "#FFD54F")
            save_preferences(prefs)
            # 刷新导出面板三卡片 + 播放器字幕预览
            self._win._export_panel.apply_prefs_from(prefs)
            self._win.player.subtitle_overlay.refresh_styles()
        except Exception:  # noqa: BLE001 — 应用失败不阻断打开
            logger.debug("[ProjectController] 工程级三件套应用失败", exc_info=True)

    def open_project_dialog(self) -> None:
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return
        path, _ = QFileDialog.getOpenFileName(
            w, "打开工程",
            w._last_export_dir or "",
            "Subtitle Studio 工程 (*.json *.qss.json);;所有文件 (*)",
        )
        if not path:
            return
        self.open_project_file(Path(path))

    def open_project_file(self, path: Path) -> None:
        """加载 .json 工程；若 source_media_path 存在则恢复播放与波形。"""
        w = self._win
        if not w.workflow.ensure_no_running_worker():
            return
        if not w._maybe_save_changes("打开其他工程将替换当前工程。"):
            return
        try:
            proj = SubtitleProject.load_json(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(w, "打开失败", f"无法解析工程文件：\n{type(e).__name__}: {e}")
            return

        # 跨机复现：应用工程级三件套（覆盖全局偏好并刷新导出面板/播放器字幕预览）
        self._apply_project_export_settings(proj)

        media = (proj.source_media_path or "").strip()
        media_ok = bool(media and Path(media).is_file())
        if media and not media_ok:
            QMessageBox.warning(
                w, "媒体文件缺失",
                f"工程记录的媒体不存在或不可读：\n{media}\n\n"
                "将只加载字幕数据；可稍后用「文件 → 重新关联媒体」修复路径。",
            )
            # 避免后续波形/播放误用死路径
            proj.source_media_path = media  # 保留记录
            if proj.audio_path and not Path(proj.audio_path).is_file():
                proj.audio_path = ""

        # 有可用媒体：载入播放器（同步轻量）；audio_path 缺失时后台修复（不阻塞打开）
        if media_ok:
            play_src = Path(media)
            try:
                w.player.load(play_src)
            except Exception as e:  # noqa: BLE001
                logger.warning("[ProjectController] 工程媒体加载播放器失败：%r", e)
            if not proj.audio_path or not Path(proj.audio_path).is_file():
                self._repair_audio_path_async(play_src, proj)

        w._apply_project(proj)
        self.load_waveform_audio(proj)
        w.workflow.set_actions_project_state(
            has_media=media_ok,
            has_sentences=bool(proj.sentences),
        )
        w._reset_project_file_state(path=path, modified=False)
        w._last_export_dir = str(path.parent)
        n = len(proj.sentences)
        if not (media_ok and (not proj.audio_path or not Path(proj.audio_path).is_file())):
            # 后台修复进行中时，状态栏由 WorkflowController 接管（mode_label/done_label）
            w._sb_mode.setText(
                f"模式：已打开工程 {path.name}（{n} 句"
                + ("，媒体已关联" if media_ok else "，无媒体/媒体缺失")
                + "）"
            )
        logger.info("[ProjectController] 工程已打开：%s（%d 句）", path, n)

    def _repair_audio_path_async(self, play_src: Path, project: SubtitleProject) -> None:
        """工程 audio_path 缺失时，后台探测/提取以修复（不阻塞打开）。"""
        w = self._win
        from workers import MediaPrepWorker
        worker = MediaPrepWorker(play_src, do_extract_vocals=False, parent=w)
        worker.prepared.connect(
            lambda ap, info, ve: self._on_audio_path_repaired(project, ap, info)
        )
        w.workflow.bind_and_start_worker(
            worker, mode_label="正在恢复工程音频…", done_label="已打开工程",
        )

    def _on_audio_path_repaired(self, project, audio_path, info) -> None:
        """后台修复完成：写入工程音频路径并补波形（工程可能已被替换，先校验）。"""
        w = self._win
        if w._project is not project or audio_path is None:
            return
        project.audio_path = str(audio_path)
        if info is not None and float(project.media_duration or 0) <= 0:
            project.media_duration = float(info.duration)
        self.load_waveform_audio(project)

    def load_waveform_audio(self, project: SubtitleProject) -> None:
        w = self._win
        if project is None or not project.audio_path:
            w.waveform.set_audio(None, 16000)
            return
        try:
            audio_np, sr = audio_io.load_audio(project.audio_path, mono=True)
            if audio_np is None or audio_np.size == 0:
                w.waveform.set_audio(None, sr or 16000)
                return
            try:
                info = audio_io.get_audio_info(project.audio_path)
                project.media_duration = float(info.duration)
            except Exception:
                logger.debug("[波形] 获取音频时长失败")
            w.waveform.set_audio(audio_np, int(sr))
        except Exception as e:  # noqa: BLE001
            logger.warning("[ProjectController] 加载波形音频失败：%r", e)
            try:
                w.waveform.set_audio(None, 16000)
            except Exception:
                logger.debug("[波形] 重置波形失败")

    def handle_drag_enter(self, event) -> None:
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1 and urls[0].isLocalFile():
                path = urls[0].toLocalFile()
                if is_media_file(path) or is_project_file(path):
                    event.acceptProposedAction()
                    return
                from subs import is_subtitle_file
                if is_subtitle_file(path):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def handle_drop(self, event) -> None:
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            path = urls[0].toLocalFile()
            if is_media_file(path):
                self.open_media_file(Path(path))
                event.acceptProposedAction()
                return
            if is_project_file(path):
                if sniff_project_file(path):
                    self.open_project_file(Path(path))
                else:
                    logger.info("[ProjectController] 拖放的 .json 非 Subtitle Studio 工程，忽略")
                event.acceptProposedAction()
                return
            from subs import is_subtitle_file
            if is_subtitle_file(path):
                self.import_subtitle_file(Path(path))
                event.acceptProposedAction()
                return
        event.ignore()


__all__ = ["ProjectController", "is_media_file", "is_project_file", "probe_duration"]
