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

- ASR：`AutoModelForMultimodalLM`；口语对齐：`AutoModelForTokenClassification`；歌词：ONNX MMS-FA。ORT Provider/cuDNN/CPU 回退唯一实现是 `core/ort_cuda.py`（原 `ort_session.py` 兼容 façade 已删除：全仓无生产 import，留着只会让「谁是真源」模糊）。Windows `os.add_dll_directory` handle 必须进程级持有。
- ASR 出字后直通全局对齐；`return_word_timestamps` 只决定是否保留 `Sentence.words`。
- 显存：ASR/对齐互斥激活。**Qwen park→RAM**；**MMS 任务结束销毁 ONNX Session**（勿只 `empty_cache`）。峰值目标 ≤4.5GB。
- MMS 后处理：频谱平坦度/RMS 每次 align 只预计算一次；CTC 使用滚动 score + uint8 回溯并受 512MiB 预算约束，帧不足/无法完整到终态必须报错，禁止返回部分路径。
- 人声分离：`last_run_separated=False` 的原音频回退不得写入确定性 `vocals_` 缓存；UI 使用 `allow_fallback=False`。整段 MDX STFT 预计工作集 >3GiB 时先于 Session 加载拒绝并回原音频。**人声缓存命名合同的唯一真源是 `core.vocal_separator.vocals_cache_path()`**（前缀 `constants.VOCALS_CACHE_PREFIX`，`.temp` 清理与 UI 守卫共用同一份，禁止各自拼字面量）；判断「当前音频轨是否已是人声」必须走 `is_vocals_track_of()`，**禁止**用 `audio_path != source_media_path`——`audio_path` 的语义是「已提取的 16kHz mono WAV」，容器媒体经 FFmpeg 降采样后同样满足该不等式，会把提取件误判成人声轨（任何视频一导入就报「无需重复提取」）。
- 句级 start/end 与最外层 WordTimestamp（含首/尾标点）双向绑定：拖句界同步外层 word；编辑首/尾 word 后用 `fix_times_from_words` 同步句界。内部字界不改变句长。波形拖动仍须先改预览副本、完成后才进 UndoCommand。
- 波形只创建 n-1 个内部字界手柄；两个句级手柄就是首/尾 word 外边界，禁止再叠加重复外侧字手柄。句块区分 dirty/confirmed/locked，并按 sid 恢复重排后的当前句。
- 卡拉OK模板最多启用一条，也允许全不选；旧多选偏好只保留第一条。全不选只导出基础 k-tag，不得回退默认模板。
- k-tag 语义：`\\k/\\ko` 本来就是到点瞬变，`\kf` 与大写 `\\K` 才是字内左→右填充；播放器可降级渲染；本机实测中 PotPlayer 除 `\kf` 不能逐字扫过、只能整字亮外，其余字幕均正常，不能把这一播放器限制归因于项目导出失败。基础颜色来自 ASS Secondary→Primary，不读取逐字黄色高亮。
- Aegisub k-tag ASS 的 Project Garbage 必须写实际 `source_media_path/video_path`（视频音频均优先原媒体），禁止硬编码 `?dummy` 覆盖已有媒体路径或写入会清理的 `.temp` 音频。
- 逐字预览必须从 `Sentence.text` 补回未进入 words 的 `♪♫♬♩`/空白等装饰字符，显示但不独立动画。
- 对齐提交采用事务语义：先在临时结果完成切句/清洗，**非空成功才覆盖原 words 并清 dirty**；失败或空产出保留旧字级与脏标记。
- >300s 单句子切必须保证每次严格缩小且每块 ≤300s；无自然边界时走字符边界，无法形成非空块则明文报错，禁止递归原问题。
- 全文/单句收尾：`apply_seam_snaps`（前句尾→后句首 ≤25ms 吸附 + 后句首→前句尾小重叠吸附，对称；批处理仅修改本轮成功提交句）。
- MMS 单句/脏句重对齐走**上下文强制对齐**（`align_with_context`：邻句文本一并交给对齐器吸收前句尾音/后句开口，只取本句字词）；邻句为脏句时退化为孤对齐。邻句语言经 `prev_language`/`next_language` 传入，保证邻句 token 的罗马化读音（数字拼读 / 日语 pykakasi）用对语言。FA2 加载失败仅对 FA2 类错误回退 SDPA，其它异常原样抛出。
- 标点：原位显示、无独立逐字动效；卡拉OK模板的颜色/缩放/描边/fad 等效果也不得作用于标点。Applied ASS 从模板 fx 正文剥离 Unicode 标点：坐标 provider 可用时，每个标点段以基础样式 `\an5\pos` 独立定位并与模板字共用同一字体度量坐标；仅无坐标时回退整句透明遮罩。模板 `$start/$end` 必须钳到真实非标点 word 的 start/end，不得把后续停顿或标点时长算入前字效果。Qt fallback 必须切出 `is_punct` 段并保持基础字体、颜色、描边、不透明度与基础 x 锚点。Enhanced LRC 单开始时间且须能读回自身纯文本；句末标点**零时间延伸**（end 紧贴前字，不制造句间重叠、不侵占真实停顿）；`strip_trailing_punct` 批量删句尾标点（字符+时间）**不标脏**、锁定句跳过、句中标点不动。
- 导出正文与格式标签分流：SRT/VTT 先 HTML escape；ASS 正文统一 `escape_ass_text`，字段统一 `sanitize_ass_field`，仅显式 tag payload 可作为 markup。禁止直接拼用户文本。
- `.txt` 是纯文本合同（数字行/`-->` 不得过滤）；SRT/VTT/LRC/ASS/TXT 经 UI 导入后统一 `is_dirty=True`，原时间/字级仍保留。
- Undo：命令快照 `_old_dirty`，撤回还原脏/锁；非 Undo 操作（ASR/对齐/导入/重关联）必须显式标记工程未保存。
- 后台取消为**合作式安全点**：不得强杀 QThread/模型前向；Worker 统一发 `cancelled`，所有成功/失败/取消最终都由 `QThread.finished` 恢复 UI。关闭时任务未停则延后销毁模型。
- **云端 ASR**（`core/cloud_asr/`，可选后端；`client`=HTTP 传输 / `language`=语言决议 / `encoding`=上传编码 / `facts`=计费账本 / `types`·`errors`·`constants`，包入口只做再导出）：识别后端二选一，`asr_backend="cloud"` 时**只把「取文本」换成 HTTP 调用**，字级时间戳仍 100% 由本地对齐器产出（官方不支持 `verbose_json`，`response_format=verbose_json` → 400）。云端分支**绝不能碰 `model_manager`**——一旦踏进 `using_asr()`，本地 1.7B 就已被搬进显存，省显存目标当场作废；有源码级反回归测试守着。默认模型 `FunAudioLLM/SenseVoiceSmall`（免费模型里唯一覆盖中/英/日/粤），上传用 OPUS 32k（MP3 会引入错字）。HTTP **402 = 余额不足，绝不重试**；仅 429/5xx/网络异常退避重试。云端失败**不自动回落本地**（回落会静默吃满显存）。
- **云端计费信息以账单实证为准**：官方没有任何可编程的价格/余额查询接口，运行时无法判断模型是否收费，只能依据 `VERIFIED_FREE_MODELS` / `PAID_MODELS` 账本；三方名单冲突时账本优先（`PAID_MODELS` > `VERIFIED_FREE_MODELS` > 定价页）。UI 说明里**不写死金额**（价格会过期），只给实证日期 + 查价去处。测试连接必须是零成本探活 `GET /v1/models`。
- 逐句语种判定：ASR 只能返回单一 `language`，混说素材（实测本地 Qwen3 26.66s → `language='Chinese'`，内含 12s 纯英文）里**必须逐句判**，否则外语句会被送进错误语言的对齐器。**本地与云端路径都要开**（`asr_engine/pipeline.py` 的 `per_sentence_lang`），不是只有云端。判据与适用范围唯一真源是 `core/cloud_asr/language.py::split_zh_en_sentence_language`：① 中/粤整段→切出**纯英文**句（有汉字即沿用整段，绝不把粤语误标成中文普通话）；② **日/韩整段→同样切出英文句**（判据不同：**不挡汉字**，因日语 Kanji 与汉字同码区，`日本語勉強中` 实测 `han=6/kana=0`；只挡假名/谚文/西里尔）。中/粤↔日/韩互判、俄语等**一律不做**（用户拍板：这些混入形式不常见，宁可漏判也不猜错语种污染时间轴）。逐字字级标注是更彻底的解法；对齐侧已按 Qwen **分词器族**收敛分段（`core/language_utils.tokenizer_family`：中/粤/英/法/德等共用官方默认「CJK 逐字 + 空格词」分词器，合并为一次调用即可；只有 ja/ko 与其它语言同段混排才真正需要分开调用）。**分段窗只用真实时间**：族冲突走两阶段（阶段一取整段音频得真实时间 → 阶段二按真实时间裁窗），绝不拿句级**占位时间**（ASR/纯文本导入的字符占比均分）去裁音频。回归判据见 `tools/check_ja_ko_en_split.py`（42 项矩阵）+ `tests/test_cloud_asr.py` 的两条护栏 + `tests/test_full_align_real_time_crop.py`。
- 进度：模型权重 I/O/GPU 搬运/单次 forward/云端 HTTP 等待用 `total=0` 不确定进度 + 已用时，禁止伪造百分比；句/语言段/MMS 音频块/编码帧块用真实 done/total。ModelState 包含 loading/activating，进度回调必须同步刷新右下角状态——**`total=0` 的进度也必须转发**，不得丢弃（否则 UI 会长时间停在上一句文案上）。
- 第六档“所选模板效果”只预览模板 Apply 后生成的 fx，**视觉上不得叠加基础 k-tag 扫过**；应用后 ASS 必须保留 template Comment 与 `Comment/effect=karaoke` 原 k-tag，生成行必须是 `Dialogue/effect=fx`。`template syl` 每条模板行只含自身，禁止旧式“每 syllable 一份 `alpha&HFF` 整句副本”；标点使用无动画的基础样式独立定位 fx（无坐标 API 才允许每句一条透明遮罩保底）。未设 noblank 时保留 kara[0]，全部源 Comment 排在追加的 fx 前。Qt 兼容路径只近似 form 的 fad/color/scale/glow/anchor；坐标按 alignment/margin/pixel font/ScaleX/Spacing 计算但仍允许字体度量容差。任意 Lua 不执行，仅安全变量与 `$var±number`；其它明确降级。
- 卡拉OK“高亮变色”的稳定语义是**原色 → 高亮色**，默认白色 → 黄色。为兼容旧工程/偏好，JSON 键 `color_restore` 继续保存原色，现役代码/UI 不得再把它显示或解释成“回落色”。效果弹窗打开时定位唯一启用项；全不选才回落第一项。
- 第五档基础 k-tag 必须实时读取 export.k_tag_mode：`kf/K` 扫过、`k` 瞬变、`ko` 瞬变且未唱字无描边；下拉变化立即刷新 PlayerPanel。
- 波形普通滚轮向上必须增加 Y 视野中心；Ctrl/Shift/Alt 既有方向不变。近重合的前句 end / 后句 start 始终是两条独立边界：无论鼠标命中哪条，向左拖只改前句尾，向右拖只改后句首；禁止再用共享命令同步两句，确保可主动拉开间隙。
- 工程 JSON：`schema_version=1`（**不 bump**，读宽松/写严格；仅破坏性变更才升版本）+ 有限时间/唯一 sid/布尔规整校验；同目录临时文件 `fsync` 后 `os.replace` 原子保存。媒体路径**相对化优先解析**（工程目录内转相对 + `media_path_hints` 兜底；跨 OS 绝对路径——Windows 盘符/POSIX 根——原样保留不误拼工程目录；相对路径统一正斜杠；畸形 `media_path_hints` 忽略）；跨机复现三件套（`ass_style_data`/`karaoke_template_data`/`export_settings`）随工程保存、打开时应用，预览模式/主题不入工程。媒体重关联只改媒体字段，不得清字幕。
- 依赖：项目直接 import 的包必须直接列入 `requirements.txt` 并限定主版本；Torch/flash-attn 仍先按部署清单单装。禁止恢复“transitive 自带所以不声明”或“未来 5.x 永远兼容”说法。
- 播放预览：`ui/player/panel.py` 是保持旧 API 的 façade（须维持 <500 行）；同画布绘制在 `player/stage.py`，画面点击/媒体门禁在 `player/focus_surface.py`，字幕生成/mpv 回调在 `player/subtitle_preview.py`，Qt 软解/首帧预卷在 `player/qt_runtime.py`，可选 QtMultimedia 导入唯一真源为 `player/qt_media.py`。`QVideoSink` 与字幕**同画布**绘制（不用 QVideoWidget，避免 Windows HWND 盖字幕）；`main.py` 启动前禁用 FFmpeg 硬解设备列表。mpv 后端为**唯一 python-mpv 接入点 `ui/player/mpv_backend.py`**（顶层不得 import mpv）；任何 import/初始化/播放/seek/字幕/terminate 原生调用都必须经 `ui/player/mpv_worker.py` daemon worker，GUI 线程只非阻塞入队、绝不 join，命令须有 watchdog + Qt 自动回退，time-pos 须限频。UI 事件循环启动后异步预热 mpv，使未导入媒体时也能显示“初始化/已就绪”；测试用 `QSS_DISABLE_MPV=1` 禁止真实 native worker。mpv 可接管视频与纯音频：纯音频必须用 `force-window` 建空白 VO、禁用封面图，并在该画布用 libass 真渲染字幕；Qt 只作 mpv 不可用/超时回退。路由必须看 active backend 而非媒体类型或 mpv 对象是否存在。mpv host 原生化前必须设置 `WA_DontCreateNativeAncestors`，应用启动前设置 `AA_DontCreateNativeWidgetSiblings`，禁止 HWND 属性扩散到 Fluent ComboBox/Popup。已加载媒体时单击画面切换应用内沉浸模式：Qt stage 直接覆写 `mousePressEvent/mouseReleaseEvent` 发 `clicked`；Windows `--wid` 下 mpv native 路径由 `player/mpv_backend.py` 设置 `input_vo_keyboard=True`、关闭默认 bindings 并强制绑定 `MOUSE_BTN0`。`player/focus_surface.py` 只汇合 Qt clicked 与 mpv binding，不做平台级鼠标轮询；底部保持播放/暂停/停止三键居中。禁止只监听 host QWidget、恢复 self-event-filter 或叠加临时沉浸按钮；`main_window/player_focus.py` 只能保存 splitter 状态并隐藏/恢复兄弟面板、菜单、工具栏和状态栏，**禁止 setParent/reparent 播放器或原生 host**；再次单击或 Esc 恢复。播放/暂停/停止位于画面下方独立居中控制条，预览模式/后端为顶部右对齐定宽紧凑组，禁止悬浮在 mpv 原生 HWND 上。字幕经按 track id 管理的 `sub-add` 临时文件交给 libass 真渲染；替换成功后须在 worker 内执行非致命的相对精确 seek 0，强制暂停帧立即重绘，不能要求用户切换预览类型。正文、句时间、字时间及增删拆并的 Undo/Redo 回调必须调用 `PlayerPanel.refresh_subtitle_content()`：Qt 侧废弃字幕像素缓存，mpv 侧重建当前字幕轨。预览字幕由继承的 `_render_preview_subtitle` 复用导出唯一真源 `render_export` 生成（档位→kind 映射 `_MPV_PREVIEW_KIND`）。
- Qt fallback 暂停：`_silence_qt_pause_buffer()` 必须先于 `QMediaPlayer.pause()` 同步静音，播放/自动恢复前调用 `_restore_qt_pause_audio()` 还原用户原静音值；`_end_priming()` 只有真实 priming 时才能恢复预卷音量，禁止普通 pause/stop 误取消暂停静音。

