"""subs — 字幕操作层

纯数据操作，无外部依赖（不涉及 torch / transformers / 播放）。
提供统一内部字幕模型、ASS 卡拉OK 生成、LRC 读写、格式互转与导出。

对外最重要的 7 个入口（含 k-tag 源与应用模板后成品）：
    1)  to_srt        — 逐字 SRT（可切 per_word / per_sentence）
    2)  to_vtt        — 逐字 VTT（可切 per_word / per_sentence）
    3)  to_ass        — 非 k-tag ASS 预览用；strategy="split" 拆事件 or "t" \\t 动画
    4)  to_lrc        — 增强 LRC 字级 A2 精确格式（或标准句级）
    5)  to_ass_karaoke — 给 Aegisub 做特效用的 k-tag ASS（必须有 word 级）
    6)  to_ass_karaoke_applied — 已应用所选模板的成品 ASS
    7)  WordHighlightStyle — 1~4 的逐字高亮开关组合
"""

from .models import (
    WordTimestamp,
    Sentence,
    SubtitleProject,
    make_sentences_from_raw,
)
from .time_fmt import (
    srt_time,
    vtt_time,
    ass_time,
    lrc_time,
    parse_srt_time,
    parse_vtt_time,
    parse_ass_time,
    parse_lrc_time,
)
from .converter import (
    WordHighlightStyle,
    merge_punct_words,
    iter_sentence_word_cues,
    iter_ass_t_segments,
    iter_lrc_enhanced_words,
)
from .exporters import (
    to_srt,
    to_vtt,
    to_ass,
    to_lrc,
    to_ass_karaoke,
    to_ass_karaoke_applied,
    AssStrategy,
)
from .ass_karaoke import (
    AssKaraokeHeader,
    KMode,
)
from .lrc_io import (
    LrcMeta,
    parse_lrc_text,
    read_lrc_file,
    read_lrc_to_project,
    write_lrc_text,
    write_lrc_file,
)
from .subtitle_io import (
    is_subtitle_file,
    parse_subtitle_or_text,
    load_subtitle_to_sentences,
)

__all__ = [
    # 数据模型
    "WordTimestamp",
    "Sentence",
    "SubtitleProject",
    "make_sentences_from_raw",
    # 时间码
    "srt_time",
    "vtt_time",
    "ass_time",
    "lrc_time",
    "parse_srt_time",
    "parse_vtt_time",
    "parse_ass_time",
    "parse_lrc_time",
    # 纯逻辑复用层
    "WordHighlightStyle",
    "merge_punct_words",
    "iter_sentence_word_cues",
    "iter_ass_t_segments",
    "iter_lrc_enhanced_words",
    # 对外导出（含 k-tag 源与应用模板后成品）
    "to_srt",
    "to_vtt",
    "to_ass",
    "to_lrc",
    "to_ass_karaoke",
    "to_ass_karaoke_applied",
    "AssStrategy",
    # k-tag 专用
    "AssKaraokeHeader",
    "KMode",
    # LRC
    "LrcMeta",
    "parse_lrc_text",
    "read_lrc_file",
    "read_lrc_to_project",
    "write_lrc_text",
    "write_lrc_file",
    # 字幕/文本导入
    "is_subtitle_file",
    "parse_subtitle_or_text",
    "load_subtitle_to_sentences",
]
