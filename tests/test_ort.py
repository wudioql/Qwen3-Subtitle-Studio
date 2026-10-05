"""tests/test_ort.py — ONNX Runtime CUDA/cuDNN 预热与回退纯逻辑（无 GPU/无模型）。

覆盖：
- cuDNN 缺失错误识别（LoadLibrary 失败 / RequireCudnnHandle / ONNX Runtime 报错消息）；
- Windows DLL 目录句柄驻留（防 GC 后 CUDA EP 失效）；
- pick_ort_providers：CPU-only / CUDA 优先。

注：原先这里还有 3 个用例测 ``core.ort_session`` 兼容 façade。该 façade 已删除
（全仓无任何生产 import，生产侧一律直接走 ``core.ort_cuda``），故一并移除——
留着只会让「谁是真源」再次模糊。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

from core.ort_cuda import is_cudnn_missing_error, pick_ort_providers

pytestmark = pytest.mark.logic


# ══════════════════════════════════════════════════════════════
# 1. cuDNN 缺失错误识别
# ══════════════════════════════════════════════════════════════

def test_cudnn_error_detection():
    assert is_cudnn_missing_error(
        Exception(
            "cuDNN is unavailable: LoadLibrary failed for cudnn64_9.dll with error 2"
        )
    )
    assert is_cudnn_missing_error(Exception("RequireCudnnHandle not implemented"))
    assert not is_cudnn_missing_error(Exception("invalid shape for Conv"))


# ══════════════════════════════════════════════════════════════
# 2. Windows DLL 句柄驻留
# ══════════════════════════════════════════════════════════════

def test_windows_dll_handle_is_retained(monkeypatch, tmp_path):
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

def test_pick_providers_cpu_only():
    p, lab = pick_ort_providers(want_cuda=True, available=["CPUExecutionProvider"])
    assert p == ["CPUExecutionProvider"]
    assert lab == "CPU"


def test_pick_providers_prefers_cuda_when_listed():
    p, lab = pick_ort_providers(
        want_cuda=True,
        available=["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    assert p[0] == "CUDAExecutionProvider"
    assert "CPUExecutionProvider" in p
    assert "GPU" in lab


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