## 4. 当前实现（摘要）

- `core/`：`model_manager`、`asr_engine/`（config 配置 / splitting 切句文本层 / sentences 句子构造 / pipeline 主流程，入口只再导出）、`align_engine/`（config 配置 / common 裁剪·接缝·语言决议·分词预检 / sentence 单句 / words 语言决议+字级切回+事务提交原语 / chunking 长媒体切块机制 / full 全文重对齐策略 / project 整项目按句或仅脏句，入口只再导出）、`mms_aligner/`、`vocal_separator`、`audio_io`、`text_utils`、`app_config`、`constants`（路径/时长/采样率等常量与 `TEMP_DIR`，`QSS_TEMP_DIR` 可重定向）、`language_utils`（11 种对齐语言的短名↔全名单一真源；`asr_engine` 与 `align_engine` 都从这里取，避免互相 import）、`task_control`（合作式取消安全点）、`ort_cuda`、`temp_cleanup`、`cloud_asr/`（云端 ASR 客户端，仅标准库；client/language/encoding/facts/types/errors/constants 七子模块，入口只再导出）、`cloud_models`（云端模型清单/计费实证账本）。
- `subs/`：模型 + 导入导出 + ASS/卡拉OK 模板（无 pysubs2）。
- `ui/`：包 `main_window/`、`commands/`、`waveform_view/`、`sentence_level_view/`、`ass_style_dialog/`、`player/`、`settings/`；播放器 façade + focus/stage/subtitle/Qt runtime 子域及主窗沉浸模式；控制器 workflow/project；导出侧栏；设置/卡拉OK 弹窗（`settings/` 目前只收云端 ASR 分区，其余仍在 `settings_dialog.py`）。
- `workers/`：`TranscribeWorker`；`AlignWorker` 仅 `sentences` | `dirty` | `full`（无 `mode=project`）。
- 菜单含 **打开/保存工程、重新关联媒体**；`.json/.qss.json` 可拖放并复用打开工程完整逻辑。重新关联媒体与普通打开共用默认人声提取流程。
- 破坏性替换/退出有中文“保存工程 / 不保存 / 取消”门禁；析构期 cleanChanged 不得重新访问已删除 QUndoStack。导入字幕后须先设句语言再手动对齐。
- `speaker` 字段仅数据/导出透传，**UI 未产品化**。
- 应用图标已接入：`assets/icon.png`（512² 透明源）+ `assets/icon.ico`（16–256 多尺寸），`main.py` 启动时 `setWindowIcon`；缺失时静默降级不影响启动。
- libmpv 真 ASS 预览已接入（**可选后端，本机已实测通过**）：根目录 `libmpv-2.dll` + `python-mpv` 均在位时异步启用，视频与纯音频均可用 libass 预览；纯音频由 force-window 提供空白画布。缺依赖、初始化/命令失败或 watchdog 超时则回退 QMediaPlayer + QPainter 兼容预览。后端无手动开关。用户已确认本机完整 E2E 正常，且各类字幕文件在 Aegisub/mpv.net 中实际测试均支持；PotPlayer 除 `\kf` 不能逐字扫过、只能整字亮外，其余字幕均正常。PlayerPanel 已拆为 <500 行 façade + focus surface/stage/subtitle/Qt runtime/QtMultimedia adapter，旧 `PlayerPanel` 与 `_VideoSubtitleStage` 导入继续兼容；播放器支持不重挂 HWND 的应用内沉浸模式。模板特效由 **Python 应用器 `subs/karaoke_templater.py`** 展开：template/karaoke Comment、kara[0]、syl/line/char、fx Dialogue 与 furigana 样式已按用户 Aegisub golden 钉样；坐标仍是 QFontMetrics 近似，任意 Lua 不执行（仅安全变量与 `$var±number` 子集）。
- **未规划**：硬字幕烧录、Nuitka 分发；当前没有明确规划、排期或验收标准，不得写成当前待办或既定路线。
- 许可证：项目自有代码为 GPL-3.0（GPL 本身不限制商业使用）；默认运行组合因 PySide6-Fluent-Widgets 双许可和 MMS CC-BY-NC-4.0 模型而定位个人/非商业。商业部署须另购 GUI 商业许可并替换/复核非商业模型；唯一清单见 `THIRD_PARTY_NOTICES.md`，不得再写“GPLv3 本身仅非商业”。

