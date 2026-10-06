"""tests/test_settings_dialog.py — 偏好设置套件（弹窗交互 + 持久化契约）

验证：SettingsDialog 按 Preferences 回显控件；模拟修改后 _on_save 写回偏好对象。
"""

from __future__ import annotations


from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)


import pytest

from core.app_config import Preferences, load_preferences, save_preferences

pytestmark = pytest.mark.ui

#: 视口外的横向 chrome：根布局边距 22*2 + 滚动区边框余量。与
#: ``SettingsDialog._CHROME_W`` 同源；写成命名常量是为了让断言意图自解释。
_CHROME_W = 22 * 2 + 4


def test_settings_dialog_tabs():
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


def test_settings_dialog_preserves_external_fields():
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


def test_settings_dialog_covers_json_fields():
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


def test_align_preferences_has_no_dead_source_language():
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

def test_preferences_serialization():
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


def test_settings_dialog_height_follows_page_content(monkeypatch):
    """反回归：设置弹窗高度必须跟随当前页内容，**不得裁切**。

    起因（GUI 真机实测）：弹窗固定 ``resize(620,570)``，而「云端 ASR」页要塞
    API Key / Base URL / 模型下拉 / 状态 / 模型说明 / 实测能力 / 支持语种 /
    编码 / 连接测试 / 用量台账 十来项，其中四段是自动换行文本——底部被裁掉。
    用户「移动一下弹窗」后看起来正常，那只是 Qt 重算 sizeHint 触发了重新布局，
    并非真的修复（换个说明更长的模型又复现）。

    契约：① 高度按页自适应；② 装不下时必须**可滚动**（内容仍可完整访问）。

    ``monkeypatch`` 把 ``start_if_idle`` 打成空操作是**必需**的，不是图省事：
    pytest 会强制隔离 ``QSS_CONFIG_DIR``，于是 ``load_cache()`` 必然为假，
    ``show()`` 就会触发一次**真实网络抓取**（后台 QThread）。测试结束弹窗析构时
    线程仍在跑 → Qt 直接 ``abort()``（``QThread: Destroyed while thread is still
    running``），表现为**没有任何输出的非零退出**，连faulthandler 都抓不到。
    本用例只关心布局，与网络无关，故必须切断。
    """
    from PySide6.QtWidgets import QApplication, QWidget
    QApplication.instance() or QApplication(["test"])
    from ui.settings_dialog import SettingsDialog

    parent = QWidget()
    parent.setGeometry(100, 80, 1280, 800)
    dlg = SettingsDialog(parent)
    monkeypatch.setattr(dlg._cloud_page, "start_if_idle", lambda: None, raising=False)
    dlg.show()
    QApplication.instance().processEvents()
    try:
        for i in range(dlg._stack.count()):
            host = dlg._stack.widget(i)
            key = host.objectName().replace("settings_", "").removesuffix("_host")
            dlg._stack.setCurrentIndex(i)
            QApplication.instance().processEvents()

            need = host.content_height()
            view_h = host._scroll.viewport().height()
            scrollable = host._scroll.verticalScrollBar().maximum() > 0
            assert need <= view_h or scrollable, (
                f"第{i} 页({key})内容被裁且无法滚动: 需要 {need}px > 可视 {view_h}px")

        # 长模型说明（切模型后说明会变长）也必须可完整访问
        cloud = dlg._cloud_page
        cloud._desc_label.setText("很长的模型说明" * 80)
        cloud._cap_label.setText("能力说明" * 60)
        QApplication.instance().processEvents()
        dlg._fit_height_to_page()
        QApplication.instance().processEvents()
        host = dlg._stack.currentWidget()
        need = host.content_height()
        view_h = host._scroll.viewport().height()
        assert (need <= view_h
                or host._scroll.verticalScrollBar().maximum() > 0), \
            f"长说明被裁: 需要 {need}px > 可视 {view_h}px"
    finally:
        dlg.close()
        parent.close()


