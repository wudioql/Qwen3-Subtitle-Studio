"""core.task_control — 后台任务的合作式取消原语。

模型单次前向通常不能安全强杀；取消只在阶段/句/块边界生效。调用方传入
``cancel_cb``（通常是 ``QThread.isInterruptionRequested``），核心层在安全点
调用 ``raise_if_cancelled``，由 Worker 转成非错误的 cancelled 信号。
"""
from __future__ import annotations

from typing import Callable, Optional

CancelCallback = Optional[Callable[[], bool]]


class TaskCancelled(RuntimeError):
    """用户请求取消任务；不是推理故障，不应弹“执行失败”。"""


def raise_if_cancelled(cancel_cb: CancelCallback) -> None:
    if cancel_cb is not None and bool(cancel_cb()):
        raise TaskCancelled("任务已由用户取消")


__all__ = ["CancelCallback", "TaskCancelled", "raise_if_cancelled"]
