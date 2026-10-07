# Qwen3 Subtitle Studio — 现役架构

> **状态**：`verified-current` 表示由当前源码/测试或用户本机实测确认；`implemented-pending` 表示已有代码但尚未在目标环境验收；`unplanned` 表示当前没有明确规划、排期或验收标准。本文只描述当前实现，不把未来方案写成现役组件。
>
> **更新基线**：2026-10-05。若本文与代码冲突，以当前代码和测试为准，并在同一变更中修正文档。

## 1. 问题与系统边界

项目解决的是一条本地桌面工作流：

```text
媒体 / 字幕 / 纯文本
        │
        ├── 媒体准备：探测、16 kHz 单声道抽音、可选人声分离
        ├── 识别：Qwen3-ASR
        ├── 对齐：Qwen3 ForcedAligner 或 MMS-FA
        ├── 编辑：句级、字/词级、波形与 Undo/Redo
        ├── 预览：Qt 兼容绘制或可选 libmpv/libass
        └── 导出：SRT / VTT / ASS / LRC 等文本产物
```

系统边界：

- 输入和输出都以本地文件为主；模型权重不随源码分发。
- `main.py` 启动 Qt 应用；没有常驻服务、端口或远程 API。
- 工程持久化使用 JSON 和文件系统，不使用数据库。
- FFmpeg、CUDA、ONNX Runtime、Qt Multimedia 和可选 libmpv 是外部运行时能力，不属于 `subs/` 的字幕数据模型。

## 2. 架构原则

1. **UI 事件循环不可被重推理阻塞**：ASR、强制对齐、人声分离和媒体准备走 `workers/` 的 QThread；取消采用合作式安全点，不强杀模型前向或原生线程。
2. **数据模型单一**：`subs.models` 的 `SubtitleProject → Sentence → WordTimestamp` 是句/字时间、语言、脏/锁状态的唯一数据合同。
3. **文本与格式标签分流**：导出器先转义用户正文，再由明确的 ASS/HTML/LRC 规则生成格式标签，不能直接拼接用户输入。
4. **资源按后端释放**：Qwen 模型可以 park 到 RAM；MMS ONNX Session 在任务结束销毁；ASR 与对齐器不能同时占用目标 GPU 的活动资源。
5. **编辑具有事务语义**：重对齐在临时结果上完成；只有非空成功结果才覆盖旧字级和脏标记。Undo/Redo 按稳定 `sid` 定位，而不是按文本相等定位。
6. **导出是真源，预览复用真源**：预览字幕生成尽量复用导出器和模板应用器；Qt 路径是兼容近似，libmpv 路径负责真实 ASS/libass 渲染。
7. **当前实现优先于未来设计**：ONNX 推理抽象仍是未来评估项；硬字幕烧录、Nuitka 分发目前没有明确规划，不应在现役架构图中当作可调用模块、当前待办或既定路线。

## 3. 分层与依赖方向

```text
main.py
   │
   ├── ui/       界面、控制器、预览、用户交互
   │      ├── workers/  异步任务封装
   │      ├── core/     推理、音频、模型生命周期、配置
   │      └── subs/     数据模型、解析、导出
   │
   └── core/     允许由入口直接读取常量/配置；重活仍由 UI 调度到 workers

workers/ ───────→ core/ + subs/
core/ ──────────→ subs.models（共享数据合同）
subs/ ──────────→ 标准库/其自身纯字幕模块
```

实际包级约束：

| 包 | 责任 | 不应承担的责任 |
|---|---|---|
| `subs/` | 统一数据模型、时间码、字幕解析/导出、ASS/卡拉 OK 模板 | 不导入 Qt、Torch、Transformers 或 UI |
| `core/` | ASR、Forced Alignment、MMS/ORT、音频、模型生命周期、应用配置 | 不直接创建 UI，不依赖 `ui/` |
| `workers/` | 将耗时核心操作封装成 QThread 和 Signal | 不持有第二套项目数据模型，不在 UI 线程执行重推理 |
| `ui/` | 主窗口、控制器、视图、播放、编辑命令、导出入口 | 不在视图中直接写模型，不绕过 Worker 调模型 |
| `main.py` | Qt 初始化、环境变量、异常/单实例、启动窗口 | 不承载业务算法 |

