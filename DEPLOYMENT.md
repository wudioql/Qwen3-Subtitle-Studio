# 部署指南

> 当前目标是 **Windows 11 / Python 3.12 / RTX 4070 Laptop 8GB / CUDA 13.x**。默认源码运行，不提供现成安装包。完整第三方授权边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 1. 运行时组成

| 组件 | 必需性 | 作用 |
|---|---|---|
| Python 3.12 | 必需 | 当前验证基线 |
| PyTorch 2.13 + 目标 cu130 wheel | Qwen 推理必需 | ASR / Qwen Forced Aligner |
| flash-attn 2.8.3 | 推荐 | Qwen GPU 注意力；代码可对 FA2 类错误回退 SDPA |
| Transformers `>=5.13,<6` | 必需 | 原生 Qwen3 API |
| PySide6、PySide6-Fluent-Widgets、pyqtgraph | GUI 必需 | 主窗口、波形与编辑器 |
| librosa、soundfile、numpy | 必需 | 音频处理与数据 |
| ONNX Runtime | 必需 | MMS-FA / Kim_Vocal_2；默认 requirements 为 CPU 版 |
| uroman | 必需 | MMS 罗马化 |
| FFmpeg CLI | 媒体处理必需 | 抽音、重采样、人声分离；硬字幕烧录当前未规划 |
| `python-mpv` + `libmpv-2.dll` | 可选 | libass 真 ASS 预览；缺少时回退 Qt |
| nagisa / soynlp / pykakasi | 按语言可选 | 日文/韩文分词、日语汉字读音 |

`requirements.txt` 是运行时直接依赖声明，但 CUDA Torch、Flash Attention wheel 和 GPU 版 ONNX Runtime 的选择必须按目标平台单独处理；项目没有 lockfile，发布前应冻结实际环境并生成 SBOM。

