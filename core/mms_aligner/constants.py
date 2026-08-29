"""core.mms_aligner.constants — 帧步长、词表、分块与拖音哨兵阈值。"""
from __future__ import annotations

# Wav2Vec2 标准帧步长 (50Hz = 20ms / frame，每帧对应 320 个 16kHz 采样点)
_WAV2VEC2_STRIDE_SAMPLES = 320
_WAV2VEC2_STRIDE_SEC = 0.020
_SAMPLE_RATE = 16000

# 分块推理参数（控制显存恒定 < 400MB）
_CHUNK_WINDOW_SEC = 30.0
_CHUNK_CONTEXT_SEC = 2.0

# 标准 MMS-FA 基础字符集 (31 tokens，0 为 <blank>)
_DEFAULT_MMS_VOCAB = {
    "<blank>": 0, "<pad>": 1, "</s>": 2, "<unk>": 3,
    "a": 4, "i": 5, "e": 6, "n": 7, "o": 8, "u": 9,
    "t": 10, "s": 11, "r": 12, "m": 13, "k": 14, "l": 15,
    "d": 16, "g": 17, "h": 18, "y": 19, "b": 20, "p": 21,
    "w": 22, "c": 23, "v": 24, "j": 25, "z": 26, "f": 27,
    "'": 28, "q": 29, "x": 30,
}

# CTC Viterbi 回溯指针内存预算。实现使用 uint8 指针 + 滚动 score 行，
# 预算主要约束 T×(2L+1)，防病态长文本直接耗尽系统内存。
_CTC_TRELLIS_MAX_BYTES = 512 * 1024 * 1024

# 末字拖音 CTC 异质发音峰哨兵
_TAIL_ONSET_PROB = 0.5
_TAIL_ONSET_PROB_SUSTAINED = 0.35
_ONSET_BACKTRACK_RATIO = 0.2

__all__ = [
    "_WAV2VEC2_STRIDE_SAMPLES", "_WAV2VEC2_STRIDE_SEC", "_SAMPLE_RATE",
    "_CHUNK_WINDOW_SEC", "_CHUNK_CONTEXT_SEC", "_DEFAULT_MMS_VOCAB",
    "_CTC_TRELLIS_MAX_BYTES", "_TAIL_ONSET_PROB", "_TAIL_ONSET_PROB_SUSTAINED", "_ONSET_BACKTRACK_RATIO",
]
