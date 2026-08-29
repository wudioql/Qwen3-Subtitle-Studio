"""tests/test_media_worker.py — 媒体准备移出主线程（prepare_media_sync / MediaPrepWorker，纯逻辑 + 函数内懒 Qt）。

覆盖：
- workers.media_worker.prepare_media_sync（probe/提取/人声分离/回退/取消）；
- workers.media_worker.MediaPrepWorker（prepared / vocal_fallback / cancelled 信号）；
- core.vocal_separator 的合作式取消（extract_vocals_to_wav 入口 / separate 安全点）。

不依赖模型权重 / GPU / FFmpeg / 显示。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.logic


class _Info:
    def __init__(self, duration=5.0):
        self.duration = duration


# ══════════════════════════════════════════════════════════════
# 1. prepare_media_sync
# ══════════════════════════════════════════════════════════════

def _case_prepare_media_sync_native_readable_no_extract(monkeypatch):
    import workers.media_worker as mw

    info = _Info()
    monkeypatch.setattr(mw.audio_io, "probe_native_audio", lambda p: info)
    prepared = {}
    monkeypatch.setattr(
        mw.audio_io, "prepare_audio",
        lambda p, sample_rate: prepared.setdefault("called", True) or ("x.wav", info),
    )
    ap, got_info, ve, fb = mw.prepare_media_sync("a.wav")
    assert ap is None and got_info is info and ve is False and fb == ""
    assert "called" not in prepared            # 原生可直读 → 不提取


def _case_prepare_media_sync_extract_when_not_native(monkeypatch):
    import workers.media_worker as mw

    info = _Info()
    monkeypatch.setattr(mw.audio_io, "probe_native_audio", lambda p: None)
    monkeypatch.setattr(mw.audio_io, "prepare_audio", lambda p, sample_rate: ("x.wav", info))
    ap, got_info, ve, fb = mw.prepare_media_sync("a.mp4")
    assert ap == "x.wav" and got_info is info and ve is False and fb == ""


def _case_prepare_media_sync_extract_failure_nonfatal(monkeypatch):
    import workers.media_worker as mw

    monkeypatch.setattr(mw.audio_io, "probe_native_audio", lambda p: None)

    def boom(p, sample_rate):
        raise RuntimeError("no ffmpeg")

    monkeypatch.setattr(mw.audio_io, "prepare_audio", boom)
    ap, info, ve, fb = mw.prepare_media_sync("a.mp4")
    assert ap is None and info is None and ve is False and fb == ""


def _case_prepare_media_sync_vocals_pack(monkeypatch):
    """人声分离成功 → vocals.wav；失败 → 回退原音频 + 携带错误信息。"""
    import workers.media_worker as mw

    info = _Info()

    # 成功
    monkeypatch.setattr(mw.audio_io, "probe_native_audio", lambda p: info)
    monkeypatch.setattr(mw, "extract_vocals_to_wav", lambda p, **kw: "vocals.wav")
    ap, got_info, ve, fb = mw.prepare_media_sync("a.wav", do_extract_vocals=True)
    assert ap == "vocals.wav" and got_info is info and ve is True and fb == ""

    # 失败：非致命，回退并透传错误信息
    def boom(p, **kw):
        raise RuntimeError("onnx missing")

    monkeypatch.setattr(mw, "extract_vocals_to_wav", boom)
    ap2, got_info2, ve2, fb2 = mw.prepare_media_sync("a.wav", do_extract_vocals=True)
    assert ap2 is None and got_info2 is info and ve2 is False
    assert "onnx missing" in fb2


def _case_prepare_media_sync_cancel_raises(monkeypatch):
    import workers.media_worker as mw
    from core.task_control import TaskCancelled

    info = _Info()
    monkeypatch.setattr(mw.audio_io, "probe_native_audio", lambda p: info)
    with pytest.raises(TaskCancelled):
        mw.prepare_media_sync("a.wav", do_extract_vocals=True, cancel_cb=lambda: True)


# ══════════════════════════════════════════════════════════════
# 2. MediaPrepWorker 信号流（直接调 run()，信号同线程直连）
# ══════════════════════════════════════════════════════════════

def _case_media_prep_worker_emits_prepared(monkeypatch):
    pytest.importorskip("PySide6", exc_type=ImportError)   # MediaPrepWorker 是 QThread 包装，无 Qt 跳过
    from workers.media_worker import MediaPrepWorker

    results = []
    worker = MediaPrepWorker("a.mp4", do_extract_vocals=False)
    worker.prepared.connect(lambda ap, info, ve: results.append((ap, info, ve)))
    monkeypatch.setattr(
        "workers.media_worker.prepare_media_sync",
        lambda path, **kw: ("x.wav", "INFO", False, ""),
    )
    worker.run()
    assert results == [("x.wav", "INFO", False)]


def _case_media_prep_worker_emits_vocal_fallback(monkeypatch):
    pytest.importorskip("PySide6", exc_type=ImportError)
    from workers.media_worker import MediaPrepWorker

    fallbacks = []
    worker = MediaPrepWorker("a.mp4", do_extract_vocals=True)
    worker.vocal_fallback.connect(fallbacks.append)
    monkeypatch.setattr(
        "workers.media_worker.prepare_media_sync",
        lambda path, **kw: (None, None, False, "ModelError: x"),
    )
    worker.run()
    assert fallbacks == ["ModelError: x"]


def _case_media_prep_worker_emits_cancelled(monkeypatch):
    pytest.importorskip("PySide6", exc_type=ImportError)
    from core.task_control import TaskCancelled
    from workers.media_worker import MediaPrepWorker

    cancelled = []
    worker = MediaPrepWorker("a.mp4", do_extract_vocals=False)
    worker.cancelled.connect(lambda: cancelled.append(True))

    def raise_cancel(path, **kw):
        raise TaskCancelled()

    monkeypatch.setattr("workers.media_worker.prepare_media_sync", raise_cancel)
    worker.run()
    assert cancelled == [True]


# ══════════════════════════════════════════════════════════════
# 3. vocal_separator 合作式取消
# ══════════════════════════════════════════════════════════════

def test_vocal_separator_cancel_safety():
    import numpy as np

    import core.vocal_separator as vs
    from core.task_control import TaskCancelled

    # 入口取消
    with pytest.raises(TaskCancelled):
        vs.extract_vocals_to_wav("fake.mp4", cancel_cb=lambda: True)

    # separate 安全点取消
    sep = vs.VocalSeparator(model_path="fake.onnx")
    audio = np.zeros((2, 44100), dtype=np.float32)
    with pytest.raises(TaskCancelled):
        sep.separate(audio, input_sr=44100, target_sr=16000, cancel_cb=lambda: True)


def test_prepare_media_sync_pack(monkeypatch):
    """test_prepare_media_sync_pack：合并 5 个场景（断言逐条保留，见各 _case_*）。"""
    _case_prepare_media_sync_native_readable_no_extract(monkeypatch=monkeypatch)
    _case_prepare_media_sync_extract_when_not_native(monkeypatch=monkeypatch)
    _case_prepare_media_sync_extract_failure_nonfatal(monkeypatch=monkeypatch)
    _case_prepare_media_sync_vocals_pack(monkeypatch=monkeypatch)
    _case_prepare_media_sync_cancel_raises(monkeypatch=monkeypatch)


def test_media_worker_signals_pack(monkeypatch):
    """test_media_worker_signals_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_media_prep_worker_emits_prepared(monkeypatch=monkeypatch)
    _case_media_prep_worker_emits_vocal_fallback(monkeypatch=monkeypatch)
    _case_media_prep_worker_emits_cancelled(monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
