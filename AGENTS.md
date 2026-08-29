# AGENTS.md — Qwen3-Subtitle-Studio

> AI 协作入口，不是第二份 README。产品入口见 [README.md](README.md)；现役架构与数据边界见 [ARCHITECTURE.md](ARCHITECTURE.md)；Python/工程/Signal 合同见 [API.md](API.md)；部署见 [DEPLOYMENT.md](DEPLOYMENT.md)；开发与验证见 [DEVELOPMENT.md](DEVELOPMENT.md)；故障排查见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)；历史变更见 [CHANGELOG.md](CHANGELOG.md)。原有中文迁移资料已完成迁移并清理，不是现役真源。

## 1. 定位

基于 Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B / MMS-FA ONNX 的本地字幕生成、歌词对齐与细粒度编辑桌面工具；目标 Windows 11 / RTX 4070 Laptop 8GB / Python 3.12。支持多语言 ASR、口语/歌曲双对齐、句/字级编辑、工程 JSON 存取、11 个导出入口。

## 2. 运行与验证

- 入口：项目内 `.venv` 下 `python main.py`（Windows）。
- 推理：torch 2.13+cu130、flash-attn 2.8.3、`transformers>=5.13,<6`、accelerate、onnxruntime（requirements 默认 CPU 版；目标机 GPU 按部署清单装 onnxruntime-gpu 覆盖）、uroman；**不要装 qwen-asr/qwen-audio**。
- UI：完整版 PySide6、PySide6-Fluent-Widgets、pyqtgraph；`python-mpv`（可选，配根目录 `libmpv-2.dll` 启用真 ASS 预览）。
- 音频：FFmpeg + librosa；权重在 `models/`（不入库）。
- 门禁：
  - `python tests\test_export_pipeline.py`
  - `python tests\test_undo_commands.py` / `test_project_models.py`
  - `ruff check .`（配置唯一真源 `pyproject.toml`）
  - `pytest`（数量随用例增减；`-m logic` / `-m ui`；真机见 `e2e/`）
  - `python tools\env_check_native_api.py`（零权重实例化；目标机发布前加 `--strict-target --require-models`）
  - 真机：`python e2e\e2e_short_talk.py`，唯一参考资产为根目录同名 `.mp4/.txt/.ass`；默认 Qwen+MMS 双后端并各导出 11 项（含应用模板后 ASS）。
  - 测试三件套唯一实现：`tests/_env.py`；`conftest.py` / `_bootstrap.py` 为薄包装；开发依赖见 `requirements-dev.txt`。

## 3. 技术硬约束

