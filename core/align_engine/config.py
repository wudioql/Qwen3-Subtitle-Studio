"""core.align_engine.config — 对齐配置与进度回调。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger("core.align_engine")

AudioSource = Union[str, Path, Tuple[np.ndarray, int]]  # 路径 或 (samples, sr)


@dataclass
class AlignConfig:
    source_language: str = "auto"          # auto / zh / en ...
    align_backend: str = "qwen"            # "qwen" (Qwen3-Aligner) | "mms" (MMS-300M-FA-ONNX 歌词长拖音)
    subchunk_min_chars: int = 6            # >ALIGNER_MAX_DURATION 时子切分最小字符
    # 裁剪窗前后对称留白（对齐器的声学上下文）。MMS 尾字长拖音的搜索上界
    # 不依赖本值：以下一句起点/固定前瞻窗为界（见 mms_aligner 尾字语义），
    # 保证重复重对齐收敛、不随自身产出漂移。
    pad_before: float = 0.12
    pad_after: float = 0.12
    progress_cb: Optional[Callable[[int, int, str], None]] = None
    cancel_cb: Optional[Callable[[], bool]] = None  # Worker 合作式取消；不持久化


def report(cfg: AlignConfig, done: int, total: int, desc: str) -> None:
    """进度回调（原 _report；包内公开名 report，align_engine 仍导出兼容别名）。"""
    if cfg.progress_cb is not None:
        try:
            cfg.progress_cb(done, total, desc)
        except Exception:
            logger.exception("[Align] 进度回调异常")


# 兼容旧内部名
_report = report

__all__ = ["AudioSource", "AlignConfig", "report", "_report"]
