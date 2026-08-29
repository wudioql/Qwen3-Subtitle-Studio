"""tests/test_temp_cleanup.py — .temp/ 清理策略契约（纯逻辑）

契约（与 core/audio_io.prepare_audio、core/vocal_separator 的缓存语义配套）：
1. 退出清理只删一次性文件（FFmpeg 探针 / uuid 兜底 WAV），
   确定性缓存 WAV（提取件 __sr*_ch1 / 人声件 vocals_*）跨会话保留；
2. 启动清理删除一切残留一次性文件，缓存与未知 WAV 按龄期删除；
3. app.log 与锁目录、其他子目录（用户导出目录等）永不触碰。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

pytestmark = pytest.mark.logic

import core.temp_cleanup as tc  # noqa: E402


CACHE_EXTRACT = "Avatar_123456_789000__sr16000_ch1.wav"
CACHE_VOCALS = "vocals_song_999_888.wav"
UUID_WAV = "abcdef0123456789abcdef0123456789.wav"
PROBE_WAV = "_ffmpeg_probe_42_deadbeef.wav"
UNKNOWN_WAV = "legacy_leftover.wav"


def _make_temp_layout(root: Path) -> None:
    for name in (CACHE_EXTRACT, CACHE_VOCALS, UUID_WAV, PROBE_WAV, UNKNOWN_WAV):
        (root / name).write_bytes(b"RIFF")
    (root / "app.log").write_text("log\n", encoding="utf-8")
    lock_dir = root / "Qwen3SubtitleStudio"
    lock_dir.mkdir()
    (lock_dir / "app.lock").write_text("", encoding="utf-8")
    export_dir = root / "short-talk-exports"
    export_dir.mkdir()
    (export_dir / "out.srt").write_text("1\n", encoding="utf-8")


def _case_shutdown_cleanup_preserves_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "TEMP_DIR", tmp_path)
    _make_temp_layout(tmp_path)

    tc.shutdown_cleanup()

    # 一次性文件被删
    assert not (tmp_path / UUID_WAV).exists()
    assert not (tmp_path / PROBE_WAV).exists()
    # 缓存 / 未知 WAV / 日志 / 锁 / 导出目录全部保留
    assert (tmp_path / CACHE_EXTRACT).exists()
    assert (tmp_path / CACHE_VOCALS).exists()
    assert (tmp_path / UNKNOWN_WAV).exists()
    assert (tmp_path / "app.log").exists()
    assert (tmp_path / "Qwen3SubtitleStudio" / "app.lock").exists()
    assert (tmp_path / "short-talk-exports" / "out.srt").exists()


def _case_startup_cleanup_age_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "TEMP_DIR", tmp_path)
    _make_temp_layout(tmp_path)

    # 把提取缓存与未知 WAV 做旧到超龄；人声缓存保持新鲜
    stale = time.time() - (tc.TEMP_MAX_AGE_DAYS * 86400 + 3600)
    os.utime(tmp_path / CACHE_EXTRACT, (stale, stale))
    os.utime(tmp_path / UNKNOWN_WAV, (stale, stale))

    tc.startup_cleanup()

    # 一次性文件：无论新旧一律删（残留即孤儿）
    assert not (tmp_path / UUID_WAV).exists()
    assert not (tmp_path / PROBE_WAV).exists()
    # 超龄缓存 / 未知 WAV 被删；新鲜缓存保留
    assert not (tmp_path / CACHE_EXTRACT).exists()
    assert not (tmp_path / UNKNOWN_WAV).exists()
    assert (tmp_path / CACHE_VOCALS).exists()
    # 日志 / 锁 / 导出目录不动
    assert (tmp_path / "app.log").exists()
    assert (tmp_path / "Qwen3SubtitleStudio" / "app.lock").exists()
    assert (tmp_path / "short-talk-exports" / "out.srt").exists()


def _case_log_rotation_truncates_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(tc, "LOG_MAX_BYTES", 1000)
    log = tmp_path / "app.log"
    log.write_bytes(b"x" * 50 + b"\n" + b"line\n" * 400)  # > 1000 字节

    tc.startup_cleanup()

    data = log.read_bytes()
    assert data.startswith(b"... [log rotated] ...")
    assert len(data) < 1000


def test_temp_cleanup_pack(tmp_path, monkeypatch):
    """test_temp_cleanup_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    for name, case in (
        ("shutdown", _case_shutdown_cleanup_preserves_caches),
        ("startup", _case_startup_cleanup_age_policy),
        ("rotation", _case_log_rotation_truncates_tail),
    ):
        d = tmp_path / name
        d.mkdir()
        case(tmp_path=d, monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
