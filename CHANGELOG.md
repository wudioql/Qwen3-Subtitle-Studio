# Changelog

本文件按 [Keep a Changelog](https://keepachangelog.com/) 的思路记录可感知变化。项目当前没有 release tag、语义化版本号或打包产物；下列日期是开发快照，不等同于发布或目标机验收。现役机制以 [ARCHITECTURE.md](ARCHITECTURE.md) 和当前代码为准。

## [Unreleased]

### Unplanned

- 硬字幕烧录及其 UI。
- Nuitka 便携分发、安装包和对应 SBOM 流程。

## [2026-10-06] — Linux CI 三条失败的定位与修复

### Fixed

- **偏好设置弹窗：视口落在死区时内容底部永久不可达**（2026-10-06 Linux CI 两条失败的真正机制，**与平台无关、Windows 同样能复现**）。`QScrollArea` 在 `setWidgetResizable(True)` 下给页面的高度是 `max(视口高, 页面 minimumHeight)`，而 `QWidget.minimumSizeHint().height()` **不含 wordWrap 标签按宽换行多出来的那截**——实测缺口 asr 28px、cloud_asr 136px、segmentation 42px、advanced 42px。于是存在一个死区：视口高落在 `(minH, need)` 之间时，页面被压成**恰好视口高**（比 minH 高、比 need 矮），溢出的那截既没算进 minH 也不进滚动范围，`sb_max == 0`、底部怎么滚都看不到。实测 segmentation 页 need=353、视口 350 时 `reach=350 < 353`；advanced 页 need=423、视口 420 同样。修法：`ui/settings_dialog.py::_PageHost.sync_minimum_height()` 在每次切页时把页面 `minimumHeight` 对齐到 `content_height()`（各页 need 从 102 到 582不等，且拖宽度会改变换行行数，故不能只在构造期设一次）。实测危险区间扫描：3 页不可达 → **0 页**。
- **偏好设置弹窗：构造期在「不可能出现的宽度」上测量内容高度**。`__init__` 末尾的 `_apply_initial_height()` 触发时弹窗尚未 `show`，几何全是未收敛的默认值——实测 asr 页视口宽只有 **98px**，而该页自身 `minimumSizeHint().width()` 是 **348px**。98 不是「窄」而是「还没布局」，在这个宽度上 `heightForWidth` 的换行行数被严重高估：`content_height()` 返回 455，而该页任何真实状态都只需 ≤ 287；cloud_asr 页更离谱，98px 处算出 **3520**，真实上界 582（6 倍）。修法：`content_height()` 改经 `_credible_measure_width()`，宽度下限取 `page.minimumSizeHint().width()`——这**不是魔法阈值**，而是宽度下限已建立的不变量（弹窗宽度下限保证视口 ≥ 各页自身最小宽），低于它的宽度根本不可能出现。在该下限处测得的是所有可信状态里的**最大**高度（实测 6 页在可信区内 `need` 均随宽度单调不增），即宁可高估一点让滚动条出现，也不低估导致内容被裁。构造期 need 实测 **455 → 287**。
- **偏好设置弹窗：宽度下限漏算垂直滚动条，险些打破上一条依赖的不变量**（复查上述修复时发现）。宽度下限原先用 `22*2+4 = 48` 作为「视口外占用」，那个 `4` 是按无滚动条估的框宽；但最窄时**垂直滚动条必然可见**，实测拆解是 `弹窗 608 - 视口 550 = 58 = 留白 44 + 滚动条 14 + 余量 0`。于是 `cloud_asr` 页在弹窗宽 **608~617** 这 10px 带内视口宽 550 < 该页最小宽 **560**——上一条修复的前提被打破，按下限测量会转成**低估**（按更宽的 560 换行→行数更少→高度偏小）。本机字体下该带内高度恰好没差（低估 0px），**但那是运气**：换字体（Linux CI 就是）就可能跨过换行阈值变成真实裁切。修法：新增 `_chrome_width()` 用 `QStyle.PixelMetric.PM_ScrollBarExtent` **现取**滚动条宽（随平台/样式变化，Linux 上未必是 14，故不写死）；`style()` 为 None 时退回 0，避免样式查询失败反把弹窗撑宽。实测弹窗最小宽 608 → **618**，6 页危险带 **10px → 0**。
- **偏好设置弹窗：`chrome_h` 在构造期退化成 0**。原先用 `self.height() - self._stack.height()` 实测差值，而构造期两者都还是 Qt 默认 30px，差值为 **0**——目标高度少算整整一个 chrome（实测 165px），弹窗被定矮。Windows 上「后面还有一次调用」会自我修正，Linux 上不一定，故不能依赖补救。改为结构式测量 `SettingsDialog._chrome_height()`：按根布局里除 stack 外各项的 `sizeHint` 累加，任何时候都成立；实测显示后它与原实测差值**完全相等**（165 = 165），说明不是又一个近似，而是同一个量的更可靠算法。
- **测试可移植性**：`tests/test_cloud_asr.py::test_encoding_stage_reports_real_progress_from_ffmpeg` 在 Linux 上失败并非产品缺陷，而是**脚手架**问题——假 ffmpeg 用 `.bat` 启动器，Linux 不可执行（`Permission denied`），产品侧按设计静默降级为 WAV，于是断言 `codec == 'opus'` 挂掉。现按 `os.name == "nt"` 分支出 `.bat` 与 `#!/bin/sh` + `chmod(0o755)`（缺可执行位同样 Permission denied，不能只改扩展名）。
- 新增四条护栏（`tests/test_settings_dialog.py`）：`test_settings_dialog_construction_need_not_measured_at_unreal_width`（hook `_fit_height_to_page` 抓构造期那一帧，断言 need 不超过可信上界）、`test_settings_dialog_chrome_height_valid_before_show`（构造期 chrome 必须等于显示后实测差值）、`test_settings_dialog_overflow_reachable_in_dead_zone`（逐步压低视口扫遍死区，判据用纯函数 `_measure_page_height` 独立重算而非被测的 `content_height`）、`test_settings_dialog_viewport_never_below_page_min_width`（逐页逐宽度直接量真实几何，守住 `_credible_measure_width` 的地基不变量）。全部均已逐项「回退修复 → 确认变红 → 恢复」验证。

### Changed

- `AGENTS.md` §6.3 补两条约定：① **「本地全绿 ≠ CI 全绿」，且本地复现不出来时先怀疑「本地根本没走到那条路径」**——本轮两条护栏在 `show()` 之后断言，回退修复后照样passed（几何已收敛，测不到构造期缺陷），这是「恒真断言」的隐蔽版本：不是没写断言，而是断言点选在了 bug 消失之后；② **CI 失败要先分清产品 bug 与测试可移植性问题**，并记录 `.bat` / `chmod(0o755)` 这类可执行文件脚手架的跨平台写法。

## [2026-10-05] — 云端 ASR 接入与测试收尾

### Added

- **云端 ASR 后端（SiliconFlow）**：工具栏「识别后端」可切 `local`/`cloud`。云端只负责出文本，字级时间戳仍由本地强制对齐器产出，因此切换后端不改变时间轴精度；本地 1.7B 不进显存。新增云端 ASR 客户端 `core/cloud_asr/`（本快照内由单文件拆成包，见 Changed）、云端模型清单 `core/cloud_models.py`、设置页「云端 ASR」分区（API Key / Base URL / 模型 / 编码 / 零成本探活 / 用量台账）。
- 云端模型清单带**账单实证徽标**（已实证免费 / 已知收费 / 未实测），三方名单冲突时以账本为准。

### Changed

- 同一云端域的模块命名统一到 `cloud_` 前缀：`core/sf_models.py` → `core/cloud_models.py`、`tools/sf_free_models.py` → `tools/cloud_models_cli.py`（原名只提「免费模型」，实际按 UI 口径输出**全部** ASR 候选并带实证徽标，名不副实）。缓存文件随之改为 `.config/cloud_models.json`，旧名文件失效后首次启动会重新抓取一次定价页（只 GET 静态页，零费用）。
- ASR 引擎拆包：`core/asr_engine.py`（1208 行）→ `core/asr_engine/`，子模块 `config`（转写配置）/ `splitting`（切句文本层）/ `sentences`（文本或片段 → Sentence 列表）/ `pipeline`（主流程），依赖单向（config、splitting ← sentences ← pipeline），包入口只再导出。（语言短名不在 `config`，单一真源是 `core/language_utils.py`。）`transcribe` / `TranscribeConfig` / `_words_to_sentences` / `_split_text_by_punct` / `_attach_words_to_sentences` 等旧路径全部不变。同轮把三处 monkeypatch 目标从包入口改到 `core.asr_engine.pipeline`——`prepare_audio` / `align_full_text` 是在 `transcribe` 所在模块内解析的，打在包上不生效。
- 云端 ASR 客户端拆包：`core/cloud_asr.py`（1138 行，文件里本就用 `═══` 分好 7 段）→ `core/cloud_asr/`，子模块 `consts`/`facts`/`errors`/`types`/`language`/`encoding`/`client`，包入口只做再导出，`__all__` 与全部公开名不变（`from core.cloud_asr import transcribe_cloud` 原样可用）。同轮修掉测试里两处「哑弹」补丁：`test_cloud_asr.py` 打的 `ca.time.perf_counter` 从未被调用过（`transcribe_cloud` 只读 `time.strftime`）已删；`ca._http_post` / `ca.encode_for_upload` 改打在真正查找它们的 `core.cloud_asr.client` 上——打在包命名空间不生效，会让用例真的去发 HTTP。
- 播放器收包：`ui/` 根目录下 9 个播放器文件移入 `ui/player/`，包内去掉冗余的 `player_` 前缀——`player_panel.py`→`panel.py`、`player_stage.py`→`stage.py`、`player_qt_runtime.py`→`qt_runtime.py`、`player_focus_surface.py`→`focus_surface.py`、`player_subtitle_preview.py`→`subtitle_preview.py`，`mpv_backend.py` / `mpv_worker.py` / `subtitle_overlay.py` / `qt_media.py` 原名带入包。对外公开名不变，改从包入口取：`from ui.player import PlayerPanel, _VideoSubtitleStage, MpvWorker`。`ui/` 根目录 .py 文件从 24 个降到 15 个。
- 测试粒度：`tests/test_realign_window.py` / `tests/test_undo_commands.py` 由「单个 `*_pack` 聚合器 + `_case_*` 逐层转发」改回逐条可收集的 `test_*`（19 + 7 条，断言逐条未动），失败定位从 1 行变成逐条。同时删掉两处恒真断言（`test_punctuation.py` 的 set-then-assert、`test_app_smoke.py` 的 `m.__name__ == "core.X"`），并把 `test_cloud_asr.py` 里 `inspect.getsource` 的源码串断言、`test_mpv_worker.py` 里 panel↔stage 的 import 字面量断言改成行为断言。
- 同一处理推广到**全部** 28 个仍用该写法的测试文件：203 个 `_case_*` 测试体改名 `test_*`（末尾冗余的 `_pack` 一并去掉），58 个 `*_pack` 聚合器与 18 个纯转发的 `_case_*_pack` 包装层删除；2 个自身含逻辑、不引用任何 `_case_*` 的聚合器（`test_token_level_pack` / `test_fa2_fallback_pack`）去掉 `_pack` 后缀保留。收集数 211 → 358，`-m logic` / `-m ui` 的 marker 语义不变（`test_app_smoke.py` / `test_karaoke_template.py` / `test_subtitle_overlay.py` 无模块级 `pytestmark`，原来挂在聚合器上的 marker 已按传递闭包下发到每个叶子）。**核对方式不是「看起来没变」**：用 AST 比对新旧文件，`assert` 1160 → 1160、mock 断言 24 → 24 完全一致，顶层函数 353 → 277（差额正好是被删的 76 个纯转发函数），确认只删掉了透传层。
  副产品：两个长期被整体 deselect 的 `*_pack` 终于暴露出真实失败点——`test_project_models.py::test_media_paths_pack` 里只有 `test_media_paths_relativize` 真失败（同包的 `test_media_paths_resolve` / `test_save_load_roundtrip_relativizes_and_resolves` 一直是通过的，只是因为聚合器顺序执行、前一条抛错后面就不会跑），`test_toolbar.py::test_toolbar_ui_assembly_pack` 里只有 `test_panel_shrink_layout` 真失败。deselect 名单相应从 pack 名改为这两个叶子名。
- 全量测试耗时优化（116.5s → 89.3s，−23%；用例数与覆盖不变，仍是 356 passed / 2 deselected）：`core/mms_aligner/engine.py` 新增模块级 `_UROMAN_SHARED` 与 `_build_shared_uroman()`，uroman 实例改为跨 `MMSAligner` 实例共享。`uroman.Uroman()` 每次构造要解析约 2.7s 的罗马化词典，而它内部表只读、`romanize_string` 无状态，与 aligner 实例无绑定关系；原先 `test_mms_aligner.py::test_k1` 一个用例就重复构造 5 次（16.1s → 0.3s），`test_mms_aligner.py` 整文件 22.9s → 3.9s。`self._uroman` 的「None=未尝试 / False=失败哨兵 / 实例=可用」语义逐字未变（`test_mms_uroman_failure_sets_sentinel` 仍是原判据，只是先清掉新增的模块级缓存）。同轮去掉 `tests/test_splitter_theme.py` 里两处会让 `conftest.py` autouse 兜底再编译一次 QSS 的主题抖动（`test_splitter_theme.py` 20.4s → 15.7s）。
- 全量测试耗时第二轮优化（89.3s → 61s，仍 0 failed）：定位到真正的成本是 `ui/main_window/window.py` 构建末尾那次 `apply_theme`——`QApplication::setStyleSheet` 会向**所有存活控件**广播样式变更并逐个 repolish，而全量测试里顶层窗口只增不减（`close()` 不销毁 C++ 对象），于是每次 `MainWindow()` 都是 O(存活数)、整体退化成 O(N²)。现拆成两步：`ui/themes.py::apply_theme` 改为**幂等**（外壳 QSS 已是一套时跳过 `setStyleSheet`；`setTheme` / `_apply_palette` 仍每次都做，实测零成本且承载 qfluentwidgets 内部主题状态，跳过会留下主题与 QSS 不一致），本窗口的 polish 由新增的 `ui/themes.py::refresh_widget_style(self)` 单独补齐。该函数三个动作缺一不可（`unpolish`+`polish`、`QEvent.StyleChange`、`updateGeometry`；少了 `StyleChange` 几何不收敛），且**自带窗口级 QSS 的控件**（qfluentwidgets `TableWidget`/`TableView`，自身 `styleSheet()` 约 1742 字符）走 Python 侧 `unpolish` 会**必段错误**，故对这类控件只补事件与几何失效。实测：`MainWindow()` 构造 251 控件 0.323s / 6275 控件 0.227s（改前 0.30s → 1.09s，线性增长消失）；test_toolbar 35.4s → 5.1s、test_waveform_view 11.6s → 1.1s。护栏：`tests/test_splitter_theme.py::test_apply_theme_skips_reinstall_of_identical_shell_qss`（同主题重复应用不得调 `setStyleSheet`、主题变化必须调）与 `test_first_theme_toggle_does_not_change_layout`（几何不跳变）。
  **仍未解决**：Qt 顶层控件累积本身（330 → 7763）没有消除——主动销毁的三种写法（`deleteLater`+事件泵、`shiboken6.delete`、teardown 批量删顶层窗口）全部段错误，还挂死过一次。但**其代价已被解耦**：构造耗时不再随存活控件数增长，故不再影响测试时间。详见 `AGENTS.md` §6.3。
- 修复一处**顺序依赖**测试失败（此前记为独立问题）：`core.app_config.load_preferences` 有模块级 `_PREFS_CACHE`，而 UI 交互会经 `save_preferences` **同时**刷新缓存与磁盘——`test_toolbar::test_ui_toolbar_and_workflow_controller` 把 k-tag 档位切成 `k` 之后，`test_playback_responsiveness::test_mpv_subtitle_generation` 断言 `{\kf` 拿到的是 `{\k50}`。全量顺序里 playback 恰好排在 toolbar **之前**才没暴露，换成任意子集或乱序必失败。`tests/conftest.py` 新增 autouse 兜底 `_isolate_preferences_after_each_test`：每个测试后清空偏好缓存并删除落盘的 preferences.json，使每个测试都从默认偏好起步（与既有的 `_restore_qt_theme_after_each_test` 同一思路）。
- 收尾去重与命名对齐（纯重构，无行为变化；`ruff` 全绿、全量 0 failed）：① `ui/commands/base.py` 新增 `_SentenceEdgeCommand`，收拢 `EditTimeCommand` 与 `BoundaryDragCommand` **逐字相同**的 `redo`/`undo`——两者只有命令文案与 `new_start`/`new_end` 是否可为 None 不同，现在共用同一状态机，避免两份复制各自漂移；② `ui/widgets.py` 新增 `hint_label()`，取代 `ui/settings_dialog.py` 与 `ui/settings/cloud_asr_tab.py` 里各一份**逐字相同**的模块私有 `_hint`（拆包遗留）；③ 常量文件命名对齐 `core/constants.py` / `core/mms_aligner/constants.py`：`core/cloud_asr/consts.py` → `constants.py`（同步改包内 4 处 import、包入口再导出与文档）。
- 复核补正文档与代码的偏差（`ARCHITECTURE.md` 约定「冲突时以代码为准并同步文档」）：`AGENTS.md` §4 的 `core/` 清单补上遗漏的 `constants` / `language_utils` / `task_control`，`ui/` 清单补上 `player/` / `settings/` 两个包；`ARCHITECTURE.md` 更新基线从 `2026-08-30` 提到 `2026-10-05`；`README.md` 状态表快照同步到 `2026-10-05`（此前仍停在 `2026-08-30`，落后同批文档更新约五周），并按证据强度重排：新增「证据日期」列区分**当场重跑**（`compileall` / `ruff` / `pytest -q`）与**用户确认、CI 无法复现**的四行（本机 E2E、libmpv/libass、Aegisub/mpv.net、PotPlayer；用户 2026-10-05 重新确认各功能正常且模型未变动，故日期刷到当天），新增「云端 ASR 后端」行标 `implemented-pending`（有单测但依赖用户自备 Key，不在 `e2e/` 与 pytest 内）。`README.md` 项目定位的「识别」条目补上云端后端，原文只写本地 Qwen3-ASR-1.7B。
- `AGENTS.md` §6.3 清掉已失效的 deselect 约定：原条写着「当前两个基线失败」，一个已自愈、一个已定位为真bug 并修复，故名单归零（`pytest -q` = 361 passed / 0 failed）。改为一条**方法论约定**——不要把失败随手归为「平台/字体度量差异」就 deselect，判据必须与被测系统不同源，并要求 deselect 名单定期重评、记录复查期限；同时登记新护栏与「加护栏后必须回退修复验证其变红」的要求。
- 版本号排查结论：**项目无应用级版本号，本次无需更新**。仓库无 `setup.py`/`setup.cfg`、无 `git tag`、`pyproject.toml` 只有 ruff 配置（无 `[project]` 段）、三个包 `__init__.py` 均无 `__version__`。现存三个「version」都不是应用版本、**不要动**：`subs/models.py::PROJECT_SCHEMA_VERSION = 1`（工程文件 schema，只在破坏性变更时升）、`core/app_config.py::Preferences.version = 1`（普通格式标记，恒为 1，不做校验与迁移）、`pyproject.toml` 的 `target-version = "py312"`（Python 目标）。这与 `CHANGELOG.md` 开头「当前没有 release tag、语义化版本号或打包产物」的口径一致。

### Fixed

- **导出侧栏窄宽度下说明文字被真实截断**（此前被误记为「本机字体度量基线差异」并长期 deselect）：五张导出卡的 `QVBoxLayout` 缺 `QLayout.SetMinAndMaxSize`约束。压到 `ExportPanel.minimumSizeHint()` 的最小宽度 222px 时，`word_style` 卡实得 633 而需要 689，其中两个 `wordWrap` 标签实得 61 / 26，按 `QTextLayout` 手工换行真实需要 96 / 36——**文字确实放不下**。修法是给卡片内容布局加 `setSizeConstraint(QLayout.SetMinAndMaxSize)`（`ui/export_panel.py` 新增 `_card_layout_constraint`，须在 `QVBoxLayout(self)` 建好后调用，与既有 `_card_size_policy` 成对）。实测 W=200~340 连续扫描：截断点 **51 → 0**。**逐项排除过且均无效的方向**（勿重复试）：标签 `setMinimumWidth(0/1)`、vpolicy 改 `Minimum`/`Fixed` + `setHeightForWidth(True)`、`setMinimumHeight(1)`、`invalidate()+activate()` 多次、多泵事件、先宽后窄、调高面板高度、`ScrollArea.setWidgetResizable(False)`、改 inner 垂直策略——都不是缓存或时序问题。
- 新增两条护栏（`tests/test_toolbar.py`）：`test_export_sidebar_text_never_truncated_across_widths` 用**连续宽度扫描 + `QTextLayout` 手工换行**做外部判据（刻意不用 `label.heightForWidth()`——那条路径与被测布局同源，布局若以错误高度分配它会跟着错，断言就成了自证）；`test_export_cards_layout_constraint_is_paired_with_size_policy` 守护「`sizePolicy` 与布局约束成对出现」（漏掉后者不报错，只是窄侧栏悄悄回到截断状态）。两条均已用「回退修复 → 确认变红」验证有效性。
- **对齐阶段进度不再冻结**：ASR 主流程原先丢弃对齐器 `total=0` 的不确定进度，UI 长时间停在「激活对齐器...」；现在原样转发文案（仍不伪造百分比）。
- **重对齐/脏句对齐不再顺带改写无关句**：`apply_seam_snaps` 此前未限定范围，会改动锁定句与本轮未参与句的时间轴；现按本轮成功提交的 sid 限定（与全文对齐一致）。
- **MMS 上下文泄漏**：`align_sentence` 手写 `__enter__()` 在 `try` 之外，异常时 ONNX Session 不再销毁且嵌套深度永久偏移；已移入 `try`。
- **人声分离不再吞掉取消**：`except Exception` 会捕获 `TaskCancelled`（`RuntimeError` 子类），用户取消后仍静默回退为「成功」；现原样重抛。
- **人声分离不再伪造进度百分比**：模型加载/STFT/iSTFT 等不可知阶段改报 `total=0` + 已用时，仅帧块循环保留真实 done/total。

### Removed

- 删除 `core/ort_session.py` 兼容 façade（全仓无生产 import，生产侧一律走 `core/ort_cuda.py`）及其 3 个测试用例。
- 删除死类 `ui/player/subtitle_overlay.py::SubtitleOverlay`（已被 `player/stage` 的 `_VideoSubtitleStage` 取代，零引用）。
- 清理零引用符号：`cloud_asr.MAX_AUDIO_DURATION_SEC`、`free_model_ids()`、`cloud_models.dump_rows()`/`clear_cache()`、`asr_engine` 两个无人使用的 `text_utils` 别名。

## [2026-08-30] — 包化重构与文档标准化

### Added

- 标准化的项目文档入口：架构、Python/数据 API、贡献、部署、开发、故障排查和变更记录。
- 文档状态分级：`verified-current`、`implemented-pending`、`unplanned`、`historical`。

### Verified

- 用户确认本机完整 ASR/对齐 E2E 正常；由于根目录三件参考资源由本项目生成，该结果证明项目自身链路自洽，不是独立外部数据集精度基准。
- 用户确认本机 `libmpv` 测试通过并可正常使用。
- 用户确认各类字幕文件在 Aegisub、mpv.net 中实际测试均支持。
- 用户确认 PotPlayer 除 `\kf` 无法逐字扫过、只能整字亮外，其他字幕均正常。

### Changed

- 将上述本机验证结果与 Linux/无权重沙箱的验证边界分开记录。
- 明确硬字幕烧录和 Nuitka 分发目前没有规划、排期或验收标准，不再把它们标成当前待办或已排期项。

### Fixed

- 修复 CI Ruff 对 `tests/test_punctuation.py` 的未使用导入/变量，以及 `tests/test_subtitle_overlay.py` 中装饰字符回归代码作用域错误的报告。
- 修复 Qt 预览回归测试中临时 `QImage` 像素 buffer 比较和平台字体度量导致的脆弱断言。
- GitHub Actions 更新到 Node 24 运行时的 `actions/checkout@v7` 与 `actions/setup-python@v7`，消除旧 Node 20 兼容性警告。
- Ubuntu CI 补充 Qt Multimedia 所需的 `libpulse0`，修复 `QVideoFrame` 导入时缺少 `libpulse.so.0` 导致的 Pytest 失败。
- 将四个小型项目 E2E/模板参考夹具从通用忽略规则中排除，确保 GitHub Actions checkout 后能读取测试所需的根目录资源。

### Removed

- 删除已完成迁移、且不再作为现役真源的三个中文历史文档；内容以根目录标准文档为准。

## [2026-08-23] — 播放与文档收敛

### Added

- 播放器沉浸模式：Qt stage 与 mpv 点击入口统一，隐藏/恢复周边 UI 时不重新挂载原生 host。
- `PlayerPanel` 拆分为 façade、stage、字幕预览、Qt runtime、focus surface 和 Qt Multimedia adapter。
- 可选 libmpv/libass 预览、纯音频 force-window、字幕轨替换和 watchdog 回退。
- 卡拉 OK 模板应用器、标点独立基础定位、模板效果与基础 k-tag 预览分离。
- 工程 JSON 的导出三件套、跨机媒体路径和原子保存能力。

### Fixed

- 字幕正文/时间 Undo/Redo 后暂停帧刷新问题。
- Qt pause 缓冲尾音、模型状态显示、MMS Session 释放、依赖/授权文档口径。

## [2026-08-22] — 导出与验证整理

### Added

- 应用图标和媒体准备 Worker。
- 11 个导出入口中的应用模板后 ASS 产物。
- `tests/_env.py` 统一测试环境，logic/ui 门禁和目标机 E2E 脚本。

### Changed

- libmpv 作为可选后端；无 DLL 或失败时回退 Qt。
- ASS/LRC/标点/卡拉 OK 语义与实际导出器同步。

## [2026-08-20]

### Changed

- 工程相对媒体路径与 `media_path_hints`。
- MMS 上下文强制对齐、句尾标点零时间延伸、超长对齐分块和 Flash Attention 回退边界。
- 依赖声明、授权清单与 `requirements.txt` 的直接依赖合同。

## [2026-08-16]

### Added

- 卡拉 OK 参数化预览、句/字编辑、波形交互、工程保存/打开、ASS 样式和模板设置。
- `align_engine`、`mms_aligner`、`ui.commands`、主窗口、波形、句级视图和样式弹窗的包化拆分。

## [2026-08-09]

### Added

- 对齐器自动切块、Workflow/Project 控制器、临时文件清理以及字幕导入导出基础链路。

## [2026-08-07]

### Changed

- 切换到 Transformers 原生 Qwen3 API，移除旧 `qwen-asr` 路线和 VAD 依赖。
- 建立自研 `subs/` 字幕数据与格式层。

## [2026-08-06]

### Added

- 确定 Python 3.12、原生 Transformers、Qwen3 ASR/Aligner、PySide6 和 Worker 异步路线。
- 建立 `AGENTS.md`、依赖声明和项目级协作规则。
