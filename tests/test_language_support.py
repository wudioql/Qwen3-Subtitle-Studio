"""tests/test_language_support.py — 语言支持域 + Qwen 分词依赖预检（纯逻辑，零 Qt）。

项目语言域 = Qwen3-ForcedAligner 官方支持的 11 种（transformers 侧
`FORCED_ALIGNER_LANGUAGES`）。钉住：

1. core.language_utils 与上游官方集合逐项同步（防漂移）+ ui.languages 两套下拉结构
   （auto 仅全局、空项仅句级）+ 短码/全名/中文名双向映射；
2. ASR auto 检测到范围外语言 → transcribe() 及早报「11 种语言」清晰错误；
3. 句级语言优先于项目语言（混语项目逐句按各自语言调用对齐器）；
4. Qwen 分词依赖预检：ja→nagisa / ko→soynlp 缺包开工前 fail-fast（含安装指引与
   MMS 替代），MMS 后端完全不受影响；align_project / align_dirty_only / align_full_text
   均开工前拦截（连音频都不加载），align_sentence 直调兜底门。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import numpy as np
import pytest

pytestmark = pytest.mark.logic

from core.language_utils import LANG_SHORT_TO_FULL, resolve_language

from core import align_engine
from core.align_engine import (
    AlignConfig,
    align_dirty_only,
    align_full_text,
    align_project,
    align_sentence,
    check_segmenter_dependency,
    preflight_segmenter_deps,
)
from core.text_utils import extract_pure_words
from subs.models import Sentence, SubtitleProject, WordTimestamp

_SUPPORTED_FULL_NAMES = {v for v in LANG_SHORT_TO_FULL.values() if v is not None}
_SUPPORTED_SHORTS = {k for k in LANG_SHORT_TO_FULL if k != "auto"}

_NO_SPEC = patch("importlib.util.find_spec", return_value=None)        # 模拟「什么都没装」
_HAS_SPEC = patch("importlib.util.find_spec", return_value=object())   # 模拟「依赖齐全」


def _ja_project(dirty: bool = False) -> SubtitleProject:
    return SubtitleProject(
        audio_path="mock.wav", source_language="auto", media_duration=10.0,
        sentences=[Sentence(
            text="桜咲く", start_time=0.0, end_time=2.0,
            language="ja", is_dirty=dirty,
        )],
    )


# ══════════════════════════════════════════════════════════════
# 1. 语言域与上游同步 + UI 下拉 + 双向映射
# ══════════════════════════════════════════════════════════════

def test_language_upstream_set_and_ui_sync():
    # ── language set matches forced aligner upstream ─────────────────────────
    from transformers.models.qwen3_asr.processing_qwen3_asr import (
        FORCED_ALIGNER_LANGUAGES,
    )
    assert _SUPPORTED_FULL_NAMES == set(FORCED_ALIGNER_LANGUAGES), (
        f"语言域漂移: 项目={sorted(_SUPPORTED_FULL_NAMES)} "
        f"上游={sorted(FORCED_ALIGNER_LANGUAGES)}"
    )
    assert len(_SUPPORTED_SHORTS) == 11
    # 每个短码都能解析回全名；范围外短码 → None
    for short, full in LANG_SHORT_TO_FULL.items():
        assert resolve_language(short) == full
    assert resolve_language("th") is None
    assert resolve_language("vi") is None
    assert resolve_language("ar") is None

    # ── ui language lists synced ─────────────────────────
    from ui.languages import (
        GLOBAL_LANGUAGES, SENTENCE_LANGUAGES, code_to_name, name_to_code,
    )
    sent_codes = [c for c, _ in SENTENCE_LANGUAGES]
    glob_codes = [c for c, _ in GLOBAL_LANGUAGES]
    assert sent_codes[0] == ""                     # 空项（未设置）仅句级列
    assert sorted(sent_codes[1:]) == sorted(_SUPPORTED_SHORTS)
    assert glob_codes[0] == "auto"                 # auto 仅全局列
    assert sorted(glob_codes[1:]) == sorted(_SUPPORTED_SHORTS)
    # 双向映射契约（含全名归一）
    assert code_to_name("zh") == "中文"
    assert code_to_name("Chinese") == "中文"
    assert code_to_name("ja") == "日语"
    assert name_to_code("日语") == "ja"
    assert name_to_code("粤语") == "yue"
    assert name_to_code("auto") == "auto"


def _case_asr_language_guard():
    import torch

    from core import asr_engine
    from core.asr_engine import TranscribeConfig

    # ── asr out of scope language early error ─────────────────────────
    proc = MagicMock()
    inputs = {"input_ids": torch.zeros((1, 2), dtype=torch.long)}
    batch = MagicMock()
    batch.to.return_value = inputs
    batch.__getitem__.side_effect = inputs.__getitem__
    proc.apply_transcription_request.return_value = batch
    proc.decode.return_value = [{"language": "Thai", "transcription": "สวัสดีครับ"}]
    model = MagicMock()
    model.generate.return_value = torch.zeros((1, 5), dtype=torch.long)

    mm = MagicMock()
    mm.using_asr.return_value.__enter__.return_value = (proc, model)

    with patch.object(
        asr_engine, "prepare_audio",
        return_value=("/tmp/x.wav", SimpleNamespace(duration=5.0, sample_rate=16000)),
    ):
        with pytest.raises(ValueError, match="11 种语言"):
            asr_engine.transcribe("/tmp/dummy.mp3", model_manager=mm, cfg=TranscribeConfig())

    # ── asr supported language passes guard ─────────────────────────
    proc2 = MagicMock()
    batch2 = MagicMock()
    batch2.to.return_value = inputs
    batch2.__getitem__.side_effect = inputs.__getitem__
    proc2.apply_transcription_request.return_value = batch2
    proc2.decode.return_value = [{"language": "Japanese", "transcription": "こんにちは。"}]
    model2 = MagicMock()
    model2.generate.return_value = torch.zeros((1, 5), dtype=torch.long)

    mm2 = MagicMock()
    mm2.using_asr.return_value.__enter__.return_value = (proc2, model2)

    # 守卫之后即返回（不真跑对齐）：用 align_full_text 抛哨兵异常证明已越过语言守卫
    with patch.object(
        asr_engine, "prepare_audio",
        return_value=("/tmp/x.wav", SimpleNamespace(duration=5.0, sample_rate=16000)),
    ), patch.object(
        asr_engine, "align_full_text", side_effect=RuntimeError("SENTINEL_PASSED_GUARD"),
    ):
        with pytest.raises(RuntimeError, match="SENTINEL_PASSED_GUARD"):
            asr_engine.transcribe("/tmp/dummy.mp3", model_manager=mm2, cfg=TranscribeConfig())


def _case_sentence_language_priority_over_project():
    from core.align_engine import _infer_full_language

    assert _infer_full_language("auto", "ja", "zh") == "Japanese"
    assert _infer_full_language("auto", "Japanese", "zh") == "Japanese"
    assert _infer_full_language("auto", "", "zh") == "Chinese"
    assert _infer_full_language("pt", "ja", "zh") == "Portuguese"

    # 行为钉样：dirty 重对齐对每句按其语言调用对齐器（混语项目逐句生效）
    proj = SubtitleProject(
        audio_path="mock.wav", source_language="zh",
        sentences=[
            Sentence(text="さくら", start_time=0.0, end_time=1.0, language="ja", is_dirty=True),
            Sentence(text="青紫色", start_time=2.0, end_time=3.0, language="zh", is_dirty=True),
        ],
    )
    audio = np.zeros(48000, dtype=np.float32)

    def fake_align(audio_tuple, text, *, language, offset_sec=0.0, tail_limit_sec=None):
        fake_align.calls.append(language)
        return [
            WordTimestamp(text=w, start_time=offset_sec + i * 0.2, end_time=offset_sec + i * 0.2 + 0.15)
            for i, w in enumerate(extract_pure_words(text))
        ]

    def fake_align_ctx(audio_tuple, prev_text, text, next_text, *, language, offset_sec=0.0):
        fake_align.calls.append(language)
        return [
            WordTimestamp(text=w, start_time=offset_sec + i * 0.2, end_time=offset_sec + i * 0.2 + 0.15)
            for i, w in enumerate(extract_pure_words(text))
        ]

    fake_align.calls = []
    mock_mms = MagicMock()
    mock_mms.is_available.return_value = True
    mock_mms.align.side_effect = fake_align
    mock_mms.align_with_context.side_effect = fake_align_ctx
    mm = MagicMock()

    with patch("core.align_engine.get_mms_aligner", return_value=mock_mms), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        align_dirty_only(proj, model_manager=mm, cfg=AlignConfig(align_backend="mms"))

    assert fake_align.calls == ["Japanese", "Chinese"], fake_align.calls
    assert proj.sentences[0].word_count() == 3
    assert proj.sentences[1].word_count() == 3


# ══════════════════════════════════════════════════════════════
# 2. Qwen 分词依赖预检
# ══════════════════════════════════════════════════════════════

def _case_segmenter_dependency_pure_logic_and_backend_gating():
    # ── check segmenter dependency pure ─────────────────────────
    with _HAS_SPEC:
        assert check_segmenter_dependency("Japanese") is None
        assert check_segmenter_dependency("Korean") is None
    with _NO_SPEC:
        msg = check_segmenter_dependency("Japanese")
        assert msg and "nagisa" in msg and "pip install nagisa" in msg and "MMS-FA" in msg
        msg_ko = check_segmenter_dependency("Korean")
        assert msg_ko and "soynlp" in msg_ko and "pip install soynlp" in msg_ko
        # 英/中/俄等不需要官方分词包
        assert check_segmenter_dependency("English") is None
        assert check_segmenter_dependency("Chinese") is None
        assert check_segmenter_dependency("Russian") is None
        assert check_segmenter_dependency("") is None

    # ── preflight backend gating ─────────────────────────
    with _NO_SPEC:
        preflight_segmenter_deps(["Japanese"], backend="mms")          # MMS 链路不校验
        preflight_segmenter_deps(["English", "Chinese"], backend="qwen")
        with pytest.raises(RuntimeError, match="nagisa") as exc_info:
            preflight_segmenter_deps(["Chinese", "Japanese", "Korean"], backend="qwen")
        assert "soynlp" in str(exc_info.value)                          # 多语言一次报全


def _case_segmenter_fail_fast_all_entry_points():
    # ── align project fail fast before loading ─────────────────────────
    proj = _ja_project()
    mm = MagicMock()
    with _NO_SPEC, patch("core.audio_io.load_audio") as m_load:
        with pytest.raises(RuntimeError, match="nagisa"):
            align_project(proj, model_manager=mm, cfg=AlignConfig())
    m_load.assert_not_called()

    # ── align dirty only fail fast before loading ─────────────────────────
    proj = _ja_project(dirty=True)
    assert list(proj.alignable_dirty_indices()) == [0]
    mm = MagicMock()
    with _NO_SPEC, patch("core.audio_io.load_audio") as m_load:
        with pytest.raises(RuntimeError, match="nagisa"):
            align_dirty_only(proj, model_manager=mm, cfg=AlignConfig())
    m_load.assert_not_called()

    # ── align full text fail fast before loading ─────────────────────────
    proj = _ja_project()
    mm = MagicMock()
    with _NO_SPEC, patch("core.audio_io.load_audio") as m_load:
        with pytest.raises(RuntimeError, match="nagisa"):
            align_full_text(proj, model_manager=mm, cfg=AlignConfig())
    m_load.assert_not_called()

    # ── align sentence direct gate ─────────────────────────
    sent = Sentence(text="桜咲く", start_time=0.0, end_time=2.0, language="ja")
    audio = np.zeros(16000 * 3, dtype=np.float32)
    with _NO_SPEC:
        with pytest.raises(ImportError, match="pip install nagisa"):
            align_sentence(sent, audio, 16000, model_manager=MagicMock(), cfg=AlignConfig())


def _case_segmenter_mms_unaffected():
    # ── mms backend not blocked by preflight ─────────────────────────
    proj = _ja_project()
    audio = np.zeros(16000 * 3, dtype=np.float32)

    def fake_align(audio_tuple, text, *, language, offset_sec=0.0, tail_limit_sec=None):
        return [
            WordTimestamp(text=w, start_time=offset_sec + i * 0.2,
                          end_time=offset_sec + i * 0.2 + 0.15, language=language)
            for i, w in enumerate(extract_pure_words(text))
        ]

    mms = MagicMock()
    mms.is_available.return_value = True
    mms.align.side_effect = fake_align
    mm = MagicMock()
    mm.using_mms_aligner.return_value.__enter__.return_value = mms

    with _NO_SPEC, \
         patch.object(align_engine, "get_mms_aligner", return_value=mms), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        align_project(proj, model_manager=mm, cfg=AlignConfig(align_backend="mms"))

    got = [w.text for w in proj.sentences[0].words if not w.is_punct]
    assert got == ["桜", "咲", "く"]
    assert all(w.language == "Japanese" for w in proj.sentences[0].words)


def test_asr_language_pack():
    """test_asr_language_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_asr_language_guard()
    _case_sentence_language_priority_over_project()


def test_segmenter_deps_pack():
    """test_segmenter_deps_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_segmenter_dependency_pure_logic_and_backend_gating()
    _case_segmenter_fail_fast_all_entry_points()
    _case_segmenter_mms_unaffected()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
