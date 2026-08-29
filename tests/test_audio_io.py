"""tests/test_audio_io.py — core.audio_io 音频准备与提取边界（纯逻辑，零 Qt）。

覆盖：
- extract_audio：end_sec 单独给出时生效（-t 而非 -ss 被忽略）；
- build_split_plan 参数校验（防死循环组合）；
- prepare_audio：超长 stem 截断、损坏缓存重建、确定性缓存复用 / force 重提 / 16k-mono 短路；
- detect_silence_points O(N) 实现与朴素滑窗语义等价；
- probe_native_audio 分流 + load_audio(target_sr) 内存重采样；
- asr_engine._prepare_asr_input：直读媒体零提取（numpy 直喂）、容器回退缓存名。
"""

from __future__ import annotations

from pathlib import Path

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import numpy as np
import pytest
import soundfile as sf

from core import audio_io

pytestmark = pytest.mark.logic


def _make_wav(path: Path, sr: int, channels: int, seconds: float = 0.5) -> None:
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    tone = 0.2 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    if channels == 2:
        tone = np.stack([tone, tone], axis=1)
    sf.write(str(path), tone, sr)


def _fake_extract_factory(counter: list[int]):
    """伪造 extract_audio：真的在 output_path 写一个 16k mono wav，并计数。"""
    def _fake(input_path, output_path=None, *, sample_rate=16000, channels=1,
              start_sec=None, end_sec=None):
        counter[0] += 1
        assert output_path is not None, "prepare_audio 应传确定性缓存路径"
        out = Path(output_path)
        _make_wav(out, 16000, 1, seconds=0.2)
        return out
    return _fake


# ══════════════════════════════════════════════════════════════
# 1. extract / split_plan / prepare_audio 边界
# ══════════════════════════════════════════════════════════════

def _case_extract_audio_end_without_start(tmp_path, monkeypatch):
    import subprocess

    inp = tmp_path / "in.mp4"
    inp.write_bytes(b"x")
    out = tmp_path / "out.wav"
    captured = {}

    def fake_run(args, check=False):
        captured["args"] = args
        out.write_bytes(b"RIFFxxxx")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(audio_io, "_run_ffmpeg", fake_run)
    audio_io.extract_audio(inp, out, start_sec=None, end_sec=5.0)
    args = captured["args"]
    assert "-t" in args and "5.000" in args
    assert "-ss" not in args          # 修复：end 单独给出时不再被忽略


def _case_build_split_plan_param_validation():
    from core.audio_io import build_split_plan

    with pytest.raises(ValueError):
        build_split_plan(100.0, [], max_duration=0)
    with pytest.raises(ValueError):
        build_split_plan(-1.0, [], max_duration=10)
    with pytest.raises(ValueError):
        build_split_plan(100.0, [], max_duration=10, overlap_sec=10)  # 会死循环的组合
    with pytest.raises(ValueError):
        build_split_plan(100.0, [], max_duration=10, min_duration=-1)
    plan = build_split_plan(100.0, [], max_duration=10, overlap_sec=1.5)
    assert plan.chunk_ranges and plan.total_duration == 100.0


def _case_prepare_audio_truncates_long_stem(tmp_path, monkeypatch):
    media = tmp_path / ("a" * 90 + ".mp4")   # 90 字符 stem：超 80 触发截断，且 < 文件名长度上限
    media.write_bytes(b"x")
    captured = {}

    def fake_extract(in_path, output_path=None, **kw):
        captured["out"] = output_path
        return output_path

    def fake_info(p):
        return audio_io.AudioInfo(p, 16000, 1, 1.0, 16000)

    monkeypatch.setattr(audio_io, "extract_audio", fake_extract)
    monkeypatch.setattr(audio_io, "get_audio_info", fake_info)
    audio_io.prepare_audio(media, sample_rate=16000)
    stem = captured["out"].name.split("_")[0]
    assert len(stem) <= 80            # 修复：超长 stem 不再突破 Windows 单组件长度