## 5. 当前优先级

1. 维护已由用户确认的本机完整 E2E、libmpv、Aegisub、mpv.net 与 PotPlayer 兼容性回归；其中 PotPlayer 的已知限制仅为 `\kf` 不能逐字扫过、只能整字亮。
2. 硬字幕烧录与 Nuitka 分发当前没有明确规划，不列为现行优先级；若未来重新提出，先建立范围、设计和验收标准，再决定是否进入路线图。

## 6. 协作约定

### 6.1 拆包约定

- 单文件 ≳500 行且 ≥2 稳定子域 → 改包；`__init__.py` **再导出**旧公开名（`__all__` 一并搬过去）。**patch 要打在「名字被查找的地方」**：包入口只是再导出的引用，对「定义在子模块里、按本模块 globals 解析」的函数无效——例：`monkeypatch.setattr(core.cloud_asr.client, "_http_post", …)` 生效，打在包上不生效；反之调用点写 `from .cloud_asr import transcribe_cloud` 懒加载的，就要打在包上。
- 不拆：≲300 行单一控制器、`subs/models`、纯 QSS 大文件。
- 禁止为分类做 `ui/dialogs/` 式大搬家。判据不是「数量」而是**形状**：`ui/player/`、`ui/waveform_view/`、`ui/sentence_level_view/`（一个控件 + 只被簇内引用的内部件）与 `core/cloud_asr/`、`core/asr_engine/`（分层栈：`__init__` 是唯一 façade，子模块单向依赖，包外只碰 `__init__`）都允许；把散落在各处的对话框按类别归堆则不允许。

