"""tests/test_project_models.py — SubtitleProject 模型契约与工程序列化（纯逻辑，零 Qt）。

覆盖：
- Sentence 脏/锁标记（dirty_indices / alignable_dirty_indices / toggle_lock）；
- 工程 JSON roundtrip（媒体路径 + 字级 + 脏标记 + schema_version=1）；
- 打开校验（schema 版本 / sid 唯一 / 有限数字 / 句内字时间单调 / 布尔规整 / 大小上限）
  与原子保存（崩溃不破坏原文件）；
- 媒体路径相对化优先解析（工程目录内转相对 + media_path_hints 兜底；跨 OS 绝对路径
  —— Windows 盘符/POSIX 根——原样保留；相对路径统一正斜杠；畸形 hints 忽略）；
- 跨机复现三件套（ASS 样式 / 卡拉OK模板 / 导出设置）随工程往返，schema_version 不 bump；
- subs.atomic_io 原子写盘（成功 / 失败不破坏原文件）。
"""

from __future__ import annotations

from pathlib import Path

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

from subs.models import Sentence, SubtitleProject, WordTimestamp

pytestmark = pytest.mark.logic


def _make_test_project() -> SubtitleProject:
    """最小双句工程（脏/锁各一，便于钉样标记语义）。"""
    s1 = Sentence(
        text="你好世界",
        start_time=1.0,
        end_time=3.0,
        words=[
            WordTimestamp(text="你", start_time=1.0, end_time=1.5),
            WordTimestamp(text="好", start_time=1.5, end_time=2.0),
            WordTimestamp(text="世", start_time=2.0, end_time=2.5),
            WordTimestamp(text="界", start_time=2.5, end_time=3.0),
        ],
        language="zh",
        is_dirty=True,
        is_locked=False,
    )
    s2 = Sentence(
        text="Hello world",
        start_time=3.5,
        end_time=5.0,
        words=[
            WordTimestamp(text="Hello", start_time=3.5, end_time=4.2),
            WordTimestamp(text="world", start_time=4.2, end_time=5.0),
        ],
        language="en",
        is_dirty=False,
        is_locked=False,
    )
    return SubtitleProject(sentences=[s1, s2], media_duration=10.0)


# ══════════════════════════════════════════════════════════════
# 1. 脏/锁标记语义
# ══════════════════════════════════════════════════════════════

def _case_models_dirty_and_locked():
    p = _make_test_project()
    assert p.dirty_indices() == [0]
    assert p.alignable_dirty_indices() == [0]
    assert p.locked_indices() == []

    # 锁定 s1：dirty 但 locked → 不可对齐
    p.mark_locked(0, True)
    assert p.locked_indices() == [0]
    assert p.alignable_dirty_indices() == []

    # 解锁 / 标净
    assert p.toggle_lock(0) is False
    assert p.locked_indices() == []
    assert p.alignable_dirty_indices() == [0]
    p.mark_clean(0)
    assert p.dirty_indices() == []

    # 序列化 roundtrip
    d = p.to_dict()
    p2 = SubtitleProject.from_dict(d)
    assert len(p2.sentences) == 2
    assert p2.sentences[0].is_locked is False


def _case_project_json_roundtrip_paths_and_words(tmp_path):
    """工程 .json：媒体路径 + 字级 + 脏标记 roundtrip（对应保存/打开工程）。"""
    src = SubtitleProject(
        source_media_path=str(tmp_path / "clip.mp4"),
        audio_path=str(tmp_path / "clip.wav"),
        media_duration=9.5,
        source_language="zh",
        sentences=[
            Sentence(
                text="你好",
                start_time=0.0,
                end_time=1.2,
                language="zh",
                is_dirty=True,
                is_locked=False,
                words=[
                    WordTimestamp(text="你", start_time=0.0, end_time=0.5, language="zh"),
                    WordTimestamp(text="好", start_time=0.5, end_time=1.2, language="zh"),
                ],
            ),
        ],
    )
    f = Path(tmp_path) / "demo.qss.json"
    src.save_json(f)
    loaded = SubtitleProject.load_json(f)
    assert loaded.source_media_path == src.source_media_path
    assert loaded.audio_path == src.audio_path
    assert abs(loaded.media_duration - 9.5) < 1e-6
    assert loaded.source_language == "zh"
    assert len(loaded.sentences) == 1
    s = loaded.sentences[0]
    assert s.text == "你好" and s.is_dirty is True
    assert len(s.words) == 2 and s.words[0].text == "你"
    assert abs(s.words[1].end_time - 1.2) < 1e-6
    assert src.to_dict()["schema_version"] == 1