项目是按职责拆分的桌面单体，不宣称已经采用完整 Clean Architecture/Hexagonal Architecture。当前分层边界稳定的部分应优先通过测试和文档维护；不要仅为了套用术语而进行大规模搬包。

## 4. 主要运行时流程

### 4.1 打开媒体

1. `ProjectController` 接收媒体路径并询问可选的人声提取。
2. `MediaPrepWorker` 在后台探测原生音频；必要时调用 FFmpeg 生成 16 kHz mono WAV；可选调用 Kim_Vocal_2。
3. 主线程收到 `prepared` 后创建/更新 `SubtitleProject`，播放器加载原媒体或音频试听源，波形读取音频。
4. 视频始终优先播放原视频；提取 WAV 仅作为识别/对齐数据源。

### 4.2 ASR + 对齐

ASR 有**两个可切换的后端**，工具栏「识别后端」即时选择为准：

```text
TranscribeWorker
  └── core.asr_engine.transcribe()
        ├── FFmpeg/音频准备
        ├── 【取文本】二选一
        │     ├── local（默认）：ModelManager.using_asr() → 本地 Qwen3-ASR 输出文本
        │     └── cloud      ：core.cloud_asr.transcribe_cloud() → SiliconFlow HTTP 输出文本
        │                      （完全不碰 model_manager，本地 1.7B 不进显存）
        ├── Forced Aligner 生成字/词时间   ← 两个后端共用，始终本地
        └── 按标点聚合 Sentence，返回 SubtitleProject
```

`core/asr_engine/` 收 4 个子模块：`config`（TranscribeConfig / 语言短名互转 / 进度上报）、`splitting`（标点保护、硬切、按标点切分）、`sentences`（文本或带时间片段 → Sentence 列表）、`pipeline`（`transcribe` 主流程）。包入口只再导出；`splitting` / `sentences` 由本地与云端两条路径共用。

**关键边界：后端只替换「取文本」这一段，字级时间戳 100% 仍由本地强制对齐器产出。** 云端接口不提供字级时间戳（`response_format=verbose_json` 返回 400），本地对齐器不可替代，因此切换后端不改变时间轴精度。

原生 ASR 文本不直接提供本项目所需的可靠句级时间；默认仍执行一次字/词级对齐，再聚合句级时间。`return_word_timestamps=False` 只影响结果是否保留 words，不把句级时间改成字符数线性分配。

#### 4.2.1 云端后端（SiliconFlow）

- 入口 `core/cloud_asr/`（包，仅标准库）：`__init__` 只再导出；`client`=HTTP 传输与 multipart、`encoding`=上传前编码（OPUS/时长探测）、`language`=语言决议与响应归一化、`facts`=模型能力与计费账本、`types`/`errors`/`constants`=数据类与常量。另有 `core/cloud_models.py`（抓定价页并与账本对账）+ `ui/settings/cloud_asr_tab.py`（设置页）；CLI 探针 `tools/cloud_models_cli.py` 是 `cloud_models` 的薄包装。
- 装配链：工具栏下拉 → `prefs.asr.asr_backend` → `WorkflowController._prepare_cloud_asr()`（缺 Key 时前置拦截，不浪费一次上传）→ `TranscribeConfig.cloud_asr`。
- **计费事实来自账单实证，不来自官方标注**：`VERIFIED_FREE_MODELS` / `PAID_MODELS` 是 2026-10-04 真机调用后的费用明细结论。官方**没有**任何可编程的价格或余额查询接口（`/v1/user/info` 已 410，`/v1/billing|usage|credits` 全部 404），运行时无法判断任意模型是否收费，只能依据这份账本。定价页的「免费」标签出现过名不副实的先例（`Qwen/Qwen3-ASR-1.7B` 标注免费但实测扣费），所以三方名单冲突时**以账本为准**（`PAID_MODELS` > `VERIFIED_FREE_MODELS` > 定价页）。
- 余额不足返回 **402 且不重试**（平台不透支）；429/5xx/网络异常才退避重试。
- 上传默认 OPUS 32k（实测与 WAV 识别结果逐字一致、体积约 1/9），MP3 会引入错字。
- 语言决议是三层兜底：模型返回 → 用户显式指定 → 从转写文本按字符脚本反推；都失败才报错（对齐器需要语言，宁可早失败也不要等到对齐阶段炸掉）。
- 用量台账 `accumulated_seconds` 只是客户端自记的流水账，**不等于账单金额**，官方无对账接口。

