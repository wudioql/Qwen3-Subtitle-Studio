"""subs.media_types — 媒体类型常量（单一真源）。

视频扩展名集合下沉到 subs（纯常量层），供 core / ui / subs 三处共享，
消除各模块各自维护一份集合的漂移（此前 asr_engine / player_panel /
ass_karaoke 三份，内容还不一致：asr_engine 多 .mpeg/.mpg）。
"""
from __future__ import annotations

VIDEO_SUFFIXES: frozenset[str] = frozenset({
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
    ".m4v", ".ts", ".mpeg", ".mpg",
})

__all__ = ["VIDEO_SUFFIXES"]