def _case_prepare_audio_rebuilds_corrupt_cache(tmp_path, monkeypatch):
    media = tmp_path / "song.mp4"
    media.write_bytes(b"x")
    st = media.stat()
    monkeypatch.setattr(audio_io, "TEMP_DIR", tmp_path / ".temp")
    audio_io.TEMP_DIR.mkdir(exist_ok=True)
    cache = audio_io.TEMP_DIR / f"song_{st.st_size}_{st.st_mtime_ns}__sr16000_ch1.wav"
    cache.write_bytes(b"not-a-wav")   # 损坏缓存

    states = {"extract_called": 0}

    def fake_extract(in_path, output_path=None, **kw):
        states["extract_called"] += 1
        cache.write_bytes(b"RIFFxxxx")
        return output_path

    def fake_info(p):
        if states["extract_called"] == 0:
            raise RuntimeError("corrupt")
        return audio_io.AudioInfo(p, 16000, 1, 1.0, 16000)

    monkeypatch.setattr(audio_io, "extract_audio", fake_extract)
    monkeypatch.setattr(audio_io, "get_audio_info", fake_info)
    audio_io.prepare_audio(media, sample_rate=16000)
    assert states["extract_called"] == 1
    assert cache.read_bytes() == b"RIFFxxxx"


def _case_detect_silence_points_matches_naive():
    from core.audio_io import detect_silence_points

    sr = 16000
    rng = np.random.default_rng(0)
    audio = rng.standard_normal(sr).astype(np.float32)
    audio[4000:9000] = 0.0            # 一段静音

    def naive(a, threshold_db=-30.0, min_silence_sec=0.2, frame_ms=25, hop_ms=10):
        frame_len = max(1, int(sr * frame_ms / 1000))
        hop = max(1, int(sr * hop_ms / 1000))
        n = max(0, (len(a) - frame_len) // hop + 1)
        if n <= 1:
            return []
        rms = [np.sqrt(np.mean(a[i * hop:i * hop + frame_len].astype(np.float64) ** 2))
               for i in range(n)]
        rms = np.maximum(np.asarray(rms), 1e-10)
        db = 20.0 * np.log10(rms)
        is_sil = db < threshold_db
        points, in_sil, start = [], False, 0
        min_frames = max(1, int(min_silence_sec * sr / hop))
        for i, s in enumerate(is_sil):
            if s and not in_sil:
                start, in_sil = i, True
            elif not s and in_sil:
                if i - start >= min_frames:
                    points.append((start + i) // 2 * hop / sr)
                in_sil = False
        if in_sil and n - start >= min_frames:
            points.append((start + n) // 2 * hop / sr)
        return sorted(points)

    got = detect_silence_points(audio, sr, threshold_db=-30.0, min_silence_sec=0.2)
    expected = naive(audio)
    assert got == pytest.approx(expected, abs=1e-9)   # O(N) 实现与朴素滑窗语义一致
    assert all(4000 / sr <= t <= 9000 / sr for t in got)


# ══════════════════════════════════════════════════════════════
# 2. WAV 零复制管线 / 重采样 / ASR 输入
# ══════════════════════════════════════════════════════════════

def _case_probe_and_in_memory_resample(tmp_path):
    # probe_native_audio：wav 直读 → AudioInfo；容器 → None
    wav = tmp_path / "a.wav"
    _make_wav(wav, 48000, 2)
    info = audio_io.probe_native_audio(wav)
    assert info is not None and info.sample_rate == 48000 and info.channels == 2
    assert abs(info.duration - 0.5) < 0.01
    fake_mp4 = tmp_path / "clip.mp4"
    fake_mp4.write_bytes(b"not a real media container")
    assert audio_io.probe_native_audio(fake_mp4) is None

    # load_audio(target_sr=…) 内存重采样；缺省不重采样（历史行为）
    data, sr = audio_io.load_audio(wav)
    assert sr == 48000
    data2, sr2 = audio_io.load_audio(wav, target_sr=16000)
    assert sr2 == 16000
    assert 0.45 * 16000 <= len(data2) <= 0.55 * 16000, f"重采样帧数不符: {len(data2)}"


def _case_prepare_audio_cache_force_and_shortcircuit(tmp_path, monkeypatch):
    wav = tmp_path / "voice.wav"
    _make_wav(wav, 48000, 2)
    calls = [0]
    monkeypatch.setattr(audio_io, "extract_audio", _fake_extract_factory(calls))

    # 确定性缓存名 + 同媒体重复提取命中复用
    p1, i1 = audio_io.prepare_audio(wav)
    p2, i2 = audio_io.prepare_audio(wav)
    assert p1 == p2, "同一媒体应命中同一缓存路径"
    assert calls[0] == 1, f"重复提取未被缓存复用（调用了 {calls[0]} 次）"
    assert p1.name.startswith("voice_") and "__sr16000_ch1.wav" in p1.name
    assert i1.sample_rate == 16000 and i2.sample_rate == 16000

    # force=True 强制重提
    audio_io.prepare_audio(wav, force=True)
    assert calls[0] == 2, f"force=True 应重新提取: {calls[0]}"

    # 16k mono wav 输入直用原文件、零提取
    ready = tmp_path / "ready.wav"
    _make_wav(ready, 16000, 1)

    def _boom(*a, **k):
        raise AssertionError("16k mono wav 不应触发提取")
    monkeypatch.setattr(audio_io, "extract_audio", _boom)
    out, info2 = audio_io.prepare_audio(ready)
    assert out == ready and info2.sample_rate == 16000


def _case_prepare_asr_input_zero_copy_and_fallback(tmp_path, monkeypatch):
    from core import asr_engine

    # 直读媒体：零提取，numpy 直喂
    wav = tmp_path / "talk44k.wav"
    _make_wav(wav, 44100, 2, seconds=0.5)

    def _boom(*a, **k):
        raise AssertionError("直读媒体不应发生 FFmpeg 提取")
    monkeypatch.setattr(audio_io, "extract_audio", _boom)

    audio_np, wav_path, total_sec = asr_engine._prepare_asr_input(wav)
    assert audio_np is not None and audio_np.ndim == 1
    assert 0.45 * 16000 <= len(audio_np) <= 0.55 * 16000, f"内存重采样帧数: {len(audio_np)}"
    assert wav_path == wav, "直读媒体的 project.audio_path 应记录原文件"
    assert abs(total_sec - 0.5) < 0.01

    # 容器类：回退 FFmpeg 缓存提取
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake container bytes")
    calls = [0]
    monkeypatch.setattr(audio_io, "extract_audio", _fake_extract_factory(calls))
    audio_np2, wav_path2, total_sec2 = asr_engine._prepare_asr_input(clip)
    assert audio_np2 is None, "容器类媒体仍需提取 16k 副本"
    assert calls[0] == 1
    assert "__sr16000_ch1.wav" in wav_path2.name
    assert abs(total_sec2 - 0.2) < 0.01


def test_audio_io_prepare_pack(tmp_path, monkeypatch):
    """test_audio_io_prepare_pack：合并 5 个场景（断言逐条保留，见各 _case_*）。"""
    _case_extract_audio_end_without_start(tmp_path=tmp_path, monkeypatch=monkeypatch)
    _case_build_split_plan_param_validation()
    _case_prepare_audio_truncates_long_stem(tmp_path=tmp_path, monkeypatch=monkeypatch)
    _case_prepare_audio_rebuilds_corrupt_cache(tmp_path=tmp_path, monkeypatch=monkeypatch)
    _case_detect_silence_points_matches_naive()


def test_audio_io_wav_passthrough_pack(tmp_path, monkeypatch):
    """test_audio_io_wav_passthrough_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_probe_and_in_memory_resample(tmp_path=tmp_path)
    _case_prepare_audio_cache_force_and_shortcircuit(tmp_path=tmp_path, monkeypatch=monkeypatch)
    _case_prepare_asr_input_zero_copy_and_fallback(tmp_path=tmp_path, monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