def _open_dialog_for_geom(monkeypatch):
    """开一个不联网的设置弹窗，供几何类断言复用。"""
    from PySide6.QtWidgets import QApplication, QWidget
    QApplication.instance() or QApplication(["test"])
    from ui.settings_dialog import SettingsDialog

    parent = QWidget()
    parent.setGeometry(100, 80, 1400, 900)
    dlg = SettingsDialog(parent)
    # 切断云端页的真实网络抓取（否则析构时线程仍在跑 → Qt abort，见上文说明）
    monkeypatch.setattr(dlg._cloud_page, "start_if_idle", lambda: None, raising=False)
    dlg.show()
    QApplication.instance().processEvents()
    return dlg, parent


def _host_key(host) -> str:
    return host.objectName().removeprefix("settings_").removesuffix("_host")


def _host_index(dlg, key: str) -> int:
    return next(i for i in range(dlg._stack.count()) if _host_key(dlg._stack.widget(i)) == key)


def test_settings_dialog_page_width_follows_viewport(monkeypatch):
    """反回归：切页后页面宽度必须**始终等于视口宽度**，且横向不被裁。

    起因（2026-10-05 GUI 真机实测，用户原话「从另外的页切过去均有概率出现
    内容宽度错误，切到任意其它页再切回来均能复现，调整宽度又好，再切页又坏」）。

    旧实现两个叠加病因：
    1. ``_fit_height_to_page`` 调``page.adjustSize()``，把页面尺寸改成它
       自己的 ``sizeHint``（实测各页 613/676/322/566/364/244 各不相同），
       从布局管理里把页抢走——这正是「切页就坏、拖宽度就好」的机制。
    2. 滚动区的 widget 是 ``QStackedWidget``，其 ``minimumSizeHint`` 宽是
       「所有页取最大」（云端 ASR 页实测 560），而视口只有 502 → stack 比视口
       宽 58px；横向滚动条当时又是 ``ScrollBarAlwaysOff`` → 右边被**静默裁掉**。

    契约：任何顺序、任何次数切页后 ``page.width() == viewport.width()``，
    且横向滚动范围为 0（视口装得下就不该出现横向滚动）。
    """
    dlg, parent = _open_dialog_for_geom(monkeypatch)
    try:
        from PySide6.QtWidgets import QApplication

        def check(tag: str) -> None:
            for i in range(dlg._stack.count()):
                dlg._stack.setCurrentIndex(i)
                QApplication.instance().processEvents()
                host = dlg._stack.widget(i)
                key = _host_key(host)
                vp = host._scroll.viewport()
                page = host.page
                assert abs(page.width() - vp.width()) <= 1, (
                    f"{tag}: 第{i}页({key})宽度 {page.width()} != 视口 {vp.width()}"
                    f"（Δ={page.width() - vp.width()}）——内容被横向裁切")
                assert host._scroll.horizontalScrollBar().maximum() == 0, (
                    f"{tag}: 第{i}页({key})出现横向滚动（max="
                    f"{host._scroll.horizontalScrollBar().maximum()}）——"
                    f"说明视口装不下最宽页，_apply_width_bounds 的下限算错了")

        # 顺序遍历三轮 + 来回切：用户说的「切到任意页再切回来均能复现」
        for round_no in range(3):
            check(f"顺序 round{round_no}")
        for key in ("asr", "paths", "asr", "appearance", "cloud_asr", "asr"):
            dlg._stack.setCurrentIndex(_host_index(dlg, key))
            QApplication.instance().processEvents()
            check(f"来回切到 {key}")
    finally:
        dlg.close()
        parent.close()


def test_settings_dialog_min_width_covers_widest_page(monkeypatch):
    """契约：弹窗宽度下限 ≥「最宽页 minimumSizeHint + 视口外框」。

    否则用户把弹窗拖到最窄时，那一页右边会被横向裁掉（虽有滚动条兜底，
    但要用户自己拖才能看全不是好体验）。这正是 ``_apply_width_bounds``
    存在的原因。
    """
    dlg, parent = _open_dialog_for_geom(monkeypatch)
    try:
        widest = max(
            dlg._stack.widget(i).page.minimumSizeHint().width()
            for i in range(dlg._stack.count())
        )
        assert dlg.minimumWidth() >= widest + _CHROME_W, (
            f"宽度下限 {dlg.minimumWidth()} 装不下最宽页 {widest}")
        # 且确实留了可拖宽的余量（否则用户没法拖宽）
        assert dlg.maximumWidth() > dlg.minimumWidth()
    finally:
        dlg.close()
        parent.close()


