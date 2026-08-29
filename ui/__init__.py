"""ui — 界面层（PySide6）

主窗口、播放预览面板、波形视图、字幕编辑器、样式/导出/设置对话框。

分层约定（与早期「不直接操作 core」表述不同，以本段为准）：
- **禁止**在 UI 线程内调用 core 重推理（ASR / 对齐 / 大模型 forward）；
  重推理必须经 ``workers/`` QThread，由 ``WorkflowController`` 调度。
- **允许** UI 直连 core 的配置/常量/轻量 I/O 编排
  （``app_config``、``constants``、``ModelManager`` 持有、打开媒体时的抽音等）。
- 字幕数据契约以 ``subs.models`` 为真源；编辑经 ``ui.commands`` Undo 栈，
  视图 emit-only，不直接改模型。
"""
