"""core.mms_aligner — MMS-300M-FA-ONNX 多语言歌词/长延音强制对齐（包）

子模块：
    constants  帧步长 / 词表 / 分块 / 哨兵阈值
    ctc        Trellis Viterbi / 末字哨兵 / 元音终点
    romanize   数字拼读表 / 汉字判定
    engine     MMSAligner + get_mms_aligner 单例

对外保持：
    from core.mms_aligner import MMSAligner, get_mms_aligner
    from core.mms_aligner import _ctc_forced_align_canonical, _DEFAULT_MMS_VOCAB, ...
"""
from __future__ import annotations

from .constants import (
    _CTC_TRELLIS_MAX_BYTES,
    _DEFAULT_MMS_VOCAB,
    _ONSET_BACKTRACK_RATIO,
    _SAMPLE_RATE,
    _TAIL_ONSET_PROB,
    _TAIL_ONSET_PROB_SUSTAINED,
    _WAV2VEC2_STRIDE_SEC,
)
from .ctc import (
    _compute_vocal_features,
    _ctc_forced_align_canonical,
    _find_next_content_onset,
    _find_singing_vocal_end_frame,
)
from .romanize import _DIGIT_SPELLINGS, _contains_kanji
from .engine import MMSAligner, get_mms_aligner

__all__ = [
    "MMSAligner",
    "get_mms_aligner",
    "_DEFAULT_MMS_VOCAB",
    "_CTC_TRELLIS_MAX_BYTES",
    "_ctc_forced_align_canonical",
    "_compute_vocal_features",
    "_find_next_content_onset",
    "_find_singing_vocal_end_frame",
    "_TAIL_ONSET_PROB",
    "_TAIL_ONSET_PROB_SUSTAINED",
    "_ONSET_BACKTRACK_RATIO",
    "_contains_kanji",
    "_DIGIT_SPELLINGS",
    "_WAV2VEC2_STRIDE_SEC",
    "_SAMPLE_RATE",
]
