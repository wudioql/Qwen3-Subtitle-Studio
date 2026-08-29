"""tests/test_app_smoke.py — 应用级冒烟（前身为 tests/phase2_import_smoke.py 手跑脚本；压缩 12 → 6）

只做 import + 纯逻辑小验证 + Qt 离屏面板契约，不加载模型：
 1. constants 数值契约 + core 模块集 smoke-import（旧 audio probe 已由 test_wav_passthrough 覆盖）
 2. subs.models 序列化往返
 3. ModelManager init + 语言解析 + align_engine 纯逻辑合并
 4. _words_to_sentences 分句（默认合并 / 低门槛切分）合并
 5. workers QThread 封装 signal 契约
 6. ui 面板/MainWindow/SubsEditor 对外属性・signal・公共 API 契约合并
"""

from __future__ import annotations


from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

from core.constants import ASR_MAX_DURATION, ALIGNER_MAX_DURATION  # noqa: E402

import pytest

from subs.models import WordTimestamp, Sentence, SubtitleProject  # noqa: E402


def _case_constants_and_core_import_smoke():
    assert ASR_MAX_DURATION == 1200.0
    assert ALIGNER_MAX_DURATION == 300.0
    import importlib
    for _mod in ("model_manager", "asr_engine", "align_engine", "audio_io",
                 "constants", "language_utils"):
        m = importlib.import_module(f"core.{_mod}")
        assert m.__name__ == f"core.{_mod}"


def _case_models_serialization_roundtrip():
    w1 = WordTimestamp(text="你", start_time=0.0, end_time=0.15, language="Chinese")
    w2 = WordTimestamp(text="好", start_time=0.15, end_time=0.3, language="Chinese")
    s1 = Sentence(text="你好。", start_time=0.0, end_time=0.3, words=[w1, w2], language="Chinese")
    p = SubtitleProject(audio_path="demo.wav", sentences=[s1])
    p2 = SubtitleProject.from_dict(p.to_dict())
    assert len(p2.sentences) == 1 and len(p2.sentences[0].words) == 2
    assert p2.sentences[0].words[0].text == "你"


def _case_core_logic_contracts():
    # ModelManager 初始化状态
    from core.model_manager import ModelManager
    mm = ModelManager()
    assert mm.status is not None

    # asr_engine 语言解析：短码 → 全名；auto/未知 → None
    from core.asr_engine import _resolve_language
    assert _resolve_language("zh") == "Chinese"
    assert _resolve_language("en") == "English"
    assert _resolve_language("auto") is None
    assert _resolve_language("__unknown__") is None

    # align_engine 语言推断 + _crop_audio 切片
    from core.align_engine import _infer_full_language, _crop_audio
    import numpy as np
    assert _infer_full_language("auto", "Chinese", "") == "Chinese"
    assert _infer_full_language("en", "", "") == "English"
    assert _infer_full_language("auto", "zh", "") == "Chinese"
    dummy = np.arange(16000 * 10, dtype=np.float32)
    cropped, _s, _e = _crop_audio(dummy, 16000, 1.0, 3.0, pad_before=0.0, pad_after=0.0)
    assert abs(cropped.shape[0] - 32000) < 10


_WORDS_SAMPLE = [
    WordTimestamp(text="你", start_time=0.0, end_time=0.1),
    WordTimestamp(text="好", start_time=0.1, end_time=0.2),
    WordTimestamp(text="。", start_time=0.2, end_time=0.25),
    WordTimestamp(text="今", start_time=0.3, end_time=0.4),
    WordTimestamp(text="天", start_time=0.4, end_time=0.5),
    WordTimestamp(text="天", start_time=0.5, end_time=0.6),
    WordTimestamp(text="气", start_time=0.6, end_time=0.7),
    WordTimestamp(text="不", start_time=0.7, end_time=0.8),
    WordTimestamp(text="错", start_time=0.8, end_time=0.9),
    WordTimestamp(text="！", start_time=0.9, end_time=0.95),
]


def _case_words_to_sentences_merge_and_split():
    from core.asr_engine import TranscribeConfig, _words_to_sentences
    # 默认门槛：短句合并成 1 句
    sents = _words_to_sentences(_WORDS_SAMPLE, cfg=TranscribeConfig(), project_language="Chinese")
    assert len(sents) == 1 and sents[0].text == "你好。今天天气不错！"
    # 低门槛：按标点切成 2 句
    sents2 = _words_to_sentences(
        _WORDS_SAMPLE,
        cfg=TranscribeConfig(min_sentence_chars=1, min_sentence_sec=0.01),
        project_language="Chinese",
    )
    assert len(sents2) == 2
    assert sents2[0].text == "你好。" and sents2[1].text.startswith("今天天气不错")


