"""core.ort_cuda — ONNX Runtime CUDA EP 启动前的 DLL/cuDNN 预热

Windows 上 ``onnxruntime-gpu`` 的 CUDA EP 需要 ``cudnn64_9.dll`` 等。
项目通常**不**单独装系统 cuDNN，而是依赖 PyTorch cu130 wheel 自带的 DLL。

旧路径：先跑 ASR → 顶层 ``import torch`` 已把 ``torch\\lib`` 挂进进程搜索路径
→ ORT 能 LoadLibrary 到 cuDNN。

自 torch 改为懒加载后：纯 MMS 对齐可能在**从未 import torch** 时建 CUDA Session
→ ``LoadLibrary failed for cudnn64_9.dll with error 2``。

本模块在创建 CUDA EP Session 前：
1. ``import torch``（触发 torch 自带 DLL 目录注入，含 cudnn）；
2. Windows 下再 ``os.add_dll_directory(torch/lib)`` 双保险。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

_PRIMED = False
# os.add_dll_directory 返回的 handle 析构时会撤销搜索路径；必须进程级持有。
_DLL_DIRECTORY_HANDLES: list[Any] = []


def prime_torch_cuda_dlls() -> bool:
    """尽量让进程能解析到 torch 自带的 CUDA/cuDNN DLL。

    Returns:
        True = 已 import torch（DLL 路径应已可用）；False = 无 torch，ORT 只能走 CPU/DML。
    """
    global _PRIMED
    if _PRIMED:
        return True
    try:
        import torch  # noqa: F401
    except Exception as e:  # noqa: BLE001
        logger.debug("[ort_cuda] 无 torch，跳过 cuDNN 预热: %s", e)
        return False

    # torch 2.x Windows wheel：lib 下含 cudnn64_9.dll / cublas 等
    try:
        import torch as _t
        lib_dir = Path(_t.__file__).resolve().parent / "lib"
        if sys.platform == "win32" and lib_dir.is_dir():
            # Python 3.8+ Windows：显式加入 DLL 搜索路径
            add = getattr(os, "add_dll_directory", None)
            if callable(add):
                handle = add(str(lib_dir))
                _DLL_DIRECTORY_HANDLES.append(handle)
            # 兼容部分仅看 PATH 的加载器
            path_now = os.environ.get("PATH", "")
            lib_s = str(lib_dir)
            if lib_s.lower() not in path_now.lower():
                os.environ["PATH"] = lib_s + os.pathsep + path_now
            logger.debug("[ort_cuda] 已注入 torch lib DLL 目录: %s", lib_dir)
    except Exception as e:  # noqa: BLE001
        logger.debug("[ort_cuda] 注入 torch lib 路径失败: %s", e)

    _PRIMED = True
    return True


def is_cudnn_missing_error(exc: BaseException) -> bool:
    """是否为 ORT CUDA EP 缺 cuDNN / 无法建 handle 一类错误。"""
    msg = str(exc).lower()
    keys = (
        "cudnn",
        "cudnn64",
        "requirecudnnhandle",
        "cudaexecutionprovider",
        "cublas",
        "cudart",
    )
    return any(k in msg for k in keys)


def pick_ort_providers(
    *,
    want_cuda: bool,
    available: Optional[Sequence[str]] = None,
) -> tuple[List[str], str]:
    """选择 ORT providers 列表与人类可读设备名。

    want_cuda 时先 prime torch DLL；若无 CUDA EP 再试 DML，否则 CPU。
    """
    if available is None:
        try:
            import onnxruntime as ort
            available = list(ort.get_available_providers())
        except Exception:  # noqa: BLE001
            available = []

    avail_set = set(available or [])
    if want_cuda and "CUDAExecutionProvider" in avail_set:
        prime_torch_cuda_dlls()
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], "GPU (CUDA)"
    if want_cuda and "DmlExecutionProvider" in avail_set:
        return ["DmlExecutionProvider", "CPUExecutionProvider"], "GPU (DirectML)"
    return ["CPUExecutionProvider"], "CPU"


def create_inference_session(
    model_path: str | Path,
    *,
    want_cuda: bool = True,
    sess_options: Any = None,
):
    """创建 InferenceSession：CUDA 失败（含缺 cuDNN）时自动回退 CPU，不让任务直接炸。"""
    import onnxruntime as ort

    if sess_options is None:
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    path = str(model_path)
    providers, label = pick_ort_providers(want_cuda=want_cuda)

    try:
        session = ort.InferenceSession(path, sess_options=sess_options, providers=providers)
    except Exception as e:  # noqa: BLE001
        if want_cuda and is_cudnn_missing_error(e) and providers[0] != "CPUExecutionProvider":
            logger.warning(
                "[ort_cuda] CUDA EP 创建失败（%s），回退 CPUExecutionProvider",
                e,
            )
            session = ort.InferenceSession(
                path, sess_options=sess_options, providers=["CPUExecutionProvider"]
            )
        else:
            raise

    active = session.get_providers()
    if "CUDAExecutionProvider" in active:
        actual = "GPU (CUDA)"
    elif "DmlExecutionProvider" in active:
        actual = "GPU (DirectML)"
    else:
        actual = "CPU"
        if want_cuda and label.startswith("GPU"):
            logger.warning(
                "[ort_cuda] 请求 %s 但 Session 实际 providers=%s，按 CPU 运行",
                label, active,
            )
    return session, actual


def run_with_cuda_fallback(session, output_names, feed_dict, *, recreate_cpu_session):
    """session.run；若运行期爆 cuDNN，销毁并用 recreate_cpu_session() 重建后重试一次。

    recreate_cpu_session: () -> new_session
    返回 (outputs, session_maybe_new)
    """
    try:
        return session.run(output_names, feed_dict), session
    except Exception as e:  # noqa: BLE001
        if not is_cudnn_missing_error(e):
            raise
        logger.warning(
            "[ort_cuda] CUDA 推理失败（%s），销毁会话并回退 CPU 重试一次",
            e,
        )
        try:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001
            pass
        del session
        cpu_sess = recreate_cpu_session()
        return cpu_sess.run(output_names, feed_dict), cpu_sess


__all__ = [
    "prime_torch_cuda_dlls",
    "is_cudnn_missing_error",
    "pick_ort_providers",
    "create_inference_session",
    "run_with_cuda_fallback",
]
