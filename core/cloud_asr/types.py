"""core.cloud_asr.types —— 配置与结果数据类（纯数据，无行为副作用）。

``CloudASRConfig`` 与 UI 偏好 ``CloudASRPreferences`` 字段 1:1 对应；
``CloudASRResult`` 是「服务端已返回、但语言尚未兜底」的中间态——最终语言由
``client._finalize_language`` 回填，所以调用方拿到它之后还应走一遍 _finalize。
"""


from __future__ import annotations


import os
from dataclasses import dataclass, field


from .constants import DEFAULT_BASE_URL, ENV_API_KEY


from .errors import CloudASRAuthError


from .facts import DEFAULT_CLOUD_MODEL


# ═══════════════════════════════════════════════════════════════
# 配置与结果
# ═══════════════════════════════════════════════════════════════

@dataclass
class CloudASRConfig:
    """云端 ASR 配置。字段与 UI 偏好 CloudASRPreferences 可 1:1 对应。"""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""                 # 空 → 回退读环境变量 ENV_API_KEY
    model: str = DEFAULT_CLOUD_MODEL
    timeout_sec: float = 300.0
    max_retries: int = 2              # 仅对 429 / 5xx / 网络异常生效；402 绝不重试
    codec: str = "opus"               # "opus"（推荐，实测等价且小 9 倍） | "wav"
    source_language: str = "auto"     # 短码；模型不返回 language 时作为兜底
    require_language: bool = True     # 语言无法决议时直接报错（对齐器需要语言）

    #: 本次调用观测到的平台计费用量——**由引擎回填**，不是输入参数。
    #: 官方没有可编程的用量查询接口（/v1/usage 全 404），只能客户端自己累账，
    #: 所以调用方应在任务成功后把它累加进 preferences 的 accumulated_seconds。
    observed_usage_seconds: float = 0.0

    def resolved_api_key(self) -> str:
        key = (self.api_key or "").strip() or os.environ.get(ENV_API_KEY, "").strip()
        if not key:
            raise CloudASRAuthError(
                f"未配置 SiliconFlow API Key：请在设置页填写，或设置环境变量 {ENV_API_KEY}。"
            )
        return key

    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/audio/transcriptions"


@dataclass
class CloudSegment:
    """句级时间戳片段（仅 Diarize 系列返回）。"""

    start: float = 0.0
    end: float = 0.0
    text: str = ""
    speaker: str = ""


@dataclass
class CloudASRResult:
    """归一化后的云端转写结果。"""

    text: str = ""                    # 已剥离说话人前缀，可直接喂给 _split_text_by_punct
    raw_text: str = ""                # 服务端原样文本（调试/排错用）
    language: str = ""                # 语言全名（如 "Chinese"），无法确定时为 ""
    duration_sec: float = 0.0         # 服务端自报音频时长
    usage_seconds: float = 0.0        # 平台计费用量（调用方应累计进本地台账）
    trace_id: str = ""                # 本次请求的 X-Trace-Id，便于回费用明细对账
    segments: list[CloudSegment] = field(default_factory=list)


@dataclass
class UploadPayload:
    """待上传的编码产物。"""

    data: bytes = b""
    filename: str = "audio.ogg"
    content_type: str = "audio/ogg"
    codec: str = "opus"


@dataclass
class ProbeResult:
    """零成本探活的结果，见 :func:`probe_models`。"""

    ok: bool = False
    message: str = ""                 # 可直接显示给用户的人话
    model_count: int = 0              # 该 Key 可见的模型数
    model_available: bool = False     # checked_model 是否在可见列表内
    checked_model: str = ""