def test_settings_dialog_no_blank_scrollbar(monkeypatch):
    """反回归：滚动条里**不许出现空白**——滚到底必须正好是内容末尾。

    起因（2026-10-05 真机回归，用户原话「除了云端 ASR 页外，都强行有了
    滚动条，下面明明没东西了，往下滚就是一大堆空白」）。

    机制：``QStackedWidget`` 的尺寸是「**所有页取最大**」。云端 ASR 页需要
    510px，于是 stack 恒为 510；而外观页只有 102px、视口 215px，可滚动的
    295px 里 182px 是空白——短页被最高的那页撑起来了。

    修法：每页各带自己的滚动容器（``_PageHost``），页面之间尺寸彻底隔离。
    判据 ``blank = sb.maximum - max(0, need - vp.height)``：可滚动总量里
    超出「内容确实装不下的部分」之外，多出来的就是空白。
    """
    dlg, parent = _open_dialog_for_geom(monkeypatch)
    try:
        from PySide6.QtWidgets import QApplication

        def check(tag: str) -> None:
            for i in range(dlg._stack.count()):
                dlg._stack.setCurrentIndex(i)
                QApplication.instance().processEvents()
                host = dlg._stack.widget(i)
                key = _host_key(host)
                vp = host._scroll.viewport()
                need = host.content_height()
                sb_max = host._scroll.verticalScrollBar().maximum()
                blank = sb_max - max(0, need - vp.height())
                assert blank <= 1, (
                    f"{tag}: 第{i}页({key})滚动条里有 {blank}px 空白"
                    f"（need={need} vp.h={vp.height()} sb.max={sb_max}）——"
                    f"页面被同栈里最高的那页撑起来了")

        for round_no in range(3):
            check(f"顺序 round{round_no}")
    finally:
        dlg.close()
        parent.close()


def test_settings_dialog_height_is_user_draggable(monkeypatch):
    """反回归：**用户手动拖高度必须生效**，且拖宽也能生效。

    起因（2026-10-05 真机回归，用户原话「为啥现在直接调不了高度了」）。
    上一轮把 ``_fit_height_to_page()`` 放进了 ``resizeEvent``，它又把高度
    算回内容高度——实测「请求 544 → 被弹回 424」，等于把高度锁死。

    契约：手动 resize 后实际高度 == 请求高度（容差 2px）。
    切页时的自动调高是另一回事，由``_on_page_changed`` 负责，见下一个用例。
    """
    dlg, parent = _open_dialog_for_geom(monkeypatch)
    try:
        from PySide6.QtWidgets import QApplication

        dlg._stack.setCurrentIndex(_host_index(dlg, "asr"))
        QApplication.instance().processEvents()

        for delta in (120, -60, 200, -40):
            before = dlg.height()
            want = before + delta
            dlg.resize(dlg.width(), want)
            QApplication.instance().processEvents()
            assert abs(dlg.height() - want) <= 2, (
                f"手动拖高度被弹回：请求 {want}，实际 {dlg.height()}"
                f"（resizeEvent 里不该再回调 _fit_height_to_page）")

        # 宽度同理：拖宽后不该被强行改回
        want_w = min(dlg.maximumWidth(), dlg.width() + 100)
        dlg.resize(want_w, dlg.height())
        QApplication.instance().processEvents()
        assert abs(dlg.width() - want_w) <= 2, (
            f"手动拖宽度被弹回：请求 {want_w}，实际 {dlg.width()}")
    finally:
        dlg.close()
        parent.close()


def test_settings_dialog_page_switch_autofits_height(monkeypatch):
    """契约：**切页时**仍按当前页内容自动调高度（用户明确要求保留）。

    与上一个用例成对：手动拖动不回调、切页才回调。两条一起把
    「什么时候该动高度、什么时候该由用户说了算」钉死。
    """
    dlg, parent = _open_dialog_for_geom(monkeypatch)
    try:
        from PySide6.QtWidgets import QApplication

        for i in range(dlg._stack.count()):
            dlg._stack.setCurrentIndex(i)
            QApplication.instance().processEvents()
            host = dlg._stack.widget(i)
            key = _host_key(host)
            vp = host._scroll.viewport()
            need = host.content_height()
            reach = vp.height() + host._scroll.verticalScrollBar().maximum()
            assert need <= reach + 1, (
                f"切到第{i}页({key})后内容装不下: 需要 {need}px > 可达 {reach}px"
                f"——切页时的自动调高没生效")
    finally:
        dlg.close()
        parent.close()