### 6.2 其它

- 改 UI 先核对 `subs/models.py`；推理不进主线程。
- 权威文档：`README.md`（入口与范围）、`ARCHITECTURE.md`（现役机制与边界）、`API.md`（Python/工程/Signal 合同）、`DEPLOYMENT.md`（目标机安装与验收）、`DEVELOPMENT.md`（开发与测试）、`TROUBLESHOOTING.md`（症状排查）、`CHANGELOG.md`（历史变更）、`THIRD_PARTY_NOTICES.md`（授权）。同一事实不要复制成长篇平行版本；原有中文迁移资料已清理。
- 审查报告、逐批改动对照和累计补丁 ZIP 只属于临时协作交付物，不进入正式项目；若未来再次生成，收尾时按 `.agents/skills/neat-freak/` 先汇报再清场。

### 6.3 测试约定

- **每个断言体必须自身可被 pytest 收集**：测试函数一律以 `test_` 开头，不要写 `_case_*` 助手 + `test_xxx_pack` 聚合器的「N 合 1」写法，也不要写只做转发、不带断言的中间层。原因有三：① 失败只报聚合器名，定位不到具体场景；② 聚合器是**顺序执行**的，前一条抛错后面几条根本不会跑，于是被跨过的用例长期无人验证；③ `_` 开头的函数 pytest 不收集，marker 挂在上面等于没挂（`-m logic` / `-m ui` 会双双漏掉整个文件）。需要每个用例独立隔离时，直接让 pytest 注入 `tmp_path` / `monkeypatch` 即可，不要手工开子目录再传进去。
- marker 用模块级 `pytestmark = pytest.mark.logic|ui`；**只有**同一文件里 logic 与 ui 混排时才用函数级 `@pytest.mark.*`（此时必须挂在被收集的 `test_*` 上）。
- 合并/重构测试文件前先做**等价核验**，不要凭「看起来一样」下结论：用 AST 统计新旧文件的 `assert` 数、mock 断言数与顶层函数数，前两项必须逐字节相等，第三项的差额必须正好等于被删的纯转发函数个数。
- 反过来也要当心：**AST 的 `assert` 计数看不见自定义断言助手**。`test_export_pipeline.py::_assert(cond, msg)` 这类「自己 raise AssertionError」的包装，以及 `pytest.raises(...)` 上下文，都不会被计入——那里有 9 个用例的计数是 0，但它们并非空转。**不要**用「assert 数 = 0」判定某个用例是死代码。
- **不要把失败随手归为「平台/字体度量基线差异」然后 deselect**。判据要**与被测系统不同源**：本项目里`label.heightForWidth()` 与被测布局同源——布局若以错误高度分配，它会跟着一起错，断言就成了自证。查UI 布局/度量类问题必须用独立判据（如 `QTextLayout` 手工换行，只依赖字体度量）。
  **deselect 名单要定期重新评估。** 真实教训（2026-10-05）：`tests/test_toolbar.py::test_panel_shrink_layout` 曾以「`WordStyleCard` 字体度量 635 < 689」为理由长期 deselect，实为**真UI 截断**——导出侧栏五张卡缺 `QLayout.SetMinAndMaxSize`，压到最小宽度 222px 时 wordWrap 标签实得 61 而真实需要 96。同批的 `test_project_models.py::test_media_paths_relativize` 则已自愈通过。故当前**无 deselect 名单**（`pytest -q` 全绿；基数与取证日期以 [README.md](README.md)「当前状态 / 测试基线」为准，不在本文件写死条数）。若将来确有平台差异必须跳过，先用外部判据证明它真是环境问题，并在本条记录叶子名与**复查期限**。