def _case_workers_signal_contract():
    from workers import TranscribeWorker, AlignWorker
    for _cls, _signals in (
        (TranscribeWorker, ("progress", "project", "failed", "cancelled", "finished_ok", "log")),
        (AlignWorker, ("progress", "project", "sentence_aligned", "failed", "cancelled", "finished_ok", "log")),
    ):
        for s in _signals:
            assert hasattr(_cls, s), f"{_cls.__name__} 缺少 signal {s}"


@pytest.mark.ui
def _case_ui_mainwindow_editor_contract():
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from ui.player_panel import PlayerPanel
    from ui.waveform_view import WaveformView
    from ui.subs_editor import SubsEditor
    from ui.main_window import MainWindow
    from main import main as _main_entry  # noqa: F401 — 入口可导入

    mw = MainWindow()
    try:
        for attr in ("model_manager", "current_project", "player", "editor", "waveform"):
            assert hasattr(mw, attr), f"MainWindow 缺少对外属性 {attr}"
        for attr in ("model_manager", "player", "editor", "waveform"):
            assert getattr(mw, attr, None) is not None, f"{attr} 不应为 None"
        for sig in ("position_changed", "duration_changed", "state_playing", "state_paused",
                    "state_stopped"):
            assert hasattr(PlayerPanel, sig), f"PlayerPanel 缺 signal {sig}"
        # 预览面板公共 API（编辑态/预览态切换已移除，本面板只负责预览）
        for api in ("set_project", "set_preview_time", "preview_mode"):
            assert hasattr(PlayerPanel, api), f"PlayerPanel 缺公共 API {api}"
        for sig in ("region_changed", "playhead_seeked"):
            assert hasattr(WaveformView, sig), f"WaveformView 缺 signal {sig}"
        for sig in ("row_selected", "row_number_clicked", "text_changed", "time_changed",
                    "language_changed"):
            assert hasattr(SubsEditor, sig), f"SubsEditor 缺 signal {sig}"
        # 跨层访问走公共 API（不触碰私有成员）
        for api in ("show_word_sentence", "current_word_sentence_index"):
            assert hasattr(SubsEditor, api), f"SubsEditor 缺公共 API {api}"
        for api in ("active_sentence_index", "refresh_sentence_visuals"):
            assert hasattr(WaveformView, api), f"WaveformView 缺公共 API {api}"
    finally:
        mw.close()

    # SubsEditor 字级视图公共 API 行为
    proj = SubtitleProject(
        source_media_path="m.wav", media_duration=10.0, sample_rate=16000,
        sentences=[
            Sentence(text="第一句", start_time=1.0, end_time=2.0, words=[]),
            Sentence(text="第二句", start_time=3.0, end_time=4.0, words=[]),
        ],
    )
    editor = SubsEditor()
    try:
        editor.set_project(proj)
        assert editor.current_word_sentence_index == -1
        editor.show_word_sentence(1)                       # 不切换 Tab 的轻量展示
        assert editor.current_word_sentence_index == 1
        assert editor.is_word_view_active() is False
    finally:
        editor.close()


def _case_model_reactivation_reports_indeterminate_stage():
    from unittest.mock import MagicMock
    from core.model_manager import ModelManager

    manager = ModelManager(device="cpu")
    manager.asr_model = MagicMock()
    manager.asr_processor = MagicMock()
    manager.asr_state = "in_ram"
    events = []
    with manager.using_asr(progress_cb=lambda d, t, text: events.append((d, t, text))):
        assert manager.asr_state == "in_vram"
    assert any(done == total == 0 and "RAM" in text for done, total, text in events)
    assert manager.asr_state == "in_ram"  # context 退出自动 park


# ── 聚合入口 ──────────────────────────────────────────────────────

@pytest.mark.logic
def _case_core_smoke_pack():
    """核心冒烟 5 合 1：常量与 import / 模型序列化 / 核心契约 / 分句合并 / worker 信号。"""
    _case_constants_and_core_import_smoke()
    _case_models_serialization_roundtrip()
    _case_core_logic_contracts()
    _case_words_to_sentences_merge_and_split()
    _case_workers_signal_contract()


def test_app_smoke_pack():
    """test_app_smoke_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_ui_mainwindow_editor_contract()
    _case_model_reactivation_reports_indeterminate_stage()
    _case_core_smoke_pack()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