def test_settings_dialog_no_content_clipping_after_page_switch(monkeypatch):
    """反回归：切页后当前页内容必须**可完整访问**（要么装得下，要么滚得动）。

    起因（2026-10-05 用户原话「上下有遮挡，只有把弹窗高度拉到足够高至整页
    内容无需上下翻才能显示正常，而切页也能再次复现」）。

    判据用「可视高度 + 滚动范围」（reach）而不是只看可视高度——后者在
    ``sb.maximum() > 0`` 时会误报。
    """
    dlg, parent = _open_dialog_for_geom(monkeypatch)
    try:
        from PySide6.QtWidgets import QApplication

        def check(tag: str) -> None:
            for i in range(dlg._stack.count()):
                dlg._stack.setCurrentIndex(i)
                QApplication.instance().processEvents()
                host = dlg._stack.widget(i)
                key = _host_key(host)
                vp = host._scroll.viewport()
                need = host.content_height()
                reach = vp.height() + host._scroll.verticalScrollBar().maximum()
                assert need <= reach + 1, (
                    f"{tag}: 第{i}页({key})内容被裁且滚不到底: "
                    f"需要 {need}px > 可达 {reach}px（视口 {vp.height()} + "
                    f"滚动 {host._scroll.verticalScrollBar().maximum()}）")

        for round_no in range(3):
            check(f"顺序 round{round_no}")

        # 大段文字：云端 ASR 页把四段说明都拉长（用户说该页也有此情况）
        dlg._stack.setCurrentIndex(_host_index(dlg, "cloud_asr"))
        cloud = dlg._cloud_page
        cloud._desc_label.setText("很长的模型说明" * 80)
        cloud._cap_label.setText("能力说明" * 60)
        cloud._usage_label.setText("用量说明" * 90)
        QApplication.instance().processEvents()
        check("长文本")

        # 切走再切回来，长文本仍必须可完整访问（用户：切页能再次复现）
        dlg._stack.setCurrentIndex(_host_index(dlg, "asr"))
        QApplication.instance().processEvents()
        check("长文本→切走")
        dlg._stack.setCurrentIndex(_host_index(dlg, "cloud_asr"))
        QApplication.instance().processEvents()
        check("长文本→切回")
    finally:
        dlg.close()
        parent.close()


# ---------------------------------------------------------------------------
# 反回归：2026-10-06 Linux CI 三条失败（need > reach）的根因护栏
#
# 机制（三个叠加缺陷，本地 Windows 全绿、Linux 必现）：
#   1. 构造期视口宽未收敛（实测 98px，而该页自身最小宽 348px），
#      ``heightForWidth`` 在不可能出现的宽度上作答 -> need 严重高估。
#   2. ``chrome_h`` 用``self.height() - stack.height()`` 实测差值，
#      构造期两者都还是默认 30px -> 差值 0 -> 目标高度少算一整个 chrome。
#   3. 页面 ``minimumSizeHint().height()`` **不含 wordWrap 换行增量**，
#      视口落在 (minH, need) 区间时页面被压成恰好视口高，溢出那截
#      既不进minH 也不进滚动范围 -> **底部永久不可达**（sb_max == 0）。
#
# 判据一律用**独立量**：``_measure_page_height``（纯函数）重算内容高度、
# ``QTextLayout`` 手工换行、以及「逐步压低视口扫遍危险区间」。
# 不用 ``host.content_height()`` 本身——它是被测对象，用它判定等于自证。
# ---------------------------------------------------------------------------


def _independent_need(dlg, idx):
    """用纯函数独立重算该页内容高度（不复用被测的 content_height 路径）。"""
    from ui.settings_dialog import _measure_page_height

    host = dlg._stack.widget(idx)
    # 用视口宽与「页面最小宽」中的较大者——与 _PageHost 的可信宽度同源，
    # 但这里独立重算，不调用被测方法。
    width = max(host._scroll.viewport().width(), host.page.minimumSizeHint().width(), 1)
    return _measure_page_height(host.page, width)