- ASR：`AutoModelForMultimodalLM`；口语对齐：`AutoModelForTokenClassification`；歌词：ONNX MMS-FA。ORT Provider/cuDNN/CPU 回退唯一实现是 `core/ort_cuda.py`；`ort_session.py` 仅旧导入兼容 façade，不得再复制逻辑。Windows `os.add_dll_directory` handle 必须进程级持有。
- ASR 出字后直通全局对齐；`return_word_timestamps` 只决定是否保留 `Sentence.words`。
- 显存：ASR/对齐互斥激活。**Qwen park→RAM**；**MMS 任务结束销毁 ONNX Session**（勿只 `empty_cache`）。峰值目标 ≤4.5GB。
- MMS 后处理：频谱平坦度/RMS 每次 align 只预计算一次；CTC 使用滚动 score + uint8 回溯并受 512MiB 预算约束，帧不足/无法完整到终态必须报错，禁止返回部分路径。
- 人声分离：`last_run_separated=False` 的原音频回退不得写入确定性 `vocals_` 缓存；UI 使用 `allow_fallback=False`。整段 MDX STFT 预计工作集 >3GiB 时先于 Session 加载拒绝并回原音频。
- 句级 start/end 与最外层 WordTimestamp（含首/尾标点）双向绑定：拖句界同步外层 word；编辑首/尾 word 后用 `fix_times_from_words` 同步句界。内部字界不改变句长。波形拖动仍须先改预览副本、完成后才进 UndoCommand。
- 波形只创建 n-1 个内部字界手柄；两个句级手柄就是首/尾 word 外边界，禁止再叠加重复外侧字手柄。句块区分 dirty/confirmed/locked，并按 sid 恢复重排后的当前句。
- 卡拉OK模板最多启用一条，也允许全不选；旧多选偏好只保留第一条。全不选只导出基础 k-tag，不得回退默认模板。
- k-tag 语义：`\\k/\\ko` 本来就是到点瞬变，`\kf` 与大写 `\\K` 才是字内左→右填充；播放器可降级渲染；本机实测中 PotPlayer 除 `\kf` 不能逐字扫过、只能整字亮外，其余字幕均正常，不能把这一播放器限制归因于项目导出失败。基础颜色来自 ASS Secondary→Primary，不读取逐字黄色高亮。
- Aegisub k-tag ASS 的 Project Garbage 必须写实际 `source_media_path/video_path`（视频音频均优先原媒体），禁止硬编码 `?dummy` 覆盖已有媒体路径或写入会清理的 `.temp` 音频。
- 逐字预览必须从 `Sentence.text` 补回未进入 words 的 `♪♫♬♩`/空白等装饰字符，显示但不独立动画。
- 对齐提交采用事务语义：先在临时结果完成切句/清洗，**非空成功才覆盖原 words 并清 dirty**；失败或空产出保留旧字级与脏标记。
- >300s 单句子切必须保证每次严格缩小且每块 ≤300s；无自然边界时走字符边界，无法形成非空块则明文报错，禁止递归原问题。
- 全文/单句收尾：`apply_seam_snaps`（前句尾→后句首 ≤25ms 吸附 + 后句首→前句尾小重叠吸附，对称；批处理仅修改本轮成功提交句）。
- MMS 单句/脏句重对齐走**上下文强制对齐**（`align_with_context`：邻句文本一并交给对齐器吸收前句尾音/后句开口，只取本句字词）；邻句为脏句时退化为孤对齐。FA2 加载失败仅对 FA2 类错误回退 SDPA，其它异常原样抛出。
- 标点：原位显示、无独立逐字动效；卡拉OK模板的颜色/缩放/描边/fad 等效果也不得作用于标点。Applied ASS 从模板 fx 正文剥离 Unicode 标点：坐标 provider 可用时，每个标点段以基础样式 `\an5\pos` 独立定位并与模板字共用同一字体度量坐标；仅无坐标时回退整句透明遮罩。模板 `$start/$end` 必须钳到真实非标点 word 的 start/end，不得把后续停顿或标点时长算入前字效果。Qt fallback 必须切出 `is_punct` 段并保持基础字体、颜色、描边、不透明度与基础 x 锚点。Enhanced LRC 单开始时间且须能读回自身纯文本；句末标点**零时间延伸**（end 紧贴前字，不制造句间重叠、不侵占真实停顿）；`strip_trailing_punct` 批量删句尾标点（字符+时间）**不标脏**、锁定句跳过、句中标点不动。
- 导出正文与格式标签分流：SRT/VTT 先 HTML escape；ASS 正文统一 `escape_ass_text`，字段统一 `sanitize_ass_field`，仅显式 tag payload 可作为 markup。禁止直接拼用户文本。
- `.txt` 是纯文本合同（数字行/`-->` 不得过滤）；SRT/VTT/LRC/ASS/TXT 经 UI 导入后统一 `is_dirty=True`，原时间/字级仍保留。
- Undo：命令快照 `_old_dirty`，撤回还原脏/锁；非 Undo 操作（ASR/对齐/导入/重关联）必须显式标记工程未保存。
- 后台取消为**合作式安全点**：不得强杀 QThread/模型前向；Worker 统一发 `cancelled`，所有成功/失败/取消最终都由 `QThread.finished` 恢复 UI。关闭时任务未停则延后销毁模型。
- 进度：模型权重 I/O/GPU 搬运/单次 forward 用 `total=0` 不确定进度 + 已用时，禁止伪造百分比；句/语言段/MMS 音频块用真实 done/total。ModelState 包含 loading/activating，进度回调必须同步刷新右下角状态。
- 第六档“所选模板效果”只预览模板 Apply 后生成的 fx，**视觉上不得叠加基础 k-tag 扫过**；应用后 ASS 必须保留 template Comment 与 `Comment/effect=karaoke` 原 k-tag，生成行必须是 `Dialogue/effect=fx`。`template syl` 每条模板行只含自身，禁止旧式“每 syllable 一份 `alpha&HFF` 整句副本”；标点使用无动画的基础样式独立定位 fx（无坐标 API 才允许每句一条透明遮罩保底）。未设 noblank 时保留 kara[0]，全部源 Comment 排在追加的 fx 前。Qt 兼容路径只近似 form 的 fad/color/scale/glow/anchor；坐标按 alignment/margin/pixel font/ScaleX/Spacing 计算但仍允许字体度量容差。任意 Lua 不执行，仅安全变量与 `$var±number`；其它明确降级。
- 卡拉OK“高亮变色”的稳定语义是**原色 → 高亮色**，默认白色 → 黄色。为兼容旧工程/偏好，JSON 键 `color_restore` 继续保存原色，现役代码/UI 不得再把它显示或解释成“回落色”。效果弹窗打开时定位唯一启用项；全不选才回落第一项。
- 第五档基础 k-tag 必须实时读取 export.k_tag_mode：`kf/K` 扫过、`k` 瞬变、`ko` 瞬变且未唱字无描边；下拉变化立即刷新 PlayerPanel。
- 波形普通滚轮向上必须增加 Y 视野中心；Ctrl/Shift/Alt 既有方向不变。近重合的前句 end / 后句 start 始终是两条独立边界：无论鼠标命中哪条，向左拖只改前句尾，向右拖只改后句首；禁止再用共享命令同步两句，确保可主动拉开间隙。
- 工程 JSON：`schema_version=1`（**不 bump**，读宽松/写严格；仅破坏性变更才升版本）+ 有限时间/唯一 sid/布尔规整校验；同目录临时文件 `fsync` 后 `os.replace` 原子保存。媒体路径**相对化优先解析**（工程目录内转相对 + `media_path_hints` 兜底；跨 OS 绝对路径——Windows 盘符/POSIX 根——原样保留不误拼工程目录；相对路径统一正斜杠；畸形 `media_path_hints` 忽略）；跨机复现三件套（`ass_style_data`/`karaoke_template_data`/`export_settings`）随工程保存、打开时应用，预览模式/主题不入工程。媒体重关联只改媒体字段，不得清字幕。
- 依赖：项目直接 import 的包必须直接列入 `requirements.txt` 并限定主版本；Torch/flash-attn 仍先按部署清单单装。禁止恢复“transitive 自带所以不声明”或“未来 5.x 永远兼容”说法。
- 播放预览：`ui/player_panel.py` 是保持旧 API 的 façade（须维持 <500 行）；同画布绘制在 `player_stage.py`，画面点击/媒体门禁在 `player_focus_surface.py`，字幕生成/mpv 回调在 `player_subtitle_preview.py`，Qt 软解/首帧预卷在 `player_qt_runtime.py`，可选 QtMultimedia 导入唯一真源为 `qt_media.py`。`QVideoSink` 与字幕**同画布**绘制（不用 QVideoWidget，避免 Windows HWND 盖字幕）；`main.py` 启动前禁用 FFmpeg 硬解设备列表。mpv 后端为**唯一 python-mpv 接入点 `ui/mpv_backend.py`**（顶层不得 import mpv）；任何 import/初始化/播放/seek/字幕/terminate 原生调用都必须经 `ui/mpv_worker.py` daemon worker，GUI 线程只非阻塞入队、绝不 join，命令须有 watchdog + Qt 自动回退，time-pos 须限频。UI 事件循环启动后异步预热 mpv，使未导入媒体时也能显示“初始化/已就绪”；测试用 `QSS_DISABLE_MPV=1` 禁止真实 native worker。mpv 可接管视频与纯音频：纯音频必须用 `force-window` 建空白 VO、禁用封面图，并在该画布用 libass 真渲染字幕；Qt 只作 mpv 不可用/超时回退。路由必须看 active backend 而非媒体类型或 mpv 对象是否存在。mpv host 原生化前必须设置 `WA_DontCreateNativeAncestors`，应用启动前设置 `AA_DontCreateNativeWidgetSiblings`，禁止 HWND 属性扩散到 Fluent ComboBox/Popup。已加载媒体时单击画面切换应用内沉浸模式：Qt stage 直接覆写 `mousePressEvent/mouseReleaseEvent` 发 `clicked`；Windows `--wid` 下 mpv native 路径由 `mpv_backend.py` 设置 `input_vo_keyboard=True`、关闭默认 bindings 并强制绑定 `MOUSE_BTN0`。`player_focus_surface.py` 只汇合 Qt clicked 与 mpv binding，不做平台级鼠标轮询；底部保持播放/暂停/停止三键居中。禁止只监听 host QWidget、恢复 self-event-filter 或叠加临时沉浸按钮；`main_window/player_focus.py` 只能保存 splitter 状态并隐藏/恢复兄弟面板、菜单、工具栏和状态栏，**禁止 setParent/reparent 播放器或原生 host**；再次单击或 Esc 恢复。播放/暂停/停止位于画面下方独立居中控制条，预览模式/后端为顶部右对齐定宽紧凑组，禁止悬浮在 mpv 原生 HWND 上。字幕经按 track id 管理的 `sub-add` 临时文件交给 libass 真渲染；替换成功后须在 worker 内执行非致命的相对精确 seek 0，强制暂停帧立即重绘，不能要求用户切换预览类型。正文、句时间、字时间及增删拆并的 Undo/Redo 回调必须调用 `PlayerPanel.refresh_subtitle_content()`：Qt 侧废弃字幕像素缓存，mpv 侧重建当前字幕轨。预览字幕由继承的 `_render_preview_subtitle` 复用导出唯一真源 `render_export` 生成（档位→kind 映射 `_MPV_PREVIEW_KIND`）。
- Qt fallback 暂停：`_silence_qt_pause_buffer()` 必须先于 `QMediaPlayer.pause()` 同步静音，播放/自动恢复前调用 `_restore_qt_pause_audio()` 还原用户原静音值；`_end_priming()` 只有真实 priming 时才能恢复预卷音量，禁止普通 pause/stop 误取消暂停静音。

