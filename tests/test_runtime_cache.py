"""tests/test_runtime_cache.py — 运行时缓存行为钉样（纯逻辑，零 Qt）。

覆盖：
- core.audio_io.ensure_ffmpeg 冒烟探测缓存（命中 / override 变化失效 / 显式失效）；
- core.vocal_separator / core.mms_aligner 的 is_available() 结果缓存（重置后重探）；
- ui.ass_style_dialog 字体枚举缓存（mock QFontDatabase.families 计数，函数内懒 import）。

（偏好缓存 → tests/test_app_config.py；FA2 回退 → tests/test_model_manager.py）

不依赖模型权重 / GPU / FFmpeg / 显示。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.logic


# ══════════════════════════════════════════════════════════════
# 1. ensure_ffmpeg 冒烟探测缓存
# ══════════════════════════════════════════════════════════════

def _case_ffmpeg_smoke_cache_hit_and_invalidate(monkeypatch):
    import core.audio_io as aio

    aio.invalidate_ffmpeg_cache()
    monkeypatch.setattr(aio, "_current_ffmpeg_override", lambda: "")
    monkeypatch.setattr(aio, "_iter_ffmpeg_candidates", lambda override="": ["fake_ffmpeg"])
    calls = {"n": 0}

    def fake_smoke(exe):
        calls["n"] += 1
        return True, "ok"

    monkeypatch.setattr(aio, "_ffmpeg_smoke_extract_ok", fake_smoke)
    assert aio.ensure_ffmpeg() == "fake_ffmpeg"
    assert calls["n"] == 1
    # 命中缓存：不重跑子进程探测
    assert aio.ensure_ffmpeg() == "fake_ffmpeg"
    assert calls["n"] == 1
    # 显式失效：重探一次
    aio.invalidate_ffmpeg_cache()
    assert aio.ensure_ffmpeg() == "fake_ffmpeg"
    assert calls["n"] == 2
    aio.invalidate_ffmpeg_cache()


def _case_ffmpeg_smoke_cache_invalidates_on_override_change(monkeypatch):
    import core.audio_io as aio

    aio.invalidate_ffmpeg_cache()
    state = {"override": ""}
    monkeypatch.setattr(aio, "_current_ffmpeg_override", lambda: state["override"])
    monkeypatch.setattr(aio, "_iter_ffmpeg_candidates", lambda override="": ["fake_ffmpeg"])
    calls = {"n": 0}

    def fake_smoke(exe):
        calls["n"] += 1
        return True, "ok"

    monkeypatch.setattr(aio, "_ffmpeg_smoke_extract_ok", fake_smoke)
    aio.ensure_ffmpeg()
    aio.ensure_ffmpeg()
    assert calls["n"] == 1
    # 偏好 ffmpeg_path 变化 → 自动失效重探
    state["override"] = "D:/tools/ffmpeg.exe"
    aio.ensure_ffmpeg()
    assert calls["n"] == 2
    aio.invalidate_ffmpeg_cache()


# ══════════════════════════════════════════════════════════════
# 2. is_available() 结果缓存
# ══════════════════════════════════════════════════════════════

def _case_vocal_separator_available_cached(tmp_path):
    from core.vocal_separator import VocalSeparator

    model_file = tmp_path / "m.onnx"
    model_file.write_bytes(b"x")
    sep = VocalSeparator(model_path=model_file)
    assert sep.is_available() is True
    model_file.unlink()
    assert sep.is_available() is True   # 缓存命中
    sep._available = None               # 重置后重探
    assert sep.is_available() is False


def _case_mms_aligner_available_cached(tmp_path):
    from core.mms_aligner import MMSAligner

    model_dir = tmp_path / "mms"
    model_dir.mkdir()
    (model_dir / "model.onnx").write_bytes(b"x")
    m = MMSAligner(model_dir=model_dir)
    assert m.is_available() is True
    (model_dir / "model.onnx").unlink()
    assert m.is_available() is True     # 缓存命中
    m._available = None                 # 重置后重探
    assert m.is_available() is False


# ══════════════════════════════════════════════════════════════
# 3. 字体枚举缓存
# ══════════════════════════════════════════════════════════════

def _case_font_families_enumerated_once(monkeypatch):
    import ui.ass_style_dialog.dialog as dlg

    monkeypatch.setattr(dlg, "_FONT_FAMILIES_CACHE", None)
    calls = {"n": 0}

    def fake_families():
        calls["n"] += 1
        return [".hidden", "Arial", "SimSun"]

    monkeypatch.setattr("PySide6.QtGui.QFontDatabase.families", fake_families)
    r1 = dlg._system_font_families()
    r2 = dlg._system_font_families()
    assert calls["n"] == 1               # 只枚举一次
    assert r1 == r2 == ["Arial", "SimSun"]


def test_ffmpeg_cache_pack(monkeypatch):
    """test_ffmpeg_cache_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_ffmpeg_smoke_cache_hit_and_invalidate(monkeypatch=monkeypatch)
    _case_ffmpeg_smoke_cache_invalidates_on_override_change(monkeypatch=monkeypatch)


def test_availability_cache_pack(tmp_path, monkeypatch):
    """test_availability_cache_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_vocal_separator_available_cached(tmp_path=tmp_path)
    _case_mms_aligner_available_cached(tmp_path=tmp_path)
    _case_font_families_enumerated_once(monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