def test_settings_dialog_construction_need_not_measured_at_unreal_width(monkeypatch):
    """反回归：``content_height()`` 不能在「不可能出现的宽度」上作答。

    起因（2026-10-06Linux CI）：构造期弹窗未 ``show``，视口宽只有 98px，
    而 asr 页自身 ``minimumSizeHint().width()`` 是 348px。在 98px 处换行
    行数被严重高估，``_measure_page_height`` 返回 455，而该页任何真实状态
    都只需 <= 287；cloud_asr 页更离谱：98px 处算出 **3520**，真实上界 582。

    契约：``content_height()`` 在任何时刻都不得**超过**该页在「最小可信
    宽度」处测得的上界。因为真实视口宽度永远 >= 页面最小宽（由
    ``_apply_width_bounds`` 保证），更窄处测出的高度只可能是假数据。

    **必须在构造期采样**：bug 只存在于「未show、视口未收敛」的那一次调用。
    若在 ``show()`` 之后再断言，视口已经可信，need 天然正常——这条用例会
    变成恒真断言（已实测：回退修复后它依然 passed）。故这里 hook
    ``_fit_height_to_page`` 抓取构造期那一帧。
    """
    import ui.settings_dialog as sd
    from ui.settings_dialog import _measure_page_height
    from PySide6.QtWidgets import QApplication, QWidget

    QApplication.instance() or QApplication(["test"])
    samples = []
    original = sd.SettingsDialog._fit_height_to_page

    def spy(self):
        host = self._stack.currentWidget()
        if isinstance(host, sd._PageHost) and not self.isVisible():
            # 构造期这一帧：记录「视口宽是否可信」与「据此测出的 need」
            vp_w = host._scroll.viewport().width()
            samples.append((host, vp_w, host.page.minimumSizeHint().width()))
        original(self)

    monkeypatch.setattr(sd.SettingsDialog, "_fit_height_to_page", spy)
    parent = QWidget()
    parent.setGeometry(100, 80, 1400, 900)
    dlg = sd.SettingsDialog(parent)
    try:
        monkeypatch.setattr(dlg._cloud_page, "start_if_idle", lambda: None, raising=False)
        assert samples, "没抓到构造期采样——spy 位置失效，这条用例会空转"

        offenders = []
        for host, vp_w, page_min_w in samples:
            key = host.objectName().removeprefix("settings_").removesuffix("_host")
            upper_bound = _measure_page_height(host.page, page_min_w)
            need = host.content_height()
            if need > upper_bound:
                offenders.append(
                    f"{key}: 构造期 need={need} > 可信上界 {upper_bound}"
                    f"（视口宽 {vp_w} < 页面最小宽 {page_min_w}）"
                )
        assert not offenders, (
            "在不可能出现的宽度上作答了：\n  " + "\n  ".join(offenders)
            + "\n>>> 构造期视口未收敛（未 show），此时 heightForWidth 的结果无意义"
        )

        # 反向：也不该因兜底而**低估**——低于当前视口宽处的真实测量同样有害。
        dlg.show()
        QApplication.instance().processEvents()
        for i in range(dlg._stack.count()):
            dlg._stack.setCurrentIndex(i)
            QApplication.instance().processEvents()
            host = dlg._stack.widget(i)
            vp_w = host._scroll.viewport().width()
            need = host.content_height()
            at_vp = _measure_page_height(host.page, max(vp_w, 1))
            assert need >= at_vp, (
                f"第{i}页({_host_key(host)}) content_height={need} 低于当前视口宽 "
                f"{vp_w} 处的真实测量 {at_vp}，会低估导致内容被裁"
            )
    finally:
        dlg.close()
        parent.close()


