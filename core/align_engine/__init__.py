"""core.align_engine — ForcedAligner 对齐引擎（包入口）

子模块：
    config    AlignConfig / AudioSource / 进度回调
    common    裁剪、接缝吸附、语言决议、分词依赖预检
    sentence  单句对齐（Qwen raw + MMS + 超长子切）
    full      全文重对齐（单段 / 静音切块）
    project   整项目按句 / 仅脏句

对外仍：``from core.align_engine import AlignConfig, align_full_text, ...``
"""

from __future__ import annotations

from .config import AlignConfig, AudioSource, _report, report
from .common import (
    _crop_audio,
    _infer_full_language,
    apply_seam_snaps,
    check_segmenter_dependency,
    preflight_segmenter_deps,
    snap_next_start_to_prev_end,
    snap_tail_to_next_start,
)
from .sentence import (
    align_sentence,
    align_sentence_raw,
    _align_long_sentence,
)
from .full import (
    align_full_text,
    _align_full_text_chunked,
    _align_full_text_single,
    _resolve_language_segments,
)
from .project import align_dirty_only, align_project
from core.mms_aligner import get_mms_aligner

__all__ = [
    "AlignConfig",
    "AudioSource",
    "align_project",
    "align_dirty_only",
    "align_full_text",
    "align_sentence",
    "align_sentence_raw",
    "check_segmenter_dependency",
    "preflight_segmenter_deps",
    "snap_tail_to_next_start",
    "snap_next_start_to_prev_end",
    "apply_seam_snaps",
    # 测试/内部仍可能引用：
    "_crop_audio",
    "_infer_full_language",
    "_report",
    "_resolve_language_segments",
    "report",
    "get_mms_aligner",
]
