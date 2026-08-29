"""tests/test_align_engine.py — core.align_engine 对齐编排（纯逻辑；align_worker 用例函数内懒 import Qt）。

覆盖：
- attach 字符级序列对齐切句（ja 形态素 1:n 插值 / en 撇号连字符拆分 / n:1 合并 /
  跨句界拆分 / 真失配回退 + WARNING / 精确路径不变）；
- 全文重对齐语言分段（11 语言归段 / 单块路径混语逐段调用 / 单语零裁剪 /
  多语言 context 只进一次 / >300s 切块 + 锁定句快照恢复 / 重叠块取最近快照）；
- 锁定保护（align_dirty_only 跳过干净句与锁定句；align_project 跳过锁定句；
  失败事务性保留旧词；MMS 缺失 fail-fast；AlignWorker dirty 无脏句早退）；
- 零时长/塌陷字平滑修复（sanitize_word_timestamps）；
- AlignWorker 逐句/脏句全失败 → 明确 RuntimeError（不静默「完成」）。
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import numpy as np

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

pytestmark = pytest.mark.logic

from core import align_engine
from core.align_engine import (
    AlignConfig,
    _resolve_language_segments,
    align_dirty_only,
    align_full_text,
    align_project,
)
from core.text_utils import (
    attach_words_to_sentences,
    extract_pure_words,
    sanitize_word_timestamps,
    words_content_match,
)
from subs.models import Sentence, SubtitleProject, WordTimestamp


class _FakeContext:
    def __enter__(self):
        return None

    def __exit__(self, *args):
        pass


class _FakeModelManager:
    def using_aligner(self, **_kwargs):
        return _FakeContext()

    def using_mms_aligner(self, **_kwargs):
        return _FakeContext()


# ═════════════════════════════════════════════════════════════
# 1. attach 字符级序列对齐切句（原 test_attach_seq_align.py）
# ═════════════════════════════════════════════════════════════
def _w(text: str, s: float, e: float, lang: str = "") -> WordTimestamp:
    # 引擎两侧（Qwen decode / MMS）产出的时间都 round 到 3 位小数，mock 保持一致
    return WordTimestamp(
        text=text, start_time=round(s, 3), end_time=round(e, 3), language=lang,
    )


def _non_punct(s: Sentence):
    return [w for w in s.words if not w.is_punct]


# ─────────────────────────────────────────────────────────────
# 1. ja 形态素（粗）→ 逐字/音拍（细）：1:n 插值拆分
# ─────────────────────────────────────────────────────────────

def _case_seq_align_1n_split_pack():
    # ── ja morpheme split interpolation ─────────────────────────
    sent = Sentence(text="桜の花が咲きました", start_time=0.0, end_time=1.0, language="Japanese")
    # nagisa 形态素 7 词；extract_pure_words 逐字 9 词 → 计数恒不等
    aligner = [
        _w("桜", 0.0, 0.1, "Japanese"), _w("の", 0.1, 0.2, "Japanese"),
        _w("花", 0.2, 0.3, "Japanese"), _w("が", 0.3, 0.4, "Japanese"),
        _w("咲き", 0.4, 0.6, "Japanese"), _w("まし", 0.6, 0.8, "Japanese"),
        _w("た", 0.8, 0.9, "Japanese"),
    ]
    attach_words_to_sentences([sent], aligner)

    got = _non_punct(sent)
    assert [w.text for w in got] == ["桜", "の", "花", "が", "咲", "き", "ま", "し", "た"]
    # 「咲き」0.4–0.6 按字符数等分：咲 [0.4,0.5]、き [0.5,0.6]
    saku = {w.text: w for w in got}
    assert (saku["咲"].start_time, saku["咲"].end_time) == (0.4, 0.5)
    assert (saku["き"].start_time, saku["き"].end_time) == (0.5, 0.6)
    assert (saku["ま"].start_time, saku["ま"].end_time) == (0.6, 0.7)
    # 序列单调 + 内容不变量
    for a, b in zip(got, got[1:]):
        assert a.start_time <= b.start_time
    assert words_content_match(sent)
    assert all(w.language == "Japanese" for w in got)

    # ── en apostrophe split and punct refill ─────────────────────────
    sent = Sentence(text="I don't know", start_time=0.0, end_time=1.0, language="English")
    aligner = [
        _w("I", 0.0, 0.2, "English"),
        _w("don't", 0.2, 0.8, "English"),
        _w("know", 0.8, 1.0, "English"),
    ]
    attach_words_to_sentences([sent], aligner)

    got = _non_punct(sent)
    assert [w.text for w in got] == ["I", "don", "t", "know"]
    # "dont" 4 字符占 0.6s：don(3 字符)=0.45s → [0.2,0.65]，t=[0.65,0.8]
    by_text = {w.text: w for w in got}
    assert (by_text["don"].start_time, by_text["don"].end_time) == (0.2, 0.65)
    assert (by_text["t"].start_time, by_text["t"].end_time) == (0.65, 0.8)
    # 撇号照常回填为标点词（比例回退路径会整体跳过回填）
    puncts = [w for w in sent.words if w.is_punct]
    assert [p.text for p in puncts] == ["'"]
    assert words_content_match(sent)

    # ── en hyphen swallowed split ─────────────────────────
    sent = Sentence(text="well-known singer", start_time=0.0, end_time=1.0, language="English")
    aligner = [_w("wellknown", 0.0, 0.6, "English"), _w("singer", 0.6, 1.0, "English")]
    attach_words_to_sentences([sent], aligner)

    got = _non_punct(sent)
    assert [w.text for w in got] == ["well", "known", "singer"]
    by_text = {w.text: w for w in got}
    # "wellknown" 9 字符占 0.6s：well(4)=0.267，known 到 0.6
    assert by_text["well"].start_time == 0.0 and abs(by_text["well"].end_time - 0.267) < 1e-3
    assert by_text["known"].end_time == 0.6
    assert {p.text for p in sent.words if p.is_punct} == {"-"}
    assert words_content_match(sent)


def _case_seq_align_n1_and_cross_boundary():
    # ── n to 1 merge range ─────────────────────────
    sent = Sentence(text="cannot stop", start_time=0.0, end_time=1.0, language="English")
    aligner = [
        _w("can", 0.0, 0.3, "English"), _w("not", 0.3, 0.5, "English"),
        _w("stop", 0.5, 1.0, "English"),
    ]
    attach_words_to_sentences([sent], aligner)

    got = _non_punct(sent)
    assert [w.text for w in got] == ["cannot", "stop"]
    assert (got[0].start_time, got[0].end_time) == (0.0, 0.5)   # [首.start, 尾.end]
    assert (got[1].start_time, got[1].end_time) == (0.5, 1.0)
    assert words_content_match(sent)

    # ── cross sentence boundary split ─────────────────────────
    s1 = Sentence(text="go on", start_time=0.0, end_time=1.0, language="English")
    s2 = Sentence(text="now please", start_time=1.0, end_time=2.0, language="English")
    aligner = [
        _w("go", 0.0, 0.2, "English"),
        _w("onnow", 0.2, 0.8, "English"),   # 横跨 s1 的 on 与 s2 的 now
        _w("please", 0.8, 1.2, "English"),
    ]
    attach_words_to_sentences([s1, s2], aligner)

    g1, g2 = _non_punct(s1), _non_punct(s2)
    assert [w.text for w in g1] == ["go", "on"]
    assert [w.text for w in g2] == ["now", "please"]
    # onnow 5 字符占 [0.2,0.8]：on=前 2 字符 → [0.2,0.44]；now=后 3 字符 → [0.44,0.8]
    assert g1[-1].end_time == 0.44
    assert g2[0].start_time == 0.44
    assert words_content_match(s1) and words_content_match(s2)


def _case_true_mismatch_fallback_and_warning(caplog):
    def _run_true_mismatch(sent: Sentence) -> None:
        aligner = [
            _w("hello", 0.0, 0.4, "English"),
            _w("brave", 0.4, 0.7, "English"),   # 内容对不上（非粒度差异）
            _w("world", 0.7, 1.0, "English"),
        ]
        attach_words_to_sentences([sent], aligner)

    # ── true mismatch still proportional behavior ─────────────────────────
    sent = Sentence(text="hello world", start_time=0.0, end_time=1.0, language="English")
    _run_true_mismatch(sent)
    # 比例回退把全部词摊给唯一的句（兜底行为），内容漂移交给字级守门暴露
    got = [w.text for w in sent.words if not w.is_punct]
    assert got == ["hello", "brave", "world"]
    assert not words_content_match(sent)

    # ── true mismatch warning logged ─────────────────────────
    sent = Sentence(text="hello world", start_time=0.0, end_time=1.0, language="English")
    with caplog.at_level(logging.WARNING):
        _run_true_mismatch(sent)
    assert any("比例回退" in r.message for r in caplog.records)


def _case_exact_path_unchanged():
    # ── exact path unchanged ─────────────────────────
    sent = Sentence(text="桜が咲く", start_time=0.0, end_time=1.0, language="Japanese")
    expected = extract_pure_words(sent.text)
    aligner = [_w(w, i * 0.2, i * 0.2 + 0.15, "Japanese") for i, w in enumerate(expected)]
    attach_words_to_sentences([sent], aligner)

    got = _non_punct(sent)
    # 词面与起点逐一来回一致；无标点相邻字的 end 延展至下字 start 是精确路径
    # 原有 merge 行为（test_punct_pipeline 钉样），这里钉住它没被 M4 改动
    assert [w.text for w in got] == [w.text for w in aligner]
    assert [w.start_time for w in got] == [w.start_time for w in aligner]
    for cur, nxt_raw in zip(got, aligner[1:]):
        assert cur.end_time == nxt_raw.start_time
    assert got[-1].end_time == aligner[-1].end_time
    assert words_content_match(sent)


# ── 聚合入口 ──────────────────────────────────────────────────────

def test_attach_seq_align_pack(caplog):
    """attach 序列对齐 4 合 1：1:n 切分 / n:1 与跨界 / 真失配回退+警告 / 精确路径不变。"""
    _case_seq_align_1n_split_pack()
    _case_seq_align_n1_and_cross_boundary()
    _case_true_mismatch_fallback_and_warning(caplog)
    _case_exact_path_unchanged()

# ═════════════════════════════════════════════════════════════
# 2. 全文重对齐语言分段（原 test_full_align_language_segments.py）
# ═════════════════════════════════════════════════════════════
def _mk_project(texts_langs, *, duration=100.0, src_lang="auto"):
    sents = []
    t = 0.0
    for text, lang in texts_langs:
        sents.append(Sentence(
            text=text, start_time=round(t, 3), end_time=round(t + 3.0, 3),
            language=lang,
        ))
        t += 4.0
    return SubtitleProject(
        audio_path="mock.wav", source_language=src_lang,
        media_duration=max(duration, t + 1.0), sentences=sents,
    )


def _mock_mms(calls: list):
    mock = MagicMock()
    mock.is_available.return_value = True

    def fake_align(audio_tuple, text, *, language, offset_sec=0.0):
        calls.append({
            "language": language,
            "text": text,
            "offset": offset_sec,
            "n_samples": len(audio_tuple[0]),
        })
        return [
            WordTimestamp(text=w, start_time=offset_sec + i * 0.25,
                          end_time=offset_sec + i * 0.25 + 0.2, language=language)
            for i, w in enumerate(extract_pure_words(text))
        ]

    mock.align.side_effect = fake_align
    return mock


_ELEVEN = [
    ("你好世界。", "zh"), ("Hello there world.", "en"), ("大家好呀。", "yue"),
    ("Bonjour le monde.", "fr"), ("Hallo liebe Welt.", "de"), ("Ciao bel mondo.", "it"),
    ("桜が咲きました。", "ja"), ("사랑해요 정말.", "ko"),
    ("Olá querido mundo.", "pt"), ("Привет ты мир.", "ru"), ("Hola mi mundo.", "es"),
]
_ELEVEN_FULL = [
    "Chinese", "English", "Cantonese", "French", "German", "Italian",
    "Japanese", "Korean", "Portuguese", "Russian", "Spanish",
]


def _case_resolve_segments_grouping_and_eleven_langs():
    # ── resolve language segments grouping ─────────────────────────
    proj = _mk_project([
        ("第一句。", "zh"), ("第二句。", "zh"),
        ("さくら咲く。", "ja"),
        ("第三句。", "zh"), ("", "zh"),  # 空文本不参与分段
    ])
    segs = _resolve_language_segments(proj, AlignConfig(source_language="auto"))
    assert [lang for lang, _ in segs] == ["Chinese", "Japanese", "Chinese"]
    assert [len(sents) for _, sents in segs] == [2, 1, 1]

    # 句级语言缺省时回落项目语言
    proj2 = _mk_project([("你好。", ""), ("世界。", "zh")], src_lang="zh")
    segs2 = _resolve_language_segments(proj2, AlignConfig(source_language="auto"))
    assert [lang for lang, _ in segs2] == ["Chinese"]

    # 语言完全不可决议 → 明确报错（错误信息带句号）
    proj3 = _mk_project([("hello", "")], src_lang="auto")
    with pytest.raises(ValueError, match="无法推断第 1 句的语言"):
        _resolve_language_segments(proj3, AlignConfig(source_language="auto"))

    # ── resolve segments all eleven languages ─────────────────────────
    # 11 种语言逐句交错 → 11 段、各携官方全名（不再只是中/日/韩）
    proj = _mk_project(_ELEVEN)
    segs = _resolve_language_segments(proj, AlignConfig(source_language="auto"))
    assert [lang for lang, _ in segs] == _ELEVEN_FULL
    assert all(len(sents) == 1 for _, sents in segs)


def _case_single_path_segments_dispatch_and_backend_lang():
    # ── single path mixed language segments ─────────────────────────
    proj = _mk_project([
        ("风掠过指尖。", "zh"),
        ("桜の花が咲きました。", "ja"),
        ("墨香未干。", "zh"),
    ])
    audio = np.zeros(int(16000 * 100.0), dtype=np.float32)
    calls: list = []
    mm = MagicMock()
    mock = _mock_mms(calls)
    mm.using_mms_aligner.return_value.__enter__.return_value = mock

    with patch("core.align_engine.get_mms_aligner", return_value=mock), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        align_full_text(proj, model_manager=mm, cfg=AlignConfig(align_backend="mms", source_language="auto"))

    assert [c["language"] for c in calls] == ["Chinese", "Japanese", "Chinese"]
    assert [c["text"] for c in calls] == ["风掠过指尖。", "桜の花が咲きました。", "墨香未干。"]
    # 混语段按各自时间窗裁剪（非零 offset 段 = 有裁剪）
    assert calls[0]["offset"] == 0.0            # 首段起点在 0
    assert calls[1]["offset"] > 0.0             # 后续段从句界前移 pad 起
    # 逐句词序列完整切回 + 语言标注正确
    for s, expect_lang, cnt in zip(proj.sentences, ["Chinese", "Japanese", "Chinese"], [5, 9, 4]):
        got = [w.text for w in s.words if not w.is_punct]
        assert got == extract_pure_words(s.text)
        assert all(w.language == expect_lang for w in s.words)
        assert len(s.words) >= cnt

    # ── single path uniform language byte equivalent ─────────────────────────
    # 单语项目：恰好 1 段、整段音频零裁剪（offset=0.0、样本数=全长）
    proj = _mk_project([("第一句来了。", "zh"), ("第二句来了。", "zh")])
    audio = np.zeros(int(16000 * 9.0), dtype=np.float32)
    calls: list = []
    mm = MagicMock()
    mock = _mock_mms(calls)
    mm.using_mms_aligner.return_value.__enter__.return_value = mock

    with patch("core.align_engine.get_mms_aligner", return_value=mock), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        align_full_text(proj, model_manager=mm, cfg=AlignConfig(align_backend="mms", source_language="auto"))

    assert len(calls) == 1
    assert calls[0]["language"] == "Chinese"
    assert calls[0]["offset"] == 0.0
    assert calls[0]["n_samples"] == len(audio)
    assert proj.sentences[0].has_word_level() and proj.sentences[1].has_word_level()

    # 锁定句快照恢复
    proj2 = _mk_project([("锁定句子甲。", "zh"), ("普通句子乙。", "zh")])
    locked_words = [WordTimestamp(text="锁", start_time=0.0, end_time=0.5)]
    proj2.sentences[0].words = locked_words
    proj2.sentences[0].is_locked = True
    proj2.sentences[0].fix_times_from_words()
    calls2: list = []
    mock2 = _mock_mms(calls2)
    mm.using_mms_aligner.return_value.__enter__.return_value = mock2
    with patch("core.align_engine.get_mms_aligner", return_value=mock2), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        align_full_text(proj2, model_manager=mm, cfg=AlignConfig(align_backend="mms", source_language="auto"))
    assert [w.text for w in proj2.sentences[0].words] == ["锁"]
    assert proj2.sentences[0].start_time == 0.0 and proj2.sentences[0].end_time == 0.5

    # ── qwen backend segments receive language ─────────────────────────
    proj = _mk_project([("风掠过。", "zh"), ("桜咲く。", "ja")])
    audio = np.zeros(int(16000 * 20.0), dtype=np.float32)
    qwen_calls: list = []
    mm = MagicMock()

    def fake_raw(audio_tuple, text, language, *, model_manager):
        qwen_calls.append(language)
        return [
            {"text": w, "start_time": i * 0.2, "end_time": i * 0.2 + 0.15}
            for i, w in enumerate(extract_pure_words(text))
        ]

    with patch.object(align_engine, "align_sentence_raw", side_effect=fake_raw), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)), \
         patch("importlib.util.find_spec", return_value=object()):  # 假装 nagisa 已装
        align_full_text(proj, model_manager=mm, cfg=AlignConfig(align_backend="qwen", source_language="auto"))

    assert qwen_calls == ["Chinese", "Japanese"]

    # ── single path dispatches all eleven languages ─────────────────────────
    # ≤300s 单块路径：11 段各自携带各自语言一次调用，词流按段精确切回
    proj = _mk_project(_ELEVEN)
    audio = np.zeros(int(16000 * 100.0), dtype=np.float32)
    calls: list = []
    mm = MagicMock()
    mock = _mock_mms(calls)
    mm.using_mms_aligner.return_value.__enter__.return_value = mock

    with patch("core.align_engine.get_mms_aligner", return_value=mock), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        align_full_text(proj, model_manager=mm, cfg=AlignConfig(align_backend="mms", source_language="auto"))

    assert [c["language"] for c in calls] == _ELEVEN_FULL
    assert len(calls) == 11
    for s, expect_lang in zip(proj.sentences, _ELEVEN_FULL):
        got = [w.text for w in s.words if not w.is_punct]
        assert got == extract_pure_words(s.text)
        assert all(w.language == expect_lang for w in s.words)


def _case_multilang_context_entered_once_not_per_segment():
    """多语言分段时 using_mms_aligner / using_aligner 只进一次，不按段反复加载。

    用户怀疑「每段对齐都向显存重载模型」。现役 full 路径在段循环外层包
    一层 context；本测试钉死 enter 次数 = 1，且 get_aligner 可被多次调用
    （段内推理）但不对应多次 context 进出。
    """
    # —— MMS：using_mms_aligner 只 enter 一次 ——
    proj = _mk_project([
        ("第一句中文。", "zh"),
        ("second english line.", "en"),
        ("第三句还是中文。", "zh"),
    ])
    audio = np.zeros(int(16000 * 30.0), dtype=np.float32)
    calls: list = []
    mock = _mock_mms(calls)
    mm = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = mock
    ctx.__exit__.return_value = None
    mm.using_mms_aligner.return_value = ctx

    with patch("core.align_engine.get_mms_aligner", return_value=mock), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        align_full_text(
            proj, model_manager=mm,
            cfg=AlignConfig(align_backend="mms", source_language="auto"),
        )

    assert mm.using_mms_aligner.call_count == 1, (
        f"MMS context 应按任务进一次，实际 enter 工厂调用 {mm.using_mms_aligner.call_count} 次"
    )
    assert ctx.__enter__.call_count == 1
    assert ctx.__exit__.call_count == 1
    # 三段语言 → 三次 align 推理，但共用同一 Session
    assert len(calls) == 3

    # —— Qwen：using_aligner 只 enter 一次 ——
    proj2 = _mk_project([
        ("第一句。", "zh"),
        ("さくら。", "ja"),
    ])
    audio2 = np.zeros(int(16000 * 20.0), dtype=np.float32)
    mm2 = MagicMock()
    ctx2 = MagicMock()
    ctx2.__enter__.return_value = (MagicMock(), MagicMock())  # proc, model
    ctx2.__exit__.return_value = None
    mm2.using_aligner.return_value = ctx2
    qwen_calls: list = []

    def fake_raw(audio_tuple, text, language, *, model_manager):
        qwen_calls.append(language)
        return [
            {"text": w, "start_time": i * 0.2, "end_time": i * 0.2 + 0.15}
            for i, w in enumerate(extract_pure_words(text))
        ]

    with patch.object(align_engine, "align_sentence_raw", side_effect=fake_raw), \
         patch("core.audio_io.load_audio", return_value=(audio2, 16000)), \
         patch("importlib.util.find_spec", return_value=object()):
        align_full_text(
            proj2, model_manager=mm2,
            cfg=AlignConfig(align_backend="qwen", source_language="auto"),
        )

    assert mm2.using_aligner.call_count == 1
    assert ctx2.__enter__.call_count == 1
    assert ctx2.__exit__.call_count == 1
    assert qwen_calls == ["Chinese", "Japanese"]


def _case_chunked_path_language_segments_and_lock_restore():
    # ── chunked path language segments and lock restore ─────────────────────────
    # 四句、两种语言交错，时间轴铺满 >300s（会让整媒体切块，但段内各 ≤240s）
    proj = _mk_project([
        ("开场白第一句。", "zh"),
        ("さくらひらひら。", "ja"),
        ("第二部分开始。", "zh"),
        ("おわりのうたです。", "ja"),
    ], duration=400.0)
    # 拉开时间：每句间隔 ~100s
    t = 0.0
    for s in proj.sentences:
        s.start_time, s.end_time = round(t, 3), round(t + 4.0, 3)
        t += 100.0

    audio = np.zeros(int(16000 * 400.0), dtype=np.float32)
    calls: list = []
    mm = MagicMock()

    # 锁定第 2 句（ja 段）
    proj.sentences[1].is_locked = True
    proj.sentences[1].words = [WordTimestamp(text="桜", start_time=100.0, end_time=100.5)]

    with patch("core.align_engine.get_mms_aligner", return_value=_mock_mms(calls)), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        align_full_text(proj, model_manager=mm, cfg=AlignConfig(align_backend="mms", source_language="auto"))

    # 每段独立一次调用（各段时长 ~4s，远小于 240s 块上限 → 每段 1 块）
    assert [c["language"] for c in calls] == ["Chinese", "Japanese", "Chinese", "Japanese"]
    # 各句按语言切回（锁定句除外）
    assert proj.sentences[0].has_word_level()
    assert proj.sentences[2].has_word_level()
    assert proj.sentences[3].has_word_level()
    # 锁定句恢复（chunked 路径与 single 路径同等保护）
    assert [w.text for w in proj.sentences[1].words] == ["桜"]


def _case_chunk_overlap_selects_nearest_snapshot_and_preserves_failures():
    """重叠块候选必须互相独立，并按原句中心选最近块；空产出不清旧数据。"""
    from contextlib import contextmanager

    class FakeManager:
        @contextmanager
        def using_mms_aligner(self, **_kwargs):
            yield fake_mms

    class FakeMMS:
        model_dir = "fake"

        def __init__(self):
            self.offsets: list[float] = []

        def is_available(self):
            return True

        def align(self, _audio, text, *, language, offset_sec=0.0, **_kwargs):
            self.offsets.append(offset_sec)
            return [
                WordTimestamp(
                    text=text, start_time=offset_sec,
                    end_time=offset_sec + 1.0, language=language,
                )
            ]

    fake_mms = FakeMMS()
    sentence = Sentence(
        text="hello", start_time=0.0, end_time=400.0,
        language="en", is_dirty=True,
    )
    project = SubtitleProject(
        audio_path="mock.wav", source_language="en",
        media_duration=400.0, sentences=[sentence],
    )
    with patch("core.align_engine.get_mms_aligner", return_value=fake_mms), \
         patch("core.audio_io.load_audio", return_value=(np.zeros(400), 1)), \
         patch("core.audio_io.detect_silence_points", return_value=[]):
        align_full_text(
            project, model_manager=FakeManager(),
            cfg=AlignConfig(align_backend="mms", source_language="auto"),
        )

    assert len(fake_mms.offsets) == 2
    # 原句中心 200s 距第 1 块中心更近；旧别名 bug 会被第 2 块覆盖成约 238s。
    assert project.sentences[0].start_time == pytest.approx(fake_mms.offsets[0])
    assert project.sentences[0].start_time < 10.0
    assert not project.sentences[0].is_dirty

    # 所有块空产出：旧 words/句界/dirty 必须原样保留。
    old_word = WordTimestamp("hello", 10.0, 11.0, language="English")
    failed = SubtitleProject(
        audio_path="mock.wav", source_language="en", media_duration=400.0,
        sentences=[Sentence(
            "hello", 10.0, 390.0, words=[old_word],
            language="en", is_dirty=True,
        )],
    )
    empty_mms = MagicMock()
    empty_mms.model_dir = "fake"
    empty_mms.is_available.return_value = True
    empty_mms.align.return_value = []
    manager = MagicMock()
    manager.using_mms_aligner.return_value.__enter__.return_value = empty_mms
    with patch("core.align_engine.get_mms_aligner", return_value=empty_mms), \
         patch("core.audio_io.load_audio", return_value=(np.zeros(400), 1)), \
         patch("core.audio_io.detect_silence_points", return_value=[]):
        align_full_text(
            failed, model_manager=manager,
            cfg=AlignConfig(align_backend="mms", source_language="auto"),
        )
    kept = failed.sentences[0]
    assert (kept.start_time, kept.end_time) == (10.0, 390.0)
    assert [(w.text, w.start_time, w.end_time) for w in kept.words] == [("hello", 10.0, 11.0)]
    assert kept.is_dirty

# ═════════════════════════════════════════════════════════════
# 3. 锁定保护（原 test_align_lock_protection.py）
# ═════════════════════════════════════════════════════════════
def _make_mock_project() -> SubtitleProject:
    s0 = Sentence(
        text="第一句干净",
        start_time=0.0,
        end_time=2.0,
        words=[WordTimestamp(text="第一句干净", start_time=0.0, end_time=2.0)],
        language="zh",
        is_dirty=False,
        is_locked=False,
    )
    s1 = Sentence(
        text="第二句手动精修并锁定",
        start_time=2.5,
        end_time=5.0,
        words=[
            WordTimestamp(text="第二句", start_time=2.5, end_time=3.5),
            WordTimestamp(text="手动精修", start_time=3.5, end_time=4.5),
            WordTimestamp(text="并锁定", start_time=4.5, end_time=5.0),
        ],
        language="zh",
        is_dirty=True,  # 曾被改过但用户加锁
        is_locked=True,
    )
    s2 = Sentence(
        text="第三句待对齐脏句",
        start_time=5.5,
        end_time=8.0,
        words=[],
        language="zh",
        is_dirty=True,
        is_locked=False,
    )
    return SubtitleProject(
        audio_path="dummy.wav",
        source_language="zh",
        media_duration=10.0,
        sentences=[s0, s1, s2],
    )


def _case_dirty_and_project_lock_respect():
    # ── align dirty only respects lock and clean ─────────────────────────
    project = _make_mock_project()
    assert project.alignable_dirty_indices() == [2]

    aligned_calls = []

    def fake_align_sentence(sent, *args, **kwargs):
        aligned_calls.append(sent.text)
        return [WordTimestamp(text=sent.text, start_time=sent.start_time, end_time=sent.end_time)]

    with patch("core.audio_io.load_audio", return_value=(None, 16000)), \
         patch("core.align_engine.align_sentence", side_effect=fake_align_sentence):
        align_dirty_only(project, model_manager=_FakeModelManager())

    # 仅对 s2 调了对齐，s0(clean) 和 s1(locked) 绝不应被调用
    assert aligned_calls == ["第三句待对齐脏句"]
    # s1 的 words 保持原样
    assert len(project.sentences[1].words) == 3
    assert project.sentences[1].words[0].text == "第二句"
    assert project.sentences[1].is_locked is True
    # s2 对齐后 is_dirty 应被清为 False
    assert project.sentences[2].is_dirty is False

    # ── align project skips locked ─────────────────────────
    project = _make_mock_project()
    # 把 s0 也设为 dirty，看 align_project 是否会重跑 s0/s2 但跳过 s1
    project.sentences[0].is_dirty = True
    aligned_calls = []

    def fake_align_sentence(sent, *args, **kwargs):
        aligned_calls.append(sent.text)
        return [WordTimestamp(text=sent.text, start_time=sent.start_time, end_time=sent.end_time)]

    with patch("core.audio_io.load_audio", return_value=(None, 16000)), \
         patch("core.align_engine.align_sentence", side_effect=fake_align_sentence):
        align_project(project, model_manager=_FakeModelManager())

    assert "第二句手动精修并锁定" not in aligned_calls
    assert "第一句干净" in aligned_calls
    assert "第三句待对齐脏句" in aligned_calls
    assert len(project.sentences[1].words) == 3


def _case_failed_alignment_is_transactional_and_mms_missing_fails_fast():
    old = WordTimestamp(text="旧", start_time=1.0, end_time=1.5)
    project = SubtitleProject(
        audio_path="dummy.wav", source_language="zh", media_duration=3.0,
        sentences=[Sentence(
            text="新文本", start_time=1.0, end_time=2.0,
            words=[old], language="zh", is_dirty=True,
        )],
    )
    with patch("core.audio_io.load_audio", return_value=(None, 16000)), \
         patch("core.align_engine.align_sentence", return_value=[]):
        align_dirty_only(project, model_manager=_FakeModelManager())
    sent = project.sentences[0]
    assert [(w.text, w.start_time, w.end_time) for w in sent.words] == [("旧", 1.0, 1.5)]
    assert (sent.start_time, sent.end_time) == (1.0, 2.0)
    assert sent.is_dirty

    missing = MagicMock()
    missing.model_dir = "missing-mms"
    missing.is_available.return_value = False
    manager = MagicMock()
    with patch("core.audio_io.load_audio", return_value=(None, 16000)), \
         patch("core.align_engine.get_mms_aligner", return_value=missing), \
         pytest.raises(FileNotFoundError, match="missing-mms"):
        align_dirty_only(
            project, model_manager=manager,
            cfg=AlignConfig(align_backend="mms"),
        )
    manager.using_aligner.assert_not_called()
    manager.using_mms_aligner.assert_not_called()


def test_align_worker_dirty_early_exit():
    # AlignWorker 是 QThread 包装，需 Qt；无 Qt 环境跳过（本测试文件其余项均为纯 core 逻辑）
    pytest.importorskip("PySide6", exc_type=ImportError)
    from workers.align_worker import AlignWorker

    # ── align worker dirty early exit ─────────────────────────
    project = _make_mock_project()
    # 若所有句都 clean 或 locked，alignable_dirty_indices 为空
    project.sentences[2].is_dirty = False
    assert project.alignable_dirty_indices() == []

    progress_events = []
    worker = AlignWorker(project, mode="dirty", model_manager=_FakeModelManager())
    worker.progress.connect(lambda d, t, desc: progress_events.append(desc))
    worker.run()

    # 应该直接 early exit，不需要实际跑对齐
    assert any("无待对齐脏句" in desc for desc in progress_events)

# ═════════════════════════════════════════════════════════════
# 4. 零时长/塌陷字平滑修复（原 test_zero_duration_repair.py）
# ═════════════════════════════════════════════════════════════
def _case_zero_duration_fill_shapes():
    # ── middle zero duration word fills gap ─────────────────────────
    raw = [
        WordTimestamp(text="我", start_time=1.000, end_time=1.200),
        WordTimestamp(text="的", start_time=1.600, end_time=1.600),
        WordTimestamp(text="书", start_time=1.600, end_time=1.900),
    ]

    fixed = sanitize_word_timestamps(raw, min_duration=0.030)
    assert len(fixed) == 3

    w_wo = fixed[0]
    w_de = fixed[1]
    w_shu = fixed[2]

    assert abs(w_wo.start_time - 1.000) < 1e-4
    assert abs(w_wo.end_time - 1.200) < 1e-4

    assert w_de.duration >= 0.030
    assert abs(w_shu.end_time - 1.900) < 1e-4

    # ── start and end zero duration words ─────────────────────────
    raw = [
        WordTimestamp(text="听", start_time=0.500, end_time=0.500),
        WordTimestamp(text="我说", start_time=0.800, end_time=1.200),
        WordTimestamp(text="完", start_time=1.200, end_time=1.200),
    ]

    fixed = sanitize_word_timestamps(raw, min_duration=0.030)
    assert len(fixed) == 3

    # 句首字获得保底时长
    assert abs(fixed[0].start_time - 0.500) < 1e-4
    assert fixed[0].duration >= 0.030

    # 句末字获得有效时长
    assert abs(fixed[2].start_time - 1.200) < 1e-4
    assert fixed[2].duration >= 0.030

    # ── multiple consecutive collapsed words ─────────────────────────
    raw = [
        WordTimestamp(text="一", start_time=1.000, end_time=1.000),
        WordTimestamp(text="个", start_time=1.000, end_time=1.000),
        WordTimestamp(text="人", start_time=1.300, end_time=1.600),
    ]

    fixed = sanitize_word_timestamps(raw, min_duration=0.030)
    assert len(fixed) == 3
    for w in fixed:
        assert w.duration >= 0.030
        assert w.end_time >= w.start_time

    assert fixed[0].start_time < fixed[1].start_time
    assert fixed[1].start_time < fixed[2].start_time


def _case_short_word_with_gap_fills_gap():
    # ── short word with gap fills gap ─────────────────────────
    raw = [
        WordTimestamp(text="前字", start_time=1.000, end_time=1.080),
        WordTimestamp(text="中间字", start_time=1.200, end_time=1.300),
        WordTimestamp(text="后字", start_time=1.400, end_time=1.560),
    ]

    fixed = sanitize_word_timestamps(raw, min_duration=0.030)
    assert len(fixed) == 3

    assert abs(fixed[0].start_time - 1.000) < 1e-4
    assert fixed[1].duration >= 0.030
    assert abs(fixed[2].end_time - 1.560) < 1e-4

# ═════════════════════════════════════════════════════════════
# 5. AlignWorker 全失败 → 明确报错
# ═════════════════════════════════════════════════════════════
def test_align_worker_dirty_all_fail_raises():
    pytest.importorskip("PySide6", exc_type=ImportError)
    from unittest.mock import patch

    from core.align_engine import AlignConfig
    from subs.models import Sentence, SubtitleProject
    from workers.align_worker import AlignWorker

    project = SubtitleProject(
        audio_path="dummy.wav", source_language="zh", media_duration=3.0,
        sentences=[
            Sentence(text="新文本", start_time=1.0, end_time=2.0, language="zh", is_dirty=True),
        ],
    )
    worker = AlignWorker(
        project, mode="dirty", model_manager=_FakeModelManager(),
        cfg=AlignConfig(align_backend="qwen"),
    )
    with patch("core.audio_io.load_audio", return_value=(None, 16000)), \
         patch("core.align_engine.align_sentence", return_value=[]):
        with pytest.raises(RuntimeError, match="未能产出"):
            worker._run_dirty_mode()


def test_align_worker_sentences_all_fail_raises():
    pytest.importorskip("PySide6", exc_type=ImportError)
    from unittest.mock import patch

    from core.align_engine import AlignConfig
    from subs.models import Sentence, SubtitleProject
    from workers.align_worker import AlignWorker

    project = SubtitleProject(
        audio_path="dummy.wav", source_language="zh", media_duration=3.0,
        sentences=[
            Sentence(text="新文本", start_time=1.0, end_time=2.0, language="zh", is_dirty=True),
        ],
    )
    worker = AlignWorker(
        project, mode="sentences", indices=[0], model_manager=_FakeModelManager(),
        cfg=AlignConfig(align_backend="qwen"),
    )
    with patch("core.audio_io.load_audio", return_value=(None, 16000)), \
         patch("core.align_engine.align_sentence", return_value=[]):
        with pytest.raises(RuntimeError, match="未能产出"):
            worker._run_sentences_mode()


def test_align_segments_pack():
    """test_align_segments_pack：合并 5 个场景（断言逐条保留，见各 _case_*）。"""
    _case_resolve_segments_grouping_and_eleven_langs()
    _case_single_path_segments_dispatch_and_backend_lang()
    _case_multilang_context_entered_once_not_per_segment()
    _case_chunked_path_language_segments_and_lock_restore()
    _case_chunk_overlap_selects_nearest_snapshot_and_preserves_failures()


def test_align_lock_pack():
    """test_align_lock_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_dirty_and_project_lock_respect()
    _case_failed_alignment_is_transactional_and_mms_missing_fails_fast()


def test_align_zero_duration_pack():
    """test_align_zero_duration_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_zero_duration_fill_shapes()
    _case_short_word_with_gap_fills_gap()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
