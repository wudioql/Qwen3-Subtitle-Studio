"""tests/test_ort.py — ONNX Runtime CUDA/cuDNN 预热与回退纯逻辑（无 GPU/无模型）。

覆盖：
- cuDNN 缺失错误识别（LoadLibrary 失败 / RequireCudnnHandle / ONNX Runtime 报错消息）；
- Windows DLL 目录句柄驻留（防 GC 后 CUDA EP 失效）；
- pick_ort_providers / preferred_providers：CPU-only / CUDA 优先；
- core.ort_session 兼容 façade 委托单一实现。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

from core.ort_cuda import is_cudnn_missing_error, pick_ort_providers

pytestmark = pytest.mark.logic


# ══════════════════════════════════════════════════════════════
# 1. cuDNN 缺失错误识别
# ══════════════════════════════════════════════════════════════

def _case_cudnn_error_detection():
    assert is_cudnn_missing_error(
        Exception(
            "cuDNN is unavailable: LoadLibrary failed for cudnn64_9.dll with error 2"
        )
    )
    assert is_cudnn_missing_error(Exception("RequireCudnnHandle not implemented"))
    assert not is_cudnn_missing_error(Exception("invalid shape for Conv"))


def _case_is_cudnn_unavailable_error_detects_ort_message():
    from core.ort_session import is_cudnn_unavailable_error

    msg = (
        "Non-zero status code returned while running Conv node. "
        "RequireCudnnHandle [ONNXRuntimeError] : 9 : NOT_IMPLEMENTED : "
        "cuDNN is unavailable or disabled for CUDA Execution Provider: "
        "LoadLibrary failed for cudnn64_9.dll with error 2"
    )
    assert is_cudnn_unavailable_error(RuntimeError(msg)) is True
    assert is_cudnn_unavailable_error(RuntimeError("ordinary OOM")) is False
    assert is_cudnn_unavailable_error(ValueError("bad shape")) is False


# ══════════════════════════════════════════════════════════════
# 2. Windows DLL 句柄驻留
# ══════════════════════════════════════════════════════════════

def _case_windows_dll_handle_is_retained(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    import core.ort_cuda as oc

    torch_pkg = tmp_path / "torch"
    torch_pkg.mkdir()
    torch_init = torch_pkg / "__init__.py"
    torch_init.write_text("", encoding="utf-8")
    (torch_pkg / "lib").mkdir()
    fake_torch = SimpleNamespace(__file__=str(torch_init))
    handle = object()

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(oc.sys, "platform", "win32")
    monkeypatch.setattr(oc.os, "add_dll_directory", lambda _path: handle, raising=False)
    monkeypatch.setattr(oc, "_PRIMED", False)
    oc._DLL_DIRECTORY_HANDLES.clear()

    assert oc.prime_torch_cuda_dlls()
    assert oc._DLL_DIRECTORY_HANDLES[-1] is handle


# ══════════════════════════════════════════════════════════════
# 3. provider 选择
# ══════════════════════════════════════════════════════════════

def _case_pick_providers_cpu_only():
    p, lab = pick_ort_providers(want_cuda=True, available=["CPUExecutionProvider"])
    assert p == ["CPUExecutionProvider"]
    assert lab == "CPU"


def _case_pick_providers_prefers_cuda_when_listed():
    p, lab = pick_ort_providers(
        want_cuda=True,
        available=["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    assert p[0] == "CUDAExecutionProvider"
    assert "CPUExecutionProvider" in p
    assert "GPU" in lab


def _case_preferred_providers_cpu_when_no_ort_or_cpu_device():
    from core.ort_session import preferred_providers

    # device=cpu 必须只要 CPU EP（不要求本机装了 onnxruntime）
    try:
        providers, label = preferred_providers("cpu")
    except ImportError:
        pytest.skip("onnxruntime 未安装")
    assert providers == ["CPUExecutionProvider"]
    assert label == "CPU"


# ══════════════════════════════════════════════════════════════
# 4. 兼容 façade
# ══════════════════════════════════════════════════════════════

def _case_compat_session_factory_delegates_to_single_implementation(monkeypatch):
    import core.ort_session as compat

    sentinel = object()
    calls = []

    def fake_create(path, *, want_cuda, sess_options):
        calls.append((str(path), want_cuda, sess_options))
        return sentinel, "CPU"

    monkeypatch.setattr(compat, "_create_session", fake_create)
    assert compat.create_inference_session("model.onnx", device="cpu") is sentinel
    assert calls == [("model.onnx", False, None)]


def test_ort_cudnn_detection_pack():
    """test_ort_cudnn_detection_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_cudnn_error_detection()
    _case_is_cudnn_unavailable_error_detects_ort_message()


def test_ort_providers_pack(monkeypatch, tmp_path):
    """test_ort_providers_pack：合并 5 个场景（断言逐条保留，见各 _case_*）。"""
    _case_windows_dll_handle_is_retained(monkeypatch=monkeypatch, tmp_path=tmp_path)
    _case_pick_providers_cpu_only()
    _case_pick_providers_prefers_cuda_when_listed()
    _case_preferred_providers_cpu_when_no_ort_or_cpu_device()
    _case_compat_session_factory_delegates_to_single_implementation(monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
