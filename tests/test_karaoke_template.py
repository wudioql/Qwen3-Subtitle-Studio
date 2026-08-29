"""tests/test_karaoke_template.py — Aegisub 卡拉OK效果定制套件

覆盖：
1.（logic）模板数据模型与渲染：默认模板等价历史硬编码 / 参数化各段开关 /
   raw 模式补前缀 / 序列化往返 / 最多单选 / 全禁用不输出模板 /
   导出管线（to_ass_karaoke）接线；
2.（ui）KaraokeTemplateDialog：回显-编辑-保存往返 / 参数导入自由模式 /
   增删与列表联动；导出面板卡片持久化。
"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

from subs.karaoke_template import (
    KaraokeTemplate, KaraokeTemplatePrefs, default_karaoke_templates,
)


# ═════════════ logic ═════════════

def _case_default_matches_legacy():
    """内置库 6 条效果、仅「弹跳放大」默认启用；渲染输出与历史硬编码行逐字节等价。"""
    from subs.karaoke_template import builtin_effects
    lib = builtin_effects()
    assert len(lib) == 6
    assert [t.name for t in lib if t.enabled] == ["弹跳放大"]
    lines = default_karaoke_templates().render_comments("Default")
    assert len(lines) == 1
    assert lines[0] == (
        "Comment: 0,0:00:00.00,0:00:05.00,Default,,0000,0000,0000,"
        "template syl all,"
        "{\\an5\\pos($scenter,$smiddle)\\fad(80,80)"
        "\\t($start,$mid,\\fscx116\\fscy116)"
        "\\t($mid,$end,\\fscx100\\fscy100)}"
    )


def _case_form_sections_toggle():
    """参数化各段独立开关：颜色/发光/缩放/fad/pos 按开关出现或消失。"""
    t = KaraokeTemplate(scale_enabled=False, fad_in_ms=0, fad_out_ms=0,
                        use_pos=False, color_enabled=True,
                        color_highlight="#FF0000", color_restore="#00FF00")
    body = t.tag_body()
    assert "\\fscx" not in body and "\\fad" not in body and "\\pos" not in body
    # 现役语义：旧键 color_restore 兼容存储“原色”，从原色绿渐变到高亮红。
    assert "\\1c&H00FF00&" in body
    assert "\\t($start,$end,\\1c&H0000FF&)" in body
    payload = t.to_dict()
    assert payload["color_restore"] == "#00FF00" and "color_original" not in payload
    restored = KaraokeTemplate.from_dict(payload)
    assert restored.color_original == "#00FF00" and restored.color_highlight == "#FF0000"

    t2 = KaraokeTemplate(glow_enabled=True, glow_bord=5.0, glow_blur=1.5)
    assert "\\bord5\\blur1.5" in t2.tag_body()

    # extra_tags 原样附加、误带大括号剥一层
    t3 = KaraokeTemplate(extra_tags="{\\frz10}")
    assert "\\frz10}" in t3.tag_body() and "{{" not in t3.tag_body()

    # line 模板用行级定位变量
    t4 = KaraokeTemplate(template_class="line")
    assert "\\pos($lcenter,$lmiddle)" in t4.tag_body()
    assert t4.effect_field() == "template line all"


def _case_raw_mode_and_effect_field():
    """raw 模式补 Comment 前缀；修饰符顺序稳定；空 raw 输出空行被过滤。"""
    t = KaraokeTemplate(mode="raw", raw_text="0,0:00:00.00,0:00:05.00,D,,0,0,0,template syl,{\\k}")
    assert t.render_comment().startswith("Comment: 0,")
    t2 = KaraokeTemplate(mode="raw", raw_text="Comment: 1,0:00:00.00,0:00:05.00,D,,0,0,0,template line,{x}")
    assert t2.render_comment().startswith("Comment: 1,")     # 已带前缀不重复
    t3 = KaraokeTemplate(modifiers=["noblank", "all"])       # 输出顺序按 TEMPLATE_MODIFIERS
    assert t3.effect_field() == "template syl all noblank"
    prefs = KaraokeTemplatePrefs(templates=[KaraokeTemplate(mode="raw", raw_text="")])
    # 空 raw 行不可产出；用户明确无效果时不得偷偷回退默认模板。
    assert prefs.render_comments() == []


def _case_serialization_roundtrip_and_single_selection():
    """to_dict/from_dict 往返；旧多选只保留首项；全禁用保持无模板。"""
    p = KaraokeTemplatePrefs(templates=[
        KaraokeTemplate(name="辉光", layer=0, glow_enabled=True, scale_enabled=False),
        KaraokeTemplate(name="本体", layer=1, scale_percent=130),
    ])
    p2 = KaraokeTemplatePrefs.from_dict(p.to_dict())
    assert [t.name for t in p2.templates] == ["辉光", "本体"]
    assert p2.templates[1].scale_percent == 130
    assert [template.enabled for template in p2.templates] == [True, False]
    assert len(p2.render_comments()) == 1

    junk = {"templates": [{"name": "x", "未知字段": 1}]}
    assert KaraokeTemplatePrefs.from_dict(junk).templates[0].name == "x"

    off = KaraokeTemplatePrefs(templates=[KaraokeTemplate(enabled=False)])
    assert off.render_comments() == []


def _case_export_pipeline_wiring():
    """to_ass_karaoke 透传 template_prefs；偏好容器默认往返。"""
    from core.app_config import Preferences
    from subs import to_ass_karaoke
    from subs.models import Sentence, SubtitleProject, WordTimestamp

    proj = SubtitleProject(sentences=[Sentence(
        text="你好", start_time=0.0, end_time=1.0,
        words=[WordTimestamp(text="你", start_time=0.0, end_time=0.5),
               WordTimestamp(text="好", start_time=0.5, end_time=1.0)],
    )])
    custom = KaraokeTemplatePrefs(templates=[KaraokeTemplate(
        name="变色", scale_enabled=False, color_enabled=True,
        color_highlight="#FFD54F", color_restore="#FFFFFF",
    )])
    doc = to_ass_karaoke(proj, template_prefs=custom)
    assert "\\1c&H4FD5FF&" in doc                 # 自定义模板进了文档
    assert "\\fscx116" not in doc                 # 默认弹跳没混进来
    doc_default = to_ass_karaoke(proj)
    assert "\\fscx116" in doc_default             # 不传 → 默认模板
    no_effect = KaraokeTemplatePrefs(templates=[KaraokeTemplate(enabled=False)])
    doc_plain = to_ass_karaoke(proj, template_prefs=no_effect)
    assert not any(line.startswith("Comment:") for line in doc_plain.splitlines())

    prefs = Preferences()
    assert prefs.karaoke_template.to_prefs().render_comments() \
        == default_karaoke_templates().render_comments()
    prefs.karaoke_template.apply(custom)
    assert prefs.karaoke_template.to_prefs().templates[0].name == "变色"


@pytest.mark.logic
def _case_karaoke_template_logic_pack():
    """模板模型 5 合 1：默认等价 / 分段开关 / raw / 单选迁移 / 导出接线。"""
    _case_default_matches_legacy()
    _case_form_sections_toggle()
    _case_raw_mode_and_effect_field()
    _case_serialization_roundtrip_and_single_selection()
    _case_export_pipeline_wiring()


# ═════════════ ui ═════════════

def _case_dialog_roundtrip_and_modes():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from ui.karaoke_template_dialog import KaraokeTemplateDialog

    # 每次打开定位唯一启用项；显式全不选时才回落第一项。
    selected = KaraokeTemplateDialog(KaraokeTemplatePrefs(templates=[
        KaraokeTemplate(name="A", enabled=False),
        KaraokeTemplate(name="B", enabled=True),
    ]))
    assert selected._list.currentRow() == 1 and selected._name_edit.text() == "B"
    selected.close()
    unselected = KaraokeTemplateDialog(KaraokeTemplatePrefs(templates=[
        KaraokeTemplate(name="A", enabled=False),
        KaraokeTemplate(name="B", enabled=False),
    ]))
    assert unselected._list.currentRow() == 0 and unselected._name_edit.text() == "A"
    unselected.close()

    src = KaraokeTemplatePrefs(templates=[KaraokeTemplate(name="A", scale_percent=120)])
    dlg = KaraokeTemplateDialog(src)
    try:
        # 回显
        assert dlg._name_edit.text() == "A"
        assert dlg._scale_spin.value() == 120
        # 编辑 → 内部对象即时回写
        dlg._scale_spin.setValue(150)
        dlg._color_cb.setChecked(True)
        out = dlg.current_prefs
        assert out.templates[0].scale_percent == 150
        assert out.templates[0].color_enabled is True
        # 深拷贝语义：调用方对象不被修改
        assert src.templates[0].scale_percent == 120

        # 参数导入自由模式：raw_text = 当前渲染结果，模式切换
        rendered = out.templates[0].render_comment()
        dlg._form_to_raw()
        assert dlg.current_prefs.templates[0].mode == "raw"
        assert dlg.current_prefs.templates[0].raw_text == rendered

        # 增删与列表联动 + 复选框即启用开关
        # 新增 = 复制当前选中效果：参数带走、命名「原名 副本」并去重、插在原件下方
        dlg._list.setCurrentRow(0)
        dlg._on_add()
        tpls = dlg.current_prefs.templates
        assert len(tpls) == 2
        assert tpls[1].name == "A 副本"
        assert tpls[1].scale_percent == tpls[0].scale_percent   # 参数复制而非默认值
        assert [template.enabled for template in tpls] == [False, True]
        dlg._on_add()                                           # 新项接替旧选择
        assert dlg.current_prefs.templates[2].name == "A 副本2"
        assert [template.enabled for template in dlg.current_prefs.templates] == [False, False, True]
        dlg._on_del()
        item0 = dlg._list.item(0)
        from PySide6.QtCore import Qt as _Qt
        item0.setCheckState(_Qt.CheckState.Unchecked)           # 取消勾选 → 停用
        assert dlg.current_prefs.templates[0].enabled is False
        dlg._on_del()
        assert len(dlg.current_prefs.templates) == 1
        dlg._on_del()                                  # 仅剩一条时拒删
        assert len(dlg.current_prefs.templates) == 1

        # 预览：全部未勾选 → 基础 k-tag；重新勾选 → 显示唯一模板代码
        assert "基础 k-tag" in dlg._preview.toPlainText()
        dlg._list.item(0).setCheckState(_Qt.CheckState.Checked)
        assert [template.enabled for template in dlg.current_prefs.templates] == [True]
        assert "Comment:" in dlg._preview.toPlainText()
    finally:
        dlg.close()


def _case_export_panel_card_persistence():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(["test"])
    from core.app_config import load_preferences
    from ui.export_panel import KaraokeTemplateCard

    card = KaraokeTemplateCard()
    try:
        # 默认态摘要：内置库仅「弹跳放大」启用
        assert "弹跳放大" in card._summary.text()
        # 模拟弹窗保存路径：直接调用持久化逻辑
        custom = KaraokeTemplatePrefs(templates=[KaraokeTemplate(name="测试层")])
        card._tpl = custom
        prefs = load_preferences()
        prefs.karaoke_template.apply(custom)
        from core.app_config import save_preferences
        save_preferences(prefs)
        reloaded = load_preferences().karaoke_template.to_prefs()
        assert reloaded.templates[0].name == "测试层"
        # current_template_prefs 深拷贝
        got = card.current_template_prefs()
        got.templates[0].name = "改名"
        assert card._tpl.templates[0].name == "测试层"
    finally:
        card.deleteLater()


@pytest.mark.ui
def _case_karaoke_template_dialog_pack():
    """弹窗与卡片 2 合 1：回显-编辑-保存与模式切换 / 面板卡片持久化。"""
    _case_dialog_roundtrip_and_modes()
    _case_export_panel_card_persistence()


def test_karaoke_template_pack():
    """test_karaoke_template_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_karaoke_template_logic_pack()
    _case_karaoke_template_dialog_pack()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
