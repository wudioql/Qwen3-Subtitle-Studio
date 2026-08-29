# 故障排查

先判断问题属于哪一层：Python/依赖、模型/推理、媒体/FFmpeg、Qt/mpv、字幕数据/导出，还是工程路径。不要用“测试通过”替代目标机验收；分层边界见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 1. 环境与依赖

### Python 版本不匹配

**现象**：Torch/Flash Attention 没有可用 wheel，或安装后导入异常。

**处理**：使用 Python 3.12，确认：

```powershell
py -3.12 --version
where python
python --version
```

不要用 Python 3.13 结果推断项目目标机状态。

### Qwen3 架构未注册、`check_model_inputs` 签名错误

**原因**：残留旧 `qwen-asr`/`qwen-audio` wrapper，或 Transformers 版本低于原生 API 基线。

```powershell
python -m pip uninstall -y qwen-asr qwen-audio
python -m pip install --upgrade "transformers>=5.13,<6.0"
python tools\env_check_native_api.py
```

### Qt / Fluent Widgets 导入失败

**现象**：缺少 `PySide6`、`qfluentwidgets`、`pyqtgraph`，或安装了 PyQt5 绑定版本。

```powershell
python -m pip install -r requirements.txt
```

本项目使用 PySide6 绑定；PySide6 6.11+ 的 Qt Multimedia 组件布局可能使 Essentials-only 环境缺少播放能力，应安装完整 PySide6/对应 Addons。

### `ruff` 不存在

Ruff 在 `requirements-dev.txt` 中。激活正确 `.venv` 后执行：

```powershell
python -m pip install -r requirements-dev.txt
ruff check .
```

## 2. 模型、CUDA 与对齐

### 找不到模型目录

先检查 `models/` 是否符合 [DEPLOYMENT.md](DEPLOYMENT.md) 的布局，或在设置中指定路径：

```powershell
python tools\env_check_native_api.py --require-models
```

探针默认只检查 Qwen 配置/Processor，不会加载完整权重；MMS ONNX 和 Kim_Vocal_2 还需单独检查文件。

### CUDA 不可用或显存 OOM

```powershell
python tools\env_check_native_api.py --strict-target --require-models
```

确认 NVIDIA 驱动、cu130 Torch、Flash Attention wheel 与 Python 版本匹配。应用按顺序激活模型；Qwen 任务结束后 park 到 RAM，MMS 任务结束销毁 ONNX Session。不要同时手工加载两套模型，也不要把 `torch.cuda.empty_cache()` 当作 MMS Session 卸载。

若仍 OOM：先确认未在同一进程重复加载模型，检查日志中的模型状态，再考虑按部署基线切换 SDPA；不要为了“过门禁”静默吞掉权重损坏、权限、dtype 或真实 OOM 异常。

### 日文/韩文对齐失败

- 日文 Qwen 对齐需要 `nagisa`；MMS 日语汉字读音建议安装 `pykakasi`。
- 韩文 Qwen 对齐需要 `soynlp`。
- 先为每句设置语言；重对齐按句级语言，项目语言作回落。
- 也可以切换到 MMS-FA 后端，但应按实际语言和歌曲场景验证质量。

### 重对齐后没有字级结果

检查：

1. 句文本是否在编辑后已重新对齐；
2. 句子是否被锁定；
3. 语言是否已设置；
4. 音频路径是否存在且能读；
5. 日/韩分词依赖是否齐全；
6. 日志是否报告空产出或模型失败。

空产出会保留旧 words 和 dirty 标记，这是保护行为，不是成功。

## 3. 媒体与 FFmpeg

### `ffmpeg` 命令不存在

```powershell
where ffmpeg
ffmpeg -version
ffmpeg -filters | findstr subtitles
```

安装包含 libass/subtitles filter 的构建并重新打开终端。Qt Multimedia 内置 FFmpeg 和命令行 FFmpeg 是两套组件，不要求版本一致。

### 视频没有画面或只显示“音频媒体·无画面”

确认打开的是原始视频路径。项目设计中：

- 视频始终用原视频播放；16 kHz WAV/人声 WAV 只服务识别和对齐；
- 纯音频才可能使用分离后 WAV 试听；
- 重新关联媒体后应由 ProjectController 重新准备音频并更新波形。

## 4. Qt 播放器与 libmpv

### 播放器显示 Qt fallback

这是允许的运行状态。没有 `libmpv-2.dll`、`python-mpv` 导入失败、初始化/命令 watchdog 超时，都会回退 QMediaPlayer + QPainter 兼容预览。用户已确认本机 `libmpv` 测试通过并可正常使用；若你的环境仍回退，再运行：

