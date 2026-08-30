# Changelog

本文件按 [Keep a Changelog](https://keepachangelog.com/) 的思路记录可感知变化。项目当前没有 release tag、语义化版本号或打包产物；下列日期是开发快照，不等同于发布或目标机验收。现役机制以 [ARCHITECTURE.md](ARCHITECTURE.md) 和当前代码为准。

## [Unreleased]

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

### Removed

- 删除已完成迁移、且不再作为现役真源的三个中文历史文档；内容以根目录标准文档为准。

### Unplanned

- 硬字幕烧录及其 UI。
- Nuitka 便携分发、安装包和对应 SBOM 流程。

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
