# 贡献指南

感谢参与 Qwen3 Subtitle Studio。项目是 Windows 优先的本地桌面工具；贡献应优先保持字幕数据、模型生命周期和 UI 响应的可靠性，而不是单纯增加代码量。

## 开始前

1. 阅读 [README.md](README.md)、[ARCHITECTURE.md](ARCHITECTURE.md) 和 [AGENTS.md](AGENTS.md)。
2. 开发环境按 [DEVELOPMENT.md](DEVELOPMENT.md) 准备；目标机部署按 [DEPLOYMENT.md](DEPLOYMENT.md) 准备。
3. 不要把模型权重、`.venv/`、`.temp/`、`libmpv-2.dll`、安装 wheel 或用户偏好提交到仓库。
4. 许可证与第三方模型/二进制边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 修改原则

- `subs/` 保持为不依赖 Qt/Torch/Transformers 的字幕数据与格式层。
- ASR、对齐、人声分离、耗时音频处理不得在 GUI 线程执行；通过 `workers/` 和现有控制器调度。
- UI 视图发信号，不直接写 `SubtitleProject`；编辑必须经过 `ui.commands`，保证 Undo/Redo、dirty、lock 和预览刷新一致。
- 句/字时间统一使用秒；导出时才格式化。
- 字幕正文必须经过对应格式转义，不能把用户文本直接拼进 ASS/HTML 标签。
- 保持 `sid` 唯一；重对齐失败不能覆盖旧 words，也不能错误清除 dirty 标记。
- mpv 原生调用只能经 `ui/mpv_backend.py` 与 `ui/mpv_worker.py`；不要在顶层或 GUI 线程直接 import/调用 native mpv。
- 直接 import 的第三方运行包必须在 `requirements.txt` 中有明确声明；改变依赖时同步检查授权清单和部署文档。
- 包化、拆分和公开符号变更要保留现有兼容导入，或在 API/CHANGELOG 中说明破坏性影响。

## 推荐工作流

```text
明确问题/行为
  → 查数据模型与现役约束
  → 写纯逻辑回归或最小 UI 回归
  → 小范围修改
  → 运行分层门禁
  → 更新受影响的标准文档
  → 提交可审查的变更
```

分支、提交信息和 Pull Request 流程遵循项目托管平台的常规约定。一次 PR 尽量只解决一个主题；不要把未验证的 Windows/CUDA 结论写成已发布能力。

## 本地验证

在项目根目录、目标 Python 环境中执行：

```powershell
# 语法
python -m compileall -q core subs ui workers tests e2e tools main.py

# 纯逻辑 / UI 分层测试
pytest -q -m logic
pytest -q -m ui

# 全量测试与静态检查
pytest -q
ruff check .

# 重点回归
python tests\test_export_pipeline.py
python tests\test_undo_commands.py
python tests\test_project_models.py
python tools\env_check_native_api.py
```

真实模型、CUDA、FFmpeg 和 Windows GUI 验收不属于普通离屏测试；按 [DEPLOYMENT.md](DEPLOYMENT.md) 执行 `--strict-target --require-models` 和 `e2e/e2e_short_talk.py`。测试环境由 `tests/_env.py` 统一隔离配置、临时目录和 Qt offscreen 设置；不要在测试中污染项目根 `.config/`。

## Pull Request 清单

- [ ] 说明用户可见变化、数据/时间语义变化或性能影响。
- [ ] 新行为有对应逻辑/UI 回归；未加载真实模型的测试不冒充 E2E。
- [ ] `ruff check .`、相关 pytest、`compileall` 已执行并记录结果；无法执行的门禁写明原因。
- [ ] 受影响的 API、工程 schema、依赖、授权、部署或故障文档已更新。
- [ ] 没有提交权重、用户媒体、临时文件、日志、缓存或本地路径。
- [ ] 变更符合 GPL-3.0 以及第三方组件/模型的授权边界。

## 兼容性与版本

工程 JSON 当前为 `schema_version=1`；非破坏性字段变化优先向后兼容，破坏性变化需要迁移策略和版本说明。稳定 Python 入口见 [API.md](API.md)，历史记录见 [CHANGELOG.md](CHANGELOG.md)。
