"""tests/test_settings_dialog.py — 偏好设置套件（弹窗交互 + 持久化契约）

验证：SettingsDialog 按 Preferences 回显控件；模拟修改后 _on_save 写回偏好对象。
"""

from __future__ import annotations


from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)


import pytest

from core.app_config import Preferences, load_preferences, save_preferences

pytestmark = pytest.mark.ui


def _case_settings_dialog_tabs():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    from ui.settings_dialog import SettingsDialog
    p = Preferences()
    p.asr.extract_vocals = True
    p.align.align_backend = "mms"

    dlg = SettingsDialog(prefs=p)
    assert dlg._extract_vocals_cb.isChecked() is True
    # 对齐后端唯一入口在主工具栏，设置页不得再出现第二个写入口
    assert not hasattr(dlg, "_align_backend_combo")

    # 模拟用户修改并保存
    dlg._extract_vocals_cb.setChecked(False)
    dlg._on_save()

    assert p.asr.extract_vocals is False
    # 设置页保存不得触碰工具栏维护的 align_backend
    assert p.align.align_backend == "mms"

    dlg.close()
    print("test_settings_dialog_tabs PASSED ✔")


def _case_settings_dialog_preserves_external_fields():
    """_on_save 以磁盘最新偏好为基底只覆写自有字段。

    场景：对话框打开期间，别处（如主窗工具栏）把 asr.source_language 改写；
    旧实现会把构造时的整份快照写回磁盘 → source_language 被悄悄改回旧值（字段漂移）。
    新实现下该外部字段必须原样保留，且对话框自有字段正常生效。
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    from core.app_config import Preferences, load_preferences, save_preferences
    from ui.settings_dialog import SettingsDialog

    # 磁盘初始：工具栏会话曾选过日语 + 旧热词
    base = Preferences()
    base.asr.source_language = "ja"
    base.asr.context = "旧热词"
    save_preferences(base)

    dlg = SettingsDialog()  # 构造时快照：context=旧热词 / source_language=ja
    try:
        # 对话框打开期间，别处把识别语言改为 en（模拟工具栏立即持久化）
        ext = load_preferences()
        ext.asr.source_language = "en"
        save_preferences(ext)

        # 用户只在对话框里改热词并保存
        dlg._context_edit.setText("新热词")
        dlg._on_save()

        disk = load_preferences()
        assert disk.asr.context == "新热词"            # 自有字段 → 生效
        assert disk.asr.source_language == "en"        # 外部字段 → 不被整对象回写抹掉
    finally:
        dlg.close()
    print("test_settings_dialog_preserves_external_fields PASSED ✔")


def _case_settings_dialog_covers_json_fields():
    """设置弹窗 ↔ preferences.json 全量对应：高级页与分句全局上限往返。

    背景（用户反馈）：偏好设置内容与 preferences.json 大面积脱节——
    fallback_*/min_*/pad_*/subchunk 只能手改 json，分句页只管 per_lang
    却谎称「关闭=原始切分」（全局 max_* 始终生效）。现契约：
    1. 高级页控件回显 json 值、保存写回；
    2. 分句页全局上限 = asr.max_sentence_chars/sec；
    3. pad 成对同步：align.pad_* 与 asr.align_pad_* 保存后一致。
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])

    from ui.settings_dialog import SettingsDialog

    p = Preferences()
    p.asr.fallback_min_sentence_sec = 3.0
    p.asr.min_sentence_chars = 6
    p.asr.max_sentence_chars = 30
    p.align.pad_before = 0.08
    p.align.subchunk_min_chars = 10

    dlg = SettingsDialog(prefs=p)
    try:
        # 1. 回显
        assert dlg._fb_min_sec.value() == 3.0
        assert dlg._min_chars.value() == 6
        assert dlg._g_max_chars.value() == 30
        assert dlg._pad_before.value() == 0.08
        assert dlg._subchunk_min.value() == 10

        # 2. 修改 + 保存写回
        dlg._fb_min_sec.setValue(2.5)
        dlg._min_sec.setValue(0.5)
        dlg._g_max_chars.setValue(20)
        dlg._g_max_sec.setValue(6.0)
        dlg._pad_before.setValue(0.1)
        dlg._pad_after.setValue(0.14)
        dlg._subchunk_min.setValue(8)
        dlg._on_save()

        assert p.asr.fallback_min_sentence_sec == 2.5
        assert p.asr.min_sentence_sec == 0.5
        assert p.asr.max_sentence_chars == 20
        assert p.asr.max_sentence_sec == 6.0
        assert p.align.subchunk_min_chars == 8
        # 3. pad 成对同步（ASR 直通与重对齐共用同一语义值）
        assert p.align.pad_before == 0.1 and p.asr.align_pad_before == 0.1
        assert p.align.pad_after == 0.14 and p.asr.align_pad_after == 0.14
    finally:
        dlg.close()
    print("test_settings_dialog_covers_json_fields PASSED ✔")


def _case_align_preferences_has_no_dead_source_language():
    """死字段清除：AlignPreferences 无 source_language（对齐语言按句级→项目决议，
    不走偏好）；旧 json 残留 key 由宽松反序列化自动忽略、不炸。"""
    import json
    import tempfile
    from pathlib import Path as _P

    from core.app_config import AlignPreferences, load_preferences

    assert "source_language" not in AlignPreferences.__dataclass_fields__

    with tempfile.TemporaryDirectory() as td:
        legacy = _P(td) / "prefs.json"
        legacy.write_text(json.dumps({
            "version": 1,
            "align": {"source_language": "zh", "align_backend": "mms"},
        }), encoding="utf-8")
        prefs = load_preferences(legacy)
        assert prefs.align.align_backend == "mms"
        assert not hasattr(prefs.align, "source_language")
    print("test_align_preferences_has_no_dead_source_language PASSED ✔")


# ═════════════ Preferences 序列化往返（纯逻辑） ═════════════

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

def _case_preferences_serialization():
    p = Preferences()
    p.asr.extract_vocals = True
    p.align.align_backend = "mms"
    p.paths.vocal_model_path = "D:/models/Kim_Vocal_2.onnx"
    p.paths.mms_aligner_model_path = "D:/models/mms_onnx"
    p.ui_theme = "light"
    p.player_preview_mode = "karaoke"

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        save_preferences(p, tmp_path)
        p2 = load_preferences(tmp_path)
        assert p2.asr.extract_vocals is True
        assert p2.align.align_backend == "mms"
        assert p2.paths.vocal_model_path == "D:/models/Kim_Vocal_2.onnx"
        assert p2.paths.mms_aligner_model_path == "D:/models/mms_onnx"
        # 升格后的正式字段（原经 extra 透传）往返不丢失
        assert p2.ui_theme == "light"
        assert p2.player_preview_mode == "karaoke"
        print("test_preferences_serialization PASSED ✔")
    finally:
        tmp_path.unlink()


# ── 聚合入口（收集 2 项） ──────────────────────────────────────────

def _case_settings_dialog_pack():
    """弹窗交互 3 合 1：控件回显与保存 / 外部字段不被抹 / 高级页+全局上限往返。"""
    _case_settings_dialog_tabs()
    _case_settings_dialog_preserves_external_fields()
    _case_settings_dialog_covers_json_fields()


def _case_preferences_persistence_pack():
    """持久化 2 合 1：死字段移除+旧 json 兼容 / 序列化往返。"""
    _case_align_preferences_has_no_dead_source_language()
    _case_preferences_serialization()


def test_settings_dialog_all_pack():
    """test_settings_dialog_all_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_settings_dialog_pack()
    _case_preferences_persistence_pack()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