```powershell
python -c "import mpv; p=mpv.MPV(); p.terminate(); print('mpv OK')"
```

确认 DLL 在项目根或系统 PATH；不要在 GUI 线程直接调用 mpv。

### 日志出现 `Failed setup for format d3d11`

Qt/FFmpeg 尝试 D3D11 硬解失败的日志不一定是致命错误。入口会在创建 QApplication 前禁用 Qt FFmpeg 硬解设备，应用设计上走软解；若仍无法播放，先换 H.264 测试并查看 `.temp/app.log`。

### 字幕停留在旧文本/旧时间

正文、句时间、字时间和增删拆并的 Undo/Redo 应调用 `PlayerPanel.refresh_subtitle_content()`；样式/模板变化应刷新样式。若是新增代码，检查是否绕过 MainWindow 的统一刷新链，而不是手工刷新某一个表格。

### 暂停后短暂尾音

Qt fallback 会先保存用户静音值、同步静音，再调用 `QMediaPlayer.pause()`，播放/自动恢复前还原。不要在普通 stop/play 路径无条件结束 priming 或覆盖暂停静音状态。

## 5. 字幕导入、时间与导出

### TXT 被错误识别或行丢失

`.txt` 是纯文本合同：非空行原样保留，数字行、编号和包含 `-->` 的文本不应被过滤。导入后显示为 `timed=False`，需要设置语言并手动触发对齐。

### 逐字高亮错位

`Sentence.text` 与 `Sentence.words` 必须表示同一字符/词序列。手动改正文后旧 words 可能失效；先重对齐，不要把旧字级直接当成新文本的真值。标点会显示，但不参与逐字动画。

### LRC 时间或 Enhanced LRC 兼容性异常

当前 Enhanced LRC 使用每个字/词的**单开始时间**，不是旧式 `<start,end>` 双时间；不支持 Enhanced LRC 的播放器应使用标准 LRC 或 k-tag ASS。`[offset]` 的正数语义为整体提前，读写时要保持一致。

### ASS 特效在剪辑软件或播放器中不显示

ASS/k-tag 主要面向 libass 播放器、Aegisub 等；主流 NLE 可能只支持基础 SRT/TTML。检查导出类型：

- `to_ass(... strategy="split")`：逐字事件，兼容性通常较好；
- `to_ass(... strategy="t")`：一句一个事件的颜色变换；
- `to_ass_karaoke()`：Aegisub k-tag 源；
- `to_ass_karaoke_applied()`：模板展开后的 fx 成品。

兼容性结论（用户本机实测）：Aegisub 和 mpv.net 对各类项目字幕文件均支持；PotPlayer 对其他字幕均正常，但 `\kf` 不能实现逐字扫过，只能整字亮。这是 PotPlayer 的渲染能力限制，不是项目导出或时间数据错误。

### 导出报 ASS/模板错误

k-tag 源和应用模板后的 ASS 要求有效字级；至少一条字级不足以代表所有句子时，应按错误信息定位缺失句。应用模板还要求至少有一个启用模板；全不选不会静默回退为默认模板。

## 6. 工程文件与路径

### 打开工程提示媒体缺失

工程 JSON 会优先用工程目录相对路径，再尝试 `media_path_hints` 的原路径。若两者都不存在，字幕数据仍可打开；使用“文件 → 重新关联媒体”只替换媒体字段，不清除字幕。

### 工程保存后文件损坏

保存使用同目录临时文件、`fsync` 和 `os.replace`。若目录只读、杀毒软件锁文件或磁盘空间不足，会在保存时报告错误；不要手工删除仍存在的目标工程，先检查临时文件和权限。

### 启动后偏好/日志污染项目目录

测试应由 `tests/_env.py` 隔离。手动调试可以设置：

```powershell
$env:QSS_CONFIG_DIR = "$pwd\.local-config"
$env:QSS_TEMP_DIR = "$pwd\.local-temp"
```

这些目录、`.temp/`、`.config/`、缓存和日志不应提交。

## 7. 验收边界

本工作区的 Linux/无权重沙箱只能确认语法、纯逻辑和部分离屏契约，不能重复 Windows/CUDA、原生窗口、FFmpeg、libmpv 或外部播放器测试。用户已确认本机完整 E2E、libmpv、Aegisub、mpv.net 和 PotPlayer 测试正常；其中 PotPlayer 的唯一已知限制是 `\kf` 只能整字亮，无法逐字扫过。遇到目标机问题时请保留 Python/Torch/Transformers/ORT/Qt/FFmpeg 版本、模型路径、`.temp/app.log` 摘要和 E2E 报告，同时去除个人路径与敏感信息。
