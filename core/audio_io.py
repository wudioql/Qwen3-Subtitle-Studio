"""core.audio_io — 音频 I/O 工具

职责：
- 从任意音/视频文件中，用 FFmpeg 提取并重采样为 16kHz 单声道 WAV
- 读取 WAV 为 numpy 数组，获取时长、采样率
- 基于能量的静音点检测（用于长音频切分，避免截断句子）
- 按静音点切片为若干块，带重叠避免边界截断

依赖：
- ffmpeg（系统 PATH 中可用）
- soundfile（读取 WAV 为 numpy）
- numpy（降采样、能量计算）
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

from .constants import DEFAULT_SAMPLE_RATE, TEMP_DIR, ensure_temp_dir

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────
@dataclass
class AudioInfo:
    """读取到的音频元信息。"""
    path: Path
    sample_rate: int
    channels: int
    duration: float     # 秒
    num_frames: int


# ─────────────────────────────────────────────────────────────
# 基础：FFmpeg 可用性检查 / 调用
# ─────────────────────────────────────────────────────────────
# 探测策略：不再依赖 -formats / -filters 文本解析（易受裁剪版排版差异欺骗）；
# 改为**实际抽音最小样本**做黑盒测试（成功生成 WAV 即通过）。
# 理由：
#  - TRAE / IDE / scoop shim 自带的 ffmpeg 大多是裁剪版，仅保留核心 codec，
#    有的缺 -formats 文本描述、有的描述里带 wavarc 却没有 wav muxer，
#    直接跑真实转换最可靠。
#  - 项目当前不提供硬字幕烧录，因此启动探针不探测 ass 滤镜。

def _ffmpeg_smoke_extract_ok(exe_path: str) -> tuple[bool, str]:
    """用 ffmpeg 对 0.5s anullsrc 静音 → 16k mono WAV 走一遍完整抽音管线，成功返回 True + note。"""
    ensure_temp_dir()
    tmp_out = TEMP_DIR / f"_ffmpeg_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}.wav"
    try:
        args = [
            exe_path, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
            "-t", "0.5",
            "-ar", "16000", "-ac", "1", "-f", "wav",
            str(tmp_out),
        ]
        r = subprocess.run(args, capture_output=True, text=True, check=False, timeout=15)
        if r.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size < 1024:
            note = (r.stderr or "").strip().splitlines()
            note_txt = note[-1] if note else f"exit={r.returncode}, out size={tmp_out.stat().st_size if tmp_out.exists() else 'n/a'}"
            return False, note_txt
        # 顺带验证输出 WAV 能被 soundfile 读
        try:
            _info = sf.info(str(tmp_out))
            if _info.samplerate == 16000 and _info.channels == 1 and _info.frames > 0:
                return True, "wav smoke ok"
            return False, f"sf.info check failed: sr={_info.samplerate}, ch={_info.channels}, frames={_info.frames}"
        except Exception as e_sf:
            return False, f"WAV generated but soundfile cannot read: {e_sf}"
    except Exception as e:
        return False, f"probe exception: {e}"
    finally:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except Exception:
            logger.debug("[ffmpeg] smoke probe failed for %s", exe_path)


def _iter_ffmpeg_candidates(override: str = "") -> list[str]:
    """遍历所有候选 ffmpeg 路径：shim 展开为真实 exe 前，先把 PATH 里所有同名可执行都枚举出来。

    override：设置页「路径」指定的 ffmpeg（空 = 未指定），由调用方传入以复用其偏好读取结果。
    """
    seen: set[str] = set()
    candidates: list[str] = []
    # 0) 用户在设置页「路径」里指定的 ffmpeg 优先（冒烟测试同样会校验它，
    #    若不可用会带着明确原因落到后续候选）
    if override:
        candidates.append(override)
        seen.add(os.path.normcase(os.path.abspath(override)))
    # 1) PATH 里所有同名 exe
    path_env = os.environ.get("PATH", "")
    exts = [e for e in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(os.pathsep) if e]
    for p_dir in path_env.split(os.pathsep):
        if not p_dir:
            continue
        for ext in exts:
            cand = os.path.join(p_dir, "ffmpeg" + ext)
            if os.path.isfile(cand):
                cand_norm = os.path.normcase(os.path.abspath(cand))
                if cand_norm not in seen:
                    seen.add(cand_norm)
                    candidates.append(cand)
    return candidates or ["ffmpeg"]


# 冒烟探测结果缓存：同一偏好 override 下只探测一次。
_FFMPEG_CHOSEN: Optional[str] = None
_FFMPEG_CHOSEN_OVERRIDE: str = ""


def _current_ffmpeg_override() -> str:
    """读取设置页「路径」里指定的 ffmpeg（空 = 未指定）。"""
    try:
        from .app_config import load_preferences
        return (load_preferences().paths.ffmpeg_path or "").strip()
    except Exception:
        return ""


def ensure_ffmpeg() -> str:
    """选择能通过抽音冒烟测试的 ffmpeg；都不通过就抛详细的逐条失败原因。

    性能：冒烟探测结果进程内缓存——同一偏好 override 下只探测一次，避免每次
    抽音/人声分离都重跑 ffmpeg 子进程（一次「打开媒体 + 人声分离」此前约触发
    2~3 次探测）。偏好 ffmpeg_path 变化会**自动失效**重新探测；PATH/安装变化
    等场景可显式调 ``invalidate_ffmpeg_cache()``。
    """
    global _FFMPEG_CHOSEN, _FFMPEG_CHOSEN_OVERRIDE
    override = _current_ffmpeg_override()
    if _FFMPEG_CHOSEN is not None and override == _FFMPEG_CHOSEN_OVERRIDE:
        return _FFMPEG_CHOSEN
    cands = _iter_ffmpeg_candidates(override)
    logger.info("[ffmpeg] 候选路径: %s", cands)
    errors: list[str] = []
    for cand in cands:
        ok, note = _ffmpeg_smoke_extract_ok(cand)
        if ok:
            logger.info("[ffmpeg] 选择: %s（%s）", cand, note)
            _FFMPEG_CHOSEN = cand
            _FFMPEG_CHOSEN_OVERRIDE = override
            return cand
        errors.append(f"{cand}: {note}")

    # 都不通过：给出排查建议 + 逐条失败原因
    bulleted = "\n  - ".join(errors)
    raise RuntimeError(
        "未找到能正常抽出 WAV 的完整版 FFmpeg（当前所有候选都是裁剪版，IDE/TRAE 自带最常见）。\n"
        f"已探测但不通过的候选及失败原因：\n  - {bulleted}\n\n"
        "解决办法（选其一即可，推荐第 1 条）：\n"
        "  1) 用 scoop 安装完整版：scoop install ffmpeg（确保 scoop shims 指向 full 版，必要时 scoop uninstall 精简版再重装）；\n"
        "  2) 或下载 BtbN GPL 版（github.com/BtbN/FFmpeg-Builds/releases）解压，并把 bin/ 放 PATH 最前面；\n"
        "  3) 或下载 gyan.dev essentials（www.gyan.dev/ffmpeg/builds），同上放到 PATH 前面。\n"
        "目标：确保 ffmpeg -formats 中包含 DE  wav，或能正常把任意视频/音频转为 WAV。"
    )


def invalidate_ffmpeg_cache() -> None:
    """清除 FFmpeg 冒烟探测缓存；下次 ``ensure_ffmpeg()`` 会重新探测。

    设置页修改 ffmpeg_path 会自动失效（override 变化即重探）；本函数用于
    PATH/安装变化等外部场景。
    """
    global _FFMPEG_CHOSEN, _FFMPEG_CHOSEN_OVERRIDE
    _FFMPEG_CHOSEN = None
    _FFMPEG_CHOSEN_OVERRIDE = ""


def _run_ffmpeg(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """统一封装 ffmpeg 调用，捕获日志。"""
    exe = ensure_ffmpeg()
    full = [exe, "-hide_banner", "-loglevel", "error", "-y", *args]
    logger.debug("[ffmpeg] 执行: %s", " ".join(full))
    return subprocess.run(
        full,
        check=check,
        capture_output=True,
        text=True,
    )


# ─────────────────────────────────────────────────────────────
# 音频提取与重采样
# ─────────────────────────────────────────────────────────────
def extract_audio(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = 1,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
) -> Path:
    """从任意音/视频文件中截取并重采样为 WAV。

    Args:
        input_path: 任意音/视频文件路径
        output_path: 输出 WAV 路径；None 则放入 TEMP_DIR/<uuid>.wav
        sample_rate: 目标采样率，默认 16000
        channels: 目标声道数，默认 1（mono）
        start_sec: 截取起始秒（None 表示 0）
        end_sec: 截取结束秒（None 表示到文件尾）

    Returns:
        实际的 WAV 输出 Path
    """
    in_path = Path(input_path)
    if not in_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {in_path}")

    if output_path is None:
        ensure_temp_dir()
        out_path = TEMP_DIR / f"{uuid.uuid4().hex}.wav"
    else:
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

    start = float(start_sec) if start_sec is not None else 0.0
    args: list[str] = []
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(in_path)]
    # end_sec 单独给出（start_sec 缺省）时同样生效：截取 [0, end_sec]。
    if end_sec is not None and float(end_sec) > start:
        args += ["-t", f"{float(end_sec) - start:.3f}"]
    args += [
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-f", "wav",
        "-vn",  # 丢弃视频流
        str(out_path),
    ]

    result = _run_ffmpeg(args, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg 提取音频失败 (exit={result.returncode}):\n"
            f"stderr: {result.stderr.strip()}"
        )

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg 输出为空: {out_path}")
    return out_path


# ─────────────────────────────────────────────────────────────
# 读取 WAV 为 numpy + 元信息
# ─────────────────────────────────────────────────────────────
def load_audio(path: str | Path, *, mono: bool = True,
               target_sr: int | None = None) -> tuple[np.ndarray, int]:
    """读取音频，返回 (samples, sample_rate)。samples 为 float32，范围 [-1, 1]。

    - 多声道默认降为 mono（沿 channel 维度平均）。
    - target_sr 传入且与实际采样率不同 → 内存重采样（librosa）；
      None 保持历史行为「不做任何重采样」。
    - 对齐等模型消费方统一传 target_sr=DEFAULT_SAMPLE_RATE，
      不再要求 project.audio_path 本身必须是 16k 提取件（WAV 零复制管线）。
    """
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)  # (frames, ch)
    if mono and data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=False)
    else:
        data = data.reshape(-1)
    data_f = data.astype(np.float32, copy=False)
    if target_sr is not None and int(sr) != int(target_sr):
        import librosa  # 懒加载：仅重采样路径才付导入成本
        data_f = librosa.resample(
            data_f, orig_sr=int(sr), target_sr=int(target_sr),
        ).astype(np.float32, copy=False)
        sr = int(target_sr)
    return data_f, int(sr)


def probe_native_audio(path: str | Path) -> AudioInfo | None:
    """soundfile 直读性探测（WAV 零复制管线）。

    soundfile/libsndfile 可读（wav/flac/ogg/aiff 及 libsndfile≥1.1 的 mp3）
    → 返回 AudioInfo；容器类/非常规格式（mp4/mkv/m4a/aac…）打不开 → None，
    调用方再走 FFmpeg 提取路径。「能直读」的媒体全程不产生 .temp 副本。
    """
    try:
        return get_audio_info(path)
    except Exception:  # noqa: BLE001 — soundfile 拒绝即视为不可直读
        return None


def get_audio_info(path: str | Path) -> AudioInfo:
    """快速获取音频信息（不读取全部采样数据）。"""
    info = sf.info(str(path))
    return AudioInfo(
        path=Path(path),
        sample_rate=int(info.samplerate),
        channels=int(info.channels),
        duration=float(info.duration),
        num_frames=int(info.frames),
    )


# ─────────────────────────────────────────────────────────────
# 静音点检测（基于帧能量）
# ─────────────────────────────────────────────────────────────
def detect_silence_points(
    audio: np.ndarray,
    sample_rate: int,
    *,
    threshold_db: float = -30.0,
    min_silence_sec: float = 0.5,
    frame_ms: int = 25,
    hop_ms: int = 10,
) -> list[float]:
    """基于能量的静音点检测，返回**建议切分的时间戳（秒）列表。

    每个切分点位于一段连续静音的中点，避免切到语音。

    Args:
        audio: float32 单声道音频 [-1,1]
        sample_rate: 采样率
        threshold_db: 静音能量阈值，dBFS 相对满刻度，-30 较保守
        min_silence_sec: 连续静音最短时长才视为"可切分"
        frame_ms: 每帧长度毫秒
        hop_ms: 帧移毫秒

    Returns:
        建议切分的时间戳列表（秒），已按升序排列
    """
    if audio.size == 0:
        return []

    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    hop = max(1, int(sample_rate * hop_ms / 1000))

    # 计算每帧 RMS 能量（dBFS）
    n_frames = max(0, (len(audio) - frame_len) // hop + 1)
    if n_frames <= 1:
        return []

    # O(N) 累计平方和差分：等价于「滑动窗口 + 每帧均方」，但不展开 (n_frames, frame_len)
    # 的临时矩阵——20 分钟音频滑窗版会分配数 GB 级 float64 中间量。
    sq = np.square(audio, dtype=np.float64)
    cs = np.concatenate((np.zeros(1, dtype=np.float64), np.cumsum(sq)))
    starts = np.arange(n_frames, dtype=np.int64) * hop
    energy = (cs[starts + frame_len] - cs[starts]) / frame_len
    rms = np.sqrt(np.maximum(energy, 0.0))
    # 避免 log(0)
    rms = np.maximum(rms, 1e-10)
    db = 20.0 * np.log10(rms)  # dBFS 相对满刻度 (rms=1 → 0 dBFS)

    # threshold_linear→(log10 还原) 往返换算数学上恰等于 threshold_db 本身，直接比较即可。
    is_silence = db < threshold_db

    # 找连续静音段
    points: list[float] = []
    in_sil = False
    start = 0
    min_frames = max(1, int(min_silence_sec * sample_rate / hop))

    for i, s in enumerate(is_silence):
        if s and not in_sil:
            start = i
            in_sil = True
        elif not s and in_sil:
            dur_frames = i - start
            if dur_frames >= min_frames:
                mid_frame = (start + i) // 2
                t_sec = mid_frame * hop / sample_rate
                points.append(t_sec)
            in_sil = False
    # 文件末尾静音（也可以作为切分点）
    if in_sil:
        dur_frames = n_frames - start
        if dur_frames >= min_frames:
            mid_frame = (start + n_frames) // 2
            t_sec = mid_frame * hop / sample_rate
            points.append(t_sec)

    points.sort()
    return points


# ─────────────────────────────────────────────────────────────
# 长音频分块（结合静音点）
# ─────────────────────────────────────────────────────────────
@dataclass
class SplitPlan:
    """长音频的切分计划。"""
    total_duration: float
    chunk_ranges: list[tuple[float, float]]   # [(start, end), ...] 秒
    overlap_sec: float                         # 相邻块重叠时长


def build_split_plan(
    total_duration: float,
    silence_points: list[float],
    *,
    max_duration: float,
    min_duration: float = 10.0,
    overlap_sec: float = 0.5,
) -> SplitPlan:
    """根据静音点和最大时长，生成分块计划。

    策略：
    - 单段 < max_duration → 不分块
    - 超过 max_duration → 在最近的静音点切开；没有静音点则在 max_duration 处强切
    - 相邻块之间留 overlap_sec 重叠，避免边界截断

    Args:
        total_duration: 音频总时长（秒）
        silence_points: 建议切分静音点列表（秒，升序）
        max_duration: 单块最大时长（秒），ASR 用 1200，Aligner 用 300
        min_duration: 单块最小时长（避免产生太短的块）
        overlap_sec: 相邻块重叠秒数

    Returns:
        SplitPlan，包含每个块的 [start, end) 时间范围
    """
    # 参数校验：错误参数会让下方 while 循环不前进（overlap ≥ max_duration 时死循环）
    if max_duration <= 0:
        raise ValueError(f"max_duration 必须 > 0，实际 {max_duration}")
    if total_duration < 0:
        raise ValueError(f"total_duration 不能为负，实际 {total_duration}")
    if min_duration < 0:
        raise ValueError(f"min_duration 不能为负，实际 {min_duration}")
    if overlap_sec < 0:
        raise ValueError(f"overlap_sec 不能为负，实际 {overlap_sec}")
    if overlap_sec >= max_duration:
        raise ValueError(
            f"overlap_sec({overlap_sec}) 必须 < max_duration({max_duration})，否则分块不前进"
        )

    if total_duration <= max_duration:
        return SplitPlan(
            total_duration=total_duration,
            chunk_ranges=[(0.0, total_duration)],
            overlap_sec=0.0,
        )

    # 在所有候选切分点中，每块长度尽量贴近 max_duration，且不超过
    ranges: list[tuple[float, float]] = []
    cur_start = 0.0

    while cur_start < total_duration - 1e-6:
        # 期望结束点
        ideal_end = min(cur_start + max_duration, total_duration)

        # 在 [cur_start + min_duration, ideal_end] 之间找最近的静音点
        best_cut: Optional[float] = None
        lo = cur_start + min_duration
        hi = ideal_end
        for sp in silence_points:
            if lo <= sp <= hi:
                best_cut = sp  # 最后一个不超过 ideal_end 的静音点
                # 继续往后找可能更大的点
            elif sp > hi:
                break

        cut = best_cut if best_cut is not None else ideal_end
        # 最后一块直接到文件尾
        if cut + min_duration >= total_duration:
            cut = total_duration

        ranges.append((cur_start, cut))
        # 下一块 start，减去 overlap；但避免 < 0
        cur_start = max(0.0, cut - overlap_sec)
        if cut >= total_duration - 1e-6:
            break

    # 修正最后一块的 end 为总时长
    if ranges:
        s, _ = ranges[-1]
        ranges[-1] = (s, total_duration)

    return SplitPlan(
        total_duration=total_duration,
        chunk_ranges=ranges,
        overlap_sec=overlap_sec,
    )


# ─────────────────────────────────────────────────────────────
# 一次性便捷函数：输入媒体 → 提取 16kHz mono WAV + 信息
# ─────────────────────────────────────────────────────────────
def prepare_audio(
    input_media: str | Path,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    force: bool = False,
) -> tuple[Path, AudioInfo]:
    """从任意媒体文件提取 16kHz mono WAV，返回 (wav_path, info)。

    若输入已是 wav 且采样率/声道匹配，则直接复用。
    提取产物使用确定性缓存名（媒体内容指纹 + 目标参数），
    同一媒体重复提取直接命中复用；缓存跨会话保留，
    生命周期由 core.temp_cleanup 的启动龄期清理管理。
    """
    in_path = Path(input_media)
    is_wav = in_path.suffix.lower() in (".wav",)
    if is_wav and not force:
        try:
            info = get_audio_info(in_path)
            if info.sample_rate == sample_rate and info.channels == 1:
                return in_path, info
        except Exception:
            logger.debug("[audio] WAV 复用检测失败，走提取流程")
    try:
        st = in_path.stat()
        # stem 截断：超长文件名会让缓存路径突破 Windows 单组件长度上限（255）。
        stem = in_path.stem[:80]
        cache_path = TEMP_DIR / f"{stem}_{st.st_size}_{st.st_mtime_ns}__sr{sample_rate}_ch1.wav"
    except OSError:
        cache_path = TEMP_DIR / f"{uuid.uuid4().hex}.wav"
    if cache_path.exists():
        if not force and cache_path.stat().st_size > 0:
            try:
                return cache_path, get_audio_info(cache_path)
            except Exception:  # noqa: BLE001 — 损坏缓存自动删除重建
                logger.warning("[audio] 提取缓存损坏，删除重建：%s", cache_path.name)
                cache_path.unlink(missing_ok=True)
        else:
            # ffmpeg 无 -y 参数遇已存在文件会失败，提取前自清脏缓存/force 重提
            try:
                cache_path.unlink()
            except OSError:
                cache_path = TEMP_DIR / f"{uuid.uuid4().hex}.wav"
    out = extract_audio(in_path, output_path=cache_path,
                        sample_rate=sample_rate, channels=1)
    return out, get_audio_info(out)
