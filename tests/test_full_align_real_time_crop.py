"""对齐：首次全文重对齐**不得用句级占位时间裁音频**（回归）。

缺陷链：ASR / 纯文本导入产出的句级时间是**占位值**（字符占比均分），旧实现在
``n_seg > 1`` 时按语言段的 ``[min(start_time), max(end_time)]`` 裁音频 → 段内内容
完全对不上。单语项目 ``n_seg == 1`` 零裁剪所以免疫，是「按语言分段」把原本能自愈
的流程弄坏的。

现役实现（本文件守护）：
1. 分段按 **Qwen 分词器族**收敛——中/粤/英等共用默认「CJK 逐字 + 空格词」分词器，
   合并为一次调用（零裁剪）；
2. MMS 后端语言**不进模型**，整段一次调用 + 逐句读音语言；
3. 真·族冲突（ja/ko 与其它语言同段混排）走**两阶段**：阶段一用整段音频拿真实
   时间，阶段二只在真实时间窗上裁音频。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.align_engine import AlignConfig, align_full_text
from core.text_utils import extract_pure_words
from subs.models import Sentence, SubtitleProject, WordTimestamp

pytestmark = pytest.mark.logic

SR = 16000


def _project(entries, *, media_duration):
    """entries: [(text, lang, start, end), ...]"""
    return SubtitleProject(
        audio_path="mock.wav",
        media_duration=media_duration,
        sentences=[
            Sentence(text=t, start_time=s, end_time=e, language=lang)
            for t, lang, s, e in entries
        ],
    )


def test_multi_family_crop_uses_phase1_real_time_not_placeholder():
    """ja 段必须按**阶段一真实时间**裁窗，而不是句级占位时间。

    占位时间（字符占比 4:3 均分整段）与真实发音位置（zh 0-2s / ja 40-42s）
    故意错开 ~17s：若代码回退到「用占位时间裁音频」，本用例必须变红。
    """
    proj = _project(
        [
            ("你好世界", "zh", 0.0, 57.143),
            ("さくら", "ja", 57.143, 100.0),
        ],
        media_duration=100.0,
    )
    audio = np.zeros(SR * 100, dtype=np.float32)
    calls: list = []

    def fake_raw(audio_tuple, text, language, *, model_manager):
        pure = extract_pure_words(text)
        calls.append({"language": language, "n_samples": len(audio_tuple[0])})
        if language == "Chinese":
            # 阶段一：整段全文一次对齐 → 返回「真实」时间
            times = [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0)]
            times += [(40.0, 42.0)] * max(0, len(pure) - len(times))
        else:
            # 阶段二：段内相对时间（绝对时间由裁剪窗 offset 加回）
            times = [(0.0, 2.0)] * len(pure)
        return [
            {"text": w, "start_time": t0, "end_time": t1}
            for w, (t0, t1) in zip(pure, times)
        ]

    with patch("core.align_engine.align_sentence_raw", side_effect=fake_raw), \
         patch("core.audio_io.load_audio", return_value=(audio, SR)), \
         patch("importlib.util.find_spec", return_value=object()):  # 假装 nagisa 已装
        align_full_text(
            proj, model_manager=MagicMock(),
            cfg=AlignConfig(align_backend="qwen", source_language="auto"),
        )

    # 两阶段：阶段一（Chinese 全文）+ 阶段二（Japanese 精修）
    assert [c["language"] for c in calls] == ["Chinese", "Japanese"]
    # 阶段一拿到的是**整段音频**（零裁剪）
    assert calls[0]["n_samples"] == len(audio)

    # 阶段二窗长必须≈真实段长（2s + pad），而不是占位段长（≈43s）
    ja_samples = calls[1]["n_samples"]
    assert ja_samples <= 5 * SR, (
        f"ja 段窗长 {ja_samples / SR:.1f}s 应≈真实段长 2s；"
        "窗长接近 40s+ 说明仍在用句级占位时间裁音频"
    )

    ja_sent = proj.sentences[1]
    assert ja_sent.language == "ja"
    assert abs(ja_sent.start_time - 40.0) <= 1.0, (
        f"ja 段起点应≈40s（阶段一真实时间），实际 {ja_sent.start_time}"
    )
    assert abs(ja_sent.start_time - 57.143) > 10.0, (
        "ja 段起点落在句级占位时间上 → 仍在用占位时间裁音频"
    )

    # 主导族（zh）句由阶段一提交，时间同样是真实的
    zh_sent = proj.sentences[0]
    assert zh_sent.start_time == 0.0
    assert zh_sent.end_time == 2.0


def test_single_family_merges_to_one_call_without_crop():
    """zh + en 同族（默认分词器）→ 一次调用、零裁剪，逐句语言仍按句标注。"""
    proj = _project(
        [("你好世界", "zh", 0.0, 5.0), ("Hello world", "en", 5.0, 10.0)],
        media_duration=10.0,
    )
    audio = np.zeros(SR * 10, dtype=np.float32)
    calls: list = []

    def fake_raw(audio_tuple, text, language, *, model_manager):
        pure = extract_pure_words(text)
        calls.append({
            "language": language, "n_samples": len(audio_tuple[0]), "text": text,
        })
        return [
            {"text": w, "start_time": i * 0.3, "end_time": i * 0.3 + 0.25}
            for i, w in enumerate(pure)
        ]

    with patch("core.align_engine.align_sentence_raw", side_effect=fake_raw), \
         patch("core.audio_io.load_audio", return_value=(audio, SR)):
        align_full_text(
            proj, model_manager=MagicMock(),
            cfg=AlignConfig(align_backend="qwen", source_language="auto"),
        )

    assert len(calls) == 1, "zh+en 同属 Qwen 默认分词器族，应合并为一次调用"
    assert calls[0]["language"] == "Chinese"
    assert calls[0]["n_samples"] == len(audio), "单族路径必须零裁剪（整段音频）"
    assert calls[0]["text"] == "你好世界 Hello world"

    # 一次调用 ≠ 语言被抹平：逐句 word.language 仍按句回填
    assert all(w.language == "Chinese" for w in proj.sentences[0].words)
    assert all(w.language == "English" for w in proj.sentences[1].words)


def test_mms_single_call_and_per_sentence_language():
    """MMS 语言不进模型 → 整段一次调用、零裁剪；读音语言按句给出。"""
    from core.mms_aligner.engine import MMSAligner

    # 独立判据（不靠被测代码自证）：语言确实改变罗马化——数字拼读表不同
    assert MMSAligner._expand_digits(None, "2024", "japanese") != \
        MMSAligner._expand_digits(None, "2024", "chinese")

    proj = _project(
        [("你好", "zh", 0.0, 5.0), ("さくら", "ja", 5.0, 10.0)],
        media_duration=10.0,
    )
    audio = np.zeros(SR * 10, dtype=np.float32)
    calls: list = []

    mock = MagicMock()
    mock.is_available.return_value = True

    def fake_align(audio_tuple, text, *, language, offset_sec=0.0,
                   word_languages=None, **_kw):
        calls.append({
            "language": language,
            "text": text,
            "n_samples": len(audio_tuple[0]),
            "offset": offset_sec,
            "word_languages": list(word_languages or []),
        })
        return [
            WordTimestamp(text=w, start_time=offset_sec + i * 0.3,
                          end_time=offset_sec + i * 0.3 + 0.25, language=language)
            for i, w in enumerate(extract_pure_words(text))
        ]

    mock.align.side_effect = fake_align
    mm = MagicMock()
    mm.using_mms_aligner.return_value.__enter__.return_value = mock

    with patch("core.align_engine.get_mms_aligner", return_value=mock), \
         patch("core.audio_io.load_audio", return_value=(audio, SR)):
        align_full_text(
            proj, model_manager=mm,
            cfg=AlignConfig(align_backend="mms", source_language="auto"),
        )

    assert len(calls) == 1, "MMS 语言不进模型，不应按语言分段"
    assert calls[0]["n_samples"] == len(audio), "MMS 必须零裁剪（整段音频）"
    assert calls[0]["offset"] == 0.0
    assert calls[0]["text"] == "你好 さくら"

    zh_n = len(extract_pure_words("你好"))
    ja_n = len(extract_pure_words("さくら"))
    wl = calls[0]["word_languages"]
    assert wl[:zh_n] == ["Chinese"] * zh_n
    assert wl[zh_n:zh_n + ja_n] == ["Japanese"] * ja_n

    # 结果按句标注语言
    assert all(w.language == "Chinese" for w in proj.sentences[0].words)
    assert all(w.language == "Japanese" for w in proj.sentences[1].words)