def _case_project_schema_validation_and_atomic_save(tmp_path):
    import copy
    from unittest.mock import patch

    project = _make_test_project()
    data = project.to_dict()

    duplicate = copy.deepcopy(data)
    duplicate["sentences"][1]["sid"] = duplicate["sentences"][0]["sid"]
    with pytest.raises(ValueError, match="唯一正整数"):
        SubtitleProject.from_dict(duplicate)

    nonfinite = copy.deepcopy(data)
    nonfinite["sentences"][0]["start_time"] = float("nan")
    with pytest.raises(ValueError, match="有限数字"):
        SubtitleProject.from_dict(nonfinite)

    reversed_time = copy.deepcopy(data)
    reversed_time["sentences"][0]["end_time"] = 0.5
    with pytest.raises(ValueError, match="不能早于"):
        SubtitleProject.from_dict(reversed_time)

    future = copy.deepcopy(data)
    future["schema_version"] = 999
    with pytest.raises(ValueError, match="不支持的工程"):
        SubtitleProject.from_dict(future)

    destination = tmp_path / "atomic.qss.json"
    destination.write_text("original", encoding="utf-8")
    with patch("subs.models.os.replace", side_effect=OSError("simulated crash")), \
         pytest.raises(OSError, match="simulated crash"):
        project.save_json(destination)
    assert destination.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".atomic.qss.json.*.tmp"))


def _case_project_string_bool_migration_and_limits():
    """字符串布尔规整（修复 bool("false")==True）、大小上限、句内字时间单调。"""
    import copy

    base = {
        "schema_version": 1,
        "media_duration": 3.0,
        "sentences": [
            {
                "text": "你好", "start_time": 1.0, "end_time": 3.0,
                "is_dirty": "false", "is_locked": "false", "timed": "false",
                "words": [
                    {"text": "你", "start_time": 1.0, "end_time": 1.5},
                    {"text": "好", "start_time": 1.5, "end_time": 2.0},
                ],
            }
        ],
    }
    # 字符串布尔 → 规整为真布尔（旧/手改文件迁移，不再误判为 True）
    p = SubtitleProject.from_dict(copy.deepcopy(base))
    s = p.sentences[0]
    assert s.is_dirty is False and s.is_locked is False and s.timed is False

    # 垃圾布尔值 → 报错
    bad = copy.deepcopy(base)
    bad["sentences"][0]["is_dirty"] = "yes"
    with pytest.raises(ValueError, match="必须是布尔"):
        SubtitleProject.from_dict(bad)

    # 布尔数字 → 拒绝（float(True)==1.0 一类陷阱）
    bad2 = copy.deepcopy(base)
    bad2["sentences"][0]["start_time"] = True
    with pytest.raises(ValueError, match="不能是布尔"):
        SubtitleProject.from_dict(bad2)

    # 句内字时间非单调 → 报错
    bad3 = copy.deepcopy(base)
    bad3["sentences"][0]["words"] = [
        {"text": "好", "start_time": 1.5, "end_time": 2.0},
        {"text": "你", "start_time": 1.0, "end_time": 1.5},
    ]
    with pytest.raises(ValueError, match="单调"):
        SubtitleProject.from_dict(bad3)

    # 文本长度超上限 → 报错
    bad4 = copy.deepcopy(base)
    bad4["sentences"][0]["text"] = "字" * 200_001
    with pytest.raises(ValueError, match="安全上限"):
        SubtitleProject.from_dict(bad4)


# ══════════════════════════════════════════════════════════════
# 2. 媒体路径相对化 / 解析（跨机复现）
# ══════════════════════════════════════════════════════════════

