"""tests/test_mms_aligner.py — MMS-300M-FA 多语言歌词对齐套件（不加载模型）。

覆盖：
- 音乐符号过滤 / 多语言切片 / CTC Trellis / 单句与脏句重对齐 /
  任务结束销毁 ORT Session（还显存）/ 模型上下文退出后恢复用户所选后端身份 /
  末字拖音 CTC 异质发音峰哨兵（连唱场景截断 + 辅音峰持续判定 + 峰回溯）；
- 数字拼读展开（11 语言逐位拼读，无 <unk> / 无伪 'a' 单 token）；
- 日语发音拍（mora）级分词与罗马化（拗音/促音/长音标点化；pykakasi 读音路由）；
- MMS 单例键含 device / 运行期 CUDA 回退不永久改 device / uroman 失败哨兵。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

from core.mms_aligner import (
    _DEFAULT_MMS_VOCAB,
    _TAIL_ONSET_PROB,
    _ctc_forced_align_canonical,
    _find_next_content_onset,
    MMSAligner,
)
from core.align_engine import AlignConfig, align_sentence, align_dirty_only
from core.model_manager import ModelManager
from core.text_utils import extract_pure_words, merge_punct_into_words, attach_words_to_sentences
from subs.models import Sentence, SubtitleProject, WordTimestamp

pytestmark = pytest.mark.logic


# ═════════════════════════════════════════════════════════════
# A. 音乐符号过滤 / 多语言切片 / CTC Trellis（原 test_phase6_mms_aligner.py）
# ═════════════════════════════════════════════════════════════

def _case_pure_words_and_no_bleeding_attachment():
    # ── special symbols and pure words ─────────────────────────
    mixed_text = "♪ 今夜的 moonlight 照亮了 sakura ♪"
    pure = extract_pure_words(mixed_text)
    assert pure == ["今", "夜", "的", "moonlight", "照", "亮", "了", "sakura"]
    assert "♪" not in pure

    mock_words = [
        WordTimestamp(text="今", start_time=1.0, end_time=1.2),
        WordTimestamp(text="夜", start_time=1.2, end_time=1.4),
        WordTimestamp(text="的", start_time=1.4, end_time=1.6),
        WordTimestamp(text="moonlight", start_time=1.6, end_time=2.2),
        WordTimestamp(text="照", start_time=2.2, end_time=2.4),
        WordTimestamp(text="亮", start_time=2.4, end_time=2.6),
        WordTimestamp(text="了", start_time=2.6, end_time=2.8),
        WordTimestamp(text="sakura", start_time=2.8, end_time=3.4),
    ]
    # 音乐符号 ♪ 在字级精度中被彻底忽略（不污染字级表格与波形手柄）
    merged = merge_punct_into_words(mixed_text, mock_words)
    assert len(merged) == 8
    assert all(w.text != "♪" for w in merged)
    assert all(not w.is_punct for w in merged)

    # ── multilingual sentence attachment no bleeding ─────────────────────────
    s0 = Sentence(text="♪ 今夜的 moonlight 照亮了 sakura ♪", start_time=1.0, end_time=3.5)
    s1 = Sentence(text="Next morning comes early.", start_time=5.0, end_time=8.0)
    sentences = [s0, s1]

    words_s0 = [WordTimestamp(text=w, start_time=1.0 + i * 0.3, end_time=1.3 + i * 0.3)
                for i, w in enumerate(extract_pure_words(s0.text))]
    words_s1 = [WordTimestamp(text=w, start_time=5.0 + i * 0.5, end_time=5.5 + i * 0.5)
                for i, w in enumerate(extract_pure_words(s1.text))]
    all_raw_words = words_s0 + words_s1

    assigned = attach_words_to_sentences(sentences, all_raw_words)

    assert len(assigned[0].words) == 8
    assert len([w for w in assigned[1].words if not w.is_punct]) == 4
    assert [w.text for w in assigned[0].words] == ["今", "夜", "的", "moonlight", "照", "亮", "了", "sakura"]
    assert [w.text for w in assigned[1].words if not w.is_punct] == ["Next", "morning", "comes", "early"]
    # 无缝衔接检验
    for i in range(len(assigned[0].words) - 1):
        assert abs(assigned[0].words[i].end_time - assigned[0].words[i + 1].start_time) < 1e-4


def _case_ctc_math_pack():
    # ── canonical ctc trellis accuracy ─────────────────────────
    num_frames = 250
    vocab = {"<blank>": 0, "a": 4, "b": 20, "c": 23, "d": 16}
    emissions = np.full((num_frames, 31), -10.0, dtype=np.float32)
    emissions[:, 0] = 0.0

    a_id, b_id, c_id, d_id = vocab["a"], vocab["b"], vocab["c"], vocab["d"]

    emissions[50:60, a_id] = 4.0
    emissions[50:60, 0] = -3.0
    emissions[60:70, b_id] = 4.0
    emissions[60:70, 0] = -3.0

    emissions[170:185, c_id] = 4.0
    emissions[170:185, 0] = -3.0
    emissions[185:200, d_id] = 4.0
    emissions[185:200, 0] = -3.0

    max_logits = np.max(emissions, axis=-1, keepdims=True)
    exp_logits = np.exp(emissions - max_logits)
    log_probs = emissions - max_logits - np.log(np.sum(exp_logits, axis=-1, keepdims=True) + 1e-12)

    targets = [a_id, b_id, c_id, d_id]
    path = _ctc_forced_align_canonical(log_probs, targets, blank_id=0)

    w0_frames = np.where((path == 1) | (path == 3))[0]
    w1_frames = np.where((path == 5) | (path == 7))[0]

    s0, e0 = int(w0_frames[0]), int(w0_frames[-1] + 1)
    s1, e1 = int(w1_frames[0]), int(w1_frames[-1] + 1)

    assert (s0, e0) == (50, 70), f"Word 0 expected (50, 70), got {(s0, e0)}"
    assert (s1, e1) == (170, 200), f"Word 1 expected (170, 200), got {(s1, e1)}"

    # 帧不足必须明文失败，不得返回“最后有限状态”的部分路径。
    with pytest.raises(ValueError, match="帧数不足"):
        _ctc_forced_align_canonical(
            np.zeros((2, 4), dtype=np.float32), [1, 1], blank_id=0,
        )

    # 分配回溯矩阵前执行内存预算守门。
    import core.mms_aligner.ctc as ctc_mod
    with patch.object(ctc_mod, "_CTC_TRELLIS_MAX_BYTES", 8), \
         pytest.raises(MemoryError, match="超过内存预算"):
        _ctc_forced_align_canonical(
            np.zeros((10, 4), dtype=np.float32), [1, 2], blank_id=0,
        )

    # ── inter sentence gap and vowel extension ─────────────────────────
    # 模拟长拖音与句间真实停顿：两句相隔 4 秒
    s0 = Sentence(text="风中的花", start_time=1.0, end_time=3.5)
    s1 = Sentence(text="月下的影", start_time=7.5, end_time=10.0)
    sentences = [s0, s1]

    raw_words = [
        WordTimestamp("风", 1.0, 1.4),
        WordTimestamp("中", 1.4, 1.8),
        WordTimestamp("的", 1.8, 2.3),
        WordTimestamp("花", 2.3, 3.5),   # 尾字长拖音
        WordTimestamp("月", 7.5, 8.0),
        WordTimestamp("下", 8.0, 8.6),
        WordTimestamp("的", 8.6, 9.0),
        WordTimestamp("影", 9.0, 10.0),  # 尾字长拖音
    ]

    res = attach_words_to_sentences(sentences, raw_words)
    gap = res[1].start_time - res[0].end_time
    assert abs(gap - 4.0) < 1e-4, f"Expected 4.0s gap, got {gap}"
    # 句内无缝衔接
    assert abs(res[0].words[0].end_time - res[0].words[1].start_time) < 1e-4
    assert abs(res[1].words[0].end_time - res[1].words[1].start_time) < 1e-4


def _case_mms_unload_clears_session():
    """MMSAligner.unload 必须摘掉 _session（ORT 还显存的唯一手段）。"""
    from core.mms_aligner import MMSAligner

    m = MMSAligner(model_dir="/nonexistent_mms_dir", device="cpu")
    m._session = object()  # 伪会话
    m.unload()
    assert m._session is None
    m.unload()  # 幂等
    assert m._session is None


def _case_realign_and_ram_residency():
    # ── mms sentence and dirty realignment ─────────────────────────
    # 1. 模拟 MMS 能够正常对齐单句
    mock_mms = MagicMock()
    mock_mms.is_available.return_value = True
    mock_mms.align.return_value = [
        WordTimestamp(text="青", start_time=1.0, end_time=1.2),
        WordTimestamp(text="紫", start_time=1.2, end_time=1.4),
        WordTimestamp(text="色", start_time=1.4, end_time=1.6),
    ]
    # dirty 模式下脏句的干净邻句触发上下文对齐（align_with_context），同样返回本句字词。
    mock_mms.align_with_context.return_value = [
        WordTimestamp(text="青", start_time=1.0, end_time=1.2),
        WordTimestamp(text="紫", start_time=1.2, end_time=1.4),
        WordTimestamp(text="色", start_time=1.4, end_time=1.6),
    ]

    sent = Sentence(text="青紫色", start_time=1.0, end_time=1.6, language="zh", is_dirty=True)
    audio = np.random.randn(32000).astype(np.float32)
    cfg = AlignConfig(align_backend="mms")
    mm = MagicMock()

    with patch("core.align_engine.get_mms_aligner", return_value=mock_mms):
        words = align_sentence(sent, audio, 16000, model_manager=mm, cfg=cfg, inferred_language_full="Chinese")
        assert len(words) == 3
        assert [w.text for w in words] == ["青", "紫", "色"]
        mm.get_aligner.assert_not_called()

    # 2. 模拟 dirty 模式
    proj = SubtitleProject(
        audio_path="mock.wav",
        sentences=[
            Sentence(sid=0, text="青紫色", start_time=1.0, end_time=1.6, language="zh", is_dirty=True),
            Sentence(sid=1, text="风掠过", start_time=2.0, end_time=2.6, language="zh", is_dirty=False),
        ],
    )

    with patch("core.align_engine.get_mms_aligner", return_value=mock_mms), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        res = align_dirty_only(proj, model_manager=mm, cfg=cfg)
        assert res.sentences[0].is_dirty is False
        assert len(res.sentences[0].words) == 3
        assert res.sentences[1].is_dirty is False
        mm.get_aligner.assert_not_called()

    # ── mms ram residency and status ─────────────────────────
    mm = ModelManager()
    mm.active_aligner = "mms"
    assert "未加载" in mm.status_text()
    assert "MMS-FA" in mm.status_text()

    unload_mock = MagicMock()
    with patch("core.mms_aligner.get_mms_aligner") as g:
        g.return_value.unload = unload_mock
        with mm.using_mms_aligner():
            assert "加载中" in mm.status_text()
            callback = g.return_value.set_progress_callback.call_args.args[0]
            callback(0, 0, "MMS 对齐器：加载完成（CPU）")
            assert "已激活" in mm.status_text()
            assert "engine=ONNX" in mm.status_text()

        # 任务结束必须销毁 ORT Session（否则 CUDA EP 显存不还）；
        # 状态回到 not_loaded（ORT 无 torch 式 RAM 驻留）。
        assert unload_mock.called, "using_mms_aligner 退出必须调用 MMSAligner.unload()"
        assert mm.mms_aligner_state == "not_loaded"
        assert "未加载" in mm.status_text()

    # 手动卸载（幂等）
    mm.unload_all()
    assert "未加载" in mm.status_text()


def _case_model_context_restores_active_aligner():
    """模型上下文执行完不得把用户在工具栏选的后端身份顶掉。

    active_aligner = 用户在工具栏选择的后端身份（状态栏显示/路径判断用）；
    using_mms_aligner / using_aligner 在任务期间临时切换身份，退出后必须还原。
    MMS（ONNX）退出时销毁 Session 还显存，状态 not_loaded（非假 RAM 驻留）。
    """
    mm = ModelManager(asr_path="/nonexistent_asr", aligner_path="/nonexistent_aligner")
    mm.active_aligner = "qwen"                        # 用户在工具栏选了 Qwen
    with patch("core.mms_aligner.get_mms_aligner") as g:
        g.return_value.unload = MagicMock()
        with mm.using_mms_aligner():                  # 一次 MMS 后台执行
            assert mm.active_aligner == "mms"
        assert g.return_value.unload.called
    assert mm.active_aligner == "qwen"                # 执行完不得顶掉用户选择
    assert mm.mms_aligner_state == "not_loaded"       # ORT Session 已销毁

    mm.active_aligner = "mms"                         # 反向：用户选了 MMS
    with patch.object(ModelManager, "get_aligner", return_value=(MagicMock(), MagicMock())):
        with mm.using_aligner():                      # 一次 Qwen 后台执行
            assert mm.active_aligner == "qwen"
        assert mm.active_aligner == "mms"


# ═════════════ 末字拖音 CTC 异质发音峰哨兵 ═════════════

BLANK = 0
VOWEL_A = 4      # 模拟末字元音 token
CHAR_N = 10      # 模拟后句首字 token（异质）
V = 32           # 字符表大小


def _mk_log_probs(rows):
    """rows: [(dominant_token, prob)] → (T, V) log 概率矩阵，其余质量均摊。"""
    T = len(rows)
    probs = np.full((T, V), 0.0, dtype=np.float32)
    for t, (tok, p) in enumerate(rows):
        rest = (1.0 - p) / (V - 1)
        probs[t, :] = rest
        probs[t, tok] = p
    return np.log(probs + 1e-12)


def _case_pure_tail_no_onset():
    """纯拖音：blank 主导 + 末字元音偶发重触发 → 不触发哨兵。"""
    rows = [(BLANK, 0.9)] * 10 + [(VOWEL_A, 0.8)] * 3 + [(BLANK, 0.9)] * 10
    lp = _mk_log_probs(rows)
    assert _find_next_content_onset(lp, 0, len(rows), {VOWEL_A}) is None


def _case_foreign_peak_detected_and_low_confidence_ignored():
    """后句异质字符峰被检出；低置信杂峰不触发。"""
    rows = ([(BLANK, 0.9)] * 8
            + [(CHAR_N, 0.3)]          # 低置信杂峰（混响/伴奏残留）→ 忽略
            + [(BLANK, 0.9)] * 4
            + [(CHAR_N, 0.85)]         # 后句真开口 → 命中
            + [(CHAR_N, 0.9)] * 5)
    lp = _mk_log_probs(rows)
    onset = _find_next_content_onset(lp, 0, len(rows), {VOWEL_A})
    assert onset == 13, f"应命中第一个高置信异质峰（帧 13），得到 {onset}"


def _case_own_token_retrigger_exempt():
    """末字自身 token 重触发（颤音重激发）不算新内容。"""
    rows = [(BLANK, 0.9)] * 5 + [(VOWEL_A, 0.95)] * 4 + [(BLANK, 0.9)] * 5
    lp = _mk_log_probs(rows)
    assert _find_next_content_onset(lp, 0, len(rows), {VOWEL_A}) is None
    # 同一矩阵、own_tokens 不含 VOWEL_A 时则应命中——豁免逻辑确实在起作用
    assert _find_next_content_onset(lp, 0, len(rows), set()) == 5


def _case_probability_threshold_contract():
    """阈值契约：单帧瞬态低置信不触发（防混响残留）；持续低置信触发（抓辅音峰）。

    辅音（塞音/擦音）峰短促且置信常在 0.35~0.5——单帧 0.5 高阈会漏检，导致
    尾音边界切到元音前（「包辅音不包整字」）。持续 2 帧 ≥0.35 判定为真实开口；
    孤立单帧 0.45（混响/伴奏残留瞬态）仍被忽略。
    """
    assert 0.3 <= _TAIL_ONSET_PROB <= 0.9

    # 孤立单帧 0.45（前后皆 blank）→ 不触发
    rows = [(BLANK, 0.9)] * 3 + [(CHAR_N, 0.45)] + [(BLANK, 0.9)] * 3
    lp = _mk_log_probs(rows)
    assert _find_next_content_onset(lp, 0, len(rows), set()) is None

    # 持续 0.45（连续 ≥2 帧）→ 触发（辅音峰场景）
    rows = [(BLANK, 0.9)] * 3 + [(CHAR_N, 0.45)] * 3 + [(BLANK, 0.9)] * 2
    lp = _mk_log_probs(rows)
    onset = _find_next_content_onset(lp, 0, len(rows), set())
    assert onset == 3, f"持续异质低置信峰应在首帧命中，得到 {onset}"

    # min_prob 显式覆盖仍有效（单帧路径）
    rows = [(BLANK, 0.9)] * 2 + [(CHAR_N, 0.45)] + [(BLANK, 0.9)] * 3
    lp = _mk_log_probs(rows)
    assert _find_next_content_onset(lp, 0, len(rows), set(), min_prob=0.4) == 2


def _case_onset_backtracks_to_pronunciation_start():
    """命中峰向前回溯到后验爬坡起脚：辅音闭塞/送气段一并让出。

    模拟：帧 5-7 为 CHAR_N 后验爬坡段（0.15→0.3→0.45，低于命中线但已在发音），
    帧 8 达峰 0.9（命中）。回溯应把 onset 放到爬坡起脚附近而非峰帧。
    """
    T, ramp = 14, {5: 0.15, 6: 0.30, 7: 0.45}
    probs = np.full((T, V), 0.0, dtype=np.float32)
    for t in range(T):
        p_n = ramp.get(t, 0.0)
        if t >= 8:
            p_n = 0.9
        p_blank = max(0.0, 0.9 - p_n)
        rest = (1.0 - p_n - p_blank) / (V - 2)
        probs[t, :] = rest
        probs[t, CHAR_N] = p_n
        probs[t, BLANK] = p_blank
    lp = np.log(probs + 1e-12)

    onset = _find_next_content_onset(lp, 0, T, {VOWEL_A})
    # 峰值 0.9 × 回溯比例 0.2 = 0.18 → 帧 6 (0.30) / 帧 7 (0.45) 在发音中，
    # 帧 5 (0.15) 低于地板 → onset 应落在帧 6（把爬坡段让出来）
    assert onset is not None and onset <= 7, f"应回溯到爬坡段起脚，得到 {onset}"
    assert onset >= 5, f"不应回溯进纯 blank 区，得到 {onset}"


def _case_align_last_word_capped_by_onset_not_anchor():
    """集成：连唱场景末字终点被哨兵截住，不再贴到 tail_limit 锚。

    构造 log_probs：末字「拖」元音在帧 [10,14] 触发后转 blank 拖音至帧 40，
    帧 41 起后句字符高置信开口直到帧 99。tail_limit 锚在帧 90。
    修复前：能量/平坦度扫描器视 41-90 为发音态 → 终点贴 90（锚）；
    修复后：哨兵在帧 41 截断 → 终点 ≈ 帧 41，远离锚。
    """
    from unittest.mock import patch as _patch
    from core.mms_aligner import MMSAligner

    T = 100
    rows = [(BLANK, 0.9)] * T
    for t in range(10, 15):
        rows[t] = (VOWEL_A, 0.9)          # 末字元音 Viterbi 触发段
    for t in range(41, T):
        rows[t] = (CHAR_N, 0.9)           # 后句人声开口（连唱：能量无衰减）
    log_probs = _mk_log_probs(rows)
    emissions = log_probs                  # log-softmax 幂等：softmax(log_softmax(x))=softmax(x)

    aligner = MMSAligner(model_dir="/nonexistent")
    audio = np.random.randn(T * 320).astype(np.float32) * 0.1  # 全程有能量（连唱）

    with _patch.object(MMSAligner, "_generate_emissions_chunked", return_value=emissions), \
         _patch.object(MMSAligner, "_load_vocab", return_value={"a": VOWEL_A, "n": CHAR_N}), \
         _patch.object(MMSAligner, "_romanize_word", return_value="a"):
        words = aligner.align((audio, 16000), "拖", language="Chinese",
                              offset_sec=0.0, tail_limit_sec=90 * 0.020)

    assert len(words) == 1
    end_frame = words[0].end_time / 0.020
    assert end_frame <= 42 + 1, f"末字终点应被哨兵截在帧 41 附近，实际帧 {end_frame:.0f}"
    assert abs(words[0].end_time - 90 * 0.020) > 0.5, "终点不得贴在 tail_limit 锚上"


def _case_align_anchor_dragged_into_next_word_still_cut():
    """集成（用户复现的「特定区间」）：锚被拖进后句首字发音中段时仍正确截断。

    旧实现哨兵扫描上界用被锚截过的 next_s_f：锚落在 [首字开口, 首字峰] 之间
    时峰被挡在扫描区间外 → 哨兵失明 → 衰减扫描器顶到锚 → 尾音包住首字前半。
    解耦后扫描上界 = 元音起点 + 前瞻，首字峰在窗内必被检出。
    """
    from unittest.mock import patch as _patch
    from core.mms_aligner import MMSAligner

    T = 100
    rows = [(BLANK, 0.9)] * T
    for t in range(10, 15):
        rows[t] = (VOWEL_A, 0.9)          # 末字元音 Viterbi 触发段
    for t in range(41, T):
        rows[t] = (CHAR_N, 0.9)           # 后句首字开口于帧 41
    log_probs = _mk_log_probs(rows)

    aligner = MMSAligner(model_dir="/nonexistent")
    audio = np.random.randn(T * 320).astype(np.float32) * 0.1  # 连唱：全程有能量

    # 锚拖进首字发音中段：帧 60（首字 41 开口后、远未结束）
    with _patch.object(MMSAligner, "_generate_emissions_chunked", return_value=log_probs), \
         _patch.object(MMSAligner, "_load_vocab", return_value={"a": VOWEL_A, "n": CHAR_N}), \
         _patch.object(MMSAligner, "_romanize_word", return_value="a"):
        words = aligner.align((audio, 16000), "拖", language="Chinese",
                              offset_sec=0.0, tail_limit_sec=60 * 0.020)

    end_frame = words[0].end_time / 0.020
    assert end_frame <= 42 + 1, \
        f"锚在首字中段时尾音应截在首字开口（帧 41），而非顶到锚（帧 60）；实际帧 {end_frame:.0f}"


def _case_align_without_onset_keeps_natural_decay():
    """集成：无异质峰（正常拖音+衰减）时行为不变——终点由声学衰减决定。"""
    from unittest.mock import patch as _patch
    from core.mms_aligner import MMSAligner

    T = 100
    rows = [(BLANK, 0.9)] * T
    for t in range(10, 15):
        rows[t] = (VOWEL_A, 0.9)
    log_probs = _mk_log_probs(rows)

    aligner = MMSAligner(model_dir="/nonexistent")
    # 音频：帧 10-50 有声（拖音），此后静音（自然衰减可测）
    audio = np.zeros(T * 320, dtype=np.float32)
    t_ax = np.arange(10 * 320, 50 * 320)
    audio[10 * 320:50 * 320] = (0.3 * np.sin(2 * np.pi * 220 * t_ax / 16000)).astype(np.float32)

    with _patch.object(MMSAligner, "_generate_emissions_chunked", return_value=log_probs), \
         _patch.object(MMSAligner, "_load_vocab", return_value={"a": VOWEL_A}), \
         _patch.object(MMSAligner, "_romanize_word", return_value="a"):
        words = aligner.align((audio, 16000), "拖", language="Chinese",
                              offset_sec=0.0, tail_limit_sec=90 * 0.020)

    assert len(words) == 1
    end_frame = words[0].end_time / 0.020
    assert 40 <= end_frame <= 60, f"自然衰减场景终点应在帧 50 附近，实际帧 {end_frame:.0f}"


def _case_mms_chunk_progress_is_reported():
    from core.mms_aligner import MMSAligner

    aligner = MMSAligner(model_dir="/nonexistent")
    fake_input = MagicMock()
    fake_input.name = "input"
    fake_input.shape = [1, "samples"]
    fake_session = MagicMock()
    fake_session.get_inputs.return_value = [fake_input]
    aligner._session = fake_session

    def fake_run(feed_dict):
        samples = next(iter(feed_dict.values())).shape[-1]
        return [np.zeros((1, max(1, samples // 320), 31), dtype=np.float32)]

    aligner._run_session = MagicMock(side_effect=fake_run)
    events = []
    aligner.set_progress_callback(lambda done, total, desc: events.append((done, total, desc)))
    aligner._generate_emissions_chunked(np.zeros(16000 * 65, dtype=np.float32))
    assert any(total >= 3 and "音频块" in desc for _done, total, desc in events)
    assert events[-1][0] == events[-1][1]


def _case_vocal_features_precomputed_once_per_align():
    """频谱平坦度/RMS 与词无关，三词对齐也只能整段计算一次。"""
    from core.mms_aligner import MMSAligner

    T = 60
    emissions = np.zeros((T, 31), dtype=np.float32)
    path = np.zeros(T, dtype=np.int64)
    path[10:15] = 1
    path[20:25] = 3
    path[30:35] = 5
    features = (np.zeros(T, dtype=np.float32), np.ones(T, dtype=np.float32))
    seen_features = []

    def fake_end(_audio, _start, max_end, *, features=None, **_kwargs):
        seen_features.append(features)
        return max_end

    aligner = MMSAligner(model_dir="/nonexistent")
    with patch.object(MMSAligner, "_generate_emissions_chunked", return_value=emissions), \
         patch.object(MMSAligner, "_load_vocab", return_value={"a": 4}), \
         patch.object(MMSAligner, "_romanize_word", return_value="a"), \
         patch("core.mms_aligner.engine._ctc_forced_align_canonical", return_value=path), \
         patch("core.mms_aligner.engine._compute_vocal_features", return_value=features) as compute, \
         patch("core.mms_aligner.engine._find_singing_vocal_end_frame", side_effect=fake_end):
        words = aligner.align(
            (np.zeros(T * 320, dtype=np.float32), 16000),
            "a b c", language="English",
        )

    assert len(words) == 3
    assert compute.call_count == 1
    assert len(seen_features) == 3 and all(item is features for item in seen_features)


# ── 哨兵聚合入口（频谱预计算另有独立回归） ────────────────────

def _case_tail_onset_sentinel_unit_pack():
    """哨兵单元 5 合 1：纯拖音不触发 / 异质峰命中 / 重触发豁免 / 阈值 / 峰回溯。"""
    _case_pure_tail_no_onset()
    _case_foreign_peak_detected_and_low_confidence_ignored()
    _case_own_token_retrigger_exempt()
    _case_probability_threshold_contract()
    _case_onset_backtracks_to_pronunciation_start()


def _case_tail_onset_sentinel_align_pack():
    """哨兵集成 3 合 1：连唱截断不贴锚 / 锚拖进首字中段仍截 / 自然衰减不变。"""
    _case_align_last_word_capped_by_onset_not_anchor()
    _case_align_anchor_dragged_into_next_word_still_cut()
    _case_align_without_onset_keeps_natural_decay()


# ═════════════════════════════════════════════════════════════
# D. 数字拼读展开（原 test_digit_spelling.py）
# ═════════════════════════════════════════════════════════════
# 罗马化只做文本处理，不触碰 ONNX 会话/模型目录
_mms = MMSAligner(model_dir="/nonexistent_test_dir")
_VOCAB = _DEFAULT_MMS_VOCAB
_UNK = _VOCAB["<unk>"]


def _tokens_of(rom: str) -> list:
    """与 align() 的词内 token 过滤逻辑保持一致。"""
    toks = []
    for c in rom:
        if c in _VOCAB:
            toks.append(_VOCAB[c])
        elif c.lower() in _VOCAB:
            toks.append(_VOCAB[c.lower()])
        elif c.isalpha():
            toks.append(_UNK)
    return toks


# ─────────────────────────────────────────────────────────────
# 1. 拉丁语系精确拼写（预折叠 ascii → 结果确定可逐字节钉样）
# ─────────────────────────────────────────────────────────────

def _case_spelling_tables():
    # ── latin digit spelling exact ─────────────────────────
    assert _mms._romanize_word("2024", "English") == "twozerotwofour"
    assert _mms._romanize_word("2024", "French") == "deuxzerodeuxquatre"
    assert _mms._romanize_word("2024", "German") == "zweinullzweivier"
    assert _mms._romanize_word("2024", "Spanish") == "doscerodoscuatro"
    assert _mms._romanize_word("1999", "Italian") == "unonovenovenove"
    assert _mms._romanize_word("2024", "Portuguese") == "doiszerodoisquatro"

    # ── cjk and russian digit expansion ─────────────────────────
    zh = _mms._romanize_word("2024", "Chinese")
    assert zh.isalpha() and zh.isascii() and "er" in zh          # 二零二四 → 拼音
    yue = _mms._romanize_word("2024", "Cantonese")
    assert yue == zh                                              # 同形字同一路径（见 M5 注释）

    ja = _mms._romanize_word("2024", "Japanese")
    assert ja.isalpha() and ja.isascii()
    assert "zero" in ja and "yon" in ja                           # ゼロいちによん

    ko = _mms._romanize_word("2024", "Korean")
    assert ko.isalpha() and ko.isascii() and len(ko) >= 8        # 이영이사 → 罗马化字母串

    ru = _mms._romanize_word("2024", "Russian")
    assert ru.isascii() and "dva" in ru                           # два ноль два четыре

    # ── fullwidth mixed and unknown language ─────────────────────────
    assert _mms._romanize_word("２０２４", "English") == "twozerotwofour"   # 全角
    assert _mms._romanize_word("b2", "English") == "btwo"                   # 混排
    assert _mms._romanize_word("21", "English") == "twoone"                 # 多位数字串
    assert _mms._romanize_word("2024", "Klingon") == "twozerotwofour"       # 未知语言回退英文
    # 无数字的词完全不受 M5 影响（回归钉样）
    assert _mms._romanize_word("hello", "English") == "hello"


def test_token_level_pack():
    # ── all eleven languages produce real tokens ─────────────────────────
    cases = [
        ("Chinese", "2024"), ("English", "2024"), ("Cantonese", "2024"),
        ("French", "2024"), ("German", "2024"), ("Italian", "2024"),
        ("Japanese", "2024"), ("Korean", "2024"), ("Portuguese", "2024"),
        ("Russian", "2024"), ("Spanish", "2024"),
    ]
    for lang, word in cases:
        rom = _mms._romanize_word(word, lang)
        toks = _tokens_of(rom)
        assert toks, f"{lang}: 展开后没有任何 token（M5 修复失效？）"
        assert _UNK not in toks, f"{lang}: 展开后仍产生 <unk>（{rom!r}）"
        assert len(toks) >= 8, f"{lang}: token 数 {len(toks)} 仍不像真实拼读（{rom!r}）"

    # ── ja digits mixed with kanji ─────────────────────────
    pytest.importorskip("pykakasi")
    r = _mms._romanize_word("2024年", "Japanese")
    assert r.isalpha() and r.isascii() and not any(c.isdigit() for c in r)
    assert r.endswith("nen")                                     # 年 → ねん

# ═════════════════════════════════════════════════════════════
# E. 日语发音拍（mora）分词与罗马化（原 test_ja_mora_pipeline.py）
# ═════════════════════════════════════════════════════════════
def _case_ja_mora_grouping_and_symbol_punct():
    # ── mora 归并 ──
    # 拗音归并：きゃ 整拍（不可拆成 き+ゃ 两拍）
    assert extract_pure_words("きゃっと歩くずっと") == ["きゃ", "っと", "歩", "く", "ず", "っと"]
    assert extract_pure_words("ちょっと待って") == ["ちょ", "っと", "待", "って"]
    # 促音开启新拍并入后基字
    assert extract_pure_words("ずっと") == ["ず", "っと"]
    assert extract_pure_words("らっしゃい") == ["ら", "っしゃ", "い"]
    # 句末孤立促音回并前拍（顿挫气口不独立成词）
    assert extract_pure_words("あっ") == ["あっ"]
    assert extract_pure_words("ずっ") == ["ずっ"]
    # 片假名同样归并（テヒョン 的 ヒョ 是整拍）
    assert extract_pure_words("テヒョン") == ["テ", "ヒョ", "ン"]
    # 汉字仍逐字（MMS 路径现状），假名单拍仍逐字
    assert extract_pure_words("桜の花が咲きました") == [
        "桜", "の", "花", "が", "咲", "き", "ま", "し", "た",
    ]
    assert extract_pure_words("さくらひらひら") == ["さ", "く", "ら", "ひ", "ら", "ひ", "ら"]

    # ── ー・〜 标点化（不占词位）──
    # ー・〜 不再成词（旧行为：各自生成伪造 'a' token）
    assert extract_pure_words("さよならー") == ["さ", "よ", "な", "ら"]
    assert extract_pure_words("キム・テヒョン〜") == ["キ", "ム", "テ", "ヒョ", "ン"]


def _case_zh_en_ko_regression_unchanged():
    # 中文逐字 / 英文逐词 / 韩语逐音节块：mora 归并不得影响
    assert extract_pure_words("青紫色的风掠过指尖") == [
        "青", "紫", "色", "的", "风", "掠", "过", "指", "尖",
    ]
    assert extract_pure_words("I will always love you") == [
        "I", "will", "always", "love", "you",
    ]
    assert extract_pure_words("사랑해요 하늘아") == [
        "사", "랑", "해", "요", "하", "늘", "아",
    ]
    # 中英混排既有钉样（♪ 彻底忽略）
    assert extract_pure_words("♪ 今夜的 moonlight 照亮了 sakura ♪") == [
        "今", "夜", "的", "moonlight", "照", "亮", "了", "sakura",
    ]


# ─────────────────────────────────────────────────────────────
# 2. mora 归并单位罗马化（MMSAligner._romanize_word）
# ─────────────────────────────────────────────────────────────
def _make_aligner():
    """不加载模型，仅注入 uroman，测纯罗马化函数。"""
    a = MMSAligner.__new__(MMSAligner)
    import uroman
    a._uroman = uroman.Uroman()
    return a


def _case_ja_mora_romanization():
    a = _make_aligner()
    assert a._romanize_word("きゃ") == "kya"
    assert a._romanize_word("ちょ") == "cho"
    assert a._romanize_word("っと") == "tto"        # 促音 → 下一辅音双写
    assert a._romanize_word("っしゃ") == "ssha"
    assert a._romanize_word("ひょ") == "hyo"
    assert a._romanize_word("ん") == "n"
    # 词尾促音剔除（顿挫气口）：あっ→a、ずっ→zu（uroman 原文是 atsu/zutsu）
    assert a._romanize_word("あっ") == "a"
    assert a._romanize_word("ずっ") == "zu"
    # 日语汉字现状钉样：桜→ying（uroman 中文读音，已知上游限制，勿静默漂移）
    assert a._romanize_word("桜") == "ying"


# ─────────────────────────────────────────────────────────────
# 3. merge / attach 计数契约（共享切分 ⇒ attach 精确路径不回退）
# ─────────────────────────────────────────────────────────────
def _case_ja_merge_and_attach_contract():
    # ── merge 计数一致性（含 sanitize 平抑重叠）──
    text = "さよならー、キム・テヒョン〜"
    pure = extract_pure_words(text)
    words = [
        WordTimestamp(text=w, start_time=1.0 + i * 0.4, end_time=1.2 + i * 0.4)
        for i, w in enumerate(pure)
    ]
    merged = merge_punct_into_words(text, words)
    # 与 align_project / align_dirty_only 等所有引擎路径一致：merge 后接 sanitize
    # 平抑重叠（连续标点 +40ms 回退槽可能探入后词起点，由 sanitize 钳回）
    from core.text_utils import sanitize_word_timestamps
    merged = sanitize_word_timestamps(merged)
    real = [w for w in merged if not w.is_punct]
    punct = [w for w in merged if w.is_punct]
    assert [w.text for w in real] == pure          # 词位一一对应（计数契约成立）
    assert [w.text for w in punct] == ["ー", "、", "・", "〜"]
    # 单调不重叠
    for i in range(len(merged) - 1):
        assert merged[i].end_time <= merged[i + 1].start_time + 1e-9

    # ── attach 精确路径不回退 ──
    # attach 精确路径要求：Σ extract_pure_words(句) == len(aligner words)
    # 日语 mora 归并后该契约仍成立（两侧共用 extract_pure_words）
    sents = [
        Sentence(text="桜の花が咲きました。", start_time=1.0, end_time=4.0),
        Sentence(text="ちょっと待って、今行く。", start_time=5.0, end_time=8.0),
    ]
    expected_per_sent = [extract_pure_words(s.text) for s in sents]
    all_texts = [w for seg in expected_per_sent for w in seg]
    t = 1.0
    raw_words = []
    for w in all_texts:
        raw_words.append(WordTimestamp(text=w, start_time=round(t, 3), end_time=round(t + 0.3, 3)))
        t += 0.35
    assigned = attach_words_to_sentences(sents, raw_words)
    for s, expected in zip(assigned, expected_per_sent):
        got = [w.text for w in s.words if not w.is_punct]
        assert got == expected, f"精确切句漂移: {got!r} != {expected!r}"


# ─────────────────────────────────────────────────────────────
# 4. 日语汉字 pykakasi 读音路由（4 项合一）
# ─────────────────────────────────────────────────────────────
def _make_k1_aligner():
    """K1 测试专用：uroman/kakasi 均真实懒加载（无模型）。"""
    a = MMSAligner.__new__(MMSAligner)
    a._uroman = None
    a._kakasi = None
    a._vocab = {}
    return a


def _case_k1_pack():
    # ── 语言门控与缺失回退（无需 pykakasi）──
    a = _make_k1_aligner()
    # 语言门控：同字形中文项目下仍按拼音（uroman），绝不进 kakasi
    assert a._romanize_word("桜", "Chinese") == "ying"
    assert a._romanize_word("桜", "") == "ying"           # 语言未标日文时不启用读音路由
    # 纯假名词不动 kakasi：mora 罗马化原生正确
    assert a._romanize_word("きゃ", "Japanese") == "kya"
    # pykakasi 缺失时回退 uroman，流程不炸
    a2 = _make_k1_aligner()
    a2._kakasi = False
    assert a2._romanize_word("桜", "Japanese") == "ying"

    # ── pykakasi 读音 + 促音て/た形修复（缺包整段跳过）──
    pytest.importorskip("pykakasi")
    a = _make_k1_aligner()
    # 名词/熟语
    assert a._romanize_word("桜", "Japanese") == "sakura"
    assert a._romanize_word("今日", "Japanese") == "kyou"
    # 送り仮名语境（整词转换保住 さき，而非 さく+き）
    assert a._romanize_word("咲き", "Japanese") == "saki"

    a = _make_k1_aligner()
    # pykakasi 词典缺陷修复：っX 尾被误读 つX → 按形态还原
    assert a._romanize_word("待って", "Japanese") == "matte"
    assert a._romanize_word("歌って", "Japanese") == "utatte"
    assert a._romanize_word("走って", "Japanese") == "hashitte"

    # ── align() 必须把关语言传到 _romanize_word ──
    # align() 必须把关语言传到 _romanize_word，否则 K1 路由失效。
    # 用合成 emissions（全 blank 峰）驱动完整 align 管线，无需真实 ONNX 模型。
    import numpy as np
    from unittest.mock import patch
    a = _make_k1_aligner()
    from core.mms_aligner import _DEFAULT_MMS_VOCAB
    a._vocab = dict(_DEFAULT_MMS_VOCAB)
    seen = []
    orig = MMSAligner._romanize_word
    with patch.object(MMSAligner, "_romanize_word",
                      lambda self, w, language="": (seen.append(language), orig(self, w, language))[1]), \
         patch.object(MMSAligner, "_generate_emissions_chunked",
                      lambda self, audio: np.full((60, 31), -10.0, dtype=np.float32)
                      + np.eye(31, dtype=np.float32)[0] * 10.0):
        words = a.align((np.zeros(16000, dtype=np.float32), 16000), "桜の花", language="Japanese")
    assert [w.text for w in words] == ["桜", "の", "花"]
    assert seen and all(language == "Japanese" for language in seen)


# ── 聚合入口 ──────────────────────────────────────────────────────

def _case_ja_mora_pack():
    """日语 mora 4 合 1：拍归并与符号标点 / 中英韩回归不变 / 罗马化 / merge+attach 契约。"""
    _case_ja_mora_grouping_and_symbol_punct()
    _case_zh_en_ko_regression_unchanged()
    _case_ja_mora_romanization()
    _case_ja_merge_and_attach_contract()


def test_ja_kanji_reading_pack():
    """日语汉字 pykakasi 读音路由（K1 组）。"""
    _case_k1_pack()

# ═════════════════════════════════════════════════════════════
# F. MMS 单例 / CUDA 回退 / uroman 哨兵
# ═════════════════════════════════════════════════════════════
def _case_get_mms_aligner_singleton_keyed_by_device():
    import core.mms_aligner.engine as eng
    from core.mms_aligner import get_mms_aligner

    saved = eng._GLOBAL_MMS_ALIGNER
    try:
        a = get_mms_aligner(model_dir="/x-mms", device="cpu")
        a_again = get_mms_aligner(model_dir="/x-mms", device="cpu")
        assert a_again is a              # 连续同键复用
        b = get_mms_aligner(model_dir="/x-mms", device="cuda")
        assert b is not a                # device 不同 → 重建（修复「cpu 后再 cuda 仍 cpu」）
        assert b.device == "cuda"
        b_again = get_mms_aligner(model_dir="/x-mms", device="cuda")
        assert b_again is b
    finally:
        eng._GLOBAL_MMS_ALIGNER = saved


def _case_mms_cuda_fallback_does_not_permanently_change_device():
    from core.mms_aligner import MMSAligner

    m = MMSAligner(model_dir="/nonexistent-mms", device="cuda")
    m._session_want_cuda = False        # 模拟运行期 CUDA 回退
    assert m.device == "cuda"           # 用户偏好不被回退改动
    m.unload()
    assert m._session_want_cuda is None  # unload 清除 → 下次任务按偏好重试 CUDA
    assert m.session_device == ""


def _case_mms_uroman_failure_sets_sentinel(monkeypatch):
    import builtins

    from core.mms_aligner import MMSAligner

    real_import = builtins.__import__
    m = MMSAligner(model_dir="/nonexistent-mms", device="cpu")
    m._uroman = None

    def fail_uroman(name, *args, **kwargs):
        if name == "uroman":
            raise ImportError("no uroman")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_uroman)
    assert m._get_uroman() is None
    assert m._uroman is False          # 失败哨兵

    imported = []

    def count_import(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", count_import)
    assert m._get_uroman() is None
    assert "uroman" not in imported     # 哨兵短路，不再逐词重试 import


# ══════════════════════════════════════════════════════════════


def test_mms_text_ctc_pack():
    """test_mms_text_ctc_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_pure_words_and_no_bleeding_attachment()
    _case_ctc_math_pack()


def test_mms_session_residency_pack():
    """test_mms_session_residency_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_mms_unload_clears_session()
    _case_realign_and_ram_residency()
    _case_model_context_restores_active_aligner()


def test_mms_align_runtime_pack():
    """test_mms_align_runtime_pack：合并 4 个场景（断言逐条保留，见各 _case_*）。"""
    _case_mms_chunk_progress_is_reported()
    _case_vocal_features_precomputed_once_per_align()
    _case_tail_onset_sentinel_unit_pack()
    _case_tail_onset_sentinel_align_pack()


def test_mms_romanization_pack():
    """test_mms_romanization_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_spelling_tables()
    _case_ja_mora_pack()


def test_mms_singleton_pack(monkeypatch):
    """test_mms_singleton_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_get_mms_aligner_singleton_keyed_by_device()
    _case_mms_cuda_fallback_does_not_permanently_change_device()
    _case_mms_uroman_failure_sets_sentinel(monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