## 4. 当前实现（摘要）

- `core/`：`model_manager`、`asr_engine`、`align_engine/`、`mms_aligner/`、`vocal_separator`、`audio_io`、`text_utils`、`app_config`、`ort_cuda`、`temp_cleanup`。
- `subs/`：模型 + 导入导出 + ASS/卡拉OK 模板（无 pysubs2）。
- `ui/`：包 `main_window/`、`commands/`、`waveform_view/`、`sentence_level_view/`、`ass_style_dialog/`；播放器 façade + focus/stage/subtitle/Qt runtime 子域及主窗沉浸模式；控制器 workflow/project；导出侧栏；设置/卡拉OK 弹窗。
- `workers/`：`TranscribeWorker`；`AlignWorker` 仅 `sentences` | `dirty` | `full`（无 `mode=project`）。
- 菜单含 **打开/保存工程、重新关联媒体**；`.json/.qss.json` 可拖放并复用打开工程完整逻辑。重新关联媒体与普通打开共用默认人声提取流程。
- 破坏性替换/退出有中文“保存工程 / 不保存 / 取消”门禁；析构期 cleanChanged 不得重新访问已删除 QUndoStack。导入字幕后须先设句语言再手动对齐。
- `speaker` 字段仅数据/导出透传，**UI 未产品化**。
- 应用图标已接入：`assets/icon.png`（512² 透明源）+ `assets/icon.ico`（16–256 多尺寸），`main.py` 启动时 `setWindowIcon`；缺失时静默降级不影响启动。
- libmpv 真 ASS 预览已接入（**可选后端，本机已实测通过**）：根目录 `libmpv-2.dll` + `python-mpv` 均在位时异步启用，视频与纯音频均可用 libass 预览；纯音频由 force-window 提供空白画布。缺依赖、初始化/命令失败或 watchdog 超时则回退 QMediaPlayer + QPainter 兼容预览。后端无手动开关。用户已确认本机完整 E2E 正常，且各类字幕文件在 Aegisub/mpv.net 中实际测试均支持；PotPlayer 除 `\kf` 不能逐字扫过、只能整字亮外，其余字幕均正常。PlayerPanel 已拆为 479 行 façade + focus surface/stage/subtitle/Qt runtime/QtMultimedia adapter，旧 `PlayerPanel` 与 `_VideoSubtitleStage` 导入继续兼容；播放器支持不重挂 HWND 的应用内沉浸模式。模板特效由 **Python 应用器 `subs/karaoke_templater.py`** 展开：template/karaoke Comment、kara[0]、syl/line/char、fx Dialogue 与 furigana 样式已按用户 Aegisub golden 钉样；坐标仍是 QFontMetrics 近似，任意 Lua 不执行（仅安全变量与 `$var±number` 子集）。
- **未规划**：硬字幕烧录、Nuitka 分发；当前没有明确规划、排期或验收标准，不得写成当前待办或既定路线。
- 许可证：项目自有代码为 GPL-3.0（GPL 本身不限制商业使用）；默认运行组合因 PySide6-Fluent-Widgets 双许可和 MMS CC-BY-NC-4.0 模型而定位个人/非商业。商业部署须另购 GUI 商业许可并替换/复核非商业模型；唯一清单见 `THIRD_PARTY_NOTICES.md`，不得再写“GPLv3 本身仅非商业”。