- **修UI 布局/度量类 bug 时，护栏断言必须用与被测系统不同源的判据**。反例：`test_panel_shrink_layout` 原用 `card.layout().heightForWidth()`，与被测布局同源，布局错它跟着错——修好后它绿了，但改成 `QTextLayout` 手工换行才发现另一处真截断。现两条护栏：`tests/test_toolbar.py::test_export_sidebar_text_never_truncated_across_widths`（连续宽度扫描 200~340 + `QTextLayout`）与 `::test_export_cards_layout_constraint_is_paired_with_size_policy`（守护 `ui/export_panel.py::_card_layout_constraint` 与 `_card_size_policy` **必须成对调用**——前者要在 `QVBoxLayout(self)` 建好后调用，漏掉不报错、只是窄侧栏悄悄回到截断）。
  **加护栏后必须验它真的能抓**：把修复回退，确认护栏变红，再恢复。恒真断言比没护栏更危险。
- **「本地全绿 ≠ CI 全绿」，且本地复现不出来时，先怀疑「本地根本没走到那条路径」**。2026-10-06 Linux CI 三条失败（`test_settings_dialog` 两条 + `test_cloud_asr` 一条）在 Windows 全绿。逐项回退验证时发现，我新写的两条护栏**回退修复后照样 passed** —— 断言点在 `show()` 之后，几何已收敛，测不到真正的 bug（构造期视口未收敛）。**写构造期/未收敛期缺陷的护栏，必须在 `show()` 之前采样**（hook `_fit_height_to_page`，或直接构造对象而不调 `show`）。这是上一条「恒真断言」的隐蔽版本：不是没写断言，而是断言点选在了 bug 消失之后。
- **CI 失败要先分清是产品 bug 还是测试可移植性问题**，别默认前者。2026-10-06 同批的 `test_cloud_asr.py::test_encoding_stage_reports_real_progress_from_ffmpeg` 纯属**脚手架**问题：假 ffmpeg 用 `.bat` 启动器，在 Linux 不可执行（`Permission denied`），产品侧静默降级 WAV，于是断言 `codec == 'opus'` 挂掉——被测代码一行没坏。凡在测试里造可执行文件，必须 `os.name == "nt"` 分支出 `.bat` 与 `#!/bin/sh` + `chmod(0o755)`（**缺可执行位同样 Permission denied**，别只改扩展名）。
- **注释/docstring 里声称的「不变量」可能早已失效——写代码时顺手用探针量一遍再用它做论证**。2026-10-06 复查时的实例：`ui/settings_dialog.py::_PageHost._credible_measure_width` 的 docstring 论证「`_apply_width_bounds` 已保证视口宽度 ≥ 各页`minimumSizeHint` 宽，所以这个下限可信」，而探针实测该不变量**是假的**——`_CHROME_W = 22*2+4 = 48` 漏算了垂直滚动条（最窄时必然可见，实测应为 58 = 留白 44 + 滚动条 14），`cloud_asr` 页在弹窗宽 608~617 这 10px 带内视口 550 <该页最小宽 560。按错误前提测量会把「安全高估」变成**低估**（按更宽的宽度换行 → 行数更少 → 高度偏小 → 内容被裁且滚不到底）。本机字体下该带内高度恰好没差（低估 0px），**但那是运气**——换字体（Linux CI 就是）就可能跨过换行阈值。
  由此两条：
  1. **平台相关的量不许写死**。滚动条宽度用 `QStyle.PixelMetric.PM_ScrollBarExtent` **现取**（Linux 上未必是 Windows 的 14）；这类值参与尺寸计算时，`style()` 可能为 None，要能退回 0 而不是抛异常。
  2. **凡以「某不变量恒成立」为前提的算法，那个不变量必须有护栏直接量它**，而不是靠注释声明。护栏：`tests/test_settings_dialog.py::test_settings_dialog_viewport_never_below_page_min_width`（逐页 × 最窄 40px 宽度，直接量 `viewport().width()` vs `page.minimumSizeHint().width()`）。写它时踩了两个坑，都值得记住：**① 只能测当前页**——未显示页几何未收敛（实测视口仍是默认 86px），拿它比会得到一堆假阳性；**② 必须在「出了垂直滚动条」的状态下测**，否则不变量天然成立、用例恒绿。回退验证时还发现改`_CHROME_W` 的**值**护栏抓不到（真正的修复点是 `_chrome_width()` 里 `+ extent`）——回退要退到**机制**那一层，否则验证的是巧合不是护栏。
