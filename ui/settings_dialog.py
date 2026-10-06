"""Fluent 偏好设置弹窗。

高频的语言、逐字效果与 ASS 文字样式分别留在主工具栏、导出侧栏和
ASS 样式弹窗；这里只保留全局且低频的识别、分句、路径与外观设置。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLayout,
    QScrollArea, QStackedWidget, QStyle, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CheckBox, ComboBox, DoubleSpinBox, LineEdit, Pivot,
    PrimaryPushButton, PushButton, SpinBox, TitleLabel,
)

from core.app_config import Preferences, load_preferences, save_preferences
from .settings import CloudASRPage
from .themes import _DEFAULT_THEME
from .widgets import hint_label


class _PageStack(QStackedWidget):
    """页面栈。**刻意不覆盖任何尺寸函数**——高度下限必须是0。

    2026-10-05 踩过的坑（真机反馈：「除云端ASR 页外都强行有滚动条，往下滚
    是一大片空白」）：``QStackedWidget`` 的 ``minimumSizeHint``/``sizeHint``
    高度都是「**所有页取最大**」。云端 ASR 页需要 510px，于是 stack 恒为 510；
    外观页只有 102px，视口 215px，可滚动的295px 里 182px 是**空白**——
    短页被最高的那页撑起来了。

    上一轮我曾用 ``page.setMinimumHeight(need)`` 顶着，恰恰是那条路把
    「当前页高度」变成了「所有页最大高度」，空白滚动条正是它造成的。

    正确解法在 ``_PageHost``：每页各自带滚动容器，栈只管切换、不碰尺寸。
    """


class _PageHost(QWidget):
    """单页容器：给该页一套**自己的**滚动区，与其它页彻底隔离。

    为什么必须是「每页一个滚动区」而不是「一个滚动区装所有页」
    ------------------------------------------------
    共享滚动区时，Qt 用 ``QStackedWidget`` 的聚合尺寸决定内容高度，
    于是「最高页」绑架了所有页（空白滚动条）；而要让每页各自正确，
    就得让stack 逐页改变自己的 minimumHeight——那等于把「弹窗高度 =
    内容高度」这个耦合又请回来，结果是**用户拖不动高度**（真机反馈：
    「为啥现在直接调不了高度了」，实测请求 544 被弹回 424）。

    拆成每页独立滚动区后：
    - 栈的高度下限为 0，谁也不绑架谁；
    - 弹窗高度完全由用户决定，``resizeEvent`` 不再回弹；
    - 内容超出时只有**当前页**出滚动条，且滚到底就是内容的末尾（无空白）。
    """

    def __init__(self, page: QWidget, parent=None) -> None:
        super().__init__(parent)
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("settings_page_scroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # 横向**不能**关：视口一旦窄于内容（用户拖到最窄、或换更长的模型名），
        # 关掉就是右边被静默裁掉且无从查看。
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setWidget(page)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._scroll)

    @property
    def page(self) -> QWidget:
        return self._scroll.widget()

    def content_height(self) -> int:
        """该页内容在**可信宽度**下真正需要的高度（含换行标签的增量）。

        见 ``_credible_measure_width``：宽度不可信时不能作答。
        """
        return _measure_page_height(self.page, self._credible_measure_width())

    def _credible_measure_width(self) -> int:
        """返回「可真实出现」的视口宽度，用于按宽算高。

        为什么需要这个下限
        ------------------
        ``heightForWidth`` 的前提是「这个宽度真的出现过」。构造期弹窗尚未
        ``show``，几何全是未收敛的默认值：实测 asr 页视口宽仅 **98px**，
        而它自身的 ``minimumSizeHint().width()`` 是 **348px**——98 不是
        「窄」，是**还没布局**。在 98px 处换行行数被严重高估，
        ``_measure_page_height`` 返回 455，而该页任何真实状态都只需 ≤ 287；
        cloud_asr 页更离谱：98px 处算出 **3520**，真实上界 582（6 倍）。

        下限取 ``page.minimumSizeHint().width()`` 而非某个魔法阈值
        ----------------------------------------------------------
        ``_apply_width_bounds`` 把「弹窗宽度 ≥ 最宽页 minimumSizeHint 宽
        + 视口外边距**与垂直滚动条**」设成硬下限（见 ``_chrome_width``），
        因此**视口宽度永远 ≥ 各页自身 minimumSizeHint 宽**。这个下限是代码
        自身已建立的不变量，不是拍脑袋的阈值：低于它的宽度根本不可能出现，
        ≥ 它的都是真实状态。

        ⚠️ 这条不变量**曾经是假的**，别再削回去：2026-10-06 复查实测
        ``_CHROME_W`` 漏算了垂直滚动条（48 vs 实测 58），使 ``cloud_asr`` 页
        在弹窗宽 608~617 这 10px 带内视口宽 550 < 该页最小宽 560。
        本机字体下该带内高度恰好没差（低估 0px），但那是运气——换字体
        （Linux CI 就是）就可能跨过换行阈值变成真实低估。故滚动条宽必须计入。

        在这个下限处测量得到的是**所有可信状态里的最大**内容高度
        （越窄→换行越多→越高，实测 6 页在可信区内 ``need`` 均随宽度单调不增）。
        即：宁可高估一点点让滚动条出现，也不低估导致内容被裁且滚不到底。
        """
        return max(self._scroll.viewport().width(), self.page.minimumSizeHint().width(), 1)

    def sync_minimum_height(self) -> None:
        """把页面最小高度对齐到真实内容高度，保证溢出永远滚得到底。

        为什么必须显式设最小高度
        ----------------------
        ``setWidgetResizable(True)`` 下，滚动区给页面的高度是
        ``max(视口高, 页面 minimumHeight)``。而 ``QWidget.minimumSizeHint()``
        **不含 wordWrap 标签按宽换行多出来的那截**——实测缺口（need 是内容
        在可信宽度下的真实高度，minH 是Qt 自己算的最小高）：

            页need    minH   缺口
            asr            259    231     28
            cloud_asr      582    446    136
            segmentation   353    311     42
            advanced       423    381     42

        缺口造成一个**底部永久不可达**的死区。设视口高 ``vp``：

        - ``vp >= need``：装得下，没事；
        - ``vp <= minH``：页面保住 minH，滚动范围 = minH - vp，能滚到底；
        - ``minH < vp < need``：页面被压成 **恰好 vp 高**（比 minH 高、比 need 矮），
          溢出的那截既没算进 minH、也不进滚动范围 → ``sb_max == 0``、
          内容底部**怎么滚都看不到**。

        实测在 Windows 上就能复现：segmentation 页need=353、视口 350 时
        ``sb_max=0``、reach=350 < 353；advanced 页 need=423、视口 420 同样。
        2026-10-06 Linux CI 上asr 页报 ``need=267 可达 251``，正是这个死区。

        设了最小高度后，``widgetResizable`` 只能给「视口高或内容高度」，
        溢出必然进滚动范围。实测危险区间扫描：3 页不可达 → **0 页**。

        必须在**每次切页**时重算（而非构造期一次）：各页 need 不同
        （102 ~ 582），且用户拖宽度会改变换行行数。
        """
        self.page.setMinimumHeight(self.content_height())


def _measure_page_height(page: QWidget, width: int) -> int:
    """页面在给定宽度下真正需要的高度（含 QFormLayout 里换行标签的增量）。"""
    need = page.sizeHint().height()
    lm = page.layout()
    if lm is not None:
        need = max(need, lm.heightForWidth(max(1, width)))
    for w in page.findChildren(QWidget):
        l2 = w.layout()
        if l2 is None:
            continue
        hh = l2.heightForWidth(max(1, width))
        if hh > 0:
            need = max(need, hh + 2 * l2.contentsMargins().top() + l2.spacing())
    return need


class SettingsDialog(QDialog):
    #: 宽度。只约束宽度，**高度不写死**——各页内容高度差异极大。
    #:
    #: 上下限的**实际值在 __init__ 末尾按页面内容算**（``_apply_width_bounds``）：
    #: 宽度下限必须 ≥「最宽页的 minimumSizeHint 宽 + 视口外的边距与框」，
    #: 否则那一页右边会被横向裁掉（虽有滚动条兜底，但要让用户自己拖才能看全
    #: 显然不是好体验）。
    _MIN_W = 560
    _MAX_W = 760
    #: 高度：只兜底「别缩成一条缝」与「别高过屏幕」，**不锁死**。
    #: 2026-10-05 用户反馈「为啥现在直接调不了高度了」——高度必须让用户自由决定，
    #: 内容超出靠每页自己的滚动区（见 ``_PageHost``），不由弹窗高度兜。
    _MIN_H = 380
    #: 弹窗根布局左右留白（``root.setContentsMargins`` 的 22*2）。
    _MARGIN_W = 22 * 2
    #: 视口外那一圈除留白外的占用：**垂直滚动条**宽度。
    #:
    #: 2026-10-06 复查实测：这里原先写死 ``22 * 2 + 4 = 48``，那个``4``是
    #: 按「无滚动条」估的框宽，而实际最窄时**垂直滚动条必然可见**（内容装不下）。
    #: 实测拆解：弹窗 608 → 视口 550，差 58 = 留白 44 + 滚动条 **14** + 余量 0。
    #: 于是最窄时视口宽 550 < ``cloud_asr`` 页自身的 minimumSizeHint 宽 **560**，
    #: 打破了 ``_PageHost._credible_measure_width`` 依赖的不变量
    #: （「视口宽度≥ 各页自身 minimumSizeHint 宽」），危险带宽10px（弹窗 608~617）。
    #: 本机字体下该带内need 恰好与按真实宽一致（低估 0px），**但这是运气**——
    #: 换字体（Linux CI 就是）可能跨过换行阈值变成真实低估 → 内容被裁且滚不到底。
    #: 所以宽度下限必须把滚动条算进去。用 ``PM_ScrollBarExtent`` 取而非写死 14，
    #: 因为它随平台/样式变化（Linux 上未必是 14）。
    _CHROME_W = _MARGIN_W

    def __init__(self, parent=None, prefs: Optional[Preferences] = None):
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.setWindowTitle("偏好设置")
        self._prefs = prefs or load_preferences()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        self._title_label = TitleLabel("偏好设置", self)
        root.addWidget(self._title_label)

        self._pivot = Pivot(self)
        root.addWidget(self._pivot)
        # 各页内容高度差异极大（云端 ASR 页要塞模型说明/实测能力/支持语种/用量
        # 台账四段自动换行文本，外观页只有两行）。**每页各带一个滚动容器**
        # （_PageHost），栈只负责切换、不参与尺寸——这样「最高页」不会绑架
        # 所有页（那会让短页滚出一大片空白），弹窗高度也能由用户自由决定。
        # 2026-10-04/05 两次返工的共同教训：别让「弹窗高度 = 内容高度」。
        self._stack = _PageStack(self)
        root.addWidget(self._stack, 1)
        # 高度**不由 layout 钉死**：SetMinAndMaxSize 会让 layout 反过来把窗口
        # 尺寸钉在 sizeHint 上（实测连 setMinimumWidth 都会被覆盖）。
        root.setSizeConstraint(QLayout.SizeConstraint.SetDefaultConstraint)

        self._build_asr_tab()
        self._build_cloud_asr_tab()
        self._build_segmentation_tab()
        self._build_paths_tab()
        self._build_advanced_tab()
        self._build_appearance_tab()
        self._stack.currentChanged.connect(self._on_page_changed)
        self._pivot.setCurrentItem("asr")

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = PushButton(self)
        cancel.setText("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = PrimaryPushButton(self)
        save.setText("保存设置")
        save.clicked.connect(self._on_save)
        buttons.addWidget(save)
        root.addLayout(buttons)

        # 页面都建好后再定初始尺寸：宽度下限此时才算得出（依赖各页
        # minimumSizeHint）。高度只给**首次**的合适值（按第一页内容），
        # 之后完全交给用户——见 _apply_initial_height。
        self._apply_width_bounds()
        self._apply_initial_height()

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt 命名
        """**刻意空实现**——用户拖动高度时绝不能弹回。

        2026-10-05 真机回归（用户原话「为啥现在直接调不了高度了」）：
        上一轮这里无条件调 `_fit_height_to_page()`，它又把高度算回内容高度，
        实测「请求 544 → 被弹回 424」，等于把高度锁死。

        切页时的自动调高已挪到 ``_on_page_changed``（那是用户**主动**切页，
        调高合理）；本方法只在用户手动拖动时触发，此时必须尊重用户。
        """
        super().resizeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 — Qt 命名
        """显示之后才允许云端页补抓清单（见 CloudASRPage._load_initial_models）。

        放在showEvent 而不是构造函数：构造期起线程会在「弹窗刚开就被关」时
        触发 ``QThread: Destroyed while thread is still running`` —— 那是 Qt
        的进程级 abort，不是 Python 异常，测试里表现为无任何输出的非零退出。
        """
        super().showEvent(event)
        page = getattr(self, "_cloud_page", None)
        if page is not None and hasattr(page, "start_if_idle"):
            page.start_if_idle()

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt 命名
        """关闭前通知子页收尾后台线程（合作式，不强杀）。"""
        page = getattr(self, "_cloud_page", None)
        if page is not None and hasattr(page, "shutdown"):
            page.shutdown()
        super().closeEvent(event)

    def reject(self) -> None:
        """点「取消」也要收尾——它不走closeEvent（exec 内直接结束事件循环）。"""
        page = getattr(self, "_cloud_page", None)
        if page is not None and hasattr(page, "shutdown"):
            page.shutdown()
        super().reject()

    def _add_page(self, key: str, title: str, page: QWidget) -> None:
        """把页面套进自己的滚动容器再入栈。

        ``QStackedWidget`` 的尺寸语义是「所有页取最大」，共享一个滚动区时
        最高页会绑架所有页（短页滚出一大片空白）。每页各带滚动区即可隔离，
        代价是栈本身高度下限为 0——**这正是我们要的**：高度只由用户决定。
        """
        page.setObjectName(f"settings_{key}_page")
        host = _PageHost(page, self._stack)
        host.setObjectName(f"settings_{key}_host")
        self._stack.addWidget(host)
        self._pivot.addItem(
            routeKey=key,
            text=title,
            onClick=lambda _checked=False, h=host: self._stack.setCurrentWidget(h),
        )

    def _on_page_changed(self, index: int) -> None:
        if 0 <= index < self._stack.count():
            host = self._stack.widget(index)
            key = host.objectName().removeprefix("settings_").removesuffix("_host")
            self._pivot.setCurrentItem(key)
            # 切页时**按当前页内容调一次高度**（用户明确要求保留这个行为）。
            # 只在切页这一刻调——手动拖动高度时绝不回弹（见 resizeEvent）。
            host.sync_minimum_height()
            self._fit_height_to_page()

    def _fit_height_to_page(self) -> None:
        """把窗口高度调到「当前页刚好装得下」，不超过屏幕可用高度。

        只在**切页**时调用（用户主动行为），**不在 resizeEvent 里调用**
        ——2026-10-05 实测：放进 resizeEvent 会导致用户拖不动高度
        （「请求 544 → 被弹回 424」）。

        高度上限取屏幕可用高度的 92%：装不下时由该页自己的滚动区负责，
        保证任何一页都能完整访问，又不会让「短页」被最高页撑出空白滚动条
        （那正是 2026-10-05 真机反馈的第二个回归）。
        """
        host = self._stack.currentWidget()
        if not isinstance(host, _PageHost):
            return
        # 先把页面最小高度对齐到内容高度，再据此算目标高度——顺序不能反：
        # 目标高度来自内容测量，而最小高度决定内容装不下时能否滚到底。
        host.sync_minimum_height()
        need = host.content_height()
        chrome_h = self._chrome_height()
        screen = self.screen() or QApplication.primaryScreen()
        avail = int(screen.availableGeometry().height() * 0.92) if screen else 900
        target = min(max(need + chrome_h, self._MIN_H), avail)
        if abs(target - self.height()) > 1:
            self.resize(self.width(), target)

    def _chrome_height(self) -> int:
        """除内容区以外占掉的高度（标题 + Pivot + 按钮行 + 边距 + 间距）。

        为什么不用「``self.height() - self._stack.height()``」实测差值
        ------------------------------------------------------
        那个差值只在几何**已收敛**时才对。构造期（``__init__`` 末尾的
        ``_apply_initial_height``）弹窗尚未 ``show``，实测差值会退化：

        - 第一次测量：stack 与弹窗都还是默认 30px → 差值 **0**
          → ``target`` 少了整整一个 chrome（实测 165px），弹窗被定矮。
        - 第二次测量：布局已收敛 → 差值 165，正常。

        Windows 上第二次调用会自我修正，Linux/CI 上不一定——所以不能靠
        「后面还有一次会修好」。改成**结构式**测量：直接按根布局里除stack
        之外各项的 sizeHint 累加，任何时候都成立。

        实测两者在显示后**完全相等**（结构式 165 = 实测差值 165），
        说明这不是又一个近似，而是把同一个量换个更可靠的算法算出来。
        """
        root = self.layout()
        if root is None:
            return 0
        margins = root.contentsMargins()
        total = margins.top() + margins.bottom()
        count = root.count()
        for i in range(count):
            item = root.itemAt(i)
            if item.widget() is self._stack:
                continue  # 内容区本身，不算进 chrome
            total += item.sizeHint().height()
        total += root.spacing() * max(0, count - 1)
        return total

    def _apply_initial_height(self) -> None:
        """首次打开给一个体面的初始高度（按第一页内容）。

        之后切页由 ``_fit_height_to_page`` 接手，用户手动拖动则完全尊重用户。
        """
        self._fit_height_to_page()

    def _apply_width_bounds(self) -> None:
        """按「最宽页的 minimumSizeHint」设定弹窗宽度下限。

        为什么需要（2026-10-05 真机实测）：视口宽 = 弹窗宽 - 根布局边距(22*2)
        - **垂直滚动条**。若视口窄于某页的 minimumSizeHint（云端 ASR 页实测 560，
        由模型行 ComboBox 的 340 + 「刷新清单」按钮共同撑起），该页右边会被
        横向裁掉。这里让弹窗**至少**能容纳最宽页，从源头消除裁切，
        比「出了滚动条让用户自己拖」体验好得多。

        这条下限同时是 ``_credible_measure_width`` 的**不变量前提**：只要
        「视口宽 ≥ 各页自身 minimumSizeHint 宽」恒成立，按「页最小宽」测量
        就是**安全的高估**（越窄→换行越多→越高），绝不会低估导致裁切。
        所以滚动条宽度必须算进来——见 ``_CHROME_W`` 的说明。
        """
        widest = 0
        for i in range(self._stack.count()):
            host = self._stack.widget(i)
            page = host.page if isinstance(host, _PageHost) else host
            widest = max(widest, page.minimumSizeHint().width())
        need_w = widest + self._chrome_width()
        # 别把上限也顶穿：留至少 40px 的可拖动余量，否则用户没法拖宽。
        self._MIN_W = max(self._MIN_W, need_w)
        self._MAX_W = max(self._MAX_W, self._MIN_W + 40)
        self.setMinimumWidth(self._MIN_W)
        self.setMaximumWidth(self._MAX_W)

    def _chrome_width(self) -> int:
        """视口外的横向占用 = 根布局左右留白 + 垂直滚动条宽。

        为什么滚动条宽要用 ``PM_ScrollBarExtent`` 现取而不是写死 14
        ------------------------------------------------------
        2026-10-06 复查实测：原先 ``_CHROME_W = 22*2+4 = 48`` 漏掉了滚动条
        （实测差值 58 = 44 + 14），使最窄时视口 550 < ``cloud_asr`` 页最小宽
        560，打破 ``_credible_measure_width`` 依赖的不变量（危险带 10px）。
        滚动条宽度随平台与样式变化，Linux 上未必是 14，故现取。

        取不到样式（``style()`` 为 None）时退回 0：宁可少算一点，
        也别因样式查询失败把弹窗撑得比内容还宽。
        """
        extent = 0
        style = self.style()
        if style is not None:
            extent = style.pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        return self._CHROME_W + extent

    @staticmethod
    def _form_page() -> tuple[QWidget, QFormLayout]:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(8, 14, 8, 8)
        f.setHorizontalSpacing(18)
        f.setVerticalSpacing(13)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return w, f

    def _build_asr_tab(self) -> None:
        w, f = self._form_page()
        asr = self._prefs.asr

        self._context_edit = LineEdit(w)
        self._context_edit.setText(asr.context)
        self._context_edit.setClearButtonEnabled(True)
        self._context_edit.setPlaceholderText("专有名词、热词、背景提示（可为空）")
        f.addRow("识别上下文 / 热词", self._context_edit)

        self._max_new_tokens = SpinBox(w)
        self._max_new_tokens.setRange(64, 8192)
        self._max_new_tokens.setValue(asr.max_new_tokens)
        f.addRow("最大生成 token 数", self._max_new_tokens)

        self._extract_vocals_cb = CheckBox(w)
        self._extract_vocals_cb.setText("默认启用人声提取 (Kim_Vocal_2 自动剥离伴奏)")
        self._extract_vocals_cb.setChecked(getattr(asr, "extract_vocals", False))
        f.addRow("", self._extract_vocals_cb)

        self._return_words = CheckBox(w)
        self._return_words.setText("保留字级时间戳（逐字导出必须）")
        self._return_words.setChecked(asr.return_word_timestamps)
        f.addRow("", self._return_words)

        self._use_cache = CheckBox(w)
        self._use_cache.setText("启用 KV cache（通常保持开启）")
        self._use_cache.setChecked(asr.use_cache)
        f.addRow("", self._use_cache)
        f.addRow(hint_label("归属指引：识别语言 / 对齐后端 / 人声提取在主工具栏即时切换（本页仅默认项）；逐字高亮样式与 k-tag 在右侧导出侧栏；ASS 文字样式在其专属弹窗——各处修改都会持久化到同一份 preferences.json。"))
        self._add_page("asr", "识别与对齐", w)

    def _build_cloud_asr_tab(self) -> None:
        """云端 ASR 分区（独立出于 ui/settings/cloud_asr_tab.py）。

        该页把偏重的逻辑（读缓存 / 后台抓取 / 探活）封装在自己的控件里，
        这里只做挂载与取值，对话框本身不必知道 SiliconFlow 的任何细节。
        """
        self._cloud_page = CloudASRPage(self._prefs, self._stack)
        self._add_page("cloud_asr", "云端 ASR", self._cloud_page)

    def _build_segmentation_tab(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 14, 8, 8)
        lay.setSpacing(12)

        seg = self._prefs.segmentation
        asr = self._prefs.asr

        # ── 全局默认上限（asr.max_sentence_chars / max_sentence_sec）────────
        # 引擎语义：无论下方按语言开关是否启用，这两条全局上限始终生效
        # （按语言条目仅在启用且 >0 时覆盖对应语言）。此前该组参数只能改
        # json，且页内文案谎称「关闭=保留原始切分」——现与真实行为对齐。
        g_form = QFormLayout()
        g_form.setVerticalSpacing(12)
        self._g_max_chars = SpinBox(w)
        self._g_max_chars.setRange(0, 200)
        self._g_max_chars.setSpecialValueText("不限制")
        self._g_max_chars.setValue(int(asr.max_sentence_chars))
        g_form.addRow("全局单句最大字数", self._g_max_chars)
        self._g_max_sec = DoubleSpinBox(w)
        self._g_max_sec.setRange(0, 60)
        self._g_max_sec.setDecimals(1)
        self._g_max_sec.setSingleStep(0.5)
        self._g_max_sec.setSpecialValueText("不限制")
        self._g_max_sec.setSuffix(" 秒")
        self._g_max_sec.setValue(float(asr.max_sentence_sec))
        g_form.addRow("全局单句最大时长", self._g_max_sec)
        lay.addLayout(g_form)
        lay.addWidget(hint_label("全局上限对所有语言始终生效（0 = 不限制）；下方按语言条目启用后可覆盖对应语言。"))

        self._seg_enabled = CheckBox(w)
        self._seg_enabled.setText("启用按语言分句上限（覆盖全局上限）")
        self._seg_enabled.setChecked(seg.enabled)
        lay.addWidget(self._seg_enabled)

        from .languages import SENTENCE_LANGUAGES
        self._lang_combo = ComboBox(w)
        for code, name in SENTENCE_LANGUAGES:
            if code:
                self._lang_combo.addItem(name, userData=code)
        self._lang_combo.currentIndexChanged.connect(self._on_seg_lang_changed)
        lay.addWidget(self._lang_combo)

        form = QFormLayout()
        form.setVerticalSpacing(12)
        self._max_chars = SpinBox(w)
        self._max_chars.setRange(0, 200)
        self._max_chars.setSpecialValueText("不限制")
        form.addRow("单句最大字数", self._max_chars)
        self._max_sec = DoubleSpinBox(w)
        self._max_sec.setRange(0, 60)
        self._max_sec.setDecimals(1)
        self._max_sec.setSingleStep(0.5)
        self._max_sec.setSpecialValueText("不限制")
        self._max_sec.setSuffix(" 秒")
        form.addRow("单句最大时长", self._max_sec)
        lay.addLayout(form)

        self._seg_lang_cache: dict[str, tuple[int, float]] = {
            k: (int(v.get("max_chars", 0) or 0), float(v.get("max_duration_sec", 0) or 0))
            for k, v in seg.per_lang.items()
        }
        if self._lang_combo.count() > 0:
            self._on_seg_lang_changed(0)
        lay.addWidget(hint_label("0 表示不限制。硬切只处理 ASR 给出的超长句，不会从一个词中间切开；短句合并与无标点回退的门槛在「高级」页。"))
        lay.addStretch(1)
        self._add_page("segmentation", "分句", w)

    def _on_seg_lang_changed(self, _idx: int) -> None:
        # 先保存离开语言前的当前值，再载入目标语言值。
        old_code = getattr(self, "_active_seg_lang", None)
        if old_code:
            self._seg_lang_cache[old_code] = (
                int(self._max_chars.value()), float(self._max_sec.value())
            )
        code = self._lang_combo.currentData()
        self._active_seg_lang = code
        c, s = self._seg_lang_cache.get(code, (0, 0.0))
        self._max_chars.blockSignals(True)
        self._max_sec.blockSignals(True)
        self._max_chars.setValue(c)
        self._max_sec.setValue(s)
        self._max_chars.blockSignals(False)
        self._max_sec.blockSignals(False)

    def _build_paths_tab(self) -> None:
        w, f = self._form_page()
        pt = self._prefs.paths
        self._ffmpeg_edit = LineEdit(w)
        self._ffmpeg_edit.setText(pt.ffmpeg_path)
        self._asr_model_edit = LineEdit(w)
        self._asr_model_edit.setText(pt.asr_model_path)
        self._aligner_model_edit = LineEdit(w)
        self._aligner_model_edit.setText(pt.aligner_model_path)
        self._vocal_model_edit = LineEdit(w)
        self._vocal_model_edit.setText(getattr(pt, "vocal_model_path", ""))
        self._mms_model_edit = LineEdit(w)
        self._mms_model_edit.setText(getattr(pt, "mms_aligner_model_path", ""))

        f.addRow("FFmpeg 路径", self._file_picker(self._ffmpeg_edit, "选择 ffmpeg 可执行文件"))
        f.addRow("ASR 模型目录", self._dir_picker(self._asr_model_edit))
        f.addRow("Qwen3 对齐器目录", self._dir_picker(self._aligner_model_edit))
        f.addRow("Kim_Vocal_2 模型文件", self._file_picker(self._vocal_model_edit, "选择 Kim_Vocal_2.onnx 文件 (*.onnx)"))
        f.addRow("MMS-FA ONNX 目录", self._dir_picker(self._mms_model_edit))
        f.addRow(hint_label("留空时自动检测并使用 constants.py 中的默认模型路径。"))
        self._add_page("paths", "路径", w)

    def _build_advanced_tab(self) -> None:
        """高级参数：此前只能手改 preferences.json 的推理细节，全部纳入 UI。

        设置弹窗从此覆盖 json 的全部可调项（除工具栏/导出侧栏各自维护的
        高频项），「偏好设置 ↔ preferences.json」一一对应。
        """
        w, f = self._form_page()
        asr = self._prefs.asr
        align = self._prefs.align

        f.addRow(hint_label("以下为推理细节参数，默认值经过调校，通常无需修改；改坏了可删 .config/preferences.json 恢复默认。"))

        # ── 无标点回退分句（ASR 未给标点时按理想时长切）────────────────
        self._fb_min_sec = DoubleSpinBox(w)
        self._fb_min_sec.setRange(0.5, 30.0)
        self._fb_min_sec.setDecimals(1)
        self._fb_min_sec.setSingleStep(0.5)
        self._fb_min_sec.setSuffix(" 秒")
        self._fb_min_sec.setValue(float(asr.fallback_min_sentence_sec))
        f.addRow("无标点回退·最短句时长", self._fb_min_sec)
        self._fb_max_sec = DoubleSpinBox(w)
        self._fb_max_sec.setRange(0, 60.0)
        self._fb_max_sec.setDecimals(1)
        self._fb_max_sec.setSingleStep(0.5)
        self._fb_max_sec.setSpecialValueText("不限制")
        self._fb_max_sec.setSuffix(" 秒")
        self._fb_max_sec.setValue(float(asr.fallback_max_sentence_sec))
        f.addRow("无标点回退·最长句时长", self._fb_max_sec)

        # ── 短句合并门槛（低于其一则并入下一句）────────────────────────
        self._min_chars = SpinBox(w)
        self._min_chars.setRange(0, 20)
        self._min_chars.setSpecialValueText("不合并")
        self._min_chars.setValue(int(asr.min_sentence_chars))
        f.addRow("短句合并·最小字数", self._min_chars)
        self._min_sec = DoubleSpinBox(w)
        self._min_sec.setRange(0, 5.0)
        self._min_sec.setDecimals(2)
        self._min_sec.setSingleStep(0.1)
        self._min_sec.setSpecialValueText("不合并")
        self._min_sec.setSuffix(" 秒")
        self._min_sec.setValue(float(asr.min_sentence_sec))
        f.addRow("短句合并·最小时长", self._min_sec)

        # ── 对齐裁剪窗留白（声学上下文；ASR 直通与重对齐共用语义）──────
        self._pad_before = DoubleSpinBox(w)
        self._pad_before.setRange(0, 1.0)
        self._pad_before.setDecimals(2)
        self._pad_before.setSingleStep(0.02)
        self._pad_before.setSuffix(" 秒")
        self._pad_before.setValue(float(align.pad_before))
        f.addRow("对齐窗·句首留白", self._pad_before)
        self._pad_after = DoubleSpinBox(w)
        self._pad_after.setRange(0, 1.0)
        self._pad_after.setDecimals(2)
        self._pad_after.setSingleStep(0.02)
        self._pad_after.setSuffix(" 秒")
        self._pad_after.setValue(float(align.pad_after))
        f.addRow("对齐窗·句尾留白", self._pad_after)

        # ── 超长句子切分（>5 分钟单句的兜底切分粒度）────────────────────
        self._subchunk_min = SpinBox(w)
        self._subchunk_min.setRange(1, 50)
        self._subchunk_min.setValue(int(align.subchunk_min_chars))
        f.addRow("超长句切分·最小字符数", self._subchunk_min)

        f.addRow(hint_label("留白同时用于 ASR 识别后的直通对齐与三种重对齐；重对齐窗口另有邻句锚与拖音前瞻逻辑（constants.py）。"))
        self._add_page("advanced", "高级", w)

    def _build_appearance_tab(self) -> None:
        w, f = self._form_page()
        self._theme_combo = ComboBox(w)
        self._theme_combo.addItem("深色", userData="dark")
        self._theme_combo.addItem("浅色", userData="light")
        cur = str(getattr(self._prefs, "ui_theme", "") or "dark")
        i = self._theme_combo.findData(cur)
        self._theme_combo.setCurrentIndex(i if i >= 0 else 0)
        f.addRow("主题", self._theme_combo)
        f.addRow(hint_label("界面由 PySide6-Fluent-Widgets 原生控件绘制；也可按 Ctrl+Shift+T 快速切换。"))
        self._add_page("appearance", "外观", w)

    def _file_picker(self, edit: LineEdit, caption: str) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(edit, 1)
        btn = PushButton(row)
        btn.setText("浏览…")
        btn.clicked.connect(lambda: self._pick_file(edit, caption))
        lay.addWidget(btn)
        return row

    def _dir_picker(self, edit: LineEdit) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(edit, 1)
        btn = PushButton(row)
        btn.setText("浏览…")
        btn.clicked.connect(lambda: self._pick_dir(edit))
        lay.addWidget(btn)
        return row

    def _pick_file(self, edit: LineEdit, caption: str) -> None:
        p, _ = QFileDialog.getOpenFileName(self, caption, edit.text() or "", "可执行文件 (*)")
        if p:
            edit.setText(p)

    def _pick_dir(self, edit: LineEdit) -> None:
        p = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or "")
        if p:
            edit.setText(p)

    def _collect_from_widgets(self, prefs: Preferences) -> None:
        """把控件值写入给定 prefs——只触碰本对话框拥有的字段组
        （asr 推理与分句参数 / 云端 ASR / segmentation / 路径 / 高级 / 主题），
        其余字段（工具栏维护的 asr.source_language、asr.asr_backend 与
        align.align_backend、导出侧栏维护的 style/export/ass_style、
        导出路径记忆等）一律不碰。"""
        asr = prefs.asr
        asr.context = self._context_edit.text().strip()
        asr.max_new_tokens = int(self._max_new_tokens.value())
        asr.return_word_timestamps = self._return_words.isChecked()
        asr.use_cache = self._use_cache.isChecked()
        asr.extract_vocals = self._extract_vocals_cb.isChecked()
        # 分句页·全局上限
        asr.max_sentence_chars = int(self._g_max_chars.value())
        asr.max_sentence_sec = float(self._g_max_sec.value())
        # 高级页
        asr.fallback_min_sentence_sec = float(self._fb_min_sec.value())
        asr.fallback_max_sentence_sec = float(self._fb_max_sec.value())
        asr.min_sentence_chars = int(self._min_chars.value())
        asr.min_sentence_sec = float(self._min_sec.value())
        # 对齐窗留白：ASR 直通与重对齐共用同一对语义值，成对同步
        prefs.align.pad_before = float(self._pad_before.value())
        prefs.align.pad_after = float(self._pad_after.value())
        asr.align_pad_before = prefs.align.pad_before
        asr.align_pad_after = prefs.align.pad_after
        prefs.align.subchunk_min_chars = int(self._subchunk_min.value())

        seg = prefs.segmentation
        seg.enabled = self._seg_enabled.isChecked()
        seg.per_lang = {
            k: {"max_chars": c, "max_duration_sec": s}
            for k, (c, s) in self._seg_lang_cache.items()
        }

        paths = prefs.paths
        paths.ffmpeg_path = self._ffmpeg_edit.text().strip()
        paths.asr_model_path = self._asr_model_edit.text().strip()
        paths.aligner_model_path = self._aligner_model_edit.text().strip()
        paths.vocal_model_path = self._vocal_model_edit.text().strip()
        paths.mms_aligner_model_path = self._mms_model_edit.text().strip()
        prefs.ui_theme = self._theme_combo.currentData() or _DEFAULT_THEME
        # 云端 ASR 分区（ui/settings/cloud_asr_tab.py）
        self._cloud_page.collect(prefs)

    def _on_save(self) -> None:
        # 分句页：把当前语言行的最新编辑先并入缓存（per_lang 由缓存整体重建）
        code = self._lang_combo.currentData()
        if code:
            self._seg_lang_cache[code] = (
                int(self._max_chars.value()), float(self._max_sec.value())
            )
        # 保持注入对象的旧契约：构造时传入的 prefs 随保存同步更新（调用方/测试可读回）
        self._collect_from_widgets(self._prefs)
        # 防字段漂移：以**磁盘最新偏好**为基底，只覆写本对话框自有字段后保存——
        # 不再把构造时的整份快照写回，避免抹掉打开对话框期间别处（如工具栏）的改动。
        disk = load_preferences()
        self._collect_from_widgets(disk)
        save_preferences(disk)
        self.accept()


__all__ = ["SettingsDialog"]
