"""ui.settings.cloud_asr_tab — 设置 → 云端 ASR 分区。

为什么独立成模块
----------------
``ui/settings_dialog.py`` 本就 400+ 行，若再把「表单取值 / 缓存策略 / 后台抓清单 /
零成本探活 / 说明渲染」这套东西塞进去，会远超 AGENTS.md §6.1 的
「单文件 ≳500 行且 ≥2 稳定子域 → 拆包」上限。这里把**表单**与**后台网络任务**
两个子域收在一处，设置对话框只负责挂载与取值。

三条不可协商的行为
------------------
1. **网络任务一律后台线程**。抓定价页和探活都是秒级 HTTP，放主线程会让设置窗僵住。
   这里用 Worker + Signal，**绝不**强杀线程。
2. **列表只列定价页标注免费的模型**，且**不再替用户做取舍**—— но 每个模型都带
   账本实证徽标（已实证免费 / 已知收费 / 未实测），把知情权还给用户。
3. **说明里不写死金额**。价格会随官方调价变动，写死的单价会比不写更误导；
   真正耐用的是「哪天实证过什么」+「去哪里查现价」，见 ``ModelInfo.note``。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, ComboBox, LineEdit, PasswordLineEdit, PushButton, isDarkTheme,
)

from core.app_config import Preferences, save_preferences
from core.cloud_asr import DEFAULT_BASE_URL, ENV_API_KEY, VERIFIED_ON, probe_models
from core.cloud_models import (
    MODEL_PRICING_URL,
    STATE_KNOWN_PAID,
    STATE_UNVERIFIED,
    STATE_VERIFIED_FREE,
    CatalogSnapshot,
    ModelInfo,
    list_asr_models,
    load_cache,
)
from ui.languages import code_to_name
from ui.widgets import hint_label

logger = logging.getLogger(__name__)

__all__ = ["CloudASRPage"]


def plain_text(md: str) -> str:
    """把可能含 Markdown 强调标记的说明压成纯文本。

    为什么需要（2026-10-05 用户真机反馈「云端模型说明里出现 ** 的 md 加粗格式」）
    ----------------------------------------------------------------------------
    ``ModelInfo.note`` 的文案写在 ``core/cloud_asr/facts.py`` 里，注释和文档习惯
    用 ``**强调**``，但设置页的说明标签是 ``Qt.TextFormat.PlainText``——**不解释
    Markdown**，于是星号被原样画到界面上。

    已在数据源去掉现存的几处，但那是**逐条手改**：下次新增模型时又带进来一次。
    所以在UI 边界统一压一次——数据源可以继续按注释习惯写强调，这里负责呈现。

    刻意只处理 ``**bold**`` 一种：不引入完整 Markdown 解析（无依赖、也不需要），
    未闭合的 ``**`` 直接原样保留（宁可显示出来，也不要静默吃掉半个词）。
    """
    out, i, n = [], 0, len(md)
    while i < n:
        if md.startswith("**", i):
            close = md.find("**", i + 2)
            if close > i + 2:                      # 有闭合的 **…** →只留内容
                out.append(md[i + 2:close])
                i = close + 2
                continue
        out.append(md[i])
        i += 1
    return "".join(out)


class _CloudTaskWorker(QThread):
    """后台跑一个网络任务，并把结果（或异常）emit 回主线程。

    异常不在这里「吞」成 None：调用方需要区分「清单是空的」和「抓取失败了」，
    所以异常原样送回 UI 线程去翻译成人话。
    """

    finished_with = Signal(object)

    def __init__(self, job: Callable[[], Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._job = job

    def run(self) -> None:  # noqa: D401 — Qt 约定
        try:
            result: Any = self._job()
        except Exception as exc:
            logger.warning("[cloud-asr-settings] 后台任务失败：%s", exc)
            result = exc
        self.finished_with.emit(result)


class CloudASRPage(QWidget):
    """云端 ASR 的表单分区。

    读取方式遵循「有缓存用缓存，没缓存自动刷新」：构造时若本地已有清单缓存就
    直接渲染（纯内存、零等待），否则后台补抓一次；用户随时可以点「刷新清单」。
    """

    def __init__(self, prefs: Preferences, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("cloud_asr_page")
        self._prefs = prefs
        self._models: list[ModelInfo] = []
        self._worker: Optional[_CloudTaskWorker] = None
        # 构造期不联网（见 _load_initial_models）：抓取推迟到显示之后。
        self._needs_fetch = False
        self._fetch_done = False
        self._closing = False
        self._build_ui()
        self._load_initial_models()

    # ── 构建 ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        cp = self._prefs.cloud_asr
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 14, 8, 8)
        root.setSpacing(12)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(13)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        root.addLayout(form)

        self._api_key = PasswordLineEdit(self)
        self._api_key.setText(cp.api_key)
        self._api_key.setPlaceholderText(f"留空则读取环境变量 {ENV_API_KEY}")
        self._api_key.setClearButtonEnabled(True)
        form.addRow("API Key", self._api_key)

        self._base_url = LineEdit(self)
        self._base_url.setText(cp.base_url or DEFAULT_BASE_URL)
        self._base_url.setClearButtonEnabled(True)
        form.addRow("Base URL", self._base_url)

        model_row = QWidget(self)
        ml = QHBoxLayout(model_row)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(8)
        self._model_combo = ComboBox(model_row)
        self._model_combo.setMinimumWidth(340)
        self._model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._model_combo.setToolTip("只列出定价页标注为免费的语音模型；每项前缀的徽标是账单实证结论")
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        ml.addWidget(self._model_combo, 1)
        self._refresh_btn = PushButton(model_row)
        self._refresh_btn.setText("刷新清单")
        self._refresh_btn.setToolTip(
            "重新抓取 SiliconFlow 定价页并更新本地缓存。\n"
            "平时不必手点——没有缓存时会自动抓。"
        )
        self._refresh_btn.clicked.connect(lambda: self._refresh_models(force=True))
        ml.addWidget(self._refresh_btn)
        form.addRow("识别模型", model_row)

        self._status_label = CaptionLabel(self)
        self._status_label.setWordWrap(True)
        form.addRow("清单状态", self._status_label)

        self._desc_label = BodyLabel(self)
        self._desc_label.setWordWrap(True)
        self._desc_label.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("模型说明", self._desc_label)

        self._cap_label = BodyLabel(self)
        self._cap_label.setWordWrap(True)
        form.addRow("实测能力", self._cap_label)

        self._lang_label = BodyLabel(self)
        self._lang_label.setWordWrap(True)
        self._lang_label.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("支持语种", self._lang_label)

        self._codec = ComboBox(self)
        self._codec.addItem("OPUS（推荐：体积小约 9 倍，实测识别结果一致）", userData="opus")
        self._codec.addItem("WAV（保真，体积大）", userData="wav")
        idx = self._codec.findData(cp.codec or "opus")
        self._codec.setCurrentIndex(idx if idx >= 0 else 0)
        self._codec.setToolTip("MP3 会引入额外错字，不要选它")
        form.addRow("上传编码", self._codec)

        self._test_result = BodyLabel(self)
        self._test_result.setWordWrap(True)
        form.addRow("连接测试", self._test_result)

        self._usage_label = BodyLabel(self)
        self._usage_label.setWordWrap(True)
        form.addRow("用量台账", self._usage_label)
        self._refresh_usage()

        row = QWidget(self)
        bl = QHBoxLayout(row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        self._test_btn = PushButton(row)
        self._test_btn.setText("测试连接")
        self._test_btn.setToolTip("只读取模型列表来验证 Key，**不产生任何费用**")
        self._test_btn.clicked.connect(self._on_test_connection)
        bl.addWidget(self._test_btn)
        price_btn = PushButton(row)
        price_btn.setText("查看官方价格")
        price_btn.setToolTip(f"打开 {MODEL_PRICING_URL}")
        price_btn.clicked.connect(self._open_pricing)
        bl.addWidget(price_btn)
        clear_btn = PushButton(row)
        clear_btn.setText("清零用量台账")
        clear_btn.setToolTip(
            "只把本机本 worktree 的本地累计清零，与云端账单无关；"
            "下次云端 ASR 调用会重新从 0 累加"
        )
        clear_btn.clicked.connect(self._on_clear_usage)
        bl.addWidget(clear_btn)
        bl.addStretch(1)
        root.addWidget(row)

        root.addWidget(hint_label(
            "本页会用到多少钱请自己留意：所选模型在上方「模型说明」里已标注账本实证结论。"
            "官方没有任何可编程的余额查询接口，所以价格请以控制台模型页为准。"
        ))
        env_hit = "（环境变量已设置，可留空）" if os.environ.get(ENV_API_KEY, "").strip() else ""
        root.addWidget(hint_label(
            f"API Key 以明文保存在本机 preferences.json（该文件已在 .gitignore 内，不会入库）"
            f"{env_hit}。若在意，请留空并改用环境变量 {ENV_API_KEY}。"
        ))
        root.addStretch(1)

    # ── 清单加载 ──────────────────────────────────────────

    def _load_initial_models(self) -> None:
        """有缓存就用缓存（零等待），没缓存才后台补抓一次。

        抓取**不在构造期启动**，而是推迟到 :meth:`start_if_idle`（由对话框的
        ``showEvent`` 触发）。原因：构造期起线程会让「打开设置弹窗」这个纯 UI
        动作变成一个随时可能未完成的网络任务——用户往往几百毫秒后就关了弹窗，
        线程仍在跑就被连带析构，Qt 直接 abort（进程级崩溃，不是 Python 异常）。
        用户没打开云端 ASR 页时也永远不会发这个请求。
        """
        if load_cache():
            self._apply_snapshot(list_asr_models())
        else:
            self._status_label.setText("本地还没有模型清单缓存，打开本页后自动抓取…")
            self._needs_fetch = True

    def start_if_idle(self) -> None:
        """弹窗 show() 之后调用：确有需要才补抓清单（见 :meth:`_load_initial_models`）。"""
        if self._needs_fetch and not self._fetch_done:
            self._fetch_done = True
            self._refresh_models(force=False)

    def shutdown(self) -> None:
        """关闭弹窗前调用：**合作式**收尾，标记不再发起新任务。

        不强杀线程（项目硬约束）。已在跑的那个抓取/探活由它自己跑完；这里只是
        保证回调不会再去操作已关闭的控件。
        """
        self._closing = True

    def _refresh_models(self, force: bool) -> None:
        if self._closing or (self._worker is not None and self._worker.isRunning()):
            return
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("刷新中…")
        self._status_label.setText("正在抓取定价页…")
        self._worker = _CloudTaskWorker(
            lambda: list_asr_models(force_refresh=force), self)
        self._worker.finished_with.connect(self._on_models_result)
        self._worker.start()

    def _on_models_result(self, result: Any) -> None:
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("刷新清单")
        if isinstance(result, Exception):
            self._status_label.setText(
                f"抓取失败：{result}\n清单暂时不可用，请检查网络后点「刷新清单」重试。")
            return
        snap: CatalogSnapshot = result
        if snap.error and not snap.models:
            self._status_label.setText(
                f"抓取失败：{snap.error}\n请检查网络后点「刷新清单」重试。")
            return
        self._apply_snapshot(snap)

    def _apply_snapshot(self, snap: CatalogSnapshot) -> None:
        models = list(snap.models)
        self._models = models
        saved = (self._prefs.cloud_asr.model or "").strip()

        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for m in models:
            self._model_combo.addItem(f"{m.badge()} {m.id}", userData=m)
        self._model_combo.blockSignals(False)

        idx = next((i for i, m in enumerate(models) if m.id == saved), -1)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)
        elif saved:
            # 已保存的模型不在清单里（下架/改名/手改过偏好）——仍保留为一个可选项，
            # 但明确标注它未经验证，避免用户以为它在免费名单内。
            info = ModelInfo(
                id=saved, state=STATE_UNVERIFIED,
                note="该模型不在当前免费清单中（可能已下架或改名）。是否可用、是否收费"
                     f"请以「测试连接」与 {MODEL_PRICING_URL} 为准。",
            )
            self._model_combo.insertItem(0, f"[不在清单中] {saved}", userData=info)
            self._model_combo.setCurrentIndex(0)

        parts = []
        if snap.source == "live":
            parts.append("已刷新")
        else:
            parts.append("本地缓存" + (f"（{snap.snapshot_date}）" if snap.snapshot_date else ""))
        counts = (
            f"共 {len(models)} 个 · 已实证免费 "
            f"{sum(1 for m in models if m.state == STATE_VERIFIED_FREE)} / "
            f"已知收费 {sum(1 for m in models if m.state == STATE_KNOWN_PAID)} / "
            f"未实测 {sum(1 for m in models if m.state == STATE_UNVERIFIED)}"
        )
        parts.append(counts)
        if snap.hint:
            parts.append(snap.hint)
        if snap.error:
            parts.append(f"上次刷新失败，当前为旧缓存：{snap.error}")
        self._status_label.setText(" · ".join(parts))
        self._on_model_changed()

    # ── 交互 ──────────────────────────────────────────────

    def _current_model(self) -> Optional[ModelInfo]:
        return self._model_combo.currentData()

    def _on_model_changed(self) -> None:
        info = self._current_model()
        if info is None:
            self._desc_label.setText("请选择一个模型。")
            self._cap_label.setText("—")
            self._lang_label.setText("—")
            return
        # 说明走 plain_text()：标签是 PlainText，不会解释 **，星号会被画到界面上。
        self._desc_label.setText(plain_text(info.note) or "暂无说明。")
        self._cap_label.setText(plain_text(info.capability) or "暂无实测能力数据")
        self._render_languages(info)
        # 未在账本里被实证过的模型给个醒目提示——金额不可信，但「有没有证据」可信。
        danger = info.state in (STATE_KNOWN_PAID, STATE_UNVERIFIED)
        color = "#F09595" if isDarkTheme() else "#A32D2D"
        self._desc_label.setStyleSheet(f"color: {color};" if danger else "")
        if danger:
            self._desc_label.setText(f"⚠ {info.state_label}：{plain_text(info.note)}")

    def _render_languages(self, info: ModelInfo) -> None:
        """渲染「支持语种」，并当场校验工具栏当前识别语言是否落在范围内。

        末尾那条校验才是本行真正的价值：模型不支持日语时，用户若把工具栏停在「日语」，
        与其等转写完再在对齐阶段炸掉，不如在这里就指出来。
        """
        if not info.languages:
            self._lang_label.setStyleSheet("")
            self._lang_label.setText("未知（该模型尚未做语言实测）")
            return

        names = "、".join(code_to_name(c) for c in info.languages)
        if info.languages_source == "verified":
            text = f"{names}（{VERIFIED_ON} 实测）"
        elif info.languages_source == "official":
            text = f"{names}（官方口径，本项目未实测）"
        else:
            text = names

        cur = (self._prefs.asr.source_language or "auto").strip()
        if cur not in ("auto", "") and cur not in info.languages:
            color = "#F09595" if isDarkTheme() else "#A32D2D"
            self._lang_label.setStyleSheet(f"color: {color};")
            text += f"\n⚠ 工具栏识别语言是「{code_to_name(cur)}」，不在该模型可用语种内"
        else:
            self._lang_label.setStyleSheet("")
        self._lang_label.setText(text)

    def _on_test_connection(self) -> None:
        if self._closing or (self._worker is not None and self._worker.isRunning()):
            return
        info = self._current_model()
        self._test_btn.setEnabled(False)
        self._test_btn.setText("测试中…")
        self._test_result.setText("正在验证 API Key（只读取模型列表，零费用）…")
        key = self._api_key.text().strip()
        base = self._base_url.text().strip() or DEFAULT_BASE_URL
        model = info.id if info is not None else ""
        self._worker = _CloudTaskWorker(
            lambda: probe_models(api_key=key, base_url=base, model=model), self)
        self._worker.finished_with.connect(self._on_probe_result)
        self._worker.start()

    def _on_probe_result(self, result: Any) -> None:
        self._test_btn.setEnabled(True)
        self._test_btn.setText("测试连接")
        if isinstance(result, Exception):
            self._test_result.setText(f"✘ 测试失败：{result}")
            return
        # ProbeResult
        mark = "✔" if result.ok else "✘"
        self._test_result.setText(f"{mark} {result.message}")

    def _refresh_usage(self) -> None:
        cp = self._prefs.cloud_asr
        total = float(cp.accumulated_seconds or 0.0)
        last = float(cp.last_usage_seconds or 0.0)
        self._usage_label.setText(
            f"累计调用 {total:.1f} 秒（约 {total / 60:.1f} 分钟）· 上次 {last:.1f} 秒\n"
            "这是客户端自己记的流水账：官方没有用量查询接口，它只是量级参照，"
            "既不等于账单金额，也覆盖不到别的设备上的调用。"
        )

    def _on_clear_usage(self) -> None:
        """把本地用量台账清零并立即落盘。

        只影响本机本 worktree 的 preferences.json，与云端账单无关；清零后下一次
        云端 ASR 调用会重新从 0 累加。立即 save_preferences 是为了不依赖用户是否
        点「确定」——即便直接关掉设置窗，清零也已持久化（对话框确定时会以磁盘
        最新值为基底再保存，不会把我刚写的 0.0 覆盖掉）。
        """
        cp = self._prefs.cloud_asr
        cp.accumulated_seconds = 0.0
        cp.last_usage_seconds = 0.0
        try:
            save_preferences(self._prefs)
        except Exception:
            logger.debug("[ASR] 清零用量台账后保存失败")
        self._refresh_usage()

    @staticmethod
    def _open_pricing() -> None:
        QDesktopServices.openUrl(QUrl(MODEL_PRICING_URL))

    # ── 取值 ──────────────────────────────────────────────

    def collect(self, prefs: Preferences) -> None:
        """把表单值写进给定 prefs（与 SettingsDialog 的其它分区同一契约）。"""
        cp = prefs.cloud_asr
        cp.api_key = self._api_key.text().strip()
        cp.base_url = self._base_url.text().strip() or DEFAULT_BASE_URL
        cp.codec = self._codec.currentData() or "opus"
        info = self._current_model()
        if info is not None:
            cp.model = info.id
