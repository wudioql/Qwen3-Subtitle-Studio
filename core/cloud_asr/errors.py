"""core.cloud_asr.errors —— 云端失败的异常层级。

全部继承 ``CloudASRError(RuntimeError)``。分这么多子类的唯一目的是**决定要不要重试**：
只有 ``RateLimit`` / ``Network`` 可重试；``Auth`` / ``Quota``(HTTP 402) / ``Param``
重试无意义，必须立刻中止（402 是余额不足，平台不会透支）。
"""


from __future__ import annotations


# ═══════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════

class CloudASRError(RuntimeError):
    """云端 ASR 失败的基类。"""


class CloudASRAuthError(CloudASRError):
    """401/403：API Key 缺失、无效或无权访问该模型。"""


class CloudASRQuotaError(CloudASRError):
    """402：账户余额不足。**不可重试**——重试只会得到同样的 402。"""


class CloudASRParamError(CloudASRError):
    """400/404：请求参数非法（含模型名不存在、response_format 不支持等）。"""


class CloudASRRateLimitError(CloudASRError):
    """429：限流。免费模型限额固定，**充值也提不高**，只能退避等待或回落本地。"""


class CloudASRNetworkError(CloudASRError):
    """5xx 或网络异常：可重试。"""


class CloudASRLanguageError(CloudASRError):
    """语言无法决议：模型不返回 language 且用户未显式指定（auto）。"""