- **测试之间的全局状态必须还原**，否则产生顺序依赖。已知两类，`tests/conftest.py` 各有一个 autouse 兜底：① **Qt 进程级主题**（`_restore_qt_theme_after_each_test`）；② **用户偏好**（`_isolate_preferences_after_each_test`）——`core.app_config.load_preferences` 有模块级 `_PREFS_CACHE`，而 UI 交互（如把 k-tag 下拉切成 `k`）会经 `save_preferences` **同时**刷新缓存与磁盘，于是「前一个测试改了偏好 → 后一个测试读到它」。典型症状（2026-10-05 实测）：`test_toolbar::test_ui_toolbar_and_workflow_controller` 把 `export.k_tag_mode` 写成 `k`，之后 `test_playback_responsiveness::test_mpv_subtitle_generation` 断言 `{\kf` 却拿到 `{\k50}`；全量顺序里 playback 恰好排在 toolbar **之前**才没暴露，换成任意子集/乱序就必失败。**新增任何写全局状态的入口，都要同时给它加还原**。
- **想缩短全量耗时，不要删用例**。实测（2026-10-05，当时 356 条）：其中 297 条合计只花 14s，时间集中在少数 UI 文件；且这些文件的用例逐条都写了「同族另一条判据抓不到这个失效模式」的理由，删任何一条都是真实覆盖损失。真正的杠杆是**别重复付固定开销**，见下条。
- **三个已验证的固定开销**，改相关代码时保持：
  1. `uroman.Uroman()` 单次约 2.7s → 走 `core/mms_aligner/engine.py::_build_shared_uroman` 的模块级共享缓存，不要每建一个 aligner 就 new 一个。
  2. 用例结尾不要手动把主题切回深色 → 会触发 conftest 的 autouse 兜底再编译一次整套 QSS（约 1.6s）。
  3. **`apply_theme` 必须保持幂等**（见下条），否则每次 `MainWindow()` 都要付一次全局 QSS 广播。
