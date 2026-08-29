"""workers — 工作线程（QThread）

将 core/ 的推理操作封装为异步 QThread，通过 Signal/Slot 与 ui/ 通信。

统一约定（两个 Worker 共享）：
- 必须外部传入 ModelManager **单例引用**（UI 层持有，保证状态机一致），Worker 内部不 new ModelManager
- Signal:
    progress(done:int, total:int, desc:str)  对齐 TranscribeConfig.progress_cb / AlignConfig.progress_cb
    project(project: SubtitleProject)         成功完成并返回完整项目
    sentence_aligned(idx:int)                 单句重对齐场景：某句完成（UI 刷新表格行）
    failed(message:str)                       任何异常 → 这里，UI 弹框
    finished_ok()                             结束（和 project/sentence_aligned 同时或稍后触发，UI 可用来停转圈）
    log(level:int, msg:str)                   冗余日志（可选接入）
- Worker 在后台线程跑；Signal emit 是线程安全的；不保证 run() 抛异常能被 UI 捕获，必须 try/except 包到 failed
"""

from .transcribe_worker import TranscribeWorker
from .align_worker import AlignWorker
from .media_worker import MediaPrepWorker

__all__ = [
    "TranscribeWorker",
    "AlignWorker",
    "MediaPrepWorker",
]
