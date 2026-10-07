"""tests/test_vocal_track_guard.py — 「当前音频轨是否已是人声分离产物」判据（纯逻辑）

回归自 2026-10-07 实测缺陷：`ui.project_controller._has_vocal_audio` 原判据是
`bool(audio_path) and audio_path != source_media_path`，而 `audio_path` 的语义是
「已提取的 16kHz mono WAV」（`subs/models.py`）——它既可能是 FFmpeg 降采样提取件，
也可能是人声分离产物，**路径不相等这一个条件分不出是哪一种**。于是容器媒体
（mp4/mkv/mov/avi/webm/ts/flv/wmv/m4a/aac）一导入就中招：提取件 ≠ 源媒体 →
被误判成「已用人声轨」→ 点「人声提取」直接弹「无需重复提取」。原生可直读的
mp3/wav/flac 因为不产生提取件（`audio_path` 保持等于源路径）反而正常，这正是
「不是所有媒体都报错」的原因。

判据必须落在人声缓存的**命名合同**上（`core.vocal_separator.vocals_cache_path`）。
本文件锁定三种状态必须可区分：

  ① 原生直读（``audio_path == source_media_path``）                  → 不是人声
  ② FFmpeg 提取件（``.temp/{stem}_{size}_{mtime}__sr16000_ch1.wav``）→ 不是人声
  ③ 人声缓存（``.temp/vocals_{stem}_{size}_{mtime}.wav`` 且文件存在）→ 是人声

不依赖模型权重 / GPU / FFmpeg / 显示。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401

import pytest

from subs.models import SubtitleProject

pytestmark = pytest.mark.logic


def _media(tmp_path, name: str = "test-short-talk.mp4"):
    """造一个「存在且有内容」的假媒体（只需可 stat，不真解码）。"""
    p = tmp_path / name
    p.write_bytes(b"not-a-real-media" * 8)
    return p


def _extract_artifact(media, tmp_path, sr: int = 16000):
    """按 `core.audio_io.prepare_audio` 的命名合同造提取件路径（不真跑 FFmpeg）。"""
    st = media.stat()
    return tmp_path / f"{media.stem}_{st.st_size}_{st.st_mtime_ns}__sr{sr}_ch1.wav"


def _make_vocals(media, tmp_path, monkeypatch):
    """按命名合同造出**真实存在**的人声缓存文件，返回其路径。"""
    import core.vocal_separator as vs

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)
    target = vs.vocals_cache_path(media)
    target.write_bytes(b"RIFF-fake-vocals")
    return target


# ═════════════ 判据本身 ═════════════

def test_native_media_using_source_path_is_not_vocals(tmp_path, monkeypatch):
    """原生直读媒体：audio_path 就是源文件本身 → 不是人声轨。"""
    import core.vocal_separator as vs

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)
    media = _media(tmp_path, "song.mp3")
    assert vs.is_vocals_track_of(media, media) is False


def test_ffmpeg_extract_artifact_is_not_mistaken_for_vocals(tmp_path, monkeypatch):
    """**核心回归**：FFmpeg 提取件曾被当成「已提取人声」（任何视频一导入即中招）。"""
    import core.vocal_separator as vs

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)
    media = _media(tmp_path)                       # test-short-talk.mp4
    extract = _extract_artifact(media, tmp_path)
    extract.write_bytes(b"RIFF-fake-extract")      # 提取件真实存在

    assert extract != media                        # 旧判据唯一依据：路径不相等
    assert vs.is_vocals_track_of(media, extract) is False


def test_real_vocals_product_is_detected(tmp_path, monkeypatch):
    """真人声分离产物 → 判为已提取（保留「无需重复提取」的提示）。"""
    import core.vocal_separator as vs

    media = _media(tmp_path)
    vocal = _make_vocals(media, tmp_path, monkeypatch)
    assert vs.is_vocals_track_of(media, vocal) is True


def test_deleted_vocals_cache_does_not_block_reextract(tmp_path, monkeypatch):
    """人声缓存已被龄期清理 → 不拦（宁可重跑，不可误拦）。"""
    import core.vocal_separator as vs

    media = _media(tmp_path)
    vocal = _make_vocals(media, tmp_path, monkeypatch)
    vocal.unlink()
    assert vs.is_vocals_track_of(media, vocal) is False


def test_missing_source_media_does_not_block(tmp_path, monkeypatch):
    """源媒体不存在（stat 失败）→ 不抛异常、也不拦。"""
    import core.vocal_separator as vs

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)
    media = _media(tmp_path)
    vocal = vs.vocals_cache_path(media)
    vocal.write_bytes(b"RIFF-fake-vocals")
    media.unlink()

    assert vs.is_vocals_track_of(media, vocal) is False
    assert vs.is_vocals_track_of(None, vocal) is False
    assert vs.is_vocals_track_of(media, "") is False


def test_extract_artifact_never_collides_with_vocals_name(tmp_path, monkeypatch):
    """源媒体自己叫 `vocals_*` 时也不得串味：两条命名合同互不重叠。

    提取件名尾是 `__sr16000_ch1.wav`，人声件名尾是 `_{size}_{mtime}.wav`；
    判据用的是**精确路径相等**，因此不存在前缀/正则层面的误认。
    """
    import core.vocal_separator as vs

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)
    media = _media(tmp_path, "vocals_song.mp4")
    extract = _extract_artifact(media, tmp_path)
    extract.write_bytes(b"RIFF-fake-extract")
    vocal = vs.vocals_cache_path(media)

    assert vocal.name.startswith("vocals_") and extract.name.startswith("vocals_")
    assert vocal != extract
    assert vs.is_vocals_track_of(media, extract) is False
    assert vs.is_vocals_track_of(media, vocal) is False  # 人声件此时尚未落盘


# ═════════════ UI 守卫（用户可见症状）═════════════

def test_project_controller_guard_uses_cache_contract(tmp_path, monkeypatch):
    """`ProjectController._has_vocal_audio` 必须跟着命名合同走，而不是路径不等式。

    这是用户实测症状的直接护栏：导入 mp4（走 FFmpeg 提取）后，守卫**不得**返回
    True；只有真人声产物才返回 True。
    """
    import core.vocal_separator as vs
    from ui.project_controller import ProjectController

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)
    media = _media(tmp_path)

    extract = _extract_artifact(media, tmp_path)
    extract.write_bytes(b"RIFF-fake-extract")
    p_after_import = SubtitleProject(
        source_media_path=str(media), audio_path=str(extract),
    )
    assert ProjectController._has_vocal_audio(p_after_import) is False

    vocal = _make_vocals(media, tmp_path, monkeypatch)
    p_after_vocals = SubtitleProject(
        source_media_path=str(media), audio_path=str(vocal),
    )
    assert ProjectController._has_vocal_audio(p_after_vocals) is True

    p_native = SubtitleProject(
        source_media_path=str(media), audio_path=str(media),
    )
    assert ProjectController._has_vocal_audio(p_native) is False


# ═════════════ 命名合同的跨消费者一致性 ═════════════

def test_cache_naming_contract_agrees_across_consumers(tmp_path, monkeypatch):
    """`.temp` 清理与 UI 守卫必须认同同一个命名合同（constants 单一真源）。"""
    import core.temp_cleanup as tc
    import core.vocal_separator as vs
    from core.constants import VOCALS_CACHE_PREFIX

    monkeypatch.setattr(vs, "TEMP_DIR", tmp_path)
    media = _media(tmp_path)
    vocal = vs.vocals_cache_path(media)
    extract = _extract_artifact(media, tmp_path)

    assert vocal.name.startswith(VOCALS_CACHE_PREFIX)
    # 两者都是「确定性缓存」→ 跨会话保留（仅按龄期清理），不得被当一次性文件删掉
    assert tc._is_cache_wav(vocal.name) is True
    assert tc._is_cache_wav(extract.name) is True
    assert tc._is_disposable_wav(vocal.name) is False
