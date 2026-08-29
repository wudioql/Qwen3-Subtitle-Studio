# API 与数据合同

> 本项目当前**没有 HTTP/REST/WebSocket API**。本文描述可复用的 Python 入口、工程 JSON 合同以及 Worker 的 Qt Signal 合同。带下划线的函数、模块内部 helper 和 UI 私有字段不属于稳定 API。

## 1. 稳定性分级

- **Public**：从包入口导出，字幕处理、工程数据和导出调用可复用。
- **Application**：供 UI/Worker 编排使用，参数和生命周期受当前应用约束。
- **Internal**：以下划线开头、具体 Qt 控件、模板安全子集内部实现；修改时只需维护内部测试和兼容 façade。

## 2. `subs` 公共 Python API

### 数据模型

```python
from subs import Sentence, SubtitleProject, WordTimestamp

word = WordTimestamp("你", 0.0, 0.2, language="Chinese")
sentence = Sentence("你好", 0.0, 0.4, words=[word])
project = SubtitleProject(sentences=[sentence])
```

主要字段：

- `WordTimestamp`: `text`, `start_time`, `end_time`, `language`, `speaker`, `is_punct`。
- `Sentence`: `text`, `start_time`, `end_time`, `words`, `language`, `speaker`, `ass_style`, `ass_extra_tags`, `is_dirty`, `is_locked`, `sid`, `timed`。
- `SubtitleProject`: `source_media_path`, `audio_path`, `video_path`, `media_duration`, `sentences`, `sample_rate`, `source_language`，以及工程级导出三件套。

内部时间统一为秒的有限浮点数。`is_punct=True` 的 word 用于保留原位文本，不作为逐字动画目标。

### 工程方法

```python
project.sort()
project.has_word_level()
project.find_sentence_at(seconds)
project.dirty_indices()
project.alignable_dirty_indices()
project.mark_dirty(index)
project.mark_clean(index)
project.mark_locked(index, True)
project.to_dict()
project = SubtitleProject.from_dict(data)
project.save_json("demo.qss.json")
project = SubtitleProject.load_json("demo.qss.json")
```

工程 JSON 当前写入 `schema_version=1`。保存使用同目录临时文件、`fsync` 和 `os.replace`；相对媒体路径优先相对化，跨平台绝对路径不会被错误拼到工程目录。读取会校验时间有限性、句内字时间单调性、sid 唯一性和布尔值。

### 导入

```python
from subs import is_subtitle_file, parse_subtitle_or_text

sentences = parse_subtitle_or_text(
    "input.srt",
    media_duration=0.0,
    language="Chinese",
)
```

支持导入：SRT、VTT、LRC、ASS、TXT。TXT 是纯文本合同：非空行保留为句子，`timed=False`；不会因为数字行或 `-->` 文本而被过滤。外部字幕导入后会标记为 dirty，但原有时间和可读字级数据仍保留。

### 导出

所有导出器返回 UTF-8 `str`，调用方负责路径选择和写盘：

| 函数 | 用途 | 关键参数 / 失败语义 |
|---|---|---|
| `to_srt(project, style=None, mode="per_word")` | 句级或逐字 SRT | 缺字级时逐句降级 |
| `to_vtt(project, style=None, mode="per_word")` | 句级或逐字 WebVTT | 缺字级时逐句降级并可写 NOTE |
| `to_ass(project, style=None, mode="per_word", strategy="split", ...)` | 句级/逐字非 k-tag ASS | `split`、`t` 或 `per_sentence` |
| `to_lrc(project, enhanced=True)` | 标准或 Enhanced LRC | 无字级时句级降级 |
| `to_ass_karaoke(project, k_mode="kf", ...)` | Aegisub k-tag 源 | 缺字级直接 `ValueError` |
| `to_ass_karaoke_applied(project, k_mode="kf", ...)` | 模板应用后的 ASS | 无有效模板或缺字级直接 `ValueError` |

