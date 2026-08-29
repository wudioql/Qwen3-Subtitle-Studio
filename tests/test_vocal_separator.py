"""tests.test_vocal_separator — 测试轻量级人声提取模块与 4 通道复数 STFT 契约

验证：
1. VocalSeparator.is_available 状态检查
2. 4 通道复数 STFT (L_real, L_imag, R_real, R_imag) 形状为 (1, 4, 3072, 256)
3. 无模型时的优雅自动回退（直接输出 16kHz 单声道音频，不崩溃）
4. extract_vocals_to_wav 音频保存与格式校验
5. ModelManager.using_vocal_separator 互斥上下文与自动释放
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

from core.vocal_separator import VocalSeparator, extract_vocals_to_wav
from core.model_manager import ModelManager
import pytest

pytestmark = pytest.mark.logic

def _case_fallback_and_extract():
    # ── vocal separator fallback ─────────────────────────
    sr = 44100
    duration = 2.0
    t = np.linspace(0, duration, int(sr * duration), dtype=np.float32)
    left = 0.5 * np.sin(2 * np.pi * 440 * t)
    right = 0.5 * np.cos(2 * np.pi * 440 * t)
    stereo_mix = np.stack([left, right], axis=0)

    fake_model_path = PROJECT_ROOT / "models" / "NonExistent_Model.onnx"
    sep = VocalSeparator(model_path=fake_model_path)
    assert sep.is_available() is False

    vocal_16k = sep.separate(stereo_mix, input_sr=44100, target_sr=16000)
    assert sep.last_run_separated is False
    assert vocal_16k.ndim == 1
    assert abs(len(vocal_16k) - int(16000 * duration)) < 100
    assert vocal_16k.dtype == np.float32
    # 同一 ndarray 以 (samples, channels) 输入也必须识别正确，不能沿 2 元声道轴重采样。
    vocal_n2 = sep.separate(stereo_mix.T, input_sr=44100, target_sr=16000)
    assert vocal_n2.ndim == 1 and abs(len(vocal_n2) - int(16000 * duration)) < 100

    # ── extract vocals to wav ─────────────────────────
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_wav = Path(tmp_dir) / "test_song.wav"
        output_wav = Path(tmp_dir) / "test_vocals_16k.wav"

        t = np.linspace(0, 2.0, 32000, dtype=np.float32)
        sf.write(str(input_wav), t, 16000)

        out_path = extract_vocals_to_wav(input_wav, output_wav)
        assert out_path.exists()

        data, sr = sf.read(str(out_path))
        assert sr == 16000
        assert data.ndim == 1
        print("test_extract_vocals_to_wav PASSED ✔")


def _case_vocals_cache_hit_skips_separation(monkeypatch, tmp_path):
    """人声分离缓存复用：同一媒体（内容指纹一致）第二次调用不再跑 ONNX。

    契约：
    1. 首次调用走 separate 并落盘确定性缓存名 vocals_{stem}_{size}_{mtime}.wav；
    2. 二次调用命中缓存 → separate 零调用，直接返回同一路径，进度回调收尾；
    3. 媒体内容变化（mtime/size 变）→ 缓存失效重新分离；
    4. 显式 output_path 时不走缓存逻辑（调用方自管）。
    """
    import core.vocal_separator as vs

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)

    media = tmp_path / "song.wav"
    t = np.linspace(0, 1.0, 16000, dtype=np.float32)
    sf.write(str(media), t, 16000)

    fake_sep = MagicMock()
    fake_sep.separate.return_value = np.zeros(16000, dtype=np.float32)
    fake_sep.last_run_separated = True
    monkeypatch.setattr(vs, "get_vocal_separator", lambda model_path=None: fake_sep)

    # 1. 首次：真跑分离
    out1 = vs.extract_vocals_to_wav(media, model_path="fake.onnx")
    assert out1.exists() and out1.name.startswith("vocals_")
    assert fake_sep.separate.call_count == 1

    # 2. 二次：命中缓存，分离零调用，路径一致，进度回调收尾
    stages = []
    out2 = vs.extract_vocals_to_wav(media, model_path="fake.onnx",
                                    progress_cb=lambda d, tot, desc: stages.append(desc))
    assert out2 == out1
    assert fake_sep.separate.call_count == 1, "缓存命中不得重跑分离"
    assert any("缓存" in s for s in stages)

    # 3. 损坏缓存不能只凭文件大小命中：删除并重新分离、同路径修复。
    out1.write_bytes(b"not-a-wave" * 10)
    repaired = vs.extract_vocals_to_wav(media, model_path="fake.onnx")
    assert repaired == out1
    assert fake_sep.separate.call_count == 2
    assert sf.info(str(repaired)).frames > 0

    # 4. 媒体内容变化 → 指纹变 → 重新分离
    import os as _os
    _os.utime(media, (media.stat().st_atime, media.stat().st_mtime + 10))
    out3 = vs.extract_vocals_to_wav(media, model_path="fake.onnx")
    assert out3 != out1
    assert fake_sep.separate.call_count == 3

    # 5. 显式 output_path：每次都跑（不走缓存）
    explicit = tmp_path / "explicit.wav"
    vs.extract_vocals_to_wav(media, explicit, model_path="fake.onnx")
    vs.extract_vocals_to_wav(media, explicit, model_path="fake.onnx")
    assert fake_sep.separate.call_count == 5
    print("test_vocals_cache_hit_skips_separation PASSED ✔")


def _case_failed_separation_never_poison_cache(monkeypatch, tmp_path):
    import core.vocal_separator as vs

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)
    media = tmp_path / "failure.wav"
    sf.write(str(media), np.zeros(16000, dtype=np.float32), 16000)

    fake_sep = MagicMock()
    fake_sep.separate.return_value = np.zeros(16000, dtype=np.float32)
    fake_sep.last_run_separated = False
    monkeypatch.setattr(vs, "get_vocal_separator", lambda model_path=None: fake_sep)

    with pytest.raises(RuntimeError, match="不会写入或复用人声缓存"):
        vs.extract_vocals_to_wav(
            media, model_path="fake.onnx", allow_fallback=False,
        )
    assert not list(tmp_path.glob("vocals_*.wav"))

    fallback = vs.extract_vocals_to_wav(
        media, model_path="fake.onnx", allow_fallback=True,
    )
    assert fallback.exists() and not fallback.name.startswith("vocals_")
    # 没有确定性缓存，下一次必须再次尝试分离。
    vs.extract_vocals_to_wav(media, model_path="fake.onnx", allow_fallback=True)
    assert fake_sep.separate.call_count == 3


def _case_mdx_memory_budget_guard():
    import core.vocal_separator as vs

    short_estimate = vs._estimate_mdx_working_set_bytes(44100 * 10)
    long_estimate = vs._estimate_mdx_working_set_bytes(44100 * 1200)
    assert short_estimate < vs._MDX_WORKING_SET_MAX_BYTES
    assert long_estimate > vs._MDX_WORKING_SET_MAX_BYTES

    sep = VocalSeparator(model_path="fake.onnx")
    fake_input = MagicMock()
    fake_input.name = "input"
    fake_input.shape = [1, 4, 3072, 256]
    fake_session = MagicMock()
    fake_session.get_inputs.return_value = [fake_input]
    sep._session = fake_session
    with patch.object(
        vs, "_estimate_mdx_working_set_bytes",
        return_value=vs._MDX_WORKING_SET_MAX_BYTES + 1,
    ), pytest.raises(MemoryError, match="超过安全预算"):
        sep._run_mdx_inference(np.zeros((2, 16000), dtype=np.float32))
    fake_session.get_inputs.assert_not_called()
    fake_session.run.assert_not_called()


def _case_context_and_multistage_progress():
    # ── model manager vocal context ─────────────────────────
    mm = ModelManager()
    with mm.using_vocal_separator() as sep:
        assert isinstance(sep, VocalSeparator)

    # ── vocal separator multistage progress ─────────────────────────
    """多段进度回调契约。"""
    sep = VocalSeparator()
    sep.is_available = MagicMock(return_value=False)

    progress_stages = []

    def _cb(done, total, desc):
        progress_stages.append((done, total, desc))

    mock_audio = np.random.randn(2, 44100).astype(np.float32)
    with patch("core.vocal_separator._load_44k_stereo_via_ffmpeg", return_value=mock_audio):
        res = sep.separate(mock_audio, input_sr=44100, target_sr=16000, progress_cb=_cb)
        assert res.size == 16000
        assert len(progress_stages) >= 2
        assert any("提取" in stage[2] for stage in progress_stages)


def test_vocal_cache_pack(monkeypatch, tmp_path):
    """test_vocal_cache_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_fallback_and_extract()
    d_cache = tmp_path / "cache"
    d_cache.mkdir()
    _case_vocals_cache_hit_skips_separation(monkeypatch=monkeypatch, tmp_path=d_cache)
    d_poison = tmp_path / "poison"
    d_poison.mkdir()
    _case_failed_separation_never_poison_cache(monkeypatch=monkeypatch, tmp_path=d_poison)


def test_vocal_guard_pack():
    """test_vocal_guard_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_mdx_memory_budget_guard()
    _case_context_and_multistage_progress()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