def test_settings_dialog_chrome_height_valid_before_show(monkeypatch):
    """反回归：构造期（未show）``_chrome_height()`` 就必须已是终值。

    起因（2026-10-06 Linux CI）：``chrome_h`` 原用
    ``self.height() - self._stack.height()`` 实测差值。构造期两者都还是
    Qt 默认 30px，差值退化成 **0** ——目标高度少算整整一个 chrome（实测
    165px），弹窗被定矮。Windows 上「后面还有一次调用」会自我修正，
    Linux 上不一定，所以不能依赖补救。

    契约：构造期的 ``_chrome_height()`` 必须等于「显示后实测差值」。

    **必须在 show() 之前断言**：几何收敛后两种算法必然相等（都返回 165），
    那时断言等于没断言。已实测：回退成旧的瞬时差值实现后，若在show 之后
    断言，这条用例会照样passed —— 恒真断言比没护栏更危险。
    """
    import ui.settings_dialog as sd
    from PySide6.QtWidgets import QApplication, QWidget

    QApplication.instance() or QApplication(["test"])
    at_construction = {}
    original = sd.SettingsDialog._fit_height_to_page

    def spy(self):
        if not self.isVisible():
            at_construction["chrome"] = self._chrome_height()
            at_construction["diff"] = max(self.height() - self._stack.height(), 0)
        original(self)

    monkeypatch.setattr(sd.SettingsDialog, "_fit_height_to_page", spy)
    parent = QWidget()
    parent.setGeometry(100, 80, 1400, 900)
    dlg = sd.SettingsDialog(parent)
    try:
        monkeypatch.setattr(dlg._cloud_page, "start_if_idle", lambda: None, raising=False)
        assert "chrome" in at_construction, (
            "没抓到构造期采样——spy 位置失效，这条用例会空转"
        )
        assert at_construction["chrome"] > 0, (
            f"构造期 chrome_h 退化成 {at_construction['chrome']}"
            f"（同期瞬时差值 = {at_construction['diff']}）"
            f"—— 目标高度会少算一整个 chrome，弹窗被定矮"
        )

        dlg.show()
        QApplication.instance().processEvents()
        measured = max(dlg.height() - dlg._stack.height(), 0)
        assert at_construction["chrome"] == measured, (
            f"构造期 chrome_h={at_construction['chrome']} != 显示后实测差值 "
            f"{measured} —— 构造期测量方式退化"
        )

        # 切到每一页都必须稳定（chrome 与页面内容无关）
        for i in range(dlg._stack.count()):
            dlg._stack.setCurrentIndex(i)
            QApplication.instance().processEvents()
            assert dlg._chrome_height() > 0, f"第{i}页 chrome_h 退化成 0"
    finally:
        dlg.close()
        parent.close()


def test_settings_dialog_overflow_reachable_in_dead_zone(monkeypatch):
    """反回归：视口落在 (minH, need) 死区时，内容底部仍必须滚得到。

    这是 2026-06 Linux CI 三条失败的真正机制，且**与 chrome/need 计算无关**。
    ``setWidgetResizable(True)`` 下滚动区给页面的高度是
    ``max(视口高, 页面 minimumHeight)``，而 ``minimumSizeHint().height()``
    **不含 wordWrap 换行增量**（实测 asr 231 vs need 259、cloud_asr 446 vs
    582）。于是存在一个死区：

        minH <视口高 < need

    页面被压成**恰好视口高**——比 minH 高、比 need 矮——溢出的那截既没
    算进 minH，也不进滚动范围，``sb_max == 0``，底部**怎么滚都看不到**。

    契约：逐步压低弹窗高度，把视口扫进死区，断言任何位置都能滚到底。
    判据用独立重算的 need，不复用被测的 ``content_height()``。
    """
    dlg, parent = _open_dialog_for_geom(monkeypatch)
    try:
        from PySide6.QtWidgets import QApplication

        offenders = []
        for i in range(dlg._stack.count()):
            dlg._stack.setCurrentIndex(i)
            QApplication.instance().processEvents()
            host = dlg._stack.widget(i)
            key = _host_key(host)
            base_h = dlg.height()
            entered_dead_zone = False
            for delta in range(0, 320, 3):
                dlg.resize(dlg.width(), max(base_h - delta, 120))
                QApplication.instance().processEvents()
                vp = host._scroll.viewport()
                sb = host._scroll.verticalScrollBar()
                need = _independent_need(dlg, i)
                min_h = host.page.minimumSizeHint().height()
                if not (min_h < vp.height() < need):
                    continue
                entered_dead_zone = True
                reach = vp.height() + sb.maximum()
                if need > reach + 1:
                    offenders.append(
                        f"{key}: 死区(视口 {vp.height()} ∈ ({min_h}, {need}))内"
                        f"滚不到底—— reach={reach} < need={need}，"
                        f"页面被压成 {host.page.height()}px"
                    )
                    break
            dlg.resize(dlg.width(), base_h)
            QApplication.instance().processEvents()

        assert not offenders, (
            "视口压进 (minH, need) 死区后内容底部不可达：\n  "
            + "\n  ".join(offenders)
            + "\n>>> 页面最小高度未反映 wordWrap 换行增量，"
            "须在每次切页时同步（见 _PageHost.sync_minimum_height）"
        )
        # 至少要真的扫进过死区，否则这条用例是空转
        assert entered_dead_zone, (
            "未能把视口压进任何死区，扫描范围可能已失效（弹窗初始高度变了？）"
        )
    finally:
        dlg.close()
        parent.close()