## 2. 创建虚拟环境

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
```

确认 `where python` 指向项目 `.venv\Scripts\python.exe`，并确认版本为 3.12.x。

## 3. 安装 Python 依赖

### 3.1 先安装目标 Torch / Flash Attention

目标 CUDA wheel 需要与 Python、Torch 和 CUDA 代际匹配。当前部署基线示例：

```powershell
python -m pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cu130
# 安装与 cp312 / torch2.13 / cu130 匹配的 flash-attn 2.8.3 预编译 wheel
python -m pip install .\flash_attn-2.8.3+cu130torch2.13-cp312-cp312-win_amd64.whl
```

若没有可用的 Flash Attention wheel，先使用 SDPA 验证功能；不要把不匹配的 wheel 强行装进环境。

### 3.2 安装项目依赖

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

目标 GPU 需要用 GPU 版 ONNX Runtime 替换默认 CPU 包：

```powershell
python -m pip install --upgrade "onnxruntime-gpu>=1.27,<2.0"
```

不要安装旧的 `qwen-asr` / `qwen-audio` wrapper；Qwen3 走 Transformers 原生 API。按需安装语言能力：

```powershell
python -m pip install nagisa soynlp pykakasi
```

## 4. FFmpeg

安装一个在 PATH 中可调用、包含 `subtitles` filter/libass 的 FFmpeg 构建，例如 BtbN GPL static：

```powershell
ffmpeg -version
ffmpeg -filters | findstr subtitles
```

Qt Multimedia 内置的 FFmpeg 与系统 FFmpeg CLI 是两套不同组件；播放器日志中的 Qt FFmpeg 版本不需要与命令行版本一致。

## 5. 模型目录

模型不入库，建议布局如下：

```text
models/
├── Qwen3-ASR-1.7B-hf/
├── Qwen3-ForcedAligner-0.6B-hf/
├── mms-300m-1130-forced-aligner-onnx/
│   ├── *.json
│   └── onnx/model_fp16.onnx       # 默认推荐，只下载需要的版本
└── Kim_Vocal_2.onnx
```

Qwen 使用带 `-hf` 的 Transformers 原生格式和 safetensors。MMS 仓库包含多个量化版本时不要整库下载；当前代码优先探测 FP16/指定候选文件。模型路径也可以在应用设置中覆盖。

下载完成后检查：

```powershell
python tools\env_check_native_api.py --require-models
```

该探针会检查 Qwen 配置/Processor，但不会加载完整权重；MMS 与 Kim 文件还需按上表手动核对。

## 6. 可选 libmpv 真 ASS 预览

`python-mpv` 已列入 requirements，但只有项目根目录存在 `libmpv-2.dll` 时才启用真渲染。DLL 应来自包含开发库的 mpv.net 或 mpv dev 构建；普通播放器压缩包通常不包含它。

```powershell
python -c "import mpv; p=mpv.MPV(); p.terminate(); print('mpv OK')"
```

失败时不影响 Qt fallback；不要把 DLL 或播放器安装目录提交到源码仓库。用户已确认本机 `libmpv` 测试通过，可以正常使用。商业分发必须复核所用 libmpv 构建的 LGPL/GPL 组合和动态链接义务。

兼容性实测记录（用户本机）：

- Aegisub：各类项目字幕文件均已测试支持；
- mpv.net：各类项目字幕文件均已测试支持；
- PotPlayer：除 `\kf` 无法逐字扫过、只能整字亮外，其他字幕均正常。

上述是播放器兼容性事实，不代表项目已经规划硬字幕烧录或 Nuitka 分发。

## 7. 分层验收

### 7.1 基础探针

```powershell
python tools\env_check_native_api.py
python tools\env_check_native_api.py --strict-target --require-models
```

### 7.2 纯逻辑与离屏测试

```powershell
python -m compileall -q core subs ui workers tests e2e tools main.py
ruff check .
pytest -q -m logic
pytest -q -m ui
pytest -q
```

这些测试不等同于真实模型推理和外部播放器验收。

### 7.3 Windows/CUDA E2E

根目录三件参考资源必须同时存在：

```text
test-short-talk.mp4
test-short-talk.txt
test-short-talk.ass
```

执行：

```powershell
python e2e\e2e_short_talk.py
# 或只验一个后端
python e2e\e2e_short_talk.py --backend qwen
python e2e\e2e_short_talk.py --backend mms
```

默认 E2E 包含媒体处理、ASR/对齐、11 个导出产物和报告生成。用户已确认本机完整 E2E 正常；根目录三件参考资源由本项目生成，因此它首先证明整条项目链路自洽，不是独立外部数据集的精度基准。本次 Linux 沙箱没有重复执行 Windows/CUDA E2E，不应覆盖用户本机实测结论。

## 8. 运行时文件与权限

- `.config/`：用户偏好；多用户或只读安装目录可用 `QSS_CONFIG_DIR` 重定向。
- `.temp/`：抽音、分块和日志；可用 `QSS_TEMP_DIR` 重定向到可写目录。
- `models/`：大模型和 ONNX 权重，不应进入源码包。
- `libmpv-2.dll`、wheel 和用户媒体不应进入源码包。
- 应用启动时会清理过期临时文件并写 `.temp/app.log`；日志不应含密钥或打包进发行版。

## 9. 授权与商业部署

项目自有代码 GPL-3.0，GPL 本身不禁止商业使用。但默认组合还涉及 PySide6-Fluent-Widgets 的上游许可政策、MMS-FA 的 CC-BY-NC-4.0 权重、FFmpeg/libmpv 构建和可选语言包。当前没有明确的硬字幕烧录或 Nuitka 分发规划；如果未来进入正式分发，至少要：

1. 取得或替换 GUI 组件的适用许可；
2. 替换 MMS 非商业权重或取得额外授权；
3. 按实际二进制构建核对 LGPL/GPL 义务；
4. 重新生成依赖、模型、二进制、资产哈希和 SBOM。
