"""core.cloud_asr.encoding —— 上传前编码：numpy→WAV、FFmpeg→OPUS、容器时长探测。

默认走 OPUS 32kbps：实测与原始 WAV **识别结果逐字一致**而体积小 9 倍；MP3 反而引入
额外错字。编码是整条链路里**唯一能给真实百分比**的阶段（按 FFmpeg ``out_time`` 上报），
之后的云端排队与推理一律 ``total=0`` 不确定进度（见 AGENTS.md §3）。
"""


from __future__ import annotations


import io
import logging
import subprocess
import uuid
import wave
from pathlib import Path
from typing import Any

from ..constants import DEFAULT_SAMPLE_RATE, ensure_temp_dir


from .constants import AudioInput, OPUS_BITRATE, ProgressCallback


from .errors import CloudASRParamError


from .types import UploadPayload


logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 编码：numpy / 媒体文件 → 上传负载
# ═══════════════════════════════════════════════════════════════

def wav_bytes_from_array(audio: Any, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    """把 numpy 数组转成 16kHz mono 16bit 的 WAV 字节（纯标准库，不落盘）。"""
    import numpy as np  # 懒加载：云端路径不需要 numpy 参与推理

    arr = np.asarray(audio)
    if arr.ndim > 1:  # (n, ch) → mono
        arr = arr.mean(axis=1)
    if np.issubdtype(arr.dtype, np.floating):
        pcm = np.clip(arr, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2")
    else:
        pcm = arr.astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _ffmpeg_transcode(
    src: Path,
    dst: Path,
    extra: list[str],
    ffmpeg_path: str = "",
    *,
    total_sec: float = 0.0,
    progress_cb: ProgressCallback = None,
) -> None:
    """调用 FFmpeg 把 src 转成 dst；失败抛 RuntimeError（不吞 ffmpeg 的错误输出）。

    ``total_sec > 0`` 且给了 ``progress_cb`` 时，按 FFmpeg 自己报告的 ``out_time``
    推进真实百分比。**FFmpeg 的 out_time 是实际已编码时长，可信**，所以这里
    报的是真进度而不是估算（对比：云端 HTTP 等待没有可信进度，仍走 total=0）。
    """
    from ..audio_io import ensure_ffmpeg  # 懒加载：避开 soundfile/numpy 的导入开销

    exe = ffmpeg_path or ensure_ffmpeg()
    want_progress = progress_cb is not None and total_sec > 0
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vn", "-ac", "1", "-ar", str(DEFAULT_SAMPLE_RATE), *extra]
    if want_progress:
        # -progress pipe:1 让 ffmpeg 把机器可读的进度行写到 stdout；
        # 不加 -nostats 时 stdout 只有进度，没有日志污染。
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd.append(str(dst))

    if not want_progress:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg 转码失败：{(proc.stderr or '').strip()[:300]}")
        return

    proc = subprocess.Popen(  # noqa: S603
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )
    stderr_chunks: list[str] = []
    last_pct = -1
    assert proc.stdout is not None
    for line in proc.stdout:                      # type: ignore[union-attr]
        key, _, val = line.strip().partition("=")
        if key != "out_time_us" and key != "out_time_ms":
            continue
        # 注意：ffmpeg 的 out_time_ms **单位其实是微秒**（历史遗留命名），
        # 两个键值相同；这里只取其一，避免重复上报。
        try:
            done_sec = int(val) / 1_000_000.0
        except ValueError:
            continue
        pct = min(100, int(done_sec / total_sec * 100))
        if pct > last_pct:
            last_pct = pct
            progress_cb(pct, 100, f"编码音频中…{pct}%")  # type: ignore[misc]
    proc.wait()
    err = (proc.stderr.read() if proc.stderr else "") or ""  # type: ignore[union-attr]
    stderr_chunks.append(err)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg 转码失败：{''.join(stderr_chunks).strip()[:300]}")


def _write_temp_wav(audio: Any, sample_rate: int) -> Path:
    """把内存数组落成临时 WAV 并返回路径（供 FFmpeg 二次编码）。"""
    tmp = ensure_temp_dir() / f"cloud_asr_{uuid.uuid4().hex}.wav"
    tmp.write_bytes(wav_bytes_from_array(audio, sample_rate))
    return tmp


def _media_duration_sec(src: Path) -> float:
    """用 FFmpeg 读媒体时长（秒）；读不到返回 0（调用方据此关闭百分比上报）。

    只需要时长，不需要精确值——它只用来把编码进度换算成百分比，
    所以解析 out_time 那行里的 ``Duration: HH:MM:SS.ss`` 足够。
    """
    from ..audio_io import ensure_ffmpeg

    try:
        proc = subprocess.run(  # noqa: S603
            [ensure_ffmpeg(), "-hide_banner", "-i", str(src)],
            check=False, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    for line in (proc.stderr or "").splitlines():
        _, _, val = line.strip().partition("Duration: ")
        if not val:
            continue
        # 形如 ``00:00:18.04, start: 0.000000, bitrate: 107 kb/s``——**三段**
        # （时/分/秒），秒段还带逗号后的尾巴，所以先按逗号切干净。
        stamp = val.split(",", 1)[0].strip()
        parts = stamp.split(":")
        if len(parts) != 3:
            return 0.0
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return 0.0
    return 0.0


def encode_for_upload(
    audio: AudioInput,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    codec: str = "opus",
    ffmpeg_path: str = "",
    progress_cb: ProgressCallback = None,
) -> UploadPayload:
    """把 numpy 数组或媒体文件编码成适合上传的负载。

    优先 OPUS（实测识别结果与 WAV 逐字一致，体积约为 1/9）；FFmpeg 不可用或转码
    失败时**静默降级**为 WAV（项目单次上限 1200s 的 WAV 约 36.6MB，仍在 50MB 内），
    因此不会因为缺 FFmpeg 就整条链路失败。
    """
    tmp_created: Path | None = None
    total_sec = 0.0
    if isinstance(audio, (str, Path)):
        src = Path(audio)
        if not src.exists():
            raise CloudASRParamError(f"音频文件不存在：{src}")
        array_input = False
        # 媒体文件：先探时长（~50ms），用于把编码进度换算成真实百分比。
        total_sec = _media_duration_sec(src)
    else:
        src = _write_temp_wav(audio, sample_rate)
        tmp_created = src
        array_input = True
        # 数组长度就是确切时长，无需探测。
        try:
            total_sec = len(audio) / float(sample_rate)
        except TypeError:
            total_sec = 0.0

    try:
        if codec == "opus":
            out = ensure_temp_dir() / f"cloud_asr_{uuid.uuid4().hex}.ogg"
            try:
                _ffmpeg_transcode(src, out, ["-c:a", "libopus", "-b:a", OPUS_BITRATE],
                                  ffmpeg_path, total_sec=total_sec, progress_cb=progress_cb)
                return UploadPayload(out.read_bytes(), "audio.ogg", "audio/ogg", "opus")
            except Exception as exc:  # noqa: BLE001 - FFmpeg 缺失/裁剪版不应炸掉整条链路
                logger.warning("[cloud-asr] OPUS 编码失败，降级为 WAV：%s", exc)
                if progress_cb:
                    progress_cb(0, 0, "OPUS 编码失败，改用 WAV 上传…")
            finally:
                out.unlink(missing_ok=True)

        # WAV 路径：内存数组直接走标准库，无需 FFmpeg
        if array_input:
            return UploadPayload(wav_bytes_from_array(audio, sample_rate),
                                 "audio.wav", "audio/wav", "wav")
        if src.suffix.lower() == ".wav":
            return UploadPayload(src.read_bytes(), "audio.wav", "audio/wav", "wav")
        out = ensure_temp_dir() / f"cloud_asr_{uuid.uuid4().hex}.wav"
        try:
            _ffmpeg_transcode(src, out, ["-c:a", "pcm_s16le"], ffmpeg_path)
            return UploadPayload(out.read_bytes(), "audio.wav", "audio/wav", "wav")
        finally:
            out.unlink(missing_ok=True)
    finally:
        if tmp_created is not None:
            tmp_created.unlink(missing_ok=True)
