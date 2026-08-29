"""兼容 façade：ORT Session 的唯一实现已收敛到 :mod:`core.ort_cuda`。

历史版本同时维护 ``ort_session.py`` 与 ``ort_cuda.py`` 两套 Provider、cuDNN
判断和 CPU 回退，行为容易漂移。生产代码应直接使用 ``core.ort_cuda``；本模块
只保留旧导入路径与旧返回合同，不再实现第二套逻辑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

from .ort_cuda import (
    create_inference_session as _create_session,
    is_cudnn_missing_error,
    pick_ort_providers,
    prime_torch_cuda_dlls,
)


def prepare_ort_cuda_dll_search_path() -> List[str]:
    """旧 API：触发统一的 torch CUDA DLL 预热并返回已发现的 torch/lib。"""
    if not prime_torch_cuda_dlls():
        return []
    try:
        import torch

        lib_dir = Path(torch.__file__).resolve().parent / "lib"
        return [str(lib_dir)] if lib_dir.is_dir() else []
    except Exception:  # noqa: BLE001
        return []


def preferred_providers(device: str = "cuda") -> tuple[List[str], str]:
    """旧 API 适配到 ``pick_ort_providers``。"""
    return pick_ort_providers(want_cuda=(device or "").lower() == "cuda")


def provider_label(session: Any) -> str:
    try:
        providers = list(session.get_providers())
    except Exception:  # noqa: BLE001
        return "unknown"
    if "CUDAExecutionProvider" in providers:
        return "GPU (CUDA)"
    if "DmlExecutionProvider" in providers:
        return "GPU (DirectML)"
    return "CPU"


def is_cudnn_unavailable_error(exc: BaseException) -> bool:
    """旧名称别名。"""
    return is_cudnn_missing_error(exc)


def create_inference_session(
    model_path: str | Path,
    *,
    device: str = "cuda",
    sess_options: Any = None,
    log_prefix: str = "[ORT]",
) -> Any:
    """旧返回合同只返回 session；统一实现返回的设备标签在此丢弃。"""
    _ = log_prefix  # 兼容旧关键字；统一 logger 使用 [ort_cuda]
    session, _actual = _create_session(
        model_path,
        want_cuda=(device or "").lower() == "cuda",
        sess_options=sess_options,
    )
    return session


def recreate_session_on_cpu(
    model_path: str | Path,
    *,
    sess_options: Any = None,
    log_prefix: str = "[ORT]",
    reason: str = "",
) -> Any:
    """旧 CPU 重建入口，委托统一 session 工厂。"""
    _ = (log_prefix, reason)
    session, _actual = _create_session(
        model_path,
        want_cuda=False,
        sess_options=sess_options,
    )
    return session


__all__ = [
    "prepare_ort_cuda_dll_search_path",
    "preferred_providers",
    "provider_label",
    "is_cudnn_unavailable_error",
    "create_inference_session",
    "recreate_session_on_cpu",
]
