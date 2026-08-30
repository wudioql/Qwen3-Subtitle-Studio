# 开发指南

本文面向日常开发和验证；目标机安装请看 [DEPLOYMENT.md](DEPLOYMENT.md)，现役分层请看 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 1. 开发环境

项目当前基线为 Python 3.12。创建项目内虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\activate
# 先按 DEPLOYMENT.md 安装目标 Torch/flash-attn
python -m pip install -r requirements-dev.txt
```

源码运行入口：

```powershell
python main.py
# 或双击 / 执行
.\start_local.bat
```

当前不是通过 `pip install .` 安装的 Python 包：`pyproject.toml` 主要提供 Ruff 配置，应用从项目根目录运行 `main.py`。

## 2. 目录速览

```text
main.py       Qt 入口
core/         推理、音频、模型生命周期、配置
subs/         字幕模型、解析、导出、ASS/LRC
workers/      QThread 异步任务
ui/           主窗口、编辑器、播放器、控制器
tests/        逻辑/UI 合同测试
e2e/          目标机真实模型验收脚本
tools/        环境探针
assets/       应用图标
```

`tests/_env.py` 是测试环境三件套的唯一实现；`tests/conftest.py` 和 `tests/_bootstrap.py` 只负责在 pytest/直跑场景调用它。测试会隔离 `QSS_CONFIG_DIR`、`QSS_TEMP_DIR`，并默认设置 Qt offscreen 与 `QSS_DISABLE_MPV=1`。

## 3. 测试分层

| 命令 | 证明什么 | 不证明什么 |
|---|---|---|
| `python -m compileall ...` | Python 文件可编译 | 运行时依赖、业务正确性 |
| `pytest -q -m logic` | 纯模型、时间码、导出、核心契约 | 真实 GPU/模型、完整 GUI |
| `pytest -q -m ui` | Qt 离屏组件和 Signal/属性合同 | Windows 原生窗口、真实播放 |
| `pytest -q` | CI/开发依赖下的全部自动套件 | 目标机 ASR、CUDA、外部播放器 |
| `python tools\env_check_native_api.py` | 依赖/API/本地 Qwen 配置探针 | 完整权重推理 |
| `python e2e\e2e_short_talk.py` | Windows/CUDA/模型/FFmpeg 端到端路径；用户本机已确认完整通过 | 其他媒体、其他 GPU、所有播放器版本；本次 Linux 沙箱未复现 |

常用门禁：

```powershell
python -m compileall -q core subs ui workers tests e2e tools main.py
ruff check .
pytest -q -m logic
pytest -q -m ui
pytest -q
python tests\test_export_pipeline.py
python tests\test_undo_commands.py
python tests\test_project_models.py
```

测试数量不要写死在长期文档中；应以当前 pytest 收集结果和 CI 日志为准。

## 4. 静态检查与代码风格

Ruff 唯一配置在 `pyproject.toml`：

- target：`py312`；
- 行宽：100；
- 当前基础规则：`E4`、`E7`、`E9`、`F`；
- `.venv`、`.temp`、`models` 被排除；测试/工具的 bootstrap 导入有明确例外。

新增直接 import 的第三方包时，必须同时更新：

1. `requirements.txt` 的版本约束；
2. `THIRD_PARTY_NOTICES.md`；
3. `DEPLOYMENT.md` 或 `TROUBLESHOOTING.md`（如果改变安装/故障路径）；
4. 依赖合同测试（必要时扩展 `tests/test_dependency_contracts.py`）。

## 5. 重要开发合同

- 重推理只在 Worker；UI 通过 Signal/Slot 接收进度、结果和错误。
- 所有时间内部用秒；导出时再格式化。
- 视图 emit-only；模型修改进入 `ui.commands`，并触发正确的预览刷新。
- `SubtitleProject.schema_version` 当前为 1；不要为非破坏性字段随意 bump。
- `subs/` 不依赖 UI/模型运行时；`core/` 不依赖 UI；mpv native 调用集中在指定模块。
- 取消不强杀前向；`QThread.finished` 是 UI 收尾的最终门。
- 标点保留显示但不成为逐字动画目标；ASS/HTML 正文必须转义。
- 失败或空的对齐结果不能覆盖旧 words。

完整硬约束和播放器边界在 [AGENTS.md](AGENTS.md)；稳定调用入口在 [API.md](API.md)。

## 6. 调试与本地运行态

- `.temp/app.log`：应用日志，启动时会轮转/清理。
- `QSS_CONFIG_DIR`：把偏好目录重定向到临时或用户目录。
- `QSS_TEMP_DIR`：把临时音频、日志和缓存重定向到可写位置。
- `QSS_DISABLE_MPV=1`：测试和无 libmpv 环境禁用真实 mpv worker。
- `QT_QPA_PLATFORM=offscreen`：离屏 Qt 测试；不要把它当成真实 Windows GUI 验收。

示例：

```powershell
$env:QSS_CONFIG_DIR = "$pwd\.local-config"
$env:QSS_TEMP_DIR = "$pwd\.local-temp"
python main.py
```

不要提交 `.local-config`、`.local-temp`、日志、用户媒体、模型权重或生成的缓存。

## 7. CI 与发布边界

`.github/workflows/quality.yml` 当前只提供 Ubuntu Linux + Python 3.12 + CPU Torch 的 Ruff/pytest 门禁，并安装 Qt 离屏运行库（`libxkbcommon0`、`libegl1`、`libpulse0`）。它不覆盖：

- Windows 原生窗口和 Qt Multimedia；
- CUDA/Flash Attention；
- 真实 Qwen/MMS 权重；
- FFmpeg CLI；
- libmpv/libass；
- PotPlayer/Aegisub/mpv.net 外部验收。

因此 CI 绿只代表自动化层通过。用户已确认本机完整 E2E、libmpv、Aegisub、mpv.net 和 PotPlayer 兼容性测试正常；这些结果不由当前 CI 复现。若未来进入正式分发，再按 [DEPLOYMENT.md](DEPLOYMENT.md) 补充相应发布验收，并在变更说明中区分 `implemented-pending`、`verified-current` 和 `unplanned`。

## 8. 文档维护

- `README.md`：入口和用户可见范围；保持短。
- `ARCHITECTURE.md`：现役机制和边界；不要放历史流水账。
- `API.md`：稳定 Python/数据/Signal 合同。
- `DEPLOYMENT.md`：目标机安装和验收。
- `TROUBLESHOOTING.md`：症状到行动的排查路径。
- `CHANGELOG.md`：历史变更和日期。
- `AGENTS.md`：AI 红线与协作规则，不复制整套架构。

同一事实只保留一个详细解释；其他文档使用短链接。历史迁移文件不再作为现役真源。