def test_settings_dialog_viewport_never_below_page_min_width(monkeypatch):
    """反回归：视口宽度必须 ≥ 各页自身 minimumSizeHint 宽（逐页、逐宽度）。

    这是 ``_PageHost._credible_measure_width`` 的**地基**。它按下限
    ``page.minimumSizeHint().width()`` 测量，安全性完全依赖「真实视口永不低于
    该下限」这个不变量：一旦视口比下限更窄，按更宽的下限测量就会**低估**
    换行后的内容高度 → 高度算小→ 内容被裁且滚不到底。

    2026-10-06 复查实测该不变量**曾经是假的**：``_CHROME_W = 22*2+4 = 48``
    漏算了垂直滚动条（实测应为 58 = 44 + 14），于是弹窗宽 608~617 这 10px 带内
    ``cloud_asr`` 视口宽 550 < 该页最小宽 560。本机字体下高度恰好没差
    （低估 0px）——**但那是运气**，换字体（Linux CI 就是）就可能跨过换行阈值。

    判据是**直接量真实几何**（``viewport().width()`` 与
    ``page.minimumSizeHint().width()``），不是复用 ``content_height()``
    （那是结果不是前提）。

    必须在**出了垂直滚动条**的状态下测：滚动条吃掉 14px 视口宽，不出滚动条
    时不变量当然成立——那样这条用例会恒绿。
    """
    dlg, parent = _open_dialog_for_geom(monkeypatch)
    try:
        from PySide6.QtWidgets import QApplication

        offenders = []
        lo = dlg.minimumWidth()
        for i in range(dlg._stack.count()):
            # **只测当前页**：未显示的页几何尚未收敛（实测视口仍是默认 86px），
            # 拿它的 viewport 和 minimumSizeHint 比毫无意义。
            dlg._stack.setCurrentIndex(i)
            QApplication.instance().processEvents()
            host = dlg._stack.widget(i)
            page_min_w = host.page.minimumSizeHint().width()
            # 只扫最窄那一段：那里最容易跌破下限（滚动条已占位）
            for w in range(lo, min(lo + 40, dlg.maximumWidth() + 1)):
                # 压到最矮 -> 必出垂直滚动条
                dlg.resize(w, dlg.minimumHeight())
                QApplication.instance().processEvents()
                vp_w = host._scroll.viewport().width()
                sb = host._scroll.verticalScrollBar()
                if vp_w < page_min_w:
                    offenders.append(
                        f"{_host_key(host)} @弹窗宽{w}: 视口{vp_w} < 页最小宽"
                        f"{page_min_w}（差 {page_min_w - vp_w}px，"
                        f"滚动条可见={sb.isVisible()}宽={sb.width()}）"
                    )
        assert not offenders, (
            "视口宽跌破页minimumSizeHint 宽——可信宽度的下限不再是可信下界，"
            "按它测量会低估高度：\n  " + "\n  ".join(offenders[:8])
            + ("\n  ..." if len(offenders) > 8 else "")
            + "\n>>> 宽度下限漏算了垂直滚动条（见 _chrome_width / _CHROME_W）"
        )
    finally:
        dlg.close()
        parent.close()


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