`WordHighlightStyle` 控制逐字 SRT/VTT/非 k-tag ASS 的字形和高亮色。k-tag 的 `k/kf/ko` 语义由 `k_mode` 决定。所有导出器必须对正文、ASS 字段和显式标签分别处理，不能把用户文本当作 markup。

## 3. `core` 应用 API

### ASR

```python
from core.asr_engine import TranscribeConfig, transcribe
from core.model_manager import ModelManager

project = transcribe(
    media_path,
    model_manager=ModelManager(),
    cfg=TranscribeConfig(),
    source_media_path=media_path,
)
```

`transcribe()` 返回 `SubtitleProject`；识别和对齐的耗时工作应从 Worker 调用，不能在 Qt 主线程直接调用。

### 对齐

```python
from core.align_engine import (
    AlignConfig,
    align_dirty_only,
    align_full_text,
    align_project,
    align_sentence,
)
```

- `AlignConfig.source_language` 默认 `auto`，`align_backend` 为 `qwen` 或 `mms`。
- `align_project()` 按句处理工程；`align_dirty_only()` 只处理未锁定脏句；`align_full_text()` 处理全文并在超长场景分块。
- `align_sentence()` 接收已有句子和音频数组，返回字/词时间戳；其上下文参数用于恢复句间边界。
- 语言缺包、模型缺失或对齐无有效产出应失败并保留旧结果，而不是静默生成假的字级时间。

### 模型生命周期

`ModelManager` 的应用入口包括 `get_asr()`、`get_aligner()`、`using_asr()`、`using_aligner()`、`using_mms_aligner()`、`using_vocal_separator()`、`park_all()`、`unload_all()` 和 `status_text()`。优先使用 context manager：

```python
with manager.using_asr() as (processor, model):
    ...
# 退出后 Qwen ASR park 到 RAM
```

MMS 任务退出时销毁 ONNX Session；不要只调用 `torch.cuda.empty_cache()`。

## 4. Worker Signal 合同

所有 Worker 都是 QThread 子类，耗时操作在 `run()` 内执行并把异常转为 `failed` Signal。UI 通用收尾必须监听 `QThread.finished`。

### `MediaPrepWorker`

- `progress(int done, int total, str description)`
- `prepared(object audio_path, object info, bool vocal_extracted)`
- `vocal_fallback(str message)`：人声分离失败但使用原音频，属于非致命提示
- `failed(str message)`、`cancelled()`、`finished_ok()`、`log(int level, str message)`

### `TranscribeWorker`

- `progress(int, int, str)`
- `project(object SubtitleProject)`：成功返回完整工程
- `failed(str)`、`cancelled()`、`finished_ok()`、`log(int, str)`

### `AlignWorker`

- `progress(int, int, str)`
- `project(object SubtitleProject)`：全文模式成功返回工程
- `sentence_aligned(int index)`：句/脏句模式的成功句索引
- `failed(str)`、`cancelled()`、`finished_ok()`、`log(int, str)`

模式只有：

```text
sentences  指定 indices
 dirty     project.alignable_dirty_indices()
 full      全文
```

## 5. UI 兼容入口

`PlayerPanel` 是 UI 内部的兼容 façade，保留 `set_project()`、`set_preview_time()`、`preview_mode()`、播放/暂停/停止和状态 Signal。它不是独立 GUI SDK；UI 私有字段和 Qt 控件层级不属于公共 API。旧的 `from ui.player_panel import PlayerPanel, _VideoSubtitleStage` 导入兼容属于现役测试约束，但以下划线名称不应被新代码依赖。

## 6. 错误与版本

- `ValueError`：工程 schema、导出前置条件、时间/字段合同或配置不合法。
- `FileNotFoundError`：模型、媒体或外部工具不存在。
- `TaskCancelled`：Worker 在安全点合作式取消；UI 应显示取消而非失败。
- 对齐失败/空产出：保留原 words 和 dirty 状态。
- 工程 schema 破坏性升级时必须提供迁移策略；当前 `schema_version=1` 不因新增非破坏性字段而随意 bump。
