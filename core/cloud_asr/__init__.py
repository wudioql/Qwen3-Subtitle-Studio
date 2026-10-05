"""core.cloud_asr — 云端 ASR 客户端（SiliconFlow HTTP 音频转写接口，包入口）。

子模块：
    constants 端点 / 环境变量 / 体积与时长上限 / 进度回调类型
    facts     模型账本：免费与付费名单、官方支持语种、定价页实证快照
    errors    异常层级（Auth / Quota / Param / RateLimit / Network / Language）
    types     数据类（CloudASRConfig / CloudASRResult / CloudSegment / UploadPayload …）
    language  语言决议、文本脚本判定（中英混说逐句判）、响应归一化
    encoding  上传前编码：numpy→WAV、FFmpeg→OPUS、容器时长探测
    client    HTTP 客户端：multipart 组装、重试退避、transcribe_cloud / probe_models

对外仍是单文件时代的同一组名字：``from core.cloud_asr import transcribe_cloud``。

本 ``__init__`` **只做再导出，不放任何实现**——把实现搬进来会让「哪个子模块负责
什么」重新变糊，也让下面这条补丁规则失效。

⚠️ 打补丁要打在**真正使用该名字的模块**上，不是包上：
``transcribe_cloud`` 在 ``.client`` 内部按 ``client.__dict__`` 解析它的
``_http_post`` / ``encode_for_upload``，所以 ``monkeypatch.setattr(ca, "_http_post", …)``
**不会生效**（包属性只是一份再导出的引用）。正确写法：
``monkeypatch.setattr(core.cloud_asr.client, "_http_post", …)``。
反之，``_cloud_transcribe_text``（core/asr_engine/pipeline.py）是在调用点
``from .cloud_asr import transcribe_cloud`` 懒加载的，所以打在该函数里的
``transcribe_cloud`` 补丁**要**打在包上才生效。
"""

from __future__ import annotations

from .client import (
    _classify_error,
    _finalize_language,
    _http_post,
    _interruptible_sleep,
    _server_message,
    build_multipart,
    probe_models,
    transcribe_cloud,
)
from .constants import (
    DEFAULT_BASE_URL,
    ENV_API_KEY,
    MAX_UPLOAD_BYTES,
    OPUS_BITRATE,
    TRACE_HEADER,
    AudioInput,
    ProgressCallback,
)
from .encoding import (
    _ffmpeg_transcode,
    _media_duration_sec,
    _write_temp_wav,
    encode_for_upload,
    wav_bytes_from_array,
)
from .errors import (
    CloudASRAuthError,
    CloudASRError,
    CloudASRLanguageError,
    CloudASRNetworkError,
    CloudASRParamError,
    CloudASRQuotaError,
    CloudASRRateLimitError,
)
from .facts import (
    DEFAULT_CLOUD_MODEL,
    MODEL_PRICING_URL,
    OFFICIAL_LANGUAGES,
    PAID_MODELS,
    VERIFIED_FREE_MODELS,
    VERIFIED_ON,
    ModelFacts,
    is_verified_free,
)
from .language import (
    _DECISIVE_SCRIPTS,
    _HAN_RE,
    _KANA_RE,
    _LATIN_CANDIDATES,
    _LATIN_RE,
    _MIN_LATIN_TAIL_CHARS,
    _MIXED_ZH_EN_PATCH_MODELS,
    _SCRIPT_TO_CODES,
    _script_counts,
    infer_language_from_text,
    languages_for_model,
    normalize_language,
    parse_response,
    split_mixed_script_tail,
    split_zh_en_sentence_language,
    strip_speaker_prefixes,
    supports_zh_en_sentence_split,
)
from .types import (
    CloudASRConfig,
    CloudASRResult,
    CloudSegment,
    ProbeResult,
    UploadPayload,
)

# 单文件时代 ``core.cloud_asr.LANG_SHORT_TO_FULL`` 直接可用（它本来是从
# core.language_utils 转进来的）。tests/test_cloud_asr.py 按这个名字导入，
# 所以这里保留同一份转出，别改成从 language_utils 直接 import。
from ..language_utils import LANG_SHORT_TO_FULL  # noqa: F401

__all__ = [
    "CloudASRConfig",
    "CloudASRResult",
    "CloudSegment",
    "CloudASRError",
    "CloudASRAuthError",
    "CloudASRQuotaError",
    "CloudASRParamError",
    "CloudASRRateLimitError",
    "CloudASRNetworkError",
    "CloudASRLanguageError",
    "UploadPayload",
    "ProbeResult",
    "DEFAULT_BASE_URL",
    "DEFAULT_CLOUD_MODEL",
    "ENV_API_KEY",
    "MAX_UPLOAD_BYTES",
    "VERIFIED_FREE_MODELS",
    "PAID_MODELS",
    "OFFICIAL_LANGUAGES",
    "MODEL_PRICING_URL",
    "VERIFIED_ON",
    "ModelFacts",
    "is_verified_free",
    "encode_for_upload",
    "wav_bytes_from_array",
    "strip_speaker_prefixes",
    "normalize_language",
    "infer_language_from_text",
    "supports_zh_en_sentence_split",
    "split_zh_en_sentence_language",
    "languages_for_model",
    "probe_models",
    "transcribe_cloud",
    "LANG_SHORT_TO_FULL",
    # 测试 / 内部仍会直接引用（与 core.align_engine 的 __all__ 同例）：
    "TRACE_HEADER",
    "OPUS_BITRATE",
    "ProgressCallback",
    "AudioInput",
    "_classify_error",
    "_finalize_language",
    "_http_post",
    "_interruptible_sleep",
    "_server_message",
    "build_multipart",
    "parse_response",
    "_ffmpeg_transcode",
    "_media_duration_sec",
    "_write_temp_wav",
    "split_mixed_script_tail",
    "_script_counts",
    "_SCRIPT_TO_CODES",
    "_DECISIVE_SCRIPTS",
    "_LATIN_CANDIDATES",
    "_MIN_LATIN_TAIL_CHARS",
    "_MIXED_ZH_EN_PATCH_MODELS",
    "_HAN_RE",
    "_LATIN_RE",
    "_KANA_RE",
]
