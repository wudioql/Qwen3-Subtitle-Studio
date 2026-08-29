"""tests/test_model_manager.py — core.model_manager FA2 回退收敛（纯逻辑，零 Qt）。

覆盖：
- _is_flash_attn_error：只对 FA2 类错误（ImportError / 消息含 flash_attn）判真，
  OOM/权重损坏/文件缺失一律 False；
- _try_load_with_attn_fallback：local_files_only=True；FA2 未安装预检跳过（仅一次
  from_pretrained，直接 SDPA）；FA2 已装但加载失败回退 SDPA；FA2 之外异常原样抛出、
  不回退重载（不掩盖真实 OOM）。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

pytestmark = pytest.mark.logic


class _FakeLoadedModel:
    config: dict = {}

    def to(self, *args, **kwargs):
        return self


def _fake_ctor(fn):
    """把 from_pretrained(path, **kwargs) 函数包装成带 from_pretrained 方法的替身类。"""
    from types import SimpleNamespace
    return SimpleNamespace(from_pretrained=fn)


def _case_flash_attn_error_classification():
    from core.model_manager import _is_flash_attn_error

    assert _is_flash_attn_error(ImportError("flash_attn backend missing")) is True
    assert _is_flash_attn_error(RuntimeError("attn_implementation flash_attention_2 not supported")) is True
    assert _is_flash_attn_error(RuntimeError("CUDA out of memory")) is False
    assert _is_flash_attn_error(ValueError("weight corrupted")) is False
    assert _is_flash_attn_error(FileNotFoundError("model dir missing")) is False


def _case_model_load_passes_local_files_only():
    from core.model_manager import _try_load_with_attn_fallback

    calls = []

    def fake_from_pretrained(path, **kwargs):
        calls.append(kwargs)
        return _FakeLoadedModel()

    _try_load_with_attn_fallback(
        _fake_ctor(fake_from_pretrained), "/models/x", dtype=None, device="cpu",
        attn_implementation="sdpa", model_label="ASR",
    )
    assert calls[0]["local_files_only"] is True
    assert calls[0]["attn_implementation"] == "sdpa"


def _case_fa2_precheck_skips_reload_when_not_installed(monkeypatch):
    import importlib.util

    from core.model_manager import _try_load_with_attn_fallback

    calls = []

    def fake_from_pretrained(path, **kwargs):
        calls.append(kwargs)
        return _FakeLoadedModel()

    # flash_attn 未安装 → 直接 SDPA，只调一次 from_pretrained
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    _try_load_with_attn_fallback(
        _fake_ctor(fake_from_pretrained), "/models/x", dtype=None, device="cpu",
        attn_implementation="flash_attention_2", model_label="ASR",
    )
    assert len(calls) == 1
    assert calls[0]["attn_implementation"] == "sdpa"
    assert calls[0]["local_files_only"] is True


def test_fa2_fallback_pack(monkeypatch):
    """FA2 类错误 → 回退 SDPA；其它异常 → 原样抛出、不回退重载。"""
    import importlib.util

    from core.model_manager import _try_load_with_attn_fallback

    # ── FA2 已装但加载失败 → 回退 SDPA（两次调用）────────────────────────
    calls = []

    def fake_fa2_fail(path, **kwargs):
        calls.append(kwargs)
        if kwargs.get("attn_implementation") == "flash_attention_2":
            raise RuntimeError("flash_attn backend load failed")
        return _FakeLoadedModel()

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    model = _try_load_with_attn_fallback(
        _fake_ctor(fake_fa2_fail), "/models/x", dtype=None, device="cpu",
        attn_implementation="flash_attention_2", model_label="ASR",
    )
    assert isinstance(model, _FakeLoadedModel)
    assert len(calls) == 2
    assert calls[0]["attn_implementation"] == "flash_attention_2"
    assert calls[1]["attn_implementation"] == "sdpa"
    assert calls[1]["local_files_only"] is True

    # ── 非 FA2 错误（OOM）→ 原样抛出，只调一次，不回退 ──────────────────
    calls2 = []

    def fake_oom(path, **kwargs):
        calls2.append(kwargs)
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    with pytest.raises(RuntimeError, match="out of memory"):
        _try_load_with_attn_fallback(
            _fake_ctor(fake_oom), "/models/x", dtype=None, device="cuda",
            attn_implementation="flash_attention_2", model_label="ASR",
        )
    assert len(calls2) == 1   # 未回退重载，不掩盖真实 OOM


def test_model_manager_contract_pack(monkeypatch):
    """test_model_manager_contract_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_flash_attn_error_classification()
    _case_model_load_passes_local_files_only()
    _case_fa2_precheck_skips_reload_when_not_installed(monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