#### 4.2.2 全文重对齐：分段按「分词器族」，音频窗只用真实时间

全文重对齐（`core/align_engine/full.py::align_full_text`，策略层；语言决议与字级提交原语在 `align_engine/words.py`，长媒体切块机制在 `align_engine/chunking.py`，三者单向依赖 `words → chunking → full`）把整个项目的文本拼成一段，对着整段音频一次性推断。分段**不按语言、按 Qwen 分词器族**（`core/language_utils.tokenizer_family`）：

- Qwen 官方分词 `split_words_for_alignment` 只对 `Japanese`/`Korean` 分支切到 nagisa / soynlp，**其余语言（含中/粤/英/法/德…）共用同一条默认「CJK 逐字 + 空格词」路径，输出逐字等价**（实测 `今天我们来聊聊 attention 机制` 在 `None` 与 `Chinese` 下完全相同）。因此同族连续句合并成**一次调用**——中英混排 = 1 段 = 零裁剪，行为与单语项目一致。
- MMS 后端语言**不进模型**（只影响罗马化读音：数字拼读表 / 日语 pykakasi 路由），因此**不做语言分段**：整段一次调用，逐句读音语言经 `MMSAligner.align(..., word_languages=...)` 传入。
- 真·族冲突（ja/ko 与其它语言同段混排）走**两阶段**：阶段一用整段音频 + 全文拿**真实时间**，阶段二只在真实时间窗上裁音频、按各自分词器精修冲突族。

**铁律：句级时间只用于把文本分组到块 / 对齐调用，绝不用于定义音频裁剪窗。** 首次全文重对齐时句级时间是 ASR / 纯文本导入的**占位值**（字符占比均分），按它裁音频会让段内内容完全对不上——旧实现按「语言段」裁剪正是此缺陷的来源。音频窗只有两个来源：① 静音点 / 长度切块；② 两阶段里阶段一产出的真实时间。长媒体（>5 分钟）同样先按整段媒体 + 静音点切块，再逐块对齐，并按「原句中心距块中心最近」合并去重。

### 4.3 导入字幕/纯文本后对齐

1. `ProjectController` 使用 `subs.parse_subtitle_or_text()` 导入 SRT/VTT/LRC/ASS/TXT。
2. TXT 保持纯文本合同，`timed=False`；有时间的字幕保留原时间但导入后标脏。
3. 用户设置句级语言后，由 `WorkflowController` 创建 `AlignWorker`。
4. `AlignWorker` 支持三种模式：`sentences`（指定句）、`dirty`（未锁定脏句）、`full`（全文）。不存在 `mode=project`。
5. 成功结果通过 `commit_aligned_words()` 或工程副本整体替换提交；失败/取消保留旧结果。

### 4.4 编辑、预览与导出

```text
视图 emit 信号
   → ui.commands 创建 QUndoCommand
   → SubtitleProject 更新 dirty/locked/sid/时间
   → MainWindow 刷新编辑器、波形和 PlayerPanel
   → ExportController 调用 subs exporter
   → atomic_io UTF-8 原子写盘
```

播放器收在 `ui/player/` 包内（2026-10-05 由 `ui/` 根目录收包，包内去掉了冗余的
`player_` 前缀），由 `ui/player/panel.py` 提供兼容 façade，对外仍从包入口取：
`from ui.player import PlayerPanel`。

- `stage.py`：QVideoSink 视频帧与 QPainter 字幕同画布；
- `subtitle_overlay.py`：六档 Qt 兼容字幕预览；
- `subtitle_preview.py`：导出真源字幕生成与 mpv 字幕轨；
- `qt_runtime.py`：Qt 软解、首帧预卷和播放状态；
- `focus_surface.py`：画面点击/沉浸模式信号汇合；
- `mpv_backend.py` + `mpv_worker.py`：可选 libmpv 原生调用的唯一入口与 daemon worker；
- `qt_media.py`：Qt Multimedia 防御性导入的唯一入口。

不属于本簇、仍留在 `ui/` 根的邻居：`karaoke_coordinates.py`、`subtitle_render_policy.py`。

Qt fallback 不使用 `QVideoWidget`，以免原生视频窗口盖住字幕。同一媒体存在 mpv 时，后端路由看 `active_backend`；mpv 初始化、命令和终止都不能阻塞 GUI。

