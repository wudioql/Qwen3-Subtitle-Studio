"""tests/test_app_config.py — 偏好持久化与临时目录契约（纯逻辑，零 Qt）。

覆盖：
- load/save_preferences 嵌套未知字段递归保留（load→save 不丢未来版本字段）；
- load_preferences 内存缓存（独立副本 / save 刷新 / 分键失效）；
- core.constants.ensure_temp_dir 惰性创建多级目录。
"""

from __future__ import annotations

import json

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

pytestmark = pytest.mark.logic


def _case_prefs_nested_unknown_fields_roundtrip(tmp_path):
    from core.app_config import load_preferences, save_preferences

    p = tmp_path / "prefs.json"
    p.write_text(json.dumps({
        "version": 1,
        "asr": {"source_language": "auto", "future_nested_key": "keep-me"},
        "align": {"align_backend": "qwen", "another_future": 42},
    }), encoding="utf-8")

    prefs = load_preferences(p)
    save_preferences(prefs, p)
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["asr"]["future_nested_key"] == "keep-me"   # 修复：嵌套未知字段不再丢失
    assert after["align"]["another_future"] == 42
    assert after["asr"]["source_language"] == "auto"


def _case_prefs_cache_isolated_copies_and_save_refresh(tmp_path):
    from core.app_config import (
        Preferences,
        invalidate_preferences_cache,
        load_preferences,
        save_preferences,
    )

    invalidate_preferences_cache()
    p = tmp_path / "prefs.json"
    base = Preferences()
    base.asr.extract_vocals = True
    save_preferences(base, p)

    a = load_preferences(p)
    b = load_preferences(p)
    assert a is not b                      # 每次返回独立副本（与原"每次读盘"语义一致）
    assert a.asr.extract_vocals is True

    # 原地改副本不污染缓存
    a.asr.extract_vocals = False
    c = load_preferences(p)
    assert c.asr.extract_vocals is True

    # save 后刷新缓存
    c.asr.extract_vocals = False
    save_preferences(c, p)
    d = load_preferences(p)
    assert d.asr.extract_vocals is False
    invalidate_preferences_cache()


def _case_prefs_cache_keyed_by_path_and_invalidate(tmp_path):
    from core.app_config import (
        Preferences,
        invalidate_preferences_cache,
        load_preferences,
        save_preferences,
    )

    invalidate_preferences_cache()
    p1 = tmp_path / "a.json"
    p2 = tmp_path / "b.json"
    a = Preferences()
    a.asr.max_new_tokens = 111
    b = Preferences()
    b.asr.max_new_tokens = 222
    save_preferences(a, p1)
    save_preferences(b, p2)

    assert load_preferences(p1).asr.max_new_tokens == 111
    assert load_preferences(p2).asr.max_new_tokens == 222

    # 只清 p1：p2 缓存仍在，p1 强制重读盘
    invalidate_preferences_cache(p1)
    assert load_preferences(p1).asr.max_new_tokens == 111
    assert load_preferences(p2).asr.max_new_tokens == 222
    invalidate_preferences_cache()


def _case_ensure_temp_dir_creates_nested(tmp_path, monkeypatch):
    import core.constants as c

    nested = tmp_path / "a" / "b"
    monkeypatch.setattr(c, "TEMP_DIR", nested)
    assert not nested.exists()
    assert c.ensure_temp_dir() == nested
    assert nested.is_dir()              # parents=True 一次建多级


def test_app_config_pack(tmp_path, monkeypatch):
    """test_app_config_pack：合并 4 个场景（断言逐条保留，见各 _case_*）。"""
    _case_prefs_nested_unknown_fields_roundtrip(tmp_path=tmp_path)
    _case_prefs_cache_isolated_copies_and_save_refresh(tmp_path=tmp_path)
    _case_prefs_cache_keyed_by_path_and_invalidate(tmp_path=tmp_path)
    _case_ensure_temp_dir_creates_nested(tmp_path=tmp_path, monkeypatch=monkeypatch)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