def _case_media_paths_relativize_pack():
    import os as _os
    from unittest.mock import patch

    from subs.models import _relativize_media_paths

    # 工程目录内 → 相对路径（统一正斜杠）+ 原绝对路径入 hints
    data = {
        "source_media_path": "/proj/song.mp4",
        "audio_path": "/proj/.temp/song.wav",
        "video_path": "/proj/song.mp4",
    }
    out = _relativize_media_paths(data, Path("/proj"))
    assert out["source_media_path"] == "song.mp4"
    assert out["video_path"] == "song.mp4"
    assert out["audio_path"] == ".temp/song.wav"
    assert out["media_path_hints"]["source_media_path"] == "/proj/song.mp4"

    # 工程目录外 → 保持绝对、无 hints
    out2 = _relativize_media_paths({"source_media_path": "/other/song.mp4"}, Path("/proj"))
    assert out2["source_media_path"] == "/other/song.mp4"
    assert "media_path_hints" not in out2

    # 非 Windows 平台：Windows 盘符绝对路径原样保留（relpath 不理解盘符，会误转相对）
    out3 = _relativize_media_paths({"source_media_path": r"C:\proj\song.mp4"}, Path("/proj"))
    assert out3["source_media_path"] == r"C:\proj\song.mp4"
    assert "media_path_hints" not in out3

    # 模拟 Windows relpath 反斜杠 → 保存统一正斜杠（跨 OS 可读）
    # （用局部 patch，避免泄漏到 pack 内后续场景）
    with patch.object(_os.path, "relpath", lambda p, s: ".temp\\song.wav"):
        out4 = _relativize_media_paths({"audio_path": "/proj/.temp/song.wav"}, Path("/proj"))
        assert out4["audio_path"] == ".temp/song.wav"


def _case_media_paths_resolve_pack(tmp_path):
    from subs.models import _resolve_media_paths

    # 相对 → 工程目录绝对
    media = tmp_path / "song.mp4"
    media.write_bytes(b"x")
    out = _resolve_media_paths({"source_media_path": "song.mp4"}, tmp_path)
    assert out["source_media_path"] == str(media)

    # 相对缺失 → 回退 media_path_hints
    hint_file = tmp_path.parent / "real_song.mp4"
    hint_file.write_bytes(b"x")
    out2 = _resolve_media_paths(
        {"source_media_path": "missing1.mp4", "media_path_hints": {"source_media_path": str(hint_file)}},
        tmp_path,
    )
    assert out2["source_media_path"] == str(hint_file)

    # 相对缺失且无有效 hint → 落工程目录绝对（UI 据此提示重关联）
    out3 = _resolve_media_paths({"source_media_path": "gone.mp4"}, tmp_path)
    assert out3["source_media_path"] == str(tmp_path / "gone.mp4")

    # 本机/Windows 盘符/POSIX 根绝对路径 → 原样保留，不拼工程目录
    assert _resolve_media_paths(
        {"source_media_path": r"C:\Users\me\song.mp4"}, tmp_path,
    )["source_media_path"] == r"C:\Users\me\song.mp4"
    assert _resolve_media_paths(
        {"source_media_path": "/media/me/song.mp4"}, tmp_path,
    )["source_media_path"] == "/media/me/song.mp4"

    # 畸形 media_path_hints（非 dict）→ 忽略，不崩溃
    out4 = _resolve_media_paths(
        {"source_media_path": "song2.mp4", "media_path_hints": "not-a-dict"}, tmp_path,
    )
    assert out4["source_media_path"] == str(tmp_path / "song2.mp4")


def _case_save_load_roundtrip_relativizes_and_resolves(tmp_path):
    import shutil

    src = tmp_path / "src_proj"
    src.mkdir()
    media = src / "song.mp4"
    media.write_bytes(b"x")
    proj = SubtitleProject(
        source_media_path=str(media), audio_path=str(media), video_path=str(media),
        sentences=[Sentence(text="测试", start_time=0.0, end_time=1.0, language="zh")],
    )
    proj.save_json(src / "demo.qss.json")

    # 整体搬运到新目录（跨机）
    dst = tmp_path / "dst_proj"
    shutil.copytree(src, dst)
    loaded = SubtitleProject.load_json(dst / "demo.qss.json")
    assert loaded.source_media_path == str(dst / "song.mp4")
    assert loaded.audio_path == str(dst / "song.mp4")


