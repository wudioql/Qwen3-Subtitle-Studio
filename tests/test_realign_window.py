"""tests/test_realign_window.py — 单句重对齐窗口语义合并套件（纯逻辑）

合并三个同主题契约组（原 test_align_padding_idempotent / test_align_window_recovery /
test_seam_snap 三文件）：

A. 稳定锚与幂等：MMS 末字拖音上界 = min(下一句起点, 元音起点+前瞻)，与裁剪窗
   长度解耦——重复重对齐收敛无棘轮；拖音可合法延伸出旧句界；pad 前后对称。
B. 窗口双侧扩展：裁剪窗 = [max(前句尾, 句首-扩展), min(后句头, 句尾+扩展)]——
   句界被手动拖短后真实发音仍在窗内，重对齐可恢复；邻句锚截窗不吞邻音频。
C. 句间接缝吸附：单句路径帧网格量化导致的 ≤25ms 缝隙吸附到后句起点，
   与全文重对齐（整段单一帧网格，天然无缝）表现一致；真实停顿不受影响。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytestmark = pytest.mark.logic

from core.align_engine import snap_tail_to_next_start, snap_next_start_to_prev_end
from core.constants import ALIGN_WIN_EXTEND, MMS_TAIL_EXTEND_MAX
from subs.models import Sentence, SubtitleProject, WordTimestamp


# ═════════════ A. 稳定锚与幂等 ═════════════


def _w(text, s, e):
    return WordTimestamp(text=text, start_time=s, end_time=e)


def _case_align_sentence_passes_stable_tail_limit_to_mms():
    """MMS 后端：tail_limit_sec = 下一句起点；窗尾放宽但被 tail_limit 截短。"""
    from core.align_engine import AlignConfig, align_sentence

    sent = Sentence(text="拖音", start_time=10.0, end_time=12.0, language="zh")
    audio = np.zeros(16000 * 30, dtype=np.float32)

    fake_mms = MagicMock()
    fake_mms.is_available.return_value = True
    fake_mms.align.return_value = [_w("拖", 10.05, 11.0), _w("音", 11.0, 12.8)]

    with patch("core.align_engine.get_mms_aligner", return_value=fake_mms):
        align_sentence(
            sent, audio, 16000,
            model_manager=MagicMock(),
            cfg=AlignConfig(align_backend="mms", source_language="zh"),
            next_sentence_start=13.5,
        )
    kwargs = fake_mms.align.call_args.kwargs
    assert kwargs["tail_limit_sec"] == 13.5
    # 裁剪窗：起点 = 10.0 - pad_before；终点 = min(12.0 + 前瞻, 13.5) = 13.5
    cropped, sr = fake_mms.align.call_args.args[0]
    win_len = len(cropped) / sr
    offset = kwargs["offset_sec"]
    assert abs((offset + win_len) - 13.5) < 0.05, "窗尾应被下一句起点截短"

    # 无下一句：不传上界（由 MMS 内部元音起点 + 前瞻封顶），窗尾 = 句尾 + 前瞻
    fake_mms.align.reset_mock()
    fake_mms.align.return_value = [_w("拖", 10.05, 11.0), _w("音", 11.0, 12.8)]
    with patch("core.align_engine.get_mms_aligner", return_value=fake_mms):
        align_sentence(
            sent, audio, 16000,
            model_manager=MagicMock(),
            cfg=AlignConfig(align_backend="mms", source_language="zh"),
        )
    kwargs = fake_mms.align.call_args.kwargs
    assert kwargs["tail_limit_sec"] is None
    cropped, sr = fake_mms.align.call_args.args[0]
    win_end = kwargs["offset_sec"] + len(cropped) / sr
    assert abs(win_end - (12.0 + MMS_TAIL_EXTEND_MAX)) < 0.05


def _case_realign_converges_no_tail_ratchet():
    """幂等钉样：模拟「贪婪追踪顶满上界」最坏情形，重复重对齐句尾收敛不递增。"""
    from core.align_engine import AlignConfig, align_sentence

    sent = Sentence(text="长音", start_time=20.0, end_time=23.0, language="zh")
    audio = np.zeros(16000 * 60, dtype=np.float32)
    cfg = AlignConfig(align_backend="mms", source_language="zh")
    NEXT_START = 26.0                      # 稳定锚：下一句起点

    fake_mms = MagicMock()
    fake_mms.is_available.return_value = True

    def fake_align(audio_tuple, text, *, language, offset_sec, tail_limit_sec=None):
        # 模拟真实修复后的 MMS 语义：末字吃到上界为止（贪婪最坏情形），
        # 上界 = min(tail_limit, 窗末端)
        cropped, sr = audio_tuple
        win_end = offset_sec + len(cropped) / sr
        cap = min(tail_limit_sec, win_end) if tail_limit_sec is not None else win_end
        return [
            _w("长", offset_sec + 0.2, offset_sec + 1.0),
            _w("音", offset_sec + 1.0, cap),
        ]

    fake_mms.align.side_effect = fake_align
    with patch("core.align_engine.get_mms_aligner", return_value=fake_mms):
        ends = []
        for _ in range(3):                 # 连续重对齐 3 次
            words = align_sentence(
                sent, audio, 16000, model_manager=MagicMock(), cfg=cfg,
                next_sentence_start=NEXT_START,
            )
            assert words
            sent.words = words
            sent.fix_times_from_words()    # 真实管线的句界回写
            ends.append(sent.end_time)

    # 上界是稳定锚：第一次就到位，之后不再变化（收敛而非每轮 +pad）
    assert ends[0] == ends[1] == ends[2], f"句尾未收敛: {ends}"
    assert ends[0] <= NEXT_START + 1e-6, "拖音不得越过下一句起点"


def _case_true_long_tail_can_extend_beyond_old_boundary():
    """拖音真值超出旧句界：一次重对齐即修正句尾出去，不被旧句界钳死。"""
    from core.align_engine import AlignConfig, align_sentence

    # 旧句界 12.0，但真实拖音到 13.2（下一句 14.0 之前）
    sent = Sentence(text="遥远", start_time=10.0, end_time=12.0, language="zh")
    audio = np.zeros(16000 * 30, dtype=np.float32)
    cfg = AlignConfig(align_backend="mms", source_language="zh")

    fake_mms = MagicMock()
    fake_mms.is_available.return_value = True
    fake_mms.align.return_value = [
        _w("遥", 10.1, 11.0),
        _w("远", 11.0, 13.2),              # 真实拖音越过旧句界 12.0
    ]
    with patch("core.align_engine.get_mms_aligner", return_value=fake_mms):
        words = align_sentence(
            sent, audio, 16000, model_manager=MagicMock(), cfg=cfg,
            next_sentence_start=14.0,
        )
    sent.words = words
    sent.fix_times_from_words()
    assert abs(sent.end_time - 13.2) < 1e-6, "真实拖音应把句尾合法修正出旧句界"


def _case_pad_symmetric_defaults_everywhere():
    """pad 前后对称：四处默认配置 pad_before == pad_after。"""
    from core.align_engine import AlignConfig
    from core.asr_engine import TranscribeConfig
    from core.app_config import AlignPreferences, ASRPreferences

    ac = AlignConfig()
    assert ac.pad_before == ac.pad_after == 0.12
    ap = AlignPreferences()
    assert ap.pad_before == ap.pad_after == 0.12
    tc = TranscribeConfig()
    assert tc.align_pad_before == tc.align_pad_after == 0.12
    sp = ASRPreferences()
    assert sp.align_pad_before == sp.align_pad_after == 0.12


# ═════════════ B. 窗口双侧扩展（拖错句界可恢复） ═════════════


def _qwen_window(sent, audio_dur=60.0, prev_end=None, next_start=None):
    """跑一次 Qwen 路径，返回 (win_start, win_end) 全局秒（含 pad）。"""
    from core.align_engine import AlignConfig, align_sentence

    audio = np.zeros(int(16000 * audio_dur), dtype=np.float32)
    captured = {}

    def fake_raw(audio_tuple, text, lang, *, model_manager):
        cropped, sr = audio_tuple
        captured["len"] = len(cropped) / sr
        return [{"text": ch, "start_time": 0.1 * i, "end_time": 0.1 * i + 0.08}
                for i, ch in enumerate(text)]

    with patch("core.align_engine.align_sentence_raw", side_effect=fake_raw), \
         patch("core.align_engine.check_segmenter_dependency", return_value=None):
        words = align_sentence(
            sent, audio, 16000, model_manager=MagicMock(),
            cfg=AlignConfig(align_backend="qwen", source_language="zh"),
            prev_sentence_end=prev_end, next_sentence_start=next_start,
        )
    assert words
    start = words[0].start_time - 0.0  # offset 已加回：首词相对 0 → 全局 = actual_start
    win_start = start                  # fake 首词 start_time=0 → 全局值即窗起点
    return win_start, win_start + captured["len"]


def _case_qwen_head_shrunk_recoverable():
    """句首被拖短（真实起音 8.0，被拖到 9.5）：窗口头侧必须覆盖 8.0。"""
    sent = Sentence(text="你好世界", start_time=9.5, end_time=11.0, language="zh")
    win_start, win_end = _qwen_window(sent, prev_end=6.0, next_start=13.0)
    assert win_start <= 8.0, f"窗口头侧未覆盖真实起音: win_start={win_start}"
    assert win_start >= 6.0 - 0.12 - 1e-6, "窗口不得越过前句尾（稳定锚，pad 容差内）"


def _case_qwen_tail_shrunk_recoverable():
    """句尾被拖短（真实收音 12.0，被拖到 10.5）：窗口尾侧必须覆盖 12.0。"""
    sent = Sentence(text="你好世界", start_time=9.0, end_time=10.5, language="zh")
    win_start, win_end = _qwen_window(sent, prev_end=7.0, next_start=13.0)
    assert win_end >= 12.0, f"窗口尾侧未覆盖真实收音: win_end={win_end}"
    assert win_end <= 13.0 + 0.12 + 1e-6, "窗口不得越过后句头（稳定锚，pad 容差内）"


def _case_qwen_window_without_neighbors_uses_extend():
    """无邻句：两侧按 ALIGN_WIN_EXTEND 放宽（媒体边缘由 _crop_audio 自身钳制）。"""
    sent = Sentence(text="你好", start_time=20.0, end_time=21.0, language="zh")
    win_start, win_end = _qwen_window(sent)
    assert win_start <= 20.0 - ALIGN_WIN_EXTEND + 0.2
    assert win_end >= 21.0 + ALIGN_WIN_EXTEND - 0.2


def _mms_window(sent, audio_dur=60.0, prev_end=None, next_start=None):
    """跑一次 MMS 路径，返回 (win_start, win_end, tail_limit)。"""
    from core.align_engine import AlignConfig, align_sentence

    audio = np.zeros(int(16000 * audio_dur), dtype=np.float32)
    fake = MagicMock()
    fake.is_available.return_value = True
    fake.align.return_value = [_w("你", sent.start_time, sent.start_time + 0.3)]
    with patch("core.align_engine.get_mms_aligner", return_value=fake):
        align_sentence(
            sent, audio, 16000, model_manager=MagicMock(),
            cfg=AlignConfig(align_backend="mms", source_language="zh"),
            prev_sentence_end=prev_end, next_sentence_start=next_start,
        )
    kwargs = fake.align.call_args.kwargs
    cropped, sr = fake.align.call_args.args[0]
    ws = kwargs["offset_sec"]
    return ws, ws + len(cropped) / sr, kwargs["tail_limit_sec"]


def _case_mms_head_shrunk_recoverable():
    """MMS 句首被拖短（真实起音 8.0，被拖到 9.5）：窗口头侧必须覆盖 8.0。"""
    sent = Sentence(text="你好", start_time=9.5, end_time=11.0, language="zh")
    win_start, win_end, tail = _mms_window(sent, prev_end=6.0, next_start=13.0)
    assert win_start <= 8.0, f"MMS 窗口头侧未覆盖真实起音: {win_start}"
    assert win_start >= 6.0 - 0.12 - 1e-6, "不得越过前句尾"
    assert tail == 13.0


def _case_mms_tail_shrunk_recoverable():
    """MMS 句尾被拖到任何位置：窗尾 = min(句尾+前瞻, 下一句头)，覆盖真值。"""
    # 真实收音 12.0；句尾被拖短到 10.2
    sent = Sentence(text="拖音", start_time=9.0, end_time=10.2, language="zh")
    win_start, win_end, tail = _mms_window(sent, prev_end=7.0, next_start=13.5)
    assert win_end >= 12.0, f"MMS 窗口尾侧未覆盖真实收音: {win_end}"
    assert win_end <= 13.5 + 1e-6, "不得越过后句头"


def _case_window_stable_across_repeated_realign():
    """锚不动 → 窗口恒定：同一句连续 3 次重对齐窗口完全一致（无棘轮）。"""
    sent = Sentence(text="你好", start_time=9.0, end_time=11.0, language="zh")
    wins = [
        _mms_window(sent, prev_end=6.0, next_start=13.0)
        for _ in range(3)
    ]
    assert wins[0] == wins[1] == wins[2], f"窗口漂移: {wins}"


def _sent(text, s, e, words=None, **kw):
    st = Sentence(text=text, start_time=s, end_time=e, language="zh", **kw)
    if words:
        st.words = [WordTimestamp(text=t, start_time=a, end_time=b) for t, a, b in words]
    return st


def _case_snap_within_threshold():
    """帧量化缝隙（20ms）→ 吸附无缝。"""
    s = _sent("你好", 1.0, 1.98, words=[("你", 1.0, 1.5), ("好", 1.5, 1.98)])
    snap_tail_to_next_start(s, 2.0)          # 间隙 20ms
    assert s.words[-1].end_time == 2.0
    assert s.end_time == 2.0


def _case_no_snap_beyond_threshold():
    """真实停顿（100ms）→ 保持原样。"""
    s = _sent("你好", 1.0, 1.9, words=[("你", 1.0, 1.5), ("好", 1.5, 1.9)])
    snap_tail_to_next_start(s, 2.0)          # 间隙 100ms
    assert s.words[-1].end_time == 1.9
    assert s.end_time == 1.9


def _case_no_snap_when_flush_or_overlap():
    """已无缝 / 已重叠 → 不动（不制造回退）。"""
    s = _sent("你好", 1.0, 2.0, words=[("你", 1.0, 1.5), ("好", 1.5, 2.0)])
    snap_tail_to_next_start(s, 2.0)          # 间隙 0
    assert s.words[-1].end_time == 2.0
    s2 = _sent("你好", 1.0, 2.05, words=[("你", 1.0, 1.5), ("好", 1.5, 2.05)])
    snap_tail_to_next_start(s2, 2.0)         # 已越过（重叠 50ms）
    assert s2.words[-1].end_time == 2.05


def _case_no_snap_without_next_or_words():
    """最后一句 / 无字级 → 不动。"""
    s = _sent("你好", 1.0, 1.98, words=[("你", 1.0, 1.5), ("好", 1.5, 1.98)])
    snap_tail_to_next_start(s, None)
    assert s.words[-1].end_time == 1.98
    s2 = _sent("你好", 1.0, 1.98)
    snap_tail_to_next_start(s2, 2.0)         # 无 words：不炸不动
    assert s2.end_time == 1.98


def _case_snap_next_start_to_prev_end():
    """D. 后句首不超前句尾（与前句尾吸附对称的收尾）。

    - 小重叠（≤25ms）→ 后句整体右移，首字 start = 前句尾，句内结构不变；
    - 大重叠 / 已对齐 / 无字级 / 锁定句 → 不动。
    """
    # 小重叠 20ms → 吸附
    nxt = _sent("世界", 1.98, 2.8, words=[("世", 1.98, 2.4), ("界", 2.4, 2.8)])
    changed = snap_next_start_to_prev_end(nxt, 2.0)
    assert changed is True
    assert nxt.start_time == 2.0
    assert nxt.words[0].start_time == 2.0
    assert nxt.words[0].end_time == 2.42          # 整体右移 0.02，句内结构不变
    assert nxt.end_time == 2.82

    # 大重叠 100ms → 不动
    nxt2 = _sent("世界", 1.9, 2.8, words=[("世", 1.9, 2.4), ("界", 2.4, 2.8)])
    assert snap_next_start_to_prev_end(nxt2, 2.0) is False
    assert nxt2.start_time == 1.9

    # 无重叠（后句首 > 前句尾）→ 不动
    nxt3 = _sent("世界", 2.1, 2.8, words=[("世", 2.1, 2.4), ("界", 2.4, 2.8)])
    assert snap_next_start_to_prev_end(nxt3, 2.0) is False

    # 无字级 → 不动
    nxt4 = _sent("世界", 1.98, 2.8)
    assert snap_next_start_to_prev_end(nxt4, 2.0) is False

    # 锁定句 → 不动
    nxt5 = _sent("世界", 1.98, 2.8, words=[("世", 1.98, 2.4), ("界", 2.4, 2.8)])
    nxt5.is_locked = True
    assert snap_next_start_to_prev_end(nxt5, 2.0) is False
    assert nxt5.start_time == 1.98


def _case_apply_seam_snaps_symmetric():
    """D. apply_seam_snaps 现在同时做「前句尾→后句首」与「后句首→前句尾」对称收尾。"""
    from core.align_engine import apply_seam_snaps

    # 相邻两句：前句尾 1.98 / 后句首 1.97（重叠 10ms）→ 后句首吸附到前句尾
    front = _sent("你好", 1.0, 1.98, words=[("你", 1.0, 1.5), ("好", 1.5, 1.98)])
    back = _sent("世界", 1.97, 2.8, words=[("世", 1.97, 2.4), ("界", 2.4, 2.8)])
    proj = SubtitleProject(sentences=[front, back])
    n = apply_seam_snaps(proj)
    assert back.start_time == 1.98           # 后句首不超前句尾
    assert back.words[0].start_time == 1.98
    assert front.words[-1].end_time <= back.start_time
    assert n == 1


def _case_dirty_realign_seam_matches_fulltext():
    """集成：脏句重对齐后连唱接缝无缝（与全文路径一致）。

    模拟单句网格量化：MMS 返回的尾字 end 停在 1.98（新网格帧边界），
    后句起点 2.0（旧网格值）。align_dirty_only 收尾后应吸附为 2.0。
    """
    from core.align_engine import AlignConfig, align_dirty_only

    proj = SubtitleProject(
        audio_path="mock.wav",
        sentences=[
            _sent("前句", 1.0, 1.9, is_dirty=True),
            _sent("后句", 2.0, 3.0, words=[("后", 2.0, 2.5), ("句", 2.5, 3.0)]),
        ],
    )
    audio = np.zeros(16000 * 5, dtype=np.float32)

    mock_mms = MagicMock()
    mock_mms.is_available.return_value = True
    # 脏句「前句」的下一句「后句」为干净句 → 走上下文强制对齐（align_with_context），
    # 只取中间句（本脏句）的字词。
    mock_mms.align_with_context.return_value = [
        WordTimestamp(text="前", start_time=1.0, end_time=1.5),
        WordTimestamp(text="句", start_time=1.5, end_time=1.98),   # 帧边界，差 20ms
    ]
    with patch("core.align_engine.get_mms_aligner", return_value=mock_mms), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        res = align_dirty_only(proj, model_manager=MagicMock(),
                               cfg=AlignConfig(align_backend="mms"))

    front = res.sentences[0]
    assert front.words[-1].end_time == 2.0, "接缝应吸附到后句起点（与全文路径无缝一致）"
    assert front.end_time == 2.0


def _case_fulltext_multilang_seam_snap():
    """全文重对齐（多语言分段）后段间接缝也必须吸附。

    旧 bug：full.py 已 import snap_tail 却从未调用；按语言分段时每段自有
    帧网格，段尾→下段首常出现 ≤25ms 空隙，用户可见「前后句 25ms 内空白
    未填」。
    """
    from core.align_engine import AlignConfig, align_full_text, apply_seam_snaps
    from core.text_utils import extract_pure_words

    # 两段语言：中→英；中段尾字刻意停在 1.98，英段从 2.0 起（20ms 缝）
    proj = SubtitleProject(
        audio_path="mock.wav",
        media_duration=10.0,
        sentences=[
            Sentence(text="你好", start_time=0.0, end_time=1.98, language="zh"),
            Sentence(text="Hello", start_time=2.0, end_time=3.5, language="en"),
        ],
    )
    audio = np.zeros(16000 * 10, dtype=np.float32)
    calls: list = []

    def _align_side_effect(audio_tuple, text, *, language="", offset_sec=0.0, **kw):
        calls.append(language)
        pure = extract_pure_words(text)
        # 按语言返回：中文段末字 1.98；英文段从 2.0 起
        if language == "Chinese":
            return [
                WordTimestamp(text=pure[0], start_time=0.0, end_time=1.0),
                WordTimestamp(text=pure[1], start_time=1.0, end_time=1.98),
            ]
        return [
            WordTimestamp(text=w, start_time=2.0 + i * 0.4, end_time=2.0 + i * 0.4 + 0.35)
            for i, w in enumerate(pure)
        ]

    mock = MagicMock()
    mock.is_available.return_value = True
    mock.align.side_effect = _align_side_effect
    mm = MagicMock()
    mm.using_mms_aligner.return_value.__enter__.return_value = mock

    with patch("core.align_engine.get_mms_aligner", return_value=mock), \
         patch("core.audio_io.load_audio", return_value=(audio, 16000)):
        res = align_full_text(
            proj, model_manager=mm,
            cfg=AlignConfig(align_backend="mms", source_language="auto"),
        )

    assert calls == ["Chinese", "English"]
    front = res.sentences[0]
    assert front.words[-1].end_time == 2.0, (
        f"全文多语言段间接缝应吸附：尾字 end={front.words[-1].end_time} 期望 2.0"
    )
    assert front.end_time == 2.0

    # apply_seam_snaps 幂等：再跑一次不应改变
    n2 = apply_seam_snaps(res)
    assert n2 == 0
    assert front.words[-1].end_time == 2.0


def _case_long_sentence_split_is_bounded_and_non_recursive():
    """>300s 无标点句也必须严格缩小；极端短文本明确报错而非递归爆栈。"""
    from core.align_engine import AlignConfig
    from core.align_engine.sentence import _align_long_sentence, _split_long_text_bounded

    text = "abcdefghijklmnopqrstuvwxyz" * 8
    parts = _split_long_text_bounded(
        text, duration=601.0, max_duration=300.0, min_chars=6,
    )
    assert "".join(parts) == text
    assert len(parts) >= 3
    assert all(len(p) / len(text) * 601.0 <= 300.0 + 1e-6 for p in parts)

    calls: list[tuple[str, float]] = []

    def fake_align(sub, *_args, **_kwargs):
        calls.append((sub.text, sub.end_time - sub.start_time))
        return [WordTimestamp(sub.text, sub.start_time, sub.end_time)]

    sentence = Sentence(text=text, start_time=0.0, end_time=601.0, language="en")
    with patch("core.align_engine.align_sentence", side_effect=fake_align):
        words = _align_long_sentence(
            sentence, np.zeros(8, dtype=np.float32), 16000,
            model_manager=MagicMock(), cfg=AlignConfig(), lang_full="English",
        )
    assert "".join(t for t, _ in calls) == text
    assert len(words) == len(calls)
    assert all(duration <= 300.0 + 1e-6 for _, duration in calls)

    with pytest.raises(ValueError, match="请先手动拆分"):
        _split_long_text_bounded(
            "a", duration=301.0, max_duration=300.0, min_chars=1,
        )


# ── 聚合入口 ─────────────────────────────────────────────────────

def _case_stable_anchor_and_idempotency():
    """A. 稳定锚与幂等：tail_limit 传递 / 3 连次收敛 / 拖音合法越旧句界 / pad 对称。"""
    _case_align_sentence_passes_stable_tail_limit_to_mms()
    _case_realign_converges_no_tail_ratchet()
    _case_true_long_tail_can_extend_beyond_old_boundary()
    _case_pad_symmetric_defaults_everywhere()


def _case_window_recovery_from_dragged_boundaries():
    """B. 窗口双侧扩展：Qwen/MMS 首尾拖短可恢复 / 邻锚截窗 / 无邻句放宽 / 窗口恒定。"""
    _case_qwen_head_shrunk_recoverable()
    _case_qwen_tail_shrunk_recoverable()
    _case_qwen_window_without_neighbors_uses_extend()
    _case_mms_head_shrunk_recoverable()
    _case_mms_tail_shrunk_recoverable()
    _case_window_stable_across_repeated_realign()


def _case_seam_snap_consistency():
    """C. 接缝吸附：阈内吸附 / 真停顿不动 / 无缝与重叠不动 / 边界情形 / 脏句+全文集成。"""
    _case_snap_within_threshold()
    _case_no_snap_beyond_threshold()
    _case_no_snap_when_flush_or_overlap()
    _case_no_snap_without_next_or_words()
    _case_dirty_realign_seam_matches_fulltext()
    _case_fulltext_multilang_seam_snap()


def _case_seam_snap_symmetric_next_start():
    """D. 后句首不超前句尾：小重叠吸附 / 大重叠·已对齐·无字级·锁定不动 / apply_seam_snaps 对称。"""
    _case_snap_next_start_to_prev_end()
    _case_apply_seam_snaps_symmetric()


def test_realign_window_pack():
    """test_realign_window_pack：合并 5 个场景（断言逐条保留，见各 _case_*）。"""
    _case_long_sentence_split_is_bounded_and_non_recursive()
    _case_stable_anchor_and_idempotency()
    _case_window_recovery_from_dragged_boundaries()
    _case_seam_snap_consistency()
    _case_seam_snap_symmetric_next_start()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