## 5. 当前优先级

1. 维护已由用户确认的本机完整 E2E、libmpv、Aegisub、mpv.net 与 PotPlayer 兼容性回归；其中 PotPlayer 的已知限制仅为 `\kf` 不能逐字扫过、只能整字亮。
2. 硬字幕烧录与 Nuitka 分发当前没有明确规划，不列为现行优先级；若未来重新提出，先建立范围、设计和验收标准，再决定是否进入路线图。

## 6. 协作约定

### 6.1 拆包约定

- 单文件 ≳500 行且 ≥2 稳定子域 → 改包；`__init__.py` **再导出**旧公开名；可 patch 符号经包入口查找。
- 不拆：≲300 行单一控制器、`subs/models`、纯 QSS 大文件。
- 禁止为分类做 `ui/dialogs/` 式大搬家。

### 6.2 其它

- 改 UI 先核对 `subs/models.py`；推理不进主线程。
- 权威文档：`README.md`（入口与范围）、`ARCHITECTURE.md`（现役机制与边界）、`API.md`（Python/工程/Signal 合同）、`DEPLOYMENT.md`（目标机安装与验收）、`DEVELOPMENT.md`（开发与测试）、`TROUBLESHOOTING.md`（症状排查）、`CHANGELOG.md`（历史变更）、`THIRD_PARTY_NOTICES.md`（授权）。同一事实不要复制成长篇平行版本；原有中文迁移资料已清理。
- 审查报告、逐批改动对照和累计补丁 ZIP 只属于临时协作交付物，不进入正式项目；若未来再次生成，收尾时按 `.agents/skills/neat-freak/` 先汇报再清场。
