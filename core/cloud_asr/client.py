"""core.cloud_asr.client —— HTTP 传输层：multipart 组装、重试退避、两个公开入口。

``transcribe_cloud`` 只负责把「编码 → 组装 → POST → 归一化」串起来，本身不做模型
与语言策略判断（那些在 ``.language`` / ``.facts``）。``probe_models`` 是零成本探活。

**本地路径不会 import 本模块**：``core/asr_engine/`` 只在云端分支里懒加载它。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional

from ..constants import DEFAULT_SAMPLE_RATE
from ..language_utils import LANG_SHORT_TO_FULL
from ..task_control import CancelCallback, raise_if_cancelled
from .constants import (
    DEFAULT_BASE_URL,
    MAX_UPLOAD_BYTES,
    TRACE_HEADER,
    AudioInput,
    ProgressCallback,
)
from .encoding import encode_for_upload
from .errors import (
    CloudASRAuthError,
    CloudASRError,
    CloudASRLanguageError,
    CloudASRNetworkError,
    CloudASRParamError,
    CloudASRQuotaError,
    CloudASRRateLimitError,
)
from .facts import PAID_MODELS, VERIFIED_ON, is_verified_free
from .language import infer_language_from_text, languages_for_model, parse_response
from .types import CloudASRConfig, CloudASRResult, ProbeResult, UploadPayload

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# HTTP 传输
# ═══════════════════════════════════════════════════════════════

def build_multipart(fields: dict[str, str], payload: UploadPayload) -> tuple[bytes, str]:
    """手工拼 multipart/form-data（标准库无内置编码器，且不值得为此引入依赖）。"""
    boundary = "----QSSCloudASR" + uuid.uuid4().hex
    body = b""
    for key, value in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += value.encode("utf-8") + b"\r\n"
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{payload.filename}"\r\nContent-Type: {payload.content_type}\r\n\r\n'
    ).encode("utf-8")
    body += payload.data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def _http_post(url: str, api_key: str, body: bytes, content_type: str,
               timeout: float, trace_id: str) -> tuple[int, str]:
    """执行一次 POST，返回 ``(status, body)``。HTTPError 不算「网络异常」，原样回状态。"""
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
        TRACE_HEADER: trace_id,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定 https 端点
        return resp.status, resp.read().decode("utf-8", "replace")


def _server_message(body: str) -> str:
    """从错误响应体里抠出人话（OpenAI 风格 {"error": {"message": ...}} 或纯文本）。"""
    try:
        err = json.loads(body).get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if isinstance(err, str):
            return err
    except Exception:  # noqa: BLE001 - 错误响应体形态不可控
        pass
    return (body or "").strip()[:300]


def _interruptible_sleep(seconds: float, cancel_cb: CancelCallback) -> None:
    """可被取消的退避等待：切成小片轮询 cancel_cb，保证取消响应及时。"""
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        raise_if_cancelled(cancel_cb)
        time.sleep(min(0.25, max(0.0, deadline - time.time())))


# ═══════════════════════════════════════════════════════════════
# 对外主接口
# ═══════════════════════════════════════════════════════════════

def transcribe_cloud(
    audio: AudioInput,
    *,
    cfg: Optional[CloudASRConfig] = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    cancel_cb: CancelCallback = None,
    progress_cb: ProgressCallback = None,
) -> CloudASRResult:
    """调用云端接口转写音频，返回 :class:`CloudASRResult`。

    与 ``asr_engine.transcribe`` 第 2 步的本地分支等价：**只产出纯文本**，字级
    时间戳仍由本地强制对齐器负责。

    Raises:
        CloudASRAuthError: 缺 Key / Key 无效。
        CloudASRQuotaError: 账户余额不足（HTTP 402，不重试）。
        CloudASRRateLimitError: 限流且重试耗尽。
        CloudASRNetworkError: 网络或服务端错误且重试耗尽。
        CloudASRParamError: 参数/模型名非法。
        CloudASRLanguageError: 语言无法决议且 require_language=True。
        TaskCancelled: 用户在安全点取消了任务。
    """
    cfg = cfg or CloudASRConfig()
    api_key = cfg.resolved_api_key()

    note = PAID_MODELS.get(cfg.model)
    if note:
        logger.warning("[cloud-asr] %s 已被账本实证为付费模型：%s", cfg.model, note)
    elif not is_verified_free(cfg.model):
        logger.warning(
            "[cloud-asr] %s 不在已实证免费清单（账单日期 %s）内，可能存在未预期的费用。",
            cfg.model, VERIFIED_ON,
        )

    if progress_cb:
        progress_cb(0, 0, "准备音频…")  # total=0：不确定进度，不伪造百分比

    payload = encode_for_upload(audio, sample_rate=sample_rate,
                                codec=cfg.codec, ffmpeg_path="", progress_cb=progress_cb)
    if len(payload.data) > MAX_UPLOAD_BYTES:
        raise CloudASRParamError(
            f"待上传音频 {len(payload.data) / 1048576:.1f}MB 超过云端 50MB 上限。"
            "请先用 FFmpeg 切成更短的片段（项目单次上限为 20 分钟）。"
        )

    logger.info("[cloud-asr] 上传 %s %.1fKB → model=%s", payload.codec,
                len(payload.data) / 1024, cfg.model)

    body, content_type = build_multipart({"model": cfg.model}, payload)
    attempt, last_error = 0, None
    while True:
        raise_if_cancelled(cancel_cb)
        attempt += 1
        trace_id = f"QSS-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        status, resp_body = -1, ""

        # 编码到此已结束，往后全是**云端排队 + 推理**。这段实测 1~32s，是整个
        # 流程里唯一的长等待，文案必须如实说明（2026-10-05 用户反馈「等待时间过长
        # 可能怀疑卡顿」：此前此处沿用「编码音频并准备上传…」且全程不再上报，
        # 于是用户盯着一句**已经过时的**文案看完整个推理过程）。
        if progress_cb:
            progress_cb(0, 0, f"云端识别中（{cfg.model.rsplit('/', 1)[-1]}，"
                               f"通常 1~30 秒）…")

        try:
            status, resp_body = _http_post(cfg.endpoint(), api_key, body, content_type,
                                           cfg.timeout_sec, trace_id)
        except urllib.error.HTTPError as exc:  # HTTPError 是 URLError 子类，必须先捕获
            try:
                status, resp_body = exc.code, exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - read 失败也要留下状态码
                status, resp_body = exc.code, ""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = CloudASRNetworkError(f"无法连接云端服务（{exc}）。请检查网络后重试。")

        if last_error is None:
            last_error = _classify_error(status, resp_body, cfg.model)
        if last_error is None:
            try:
                data = json.loads(resp_body)
            except ValueError:
                raise CloudASRError(
                    f"云端返回了无法解析的内容（HTTP {status}）：{resp_body[:200]}"
                ) from None
            result = parse_response(data, model=cfg.model, trace_id=trace_id)
            logger.info("[cloud-asr] 完成：%d 字 / lang=%r / usage=%ss / trace=%s",
                        len(result.text), result.language, result.usage_seconds, trace_id)
            return _finalize_language(result, cfg)

        if not isinstance(last_error, (CloudASRRateLimitError, CloudASRNetworkError)):
            raise last_error                      # 401/402/400 等：重试无意义
        if attempt > cfg.max_retries:
            raise last_error
        delay = min(2.0 ** attempt, 15.0)
        logger.warning("[cloud-asr] %s（第 %d 次），%.1fs 后退避重试", type(last_error).__name__,
                       attempt, delay)
        last_error = None
        if progress_cb:
            progress_cb(0, 0, f"云端繁忙，{delay:.0f} 秒后重试（第 {attempt} 次）…")
        _interruptible_sleep(delay, cancel_cb)


def probe_models(
    *,
    api_key: str = "",
    base_url: str = DEFAULT_BASE_URL,
    model: str = "",
    timeout: float = 20.0,
    cancel_cb: CancelCallback = None,
) -> ProbeResult:
    """零成本探活：验证 API Key 有效，并（可选）确认模型名真实存在。

    为什么**不**用真跑一次来「测试连接」
    ------------------------------------
    真跑会产生真实用量。而 ``GET /v1/models``**不花一分钱**，却已经能覆盖绝大多数
    失败原因：Key 缺失/无效、网络不通、Base URL 写错、模型名下架或拼错。
    代价只是无法验证「这个模型能不能真的出字」——那是深度测试的事，需要用户单独确认。

    Args:
        api_key: 留空则回退环境变量 ``QSS_SILICONFLOW_API_KEY``。
        model: 需要一并校验的模型名（可空，空则只验 Key）。

    Returns:
        :class:`ProbeResult`。``ok`` 为真表示 Key 有效且服务端可达。
        ``model`` 为空时 ``model_available`` 恒为 False（未校验而非校验失败）。
    """
    target = (model or "").strip()
    try:
        key = CloudASRConfig(base_url=base_url, api_key=api_key).resolved_api_key()
    except CloudASRError as exc:
        return ProbeResult(False, str(exc), checked_model=target)

    req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        raise_if_cancelled(cancel_cb)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定 https
            raw = resp.read().decode("utf-8", "replace") or "{}"
    except urllib.error.HTTPError as exc:  # HTTPError 是 URLError 子类，必须先捕获
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - read 失败也要留下状态码翻译结果
            body = ""
        err = _classify_error(exc.code, body, target)
        return ProbeResult(False, str(err), checked_model=target)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ProbeResult(
            False, f"无法连接云端服务（{exc}）。请检查网络，或确认 Base URL 是否正确。",
            checked_model=target,
        )

    try:
        payload = json.loads(raw)
    except ValueError:
        return ProbeResult(
            False, "云端返回了无法解析的模型列表。请确认 Base URL 指向 SiliconFlow 兼容接口。",
            checked_model=target,
        )

    ids = [str(it.get("id", "")) for it in (payload.get("data") or []) if isinstance(it, dict)]
    if not target:
        return ProbeResult(True, f"连接成功：API Key 有效，该账号可见 {len(ids)} 个模型。",
                           model_count=len(ids), checked_model=target)
    available = target in ids
    msg = (
        f"连接成功：该账号可见 {len(ids)} 个模型，所选模型在列表中。"
        if available else
        f"连接成功（账号可见 {len(ids)} 个模型），但所选模型 {target} 不在可见列表中"
        "——实际调用很可能返回 404。请刷新清单或改用列表中的模型。"
    )
    return ProbeResult(True, msg, model_count=len(ids), model_available=available,
                       checked_model=target)


def _classify_error(status: int, body: str, model: str) -> Optional[CloudASRError]:
    """把 HTTP 状态码翻译成带中文解释的异常；成功（200）返回 None。"""
    if status == 200:
        return None
    msg = _server_message(body)
    if status == 402:
        return CloudASRQuotaError(
            f"云端账户余额不足（HTTP 402：{msg}）。\n"
            "请在 SiliconFlow 控制台充值，或在设置页切换回本地模型识别。"
        )
    if status in (401, 403):
        return CloudASRAuthError(f"API Key 无效或无权访问该模型（HTTP {status}：{msg}）。")
    if status == 404:
        return CloudASRParamError(
            f"云端接口不存在或模型名有误（HTTP 404）：{model}。{msg}"
        )
    if status == 400:
        return CloudASRParamError(f"云端接口拒绝了请求参数（HTTP 400：{msg}）。")
    if status == 429:
        return CloudASRRateLimitError(
            f"触发云端限流（HTTP 429：{msg}）。免费模型限额固定，充值也无法提高。"
        )
    if status >= 500:
        return CloudASRNetworkError(f"云端服务暂时不可用（HTTP {status}：{msg}）。")
    return CloudASRError(f"云端调用失败（HTTP {status}）：{msg}")


def _finalize_language(result: CloudASRResult, cfg: CloudASRConfig) -> CloudASRResult:
    """语言三层兜底：模型返回 → 用户显式指定 → 从转写文本反推；都不行才报错。

    为什么需要后两层：XingChen 全系及多数新模型都不返回 ``language``，
    只要用户在工具栏选了 ``auto``，对齐阶段就会因拿不到语言而炸掉
    （``align_engine/full.py``）。与其让它以谜面失败，不如在这里要么可靠地补上、
    要么给出可操作的提示。
    """
    if result.language:
        return result

    candidates = languages_for_model(cfg.model)
    hint = (cfg.source_language or "auto").strip().lower()

    # 第 2 层：用户显式指定
    full = LANG_SHORT_TO_FULL.get(hint)
    if full:
        if hint not in candidates:
            logger.warning(
                "[cloud-asr] 指定语言 %s 不在模型 %s 的可用语种 %s 内，转写结果可能不可用",
                hint, cfg.model, list(candidates),
            )
        result.language = full
        logger.info("[cloud-asr] 模型未返回 language，采用用户指定语言：%s", full)
        return result

    # 第 3 层：按转写文本的字符脚本反推（候选集窄到能唯一判定才采信）
    inferred = infer_language_from_text(result.text, candidates)
    if inferred:
        result.language = LANG_SHORT_TO_FULL.get(inferred) or ""
        logger.info(
            "[cloud-asr] 模型未返回 language，按转写文本脚本推断为 %s（候选集 %s）",
            result.language, list(candidates),
        )
        return result

    if cfg.require_language:
        raise CloudASRLanguageError(
            f"云端模型 {cfg.model} 不返回语言，识别语言又是 auto，"
            "且转写文本也无法判断语种，无法进行强制对齐。\n"
            f"该模型可用语种：{', '.join(candidates) or '未知'}。\n"
            "请在工具栏把识别语言显式指定为其中一种后重试。"
        )
    return result