### 播放器兼容性验证

以下为用户本机实测结论：

- 完整项目 E2E 正常；
- `libmpv` 本机测试通过，可以正常使用；
- 各类字幕文件在 Aegisub 和 mpv.net 中均已实际测试支持；
- PotPlayer 对普通字幕和其他字幕效果均正常，唯一已知限制是 `\kf` 无法实现逐字扫过，只能整字亮。这是播放器能力差异，不是项目导出失败。

本工作区的 Linux 沙箱没有重复执行上述 Windows 本机验证，因此文档保留“用户本机实测”证据来源。

## 5. 数据与持久化

### 5.1 内部模型

```text
SubtitleProject
├── source_media_path / audio_path / video_path
├── media_duration / sample_rate / source_language
├── sentences: list[Sentence]
│   ├── text / start_time / end_time / timed
│   ├── language / speaker / ass_style / ass_extra_tags
│   ├── is_dirty / is_locked / sid
│   └── words: list[WordTimestamp]
│       ├── text / start_time / end_time / language / speaker
│       └── is_punct
└── ass_style_data / karaoke_template_data / export_settings
```

时间在内部统一为秒的有限浮点数；导出时才转换为毫秒、厘秒或 ASS 时间码。标点可以保留在 words 中显示，但不参与逐字动效。

### 5.2 工程 JSON

- 当前写入版本为 `schema_version=1`；破坏性变更才升版本。
- 读取会检查有限时间、时间顺序、唯一正整数 `sid`、布尔字段和安全数量上限。
- 保存使用同目录临时文件、`fsync`、`os.replace`，避免半截工程。
- 媒体路径优先相对化并统一正斜杠；原绝对路径可写入 `media_path_hints` 作为回退。
- 工程缺少媒体时保留字幕数据，并由 UI 提供重新关联媒体入口。
- 偏好设置另存于 `.config/preferences.json`；预览模式/主题属于 UI 偏好，不写入工程。

**本项目没有数据库。** 不创建 SQL schema，也没有迁移工具；工程可移植性由 JSON schema、媒体路径解析和文件系统布局负责。

## 6. 资源、状态与错误语义

### 模型状态

| 后端 | 加载中 | 任务中 | 任务结束 |
|---|---|---|---|
| Qwen ASR / Qwen Aligner | `loading` | `in_vram` | `in_ram`（park）或完全卸载 |
| MMS-FA ONNX | `loading` | ONNX Session 活跃 | 销毁 Session，回到 `not_loaded` |
| Kim_Vocal_2 | 按需加载 | ONNX Session 活跃 | context 退出时卸载 |

`ModelManager` 负责状态和互斥；Qwen 的模型对象可以在 CPU/RAM 驻留，MMS 不能用 `torch.cuda.empty_cache()` 伪装成 RAM 驻留。

### 取消与失败

- Worker 只在安全点检查取消，不强杀 QThread、Torch forward、ONNX forward 或 FFmpeg 子进程。
- 成功、失败和取消最终都由 `QThread.finished` 触发 UI 收尾；错误通过 `failed` Signal 传给 UI。
- 重对齐空结果视为失败，不清除旧字级和 dirty 标记。
- 真实依赖缺失、模型不存在、语言分词包缺失应在可行时 fail-fast，并给出可执行提示。

## 7. 现役限制与未来项

`verified-current`（用户本机）：完整 ASR/对齐 E2E、libmpv 真 ASS 预览，以及 Aegisub/mpv.net 字幕兼容性均已由用户确认正常。PotPlayer 的已知兼容性限制仅为 `\kf` 不能逐字扫过，只能整字亮。

`implemented-pending`：本次 Linux 沙箱没有复现 Windows/CUDA/Qt 原生环境，因此不能把沙箱结果写成目标机复验记录。

`unplanned`：

- 硬字幕烧录及其 UI；
- Nuitka 便携分发、安装包和对应 SBOM 流程。

以上两项目前没有明确规划、排期、设计合同或验收标准；若未来重新提出，应先补充范围说明和验收标准。经过 CJK/歌曲/精度验证后再评估 ONNX ASR/Aligner 替换，仍属于独立的未来评估项。

未来项没有当前 Python 入口、配置文件或可承诺的兼容合同，不能在 `API.md` 中当作已实现 API。
