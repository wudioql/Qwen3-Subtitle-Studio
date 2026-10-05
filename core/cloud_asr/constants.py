"""core.cloud_asr.constants —— 与外部服务约定的常量：端点、环境变量、上限、回调类型。

只放**不依赖任何内部状态**的字面量。模型能力与价格在 ``.facts``，异常在 ``.errors``。
"""


from __future__ import annotations


from pathlib import Path
from typing import Any, Callable, Optional, Union


DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"


ENV_API_KEY = "QSS_SILICONFLOW_API_KEY"          # 与项目既有的 QSS_CONFIG_DIR / QSS_TEMP_DIR 同前缀


TRACE_HEADER = "X-Trace-Id"                       # 服务端透传，便于回控制台费用明细对账


MAX_UPLOAD_BYTES = 50 * 1024 * 1024               # 官方限制 50MB


# 注：时长上限不在此处重复定义——真源是 core/constants.py 的 ASR_MAX_DURATION（1200s），
# 云端路径直接复用它。此处曾有一份 MAX_AUDIO_DURATION_SEC=3600，与真源冲突且无人引用。

OPUS_BITRATE = "32k"                              # 实测与 WAV 等价，20 分钟约 4MB


ProgressCallback = Optional[Callable[[int, int, str], None]]


AudioInput = Union[str, Path, Any]                # Path / str 为媒体文件；其余按 numpy 数组处理