- **`ui/main_window/window.py` 构建末尾那次主题处理不能简化掉，但也不能退回无条件全局广播**。实测根因（2026-10-05）：应用级 QSS 若早于控件创建装上，控件「首次 polish」的几何与「re-polish」不一致（工具栏 `sizeHint().height()` 停在 35 而非 47），首次切换主题时布局会跳一下——所以那次「补 polish」是**必要的**。但它的**实现方式**决定了测试复杂度：`QApplication::setStyleSheet` 会向**所有存活控件**广播样式变更并逐个 repolish，而全量测试里顶层窗口只增不减（`close()` 不销毁 C++ 对象、`WorkflowController`/`ProjectController` 与窗口互为强引用），于是每次 `MainWindow()` 都是 O(存活数)、全量退化成 **O(N²)**。现拆成两步：① `ui/themes.py::apply_theme` **幂等**——外壳 QSS 已是一套时跳过 `setStyleSheet`，但 `setTheme`/`_apply_palette` 仍每次都做（实测零成本，且承载 qfluentwidgets 内部主题状态，跳过会留下主题/QSS 不一致）；② 本窗口的 polish 由 `ui/themes.py::refresh_widget_style(self)` 单独补齐（~205 控件约 10~16ms，**不随全局存活数增长**）。
  实测效果：`MainWindow()` 构造 251 控件 0.323s / 6275 控件 0.227s（改前 0.30s → 1.09s）；test_toolbar 35.4s → 5.1s；全量 116.5s → 61s。护栏：`tests/test_splitter_theme.py::test_apply_theme_skips_reinstall_of_identical_shell_qss`（同主题重复应用不得调 `setStyleSheet`、主题变化必须调）+ `test_first_theme_toggle_does_not_change_layout`（几何不跳变）。
  ⚠️ `refresh_widget_style` 三个动作**缺一不可**（`unpolish`+`polish`、`QEvent.StyleChange`、`updateGeometry`），少了 `StyleChange` 几何不收敛。且**自带窗口级 QSS 的控件**（qfluentwidgets 的 `TableWidget`/`TableView`，自身 `styleSheet()` 约 1742 字符）走 Python 侧 `unpolish` 会**必段错误**（已最小复现；保活 style 包装器也崩，与 GC 无关）——该函数因此对这类控件只补事件与几何失效。控件累积本身（330 → 7763）**仍未解决**，只是代价被解耦；主动销毁的三种写法（`deleteLater`+事件泵、`shiboken6.delete`、teardown 批量删顶层窗口）**全部段错误**，不要重试。

<!-- neat-freak: initialized at 2026-10-07 -->
