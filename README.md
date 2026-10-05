# Qwen3 Subtitle Studio

本地多语言字幕生成、对齐与细粒度编辑桌面工具，Windows 优先。

> **文档口径**：标准文档使用英文文件名，正文以中文为主。当前项目没有 HTTP 服务和数据库；`API.md` 描述的是 Python/数据合同，不是 REST API。

## 项目定位

- **识别**：Qwen3-ASR-1.7B 原生 Transformers API（本地），或 SiliconFlow 云端 ASR（只出文本，字级时间戳仍由本地对齐器产出）
- **对齐**：Qwen3-ForcedAligner-0.6B（口语）或 MMS-FA ONNX（歌词）
- **编辑**：句级、字/词级、波形边界、Undo/Redo、锁定/脏标记
- **预览**：PySide6 `QVideoSink` 同画布兼容预览；可选 libmpv/libass 真 ASS 预览
- **导出**：SRT、VTT、ASS、LRC 等句级与逐字产物，共 11 个导出入口
- **工程**：工程 JSON（`schema_version=1`）、偏好 JSON、文件系统媒体和模型资源

## 当前状态（审计快照：2026-10-05）

表里的状态词含义见 [DEVELOPMENT.md](DEVELOPMENT.md) §8。**证据列同时标明取证日期**：自动化行由本次快照当场重跑得到，用户确认行来自用户本机实测、无法由 CI 复现。

| 范围 | 状态 | 证据 / 边界 | 证据日期 |
|---|---|---|---|
| 源码语法与静态检查 | `verified-current` | 本机 `compileall` 通过；`ruff check .` 全绿 | 2026-10-05 |
| 纯逻辑、字幕模型、导入导出 | `verified-current` | 本机 `pytest -q`全绿（详见下方「测试基线」） | 2026-10-05 |
| 云端 ASR（SiliconFlow）后端 | `implemented-pending` | 取文本、计费账本、逐句语种判定均有单测；但真实 HTTP 调用依赖用户自备API Key，且不在 `e2e/` 与 pytest 套件内，门禁无法复现 | 2026-10-04 |
| 本机完整 ASR/对齐 E2E | `verified-current` | 用户确认各功能均正常工作，且模型未变动，故行为与上次实测一致；根目录三件参考资源由本项目生成，属于链路自洽验证，不是独立数据集基准 | 2026-10-05 |
| 本机 libmpv/libass | `verified-current` | 同上（用户确认；模型与播放链路未变动） | 2026-10-05 |
| Aegisub / mpv.net 字幕兼容性 | `verified-current` | 同上（用户确认；字幕导出代码未变动） | 2026-10-05 |
| PotPlayer 兼容性 | `verified-current` | 同上；除 `\kf` 无法逐字扫过、只能整字亮外，其余字幕均正常 | 2026-10-05 |
| 硬字幕烧录、Nuitka 分发 | `unplanned` | 当前没有明确规划、排期、验收标准或对应 UI/分发产物 | — |

### 测试基线

本机 `pytest -q` 于 2026-10-05 **全绿**（此前唯一的失败
`test_toolbar.py::test_panel_shrink_layout` 已定位并修复：导出侧栏卡片缺
`QLayout.SetMinAndMaxSize` 约束，压到最小宽度 222px 时两个 `word_wrap`
说明标签被真实截断——按字体度量分别需要 96px / 36px 却只拿到 61px / 26px，
非度量误差）。

完整验证命令和证据边界见 [DEVELOPMENT.md](DEVELOPMENT.md) 与 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 快速开始

目标环境为 **Windows 11 + Python 3.12**。完整安装、模型、FFmpeg、libmpv 与授权说明见 [DEPLOYMENT.md](DEPLOYMENT.md)。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\activate
# 先按 DEPLOYMENT.md 安装目标 cu130 Torch / flash-attn
python -m pip install -r requirements-dev.txt
python tools\env_check_native_api.py
python main.py
```

没有模型时可以先运行字幕导入、编辑、导出和纯逻辑测试；完整 ASR/对齐必须在目标机安装权重后执行。

## 文档导航

| 文档 | 用途 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 现役系统边界、分层、运行时流程、数据与资源生命周期 |
| [API.md](API.md) | Python 公共入口、工程 JSON schema、Worker Signal 合同 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 人类贡献者的修改、测试、审查与许可证约定 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | 目标机安装、模型、原生依赖、验收与商业部署边界 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 日常开发、测试分层、CI、调试与文档维护 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 依赖、推理、播放器、字幕和工程文件故障排查 |
| [CHANGELOG.md](CHANGELOG.md) | 按日期整理的历史变更；不代替现役架构文档 |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | 第三方组件、模型、二进制和资产授权唯一清单 |
| [AGENTS.md](AGENTS.md) | AI 协作约束与不可违反的工程红线 |

原有中文迁移资料已完成迁移并清理；现役内容以标准文档和当前代码为准。

## 非目标与限制

- 当前没有 Web/HTTP API，也没有 SQL/NoSQL 数据库。
- ASR 输入超过 1200 秒时明确报错，不自动切块；对齐器侧支持超过 300 秒的全文分块。
- `speaker` 字段目前只做数据/导出透传，没有产品化的多说话人 UI。
- 任意 Lua 不执行；卡拉 OK 模板只支持项目定义的安全子集。
- ASS 特效主要面向 Aegisub、mpv.net 等支持环境；PotPlayer 已验证普通字幕正常，但 `\kf` 不能实现逐字扫过，只能整字亮；主流 NLE 不一定原生渲染 ASS 特效。
- 硬字幕烧录和 Nuitka 分发目前只是未规划事项，不应被描述为当前待办、已排期功能或发布路线。

## 许可证

项目自有源代码按 GPL-3.0 发布；GPL 本身不禁止商业使用。当前默认运行组合仍受到 PySide6-Fluent-Widgets 上游许可政策和默认 MMS-FA 权重 CC-BY-NC-4.0 的额外约束。商业部署前请阅读 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，并按实际依赖重新复核许可证与 SBOM。