# ══════════════════════════════════════════════════════════════
# 3. 跨机复现三件套随工程往返
# ══════════════════════════════════════════════════════════════

def _case_triple_settings_roundtrip(tmp_path):
    import json

    proj = SubtitleProject(
        sentences=[Sentence(text="测试", start_time=0.0, end_time=1.0, language="zh")],
        ass_style_data={"name": "Default", "font_name": "X", "font_size": 64.0},
        karaoke_template_data={"templates": [{"name": "弹跳放大", "enabled": True}]},
        export_settings={"k_tag_mode": "kf", "word_style": {"bold": True, "underline": False}},
    )
    p = tmp_path / "a.qss.json"
    proj.save_json(p)

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1              # schema 保持 v1
    assert raw["ass_style_data"]["font_name"] == "X"
    assert raw["karaoke_template_data"]["templates"][0]["enabled"] is True

    loaded = SubtitleProject.load_json(p)
    assert loaded.ass_style_data["font_size"] == 64.0
    assert loaded.karaoke_template_data["templates"][0]["name"] == "弹跳放大"
    assert loaded.export_settings["word_style"]["bold"] is True


def _case_triple_settings_absent_on_old_project(tmp_path):
    import json

    proj = SubtitleProject(sentences=[Sentence(text="测试", start_time=0.0, end_time=1.0)])
    p = tmp_path / "old.qss.json"
    proj.save_json(p)
    # 旧工程没有三件套字段 → 加载后为 None（读宽松，用全局偏好）
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert "ass_style_data" not in raw or raw["ass_style_data"] is None
    loaded = SubtitleProject.load_json(p)
    assert loaded.ass_style_data is None
    assert loaded.karaoke_template_data is None
    assert loaded.export_settings is None


# ══════════════════════════════════════════════════════════════
# 4. subs.atomic_io 原子写盘
# ══════════════════════════════════════════════════════════════

def _case_atomic_write_text_success(tmp_path):
    import subs.atomic_io as aio

    dest = tmp_path / "sub" / "out.vtt"
    aio.atomic_write_text(dest, "WEBVTT\n")
    assert dest.read_text(encoding="utf-8") == "WEBVTT\n"


def _case_atomic_write_text_does_not_corrupt_on_failure(tmp_path, monkeypatch):
    import subs.atomic_io as aio

    dest = tmp_path / "out.srt"
    dest.write_text("original", encoding="utf-8")
    monkeypatch.setattr(aio.os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        aio.atomic_write_text(dest, "new")
    assert dest.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".out.srt.*.tmp"))


def test_project_models_contract_pack(tmp_path):
    """test_project_models_contract_pack：合并 4 个场景（断言逐条保留，见各 _case_*）。"""
    _case_models_dirty_and_locked()
    _case_project_json_roundtrip_paths_and_words(tmp_path=tmp_path)
    _case_project_schema_validation_and_atomic_save(tmp_path=tmp_path)
    _case_project_string_bool_migration_and_limits()


def test_project_media_paths_pack(tmp_path):
    """test_project_media_paths_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_media_paths_relativize_pack()
    _case_media_paths_resolve_pack(tmp_path=tmp_path)
    _case_save_load_roundtrip_relativizes_and_resolves(tmp_path=tmp_path)


def test_project_triple_settings_pack(tmp_path):
    """test_project_triple_settings_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_triple_settings_roundtrip(tmp_path=tmp_path)
    _case_triple_settings_absent_on_old_project(tmp_path=tmp_path)


def test_project_atomic_io_pack(tmp_path, monkeypatch):
    """test_project_atomic_io_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_atomic_write_text_success(tmp_path=tmp_path)
    _case_atomic_write_text_does_not_corrupt_on_failure(tmp_path=tmp_path, monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
