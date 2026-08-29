"""core.mms_aligner.ctc — CTC Trellis、末字哨兵、歌唱元音终点。"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import librosa

from .constants import (
    _CTC_TRELLIS_MAX_BYTES,
    _ONSET_BACKTRACK_RATIO,
    _TAIL_ONSET_PROB,
    _TAIL_ONSET_PROB_SUSTAINED,
    _WAV2VEC2_STRIDE_SAMPLES,
)

def _ctc_forced_align_canonical(
    log_probs: np.ndarray,
    targets: List[int],
    blank_id: int = 0,
) -> np.ndarray:
    """经典 2L+1 CTC Viterbi；滚动 score 行 + uint8 回溯指针。

    旧实现保存完整 float32 ``alpha[T,M]`` 和 int16 指针，另物化
    ``emission_tokens[T,M]``，峰值约 10 bytes/cell。现只保留 1 byte/cell
    回溯指针与 O(M) 工作行；病态输入在分配前按预算明文失败。
    """
    if log_probs.ndim != 2:
        raise ValueError(f"CTC log_probs 必须是二维数组，实际 shape={log_probs.shape}")
    T, vocab_size = log_probs.shape
    L = len(targets)
    if L == 0 or T == 0:
        return np.zeros(T, dtype=np.int64)
    if not 0 <= blank_id < vocab_size:
        raise ValueError(f"CTC blank_id={blank_id} 超出词表大小 {vocab_size}")
    if any(token < 0 or token >= vocab_size for token in targets):
        raise ValueError("CTC targets 含超出词表范围的 token id")

    # 相邻重复 token 之间必须插 blank，因此所需最少帧数 = L + 重复次数。
    min_frames = L + sum(targets[i] == targets[i - 1] for i in range(1, L))
    if T < min_frames:
        raise ValueError(
            f"CTC 帧数不足：{T} 帧无法容纳 {L} 个 token（至少需 {min_frames} 帧）"
        )

    M = 2 * L + 1
    pointer_bytes = T * M * np.dtype(np.uint8).itemsize
    working_bytes = M * (
        5 * np.dtype(np.float32).itemsize
        + np.dtype(np.uint8).itemsize
        + np.dtype(bool).itemsize
    )
    estimated_bytes = pointer_bytes + working_bytes
    if estimated_bytes > _CTC_TRELLIS_MAX_BYTES:
        raise MemoryError(
            "CTC 对齐规模超过内存预算："
            f"T={T}, states={M}, 预计 {estimated_bytes / 1024**2:.1f} MiB > "
            f"{_CTC_TRELLIS_MAX_BYTES / 1024**2:.0f} MiB。"
            "请缩短单次音频/文本或拆分字幕段。"
        )

    y_prime = np.zeros(M, dtype=np.int64)
    y_prime[1::2] = np.asarray(targets, dtype=np.int64)

    backtrack_ptr = np.zeros((T, M), dtype=np.uint8)
    previous = np.full(M, -np.inf, dtype=np.float32)
    current = np.full(M, -np.inf, dtype=np.float32)
    best_previous = np.empty(M, dtype=np.float32)
    shift1 = np.empty(M, dtype=np.float32)
    shift2 = np.empty(M, dtype=np.float32)
    best_step = np.empty(M, dtype=np.uint8)

    previous[0] = log_probs[0, blank_id]
    if M > 1:
        previous[1] = log_probs[0, y_prime[1]]

    can_skip_blank = np.zeros(M, dtype=bool)
    for state in range(3, M, 2):
        if y_prime[state] != y_prime[state - 2]:
            can_skip_blank[state] = True

    for frame in range(1, T):
        np.copyto(best_previous, previous)
        best_step.fill(0)

        shift1.fill(-np.inf)
        shift1[1:] = previous[:-1]
        mask1 = shift1 > best_previous
        best_previous[mask1] = shift1[mask1]
        best_step[mask1] = 1

        if M >= 3:
            shift2.fill(-np.inf)
            shift2[2:] = previous[:-2]
            mask2 = can_skip_blank & (shift2 > best_previous)
            best_previous[mask2] = shift2[mask2]
            best_step[mask2] = 2

        current[:] = best_previous + log_probs[frame, y_prime]
        backtrack_ptr[frame] = best_step
        previous, current = current, previous

    candidates = [M - 1]
    if M >= 2:
        candidates.append(M - 2)
    best_final_state = max(candidates, key=lambda state: float(previous[state]))
    best_final_score = float(previous[best_final_state])
    if not np.isfinite(best_final_score):
        raise ValueError(
            "CTC 无法完整对齐全部文本 token；拒绝返回部分路径以免生成虚假时间戳"
        )

    path = np.zeros(T, dtype=np.int64)
    state = int(best_final_state)
    for frame in range(T - 1, -1, -1):
        path[frame] = state
        state -= int(backtrack_ptr[frame, state])
        if state < 0:  # 防御：有效 Viterbi 路径不应越界
            raise ValueError("CTC 回溯状态越界")
    return path

def _find_next_content_onset(
    log_probs: np.ndarray,
    start_frame: int,
    end_frame: int,
    own_tokens: "set[int]",
    blank_id: int = 0,
    min_prob: float = _TAIL_ONSET_PROB,
) -> Optional[int]:
    """在 [start_frame, end_frame) 内找「异质发音内容」的开口帧。

    命中条件（满足其一）：
      - 单帧：最大后验 token 非 blank / 非 own_tokens 且概率 ≥ min_prob；
      - 持续：连续 2 帧同为异质 token 且概率 ≥ _TAIL_ONSET_PROB_SUSTAINED
        （辅音峰短促、置信偏低，单帧高阈会漏检）。
    命中后向前回溯到该 token 后验爬坡的起脚（跌破峰值 _ONSET_BACKTRACK_RATIO
    即停），返回发音真正开始的帧——辅音的闭塞/送气段一并让出，避免
    「尾音包住后句首字辅音」。未找到返回 None。
    """
    t0 = max(0, int(start_frame))
    t1 = min(log_probs.shape[0], int(end_frame))
    if t1 <= t0:
        return None
    seg = log_probs[t0:t1]
    best_tok = np.argmax(seg, axis=-1)
    best_logp = seg[np.arange(seg.shape[0]), best_tok]
    log_hi = float(np.log(min_prob))
    log_lo = float(np.log(_TAIL_ONSET_PROB_SUSTAINED))

    def _is_foreign(k: int) -> bool:
        tok = int(best_tok[k])
        return tok != blank_id and tok not in own_tokens

    hit_k: Optional[int] = None
    for k in range(seg.shape[0]):
        if not _is_foreign(k):
            continue
        if best_logp[k] >= log_hi:
            hit_k = k
            break
        # 持续判定：本帧与下帧都是异质且过低阈
        if (best_logp[k] >= log_lo and k + 1 < seg.shape[0]
                and _is_foreign(k + 1) and best_logp[k + 1] >= log_lo):
            hit_k = k
            break
    if hit_k is None:
        return None

    # 回溯：沿命中 token 的后验向前走，跌破峰值比例即为发音起脚
    tok = int(best_tok[hit_k])
    peak_logp = float(log_probs[t0 + hit_k, tok])
    floor = peak_logp + float(np.log(_ONSET_BACKTRACK_RATIO))
    onset = hit_k
    for k in range(hit_k - 1, max(-1, hit_k - 25), -1):  # 最多回溯 25 帧（500ms）
        if k < 0 or float(log_probs[t0 + k, tok]) < floor:
            break
        onset = k
    return t0 + onset


def _compute_vocal_features(
    audio_np: np.ndarray,
    frame_stride: int = _WAV2VEC2_STRIDE_SAMPLES,
) -> tuple[np.ndarray, np.ndarray]:
    """每次 align 只计算一次频谱平坦度与 RMS，供所有词共享。"""
    try:
        flatness = librosa.feature.spectral_flatness(
            y=audio_np, n_fft=512, hop_length=frame_stride,
        )[0]
        rms = librosa.feature.rms(
            y=audio_np, frame_length=512, hop_length=frame_stride,
        )[0]
        return (
            np.asarray(flatness, dtype=np.float32),
            np.asarray(rms, dtype=np.float32),
        )
    except Exception:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)


def _find_singing_vocal_end_frame(
    audio_np: np.ndarray,
    vowel_start_frame: int,
    max_end_frame: int,
    frame_stride: int = _WAV2VEC2_STRIDE_SAMPLES,
    *,
    features: Optional[tuple[np.ndarray, np.ndarray]] = None,
) -> int:
    """从元音起点向后结合语音频谱平坦度 (Spectral Flatness 谐波结构) 与局部能量，精确定位长元音的自然衰减结束点。

    - 辅音已在前置帧区间内完整保护；
    - 从元音起点扫描：能量高于阈值且平坦度低（富含谐波共振峰）判定为歌唱元音发音态；
    - 遇到停顿/呼吸态（平坦度跳升且能量跌落持续 60ms），发音自然截断；
    - 严格在上界 max_end_frame 处截断，绝不跨越停顿侵入后句。
    """
    total_samples = len(audio_np)
    num_frames = total_samples // frame_stride
    max_frame = min(num_frames, max_end_frame)
    if vowel_start_frame >= max_frame:
        return max_frame

    flatness, rms = features if features is not None else _compute_vocal_features(
        audio_np, frame_stride,
    )
    n_f = min(len(flatness), len(rms), max_frame)
    if n_f <= vowel_start_frame:
        return max_frame

    vocal_rms_peak = float(np.max(rms[vowel_start_frame:n_f])) if n_f > vowel_start_frame else 0.0
    if vocal_rms_peak < 1e-4:
        return min(max_frame, vowel_start_frame + 2)

    rms_thresh = max(vocal_rms_peak * 0.12, 0.008)

    end_f = vowel_start_frame + 2
    for t in range(vowel_start_frame, n_f):
        is_voiced = bool((rms[t] >= rms_thresh) and (flatness[t] < 0.22 or t <= vowel_start_frame + 6))
        if is_voiced:
            end_f = t + 1
        else:
            if t + 2 < n_f:
                check_voiced = [
                    bool((rms[k] >= rms_thresh) and (flatness[k] < 0.22 or k <= vowel_start_frame + 6))
                    for k in range(t, min(n_f, t + 3))
                ]
                if not any(check_voiced):
                    break
            else:
                break

    return min(max_frame, max(vowel_start_frame + 2, end_f))


__all__ = [
    "_ctc_forced_align_canonical",
    "_compute_vocal_features",
    "_find_next_content_onset",
    "_find_singing_vocal_end_frame",
]
