# Phase 4 UI 重构 v2：手动编辑能力 + Undo/Redo + 视觉反馈 + ffmpeg 部署更新

> 范围：在 [phase4_ui_alignment.md](file:///d:/AI-tools/Qwen3-Subtitle-Studio/.trae/documents/phase4_ui_alignment.md) 既有结论基础上，把"用户最新一轮的 4 个决策 + 2 个新增诉求"重新整理为可执行计划。
> 用户最新决策（已确认）：
>
> 1. **标点 word 处理**：维持 `merge_punct=True` 默认（合并到前一字）— 不改。
> 2. **Undo 范围**：`QUndoStack` 全功能 + `redo`（Ctrl+Z / Ctrl+Y）。
> 3. **视觉反馈**：仅"改过的行号列底色变黄"（无闪烁、无状态栏文字）。
> 4. **拆分/合并行为**：完全 silent，按当前播放头位置拆（无校验、无提示）。
> 5. **重对齐**：手动 Ctrl+Shift+R 触发，**不弹任何确认框**（"手动改时不需要任何提示"）。
> 6. **改动撤回 + 视觉反馈**：手动改的文字/时间/拆分/合并/拖边界都要可撤回；改过的行号列底色变黄。

---

## 1. Summary

本 plan 完整覆盖 6 个能力点：

1. **`Sentence.is_dirty` 字段 + `SubtitleProject.dirty_indices / clear_dirty`**：脏标记贯穿编辑流程，重对齐只对脏句生效。
2. **`align_project` 对脏句强制重跑**：即使已有 `words` 也跑（覆盖上一轮 phase4 的"有 words 就跳过"逻辑）；句级重对齐改为 dirty 驱动而非 selected-rows 驱动。
3. **`QUndoStack` 全栈 + `redo`**：6 个 `QUndoCommand` 子类覆盖所有手动操作；菜单栏"编辑 → 撤销/重做"接入。
4. **表格行号列底色变黄**：脏句行号列 `QTableWidgetItem.setBackground(QBrush(QColor("#F5D061")))`，clear_dirty 时恢复默认。
5. **拆分/合并/插入/删除 silent 落地**：4 个工具按钮 + 1 个波形拖边界 = 5 类 silent 操作接入槽函数；不弹任何 `QMessageBox`。
6. **ffmpeg 错误文案更新**：`ensure_ffmpeg` 失败时的推荐顺序从 `scoop` 优先改为 `winget BtbN.FFmpeg.GPL static` 优先。

---

## 2. Current State Analysis（与上轮 plan 不同的点）

### 2.1 上轮 plan 已落地（不再重做）
- `core/asr_engine.py` `_split_text_by_punct` + `_merge_punct_into_words`（中文 5 句 + 51 token 测试通过）
- `subs/converter.py` `WordHighlightStyle.merge_punct=True` 默认
- `tests/test_punct_segmentation.py` 7 项 ALL PASSED
- `tests/phase4_e2e_test_short_talk.py` ALL METRICS GREEN

### 2.2 本 plan 新增的改动点

| 模块 | 上轮状态 | 本轮需新增/修改 |
|---|---|---|
| [subs/models.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/subs/models.py) | `Sentence` 无脏标记 | **+ `is_dirty: bool = False`** 字段；**`SubtitleProject` + `dirty_indices()`** + **`clear_dirty()`** |
| [core/align_engine.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/core/align_engine.py) | `align_project` 遇到已有 `words` 就跳过 | **修改**：脏句（`sent.is_dirty=True`）强制重跑（不跳过）；新增 `align_dirty_only` 入口 |
| [workers/align_worker.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/workers/align_worker.py) | `mode="project" / "sentences"` | **+ `mode="dirty"`**：自动用 `project.dirty_indices()` 作为目标索引 |
| [ui/subs_editor.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/subs_editor.py) | 4 工具按钮 setEnabled(False) 无 click 槽；编辑触发 signal 但不 mark dirty | **+ 5 个 `_on_*` 槽**：add/del/split/merge/未做（边界拖动在 waveform）；**+ `mark_dirty(idx)` 内部接口**（接 QUndoCommand） |
| [ui/waveform_view.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/waveform_view.py) | `set_audio` 只存不画；无脏色块；无边界拖手柄 | **+ 真实波形曲线降采样渲染**；**+ 句级色块（脏句黄色）**；**+ 边界拖手柄 emit `boundary_dragged`** |
| [ui/main_window.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/main_window.py) | `_act_undo`/`_act_redo` 占位；`_on_act_align_selected` 弹"请先选句" | **+ 持有 `QUndoStack`**；**+ `_on_act_align_dirty` 改走 dirty 模式**；**+ dirty 数量显示在状态栏**；**+ 波形拖边界 → 表格 + undo stack** |
| [core/audio_io.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/core/audio_io.py) | `ensure_ffmpeg` 错误文案推荐 scoop 优先 | **修改文案**：winget BtbN → scoop → gyan 顺序；提示"与你当前部署一致" |
| [tests/](file:///d:/AI-tools/Qwen3-Subtitle-Studio/tests) | 无 UI 冒烟 | **新增 `tests/test_ui_phase4_v2_smoke.py`**：QApplication + QUndoStack 6 命令单元 + dirty 行为 + 拆分/合并纯逻辑 + 视觉反馈断言 |

### 2.3 不在本 plan 范围

- ❌ mpv / libmpv-2.dll 真实嵌入
- ❌ ASS 样式编辑器
- ❌ 完整偏好设置面板（只在 ffmpeg 文案 + 注释里微调）
- ❌ 自动重对齐（必须手动 Ctrl+Shift+R）
- ❌ 跨项目的 .qsproj 持久化格式
- ❌ 多线程切分长音频（>5min 的 aligner 上限）
- ❌ 标点 word 处理方式变更（维持 merge_punct=True）

---

## 3. Proposed Changes

### 3.1 `subs/models.py` — `Sentence` + `SubtitleProject` 增脏标记

**改动 1**：`Sentence` 新增字段：
```python
@dataclass
class Sentence:
    text: str
    start_time: float
    end_time: float
    words: List[WordTimestamp] = field(default_factory=list)
    language: str = ""
    speaker: str = ""
    ass_style: str = ""
    ass_extra_tags: str = ""
    is_dirty: bool = False     # ← 新增：脏标记（被改过，待重对齐）
```

**改动 2**：`SubtitleProject` 新增方法：
```python
def dirty_indices(self) -> list[int]:
    """返回所有 is_dirty=True 的句索引（按 start_time 升序）"""
    return [i for i, s in enumerate(self.sentences) if s.is_dirty]

def mark_dirty(self, idx: int) -> None:
    if 0 <= idx < len(self.sentences):
        self.sentences[idx].is_dirty = True

def clear_dirty(self) -> None:
    for s in self.sentences:
        s.is_dirty = False
```

**改动 3**：`to_dict` / `from_dict` 透传 `is_dirty` 字段（保持可序列化）。

**改动 4**：`to_dict` 输出顺序保持，`is_dirty` 放在最后（避免破坏 JSON 兼容性）。

### 3.2 `core/align_engine.py` — 脏句强制重跑

**改动 1**：`align_project` 主循环，对脏句不跳过：
```python
# 旧：if sent.words: skip
# 新：
if sent.words and not sent.is_dirty:
    logger.debug("[Align] 跳过句 %d（已有 words，未脏）", idx)
    continue
# 脏句 → 强制清空旧 words 再对齐
sent.words = []
sent.is_dirty = False  # 标 dirty 时清；这里再清一次防御
```

**改动 2**：新增便捷函数：
```python
def align_dirty_only(
    project: SubtitleProject,
    *,
    model_manager: ModelManager,
    cfg: Optional[AlignConfig] = None,
) -> SubtitleProject:
    """只对 project.dirty_indices() 的句对齐；其他句原样保留。"""
    dirty = project.dirty_indices()
    if not dirty:
        return project
    cfg = cfg or AlignConfig()
    audio_np, sr = audio_io.load_audio(project.audio_path, mono=True)
    with model_manager.using_aligner() as _ref:
        for pos, idx in enumerate(dirty):
            # ... 单句对齐逻辑（与 _run_sentences_mode 等价）
            sent = project.sentences[idx]
            # ... align_sentence + emit/sent.words = ...
            sent.is_dirty = False
    project.sort()
    return project
```

### 3.3 `workers/align_worker.py` — 新增 `mode="dirty"`

**改动 1**：扩展 `mode` 校验：
```python
assert mode in ("project", "sentences", "dirty"), f"unknown mode: {mode!r}"
```

**改动 2**：新增 `_run_dirty_mode()`：
```python
def _run_dirty_mode(self) -> None:
    from core.align_engine import align_dirty_only
    indices = self._project.dirty_indices()
    if not indices:
        logger.info("[AlignWorker] dirty 模式无脏句")
        return
    self._on_progress(0, len(indices) + 1, f"重对齐 {len(indices)} 句")
    proj = align_dirty_only(
        self._project,
        model_manager=self._mm,
        cfg=self._cfg,
    )
    for idx in indices:
        self.sentence_aligned.emit(idx)
    self._on_progress(len(indices) + 1, len(indices) + 1, "完成")
```

### 3.4 `ui/subs_editor.py` — 4 工具按钮 + mark_dirty 接口

**改动 1**：内部接口 `mark_dirty_visual(idx)`：把第 0 列（行号列）的 `QTableWidgetItem.setBackground(QBrush(QColor("#F5D061")))`；`clear_dirty_visual()` 恢复 `Qt.NoBrush`。

**改动 2**：5 个 `_on_*` 槽函数（全部 silent，**不弹任何 QMessageBox**）：

```python
def _on_add(self) -> None:
    """在选中行后插入空行，start=上一行 end，end=start+3s，mark dirty。"""
    row = self._current_row()  # 选中行（多选用第一行）
    s = self._project.sentences[row]
    new_sentence = Sentence(
        text="", start_time=s.end_time, end_time=s.end_time + 3.0,
        language=s.language, words=[], is_dirty=True,
    )
    # 通过 undo command 走（见 §3.6）；这里只暴露接口
    self.add_sentence_requested.emit(row + 1, new_sentence)

def _on_del(self) -> None:
    """删除所有选中行（多选），mark dirty 邻句。"""
    rows = sorted({i.row() for i in self._table.selectedIndexes()}, reverse=True)
    self.delete_sentences_requested.emit(rows)

def _on_split(self) -> None:
    """在当前播放头位置（外部传入）拆选中句为 2 句。"""
    cut_time = self._playhead_s  # 由 MainWindow 在播放位置变更时 set
    row = self._current_row()
    self.split_sentence_requested.emit(row, cut_time)

def _on_merge(self) -> None:
    """合并多选 N 句为 1 句（按 row 顺序），text 拼接，words 保留并按 start 排序。"""
    rows = sorted({i.row() for i in self._table.selectedIndexes()})
    if len(rows) < 2:
        return
    self.merge_sentences_requested.emit(rows)

def _on_realign_dirty(self) -> None:
    """暴露给 MainWindow 的快捷入口：Ctrl+Shift+R 触发。"""
    self.realign_dirty_requested.emit()
```

**新增 Signal**（MainWindow 接收后构造 QUndoCommand）：
- `add_sentence_requested = Signal(int, object)`  # row, Sentence
- `delete_sentences_requested = Signal(list)`  # rows
- `split_sentence_requested = Signal(int, float)`  # row, cut_time
- `merge_sentences_requested = Signal(list)`  # rows
- `realign_dirty_requested = Signal()`

**改动 3**：`set_project` 时清空所有视觉脏色（防御性）。

**改动 4**：`_on_item_changed` 在 col 1/2 编辑时，无校验回退：保留用户输入；mark dirty + 触发 dirty_changed signal。

### 3.5 `ui/waveform_view.py` — 真实波形 + 句级色块 + 边界拖手柄

**改动 1**：`set_audio(audio_np, sample_rate)` 真实实现：
- 降采样到 ≤ 50000 采样点（min/max 每像素法）
- `self.curve.setData(x, y)` 画真实波形
- 缓存 `_downsampled_x`、`_downsampled_y`

**改动 2**：`set_project(project)` 增：
- 遍历 `project.sentences`，为每句画一个 `pg.LinearRegionItem` 半透明色块
- 句文本前 8 字 + 句号作为 label（用 `pg.TextItem` 浮在色块上）
- **脏句色块底色变黄**（QColor("#F5D061") alpha 60）；其他句默认灰

**改动 3**：边界拖手柄（每句 start/end 两条 `pg.InfiniteLine`，angle=90）：
- 拖动时实时 emit `boundary_dragged(idx, "start"|"end", new_s)`
- 颜色：脏句手柄黄色，其他句蓝色
- 拖动结束后 emit `boundary_drag_finished(idx, edge, new_s)`（区分实时和最终值）

**新增 Signal**：
- `boundary_dragged = Signal(int, str, float)`  # idx, "start"|"end", new_s
- `boundary_drag_finished = Signal(int, str, float)`

**新增方法**：
- `set_playhead(s)` 已有
- `update_dirty_visuals(indices: list[int])` —— MainWindow 调，把传入 idx 的色块/手柄/表格底色更新

### 3.6 `ui/commands.py`（**新建**）— QUndoCommand 6 子类

**新增文件** `ui/commands.py`：

```python
class EditTextCommand(QUndoCommand):
    """编辑单句文本：记录旧 text，redo 时设新 text，undo 时回旧 text + 标 dirty。"""

class EditTimeCommand(QUndoCommand):
    """编辑单句 start/end：记录旧 start/end，redo/undo 切换。"""

class AddSentenceCommand(QUndoCommand):
    """插入新句：undo 时删，redo 时在原位置插回。"""

class DeleteSentencesCommand(QUndoCommand):
    """删除多句：记录所有被删句的完整 deepcopy，undo 时按原 row 顺序恢复。"""

class SplitSentenceCommand(QUndoCommand):
    """按 cut_time 拆单句为 2 句：undo 时合并，redo 时按原句重拆。
       切点 words 算法：s.words 里 w.start < cut_time → 归前半；其余归后半。
       拆后两句都 mark dirty。"""

class MergeSentencesCommand(QUndoCommand):
    """合并多选 N 句为 1 句：undo 时按原 row 顺序拆回 N 句。
       新句 start=min, end=max, text=拼接, words=按 start 排序。"""

class BoundaryDragCommand(QUndoCommand):
    """波形拖单句边界：记录旧 start/end，redo/undo 切换 + mark dirty。"""
```

**每个 Command 必须**：
- `redo()`：应用新状态 + 调 `editor.mark_dirty_visual(idx)` + 发 `project_changed` signal
- `undo()`：回退状态 + 调 `editor.mark_dirty_visual(idx)` + 发 `project_changed` signal
- 不弹任何提示框（完全 silent）

**新增** `ui/commands.py:build_undo_stack(editor, project, on_dirty_change)` 工厂：
- 返回 `QUndoStack` 实例
- 注册所有 6 个 command 类（用 `QUndoStack.createUndoAction` / `createRedoAction` 给 MainWindow 接菜单）

### 3.7 `ui/main_window.py` — Undo/Redo 接入 + dirty 驱动重对齐 + 波形

**改动 1**：新增成员：
```python
from PySide6.QtWidgets import QUndoStack
self._undo_stack = QUndoStack(self)
self._undo_stack.setUndoLimit(100)  # 上限 100 步
```

**改动 2**：菜单栏接入（已有 `_act_undo` / `_act_redo` 占位）：
```python
self._act_undo = self._undo_stack.createUndoAction(self, "撤销")
self._act_undo.setShortcut(QKeySequence.Undo)  # Ctrl+Z
self._act_redo = self._undo_stack.createRedoAction(self, "重做")
self._act_redo.setShortcut(QKeySequence.Redo)  # Ctrl+Y
```

**改动 3**：`_on_act_align_dirty` 改造（**不弹任何确认框**）：
```python
def _on_act_align_dirty(self) -> None:
    if self._project is None:
        return
    if not self._project.dirty_indices():
        # silent 提示（不弹框）
        self._sb_mode.setText("模式：没有待重对齐的句")
        return
    if self._ensure_no_running_worker() is False:
        return
    worker = AlignWorker(
        self._project, mode="dirty",
        model_manager=self._model_manager, parent=self,
    )
    self._bind_and_start_worker(worker, mode_label=f"重对齐 {len(self._project.dirty_indices())} 句")
    worker.sentence_aligned.connect(self._on_dirty_sentence_aligned)

def _on_dirty_sentence_aligned(self, idx: int) -> None:
    self.editor.refresh_row(idx)
    self.editor.clear_dirty_visual(idx)
    self.waveform.update_dirty_visuals(self._project.dirty_indices())
    self._update_dirty_count()

def _update_dirty_count(self) -> None:
    n = len(self._project.dirty_indices()) if self._project else 0
    if n > 0:
        self._sb_mode.setText(f"模式：{n} 句待重对齐（Ctrl+Shift+R）")
    else:
        self._sb_mode.setText("模式：空闲")
```

**改动 4**：`Ctrl+Shift+R` 改走 `_on_act_align_dirty`（不再按 selected rows）。

**改动 5**：接 SubsEditor 5 个 `_on_*` 信号 → 构造对应 QUndoCommand → push 到 stack：
```python
self.editor.add_sentence_requested.connect(self._push_add_cmd)
self.editor.delete_sentences_requested.connect(self._push_del_cmd)
self.editor.split_sentence_requested.connect(self._push_split_cmd)
self.editor.merge_sentences_requested.connect(self._push_merge_cmd)
self.editor.realign_dirty_requested.connect(self._on_act_align_dirty)
```

**改动 6**：接 `waveform.boundary_drag_finished` → 构造 `BoundaryDragCommand` → push。

**改动 7**：媒体打开后自动调 `audio_io.load_audio(project.audio_path)` → `waveform.set_audio(...)`。

**改动 8**：所有编辑完成后调 `_update_dirty_count()` 刷新状态栏。

### 3.8 `core/audio_io.py` — ffmpeg 错误文案

**改动 1**：`ensure_ffmpeg` 失败时 `RuntimeError` 文案推荐顺序：

```
解决办法（选其一即可，推荐第 1 条，与你当前部署一致）：
  1) winget 安装 BtbN GPL static 版（推荐）：winget install BtbN.FFmpeg.GPL static
  2) scoop 安装完整版：scoop install ffmpeg（确认 shim 指向 full 版）
  3) 下载 gyan.dev essentials（www.gyan.dev/ffmpeg/builds），解压到 PATH 最前面
```

**改动 2**：探测成功日志中加版本号（`ffmpeg -version` 第一行），便于排查多个 ffmpeg 时的版本混淆。

**改动 3**：不动 `_iter_ffmpeg_candidates` / `_ffmpeg_smoke_extract_ok` 逻辑（已能自动找到 winget 装的 ffmpeg）。

### 3.9 `core/app_config.py` — 注释补充

**改动 1**：在 `PathPreferences.ffmpeg_path` 字段上方加注释：

```python
# 用户若装了多个 ffmpeg（如 winget 装的 BtbN.FFmpeg.GPL static + IDE 自带的裁剪版），
# 可以在 preferences.json 的 paths.ffmpeg_path 写绝对路径覆盖自动探测。
# 推荐：winget install BtbN.FFmpeg.GPL static
ffmpeg_path: str = ""
```

**不动** dataclass 字段（已存在）。

### 3.10 `tests/test_ui_phase4_v2_smoke.py`（**新建**）— UI 集成冒烟

只做 QApplication + 纯逻辑断言（不跑模型）：

```python
def test_sentence_dirty_field():
    s = Sentence(text="hi", start_time=0, end_time=1)
    assert s.is_dirty is False
    s.is_dirty = True
    assert s.is_dirty

def test_project_dirty_indices():
    p = SubtitleProject(audio_path="x", sentences=[
        Sentence(text="a", start_time=0, end_time=1, is_dirty=True),
        Sentence(text="b", start_time=1, end_time=2, is_dirty=False),
        Sentence(text="c", start_time=2, end_time=3, is_dirty=True),
    ])
    assert p.dirty_indices() == [0, 2]
    p.clear_dirty()
    assert p.dirty_indices() == []

def test_split_sentence_pure():
    # 用 mock Sentence 验证 _split_sentence_at（抽到 commands.py）行为
    s = Sentence(
        text="abc def", start_time=0, end_time=2,
        words=[
            WordTimestamp("a", 0.0, 0.3),
            WordTimestamp("b", 0.3, 0.6),
            WordTimestamp(" ", 0.6, 0.7),
            WordTimestamp("d", 0.7, 1.0),
            WordTimestamp("e", 1.0, 1.3),
            WordTimestamp("f", 1.3, 1.6),
        ],
    )
    left, right = _split_sentence_at(s, cut_time=0.65)
    assert left.text == "abc " and left.end_time == 0.65
    assert right.text == "def" and right.start_time == 0.65

def test_merge_sentences_pure():
    a = Sentence(text="hello", start_time=0, end_time=1)
    b = Sentence(text="world", start_time=1.2, end_time=2.0)  # 0.2s gap
    merged = _merge_sentences([a, b])
    assert merged.text == "hello world"
    assert merged.start_time == 0
    assert merged.end_time == 2.0
    # 注：gap 不做回填（用户决策：silent，不校验）

def test_undo_stack_round_trip():
    # 用 QApplication + 真实 QUndoStack 验证 6 个 command 都能 undo/redo
    app = QApplication.instance() or QApplication([])
    editor = SubsEditor()
    project = SubtitleProject(audio_path="x", sentences=[
        Sentence(text="abc", start_time=0, end_time=1, is_dirty=False),
    ])
    editor.set_project(project)
    stack = QUndoStack()
    stack.push(EditTextCommand(project, 0, "abc", "abcd"))
    assert project.sentences[0].text == "abcd"
    stack.undo()
    assert project.sentences[0].text == "abc"
    stack.redo()
    assert project.sentences[0].text == "abcd"

def test_visual_dirty_highlight():
    # 验证 mark_dirty_visual 把行号列底色设为黄色
    app = QApplication.instance() or QApplication([])
    project = SubtitleProject(audio_path="x", sentences=[
        Sentence(text="a", start_time=0, end_time=1),
    ])
    editor = SubsEditor()
    editor.set_project(project)
    editor.mark_dirty_visual(0)
    it = editor._table.item(0, 0)  # 行号列
    assert it.background().color().name() == "#f5d061"
    editor.clear_dirty_visual(0)
    assert it.background().style() == Qt.NoBrush  # 恢复默认
```

---

## 4. Assumptions & Decisions

| 决策点 | 选择 | 依据 |
|---|---|---|
| 标点 word 在字级导出时 | 合并到前一字（`merge_punct=True` 默认） | 用户答 |
| 拆分/合并/插入/删除的提示 | **完全 silent**（不弹任何 QMessageBox） | 用户原话 |
| 重对齐触发方式 | 手动 Ctrl+Shift+R，不弹确认框 | 用户原话 + 决策 5 |
| Undo 范围 | QUndoStack 全栈（6 个 command）+ redo，上限 100 步 | 用户答 |
| 视觉反馈 | 仅改过的行号列底色变黄（#F5D061），无闪烁 | 用户答 |
| 拆分光标非法时 | 完全 silent，按光标位置直接拆（不回退、不弹框） | 用户答 |
| 合并时句间有 gap | 直接拼接 text/words，不管时间 gap | 用户答 |
| max_sentence_chars 默认值 | 维持 24 | 上轮已决策 |
| ffmpeg 部署推荐 | BtbN.FFmpeg.GPL static（winget）→ scoop → gyan 顺序 | 用户原话 |
| 重对齐后导出 | 内存里 project 更新；导出需要用户主动点菜单 | 不在 scope |
| 波形拖边界 | emit `boundary_dragged` + `boundary_drag_finished`；QUndoCommand 接 finished 事件 | 上轮已决策 + 视觉反馈 |
| 脏色块同步 | 表格行号 + 波形色块 + 波形手柄 三处同时变黄 | 视觉一致性 |
| dirty 持久化 | `is_dirty` 字段进 `to_dict`/`from_dict`，可保存到 JSON | 与 Sentence 其他字段一致 |
| 重新对齐触发后是否清 dirty | 是（`align_dirty_only` 完成后 `sent.is_dirty = False`） | 流程闭环 |

---

## 5. Verification

按以下顺序验证（每步必须绿才能进下一步）：

### 5.1 单元冒烟（无模型）

```bash
.venv/Scripts/python tests/test_ui_phase4_v2_smoke.py
```

**预期全绿**：
- `test_sentence_dirty_field`
- `test_project_dirty_indices`
- `test_split_sentence_pure`（含「光标在句外 → 仍按光标拆」）
- `test_merge_sentences_pure`（含「句间有 gap → 仍合并」）
- `test_undo_stack_round_trip`（6 个 command 各跑一次 undo/redo）
- `test_visual_dirty_highlight`（行号列底色变黄 + 恢复）

### 5.2 回归验证

```bash
.venv/Scripts/python tests/phase2_import_smoke.py
.venv/Scripts/python tests/phase3_subs_smoke.py
.venv/Scripts/python tests/test_punct_segmentation.py
```

**必须全绿**（标点切分 + 5 句中文测试不退化）。

### 5.3 E2E 验证（不动）

```bash
.venv/Scripts/python tests/phase4_e2e_test_short_talk.py
```

**必须** `ALL METRICS GREEN ✔`（上轮已通过；本 plan 不动 ASR/align 核心）。

### 5.4 ffmpeg 验证

```bash
python -c "from core.audio_io import ensure_ffmpeg; print(ensure_ffmpeg())"
```

应返回 winget 装的 BtbN 路径（而非 IDE 裁剪版）。

### 5.5 UI 手动验证（开发者点）

启动 `python main.py`：
1. 打开 `test-short-talk.mp4` → 波形自动加载，6 个句级色块 + 6 条文本
2. 改第 3 句文本（漏字补一个）→ **行号列变黄**，状态栏 `模式：1 句待重对齐（Ctrl+Shift+R）`
3. 拖第 3 句波形上的开始边界 → 表格对应 cell 更新，**行号列仍黄**，不弹任何框
4. 选中第 4 句 + 第 5 句 → 点"合并选中" → 表格合并为 1 行，**行号列变黄**，不弹任何框
5. Ctrl+Z 一次 → 撤销"合并"，回到 2 行，**行号列恢复原色**
6. Ctrl+Z 再一次 → 撤销"拖边界"，回到原 start，**行号列恢复原色**
7. Ctrl+Y → 重做"拖边界"，**行号列变黄**
8. Ctrl+Shift+R → 状态栏 `模式：重对齐 N 句…` → 完成后 `模式：空闲`，**所有行号列恢复原色**
9. 选中第 3 句 → 点"在光标处拆分"（光标在句中点）→ 表格拆为 2 行，**2 行行号列变黄**
10. 启动 5 个操作 → Ctrl+Z 连按 5 次 → 全部撤销，行号列全部恢复
11. 启动 5 个操作 → Ctrl+Z 3 次 → Ctrl+Y 2 次 → 验证 redo 工作
12. 切到 6 个编辑过的状态 → 关闭 app → 重开 → 验证 `is_dirty` 持久化（暂不在本 plan 范围；只在 phase5 加 .qsproj）

---

## 6. Out of Scope（明确不做）

- ❌ mpv / libmpv-2.dll 真实嵌入
- ❌ ASS 样式编辑器（Aegisub Import/Export）
- ❌ 完整偏好设置面板（只更新了 ffmpeg 文案 + 注释）
- ❌ 自动重对齐策略
- ❌ 跨项目的 .qsproj 持久化（`is_dirty` 字段已支持 JSON 序列化，phase5 再做完整持久化）
- ❌ 多线程切分长音频（>5min 的 aligner 上限）
- ❌ 标点 word 处理方式变更（维持 merge_punct=True）
- ❌ 拆分/合并/插入/删除的任何确认弹窗
- ❌ 重对齐的任何确认弹窗
- ❌ 视觉闪烁动画（用户决策：仅行号列底色变黄，无闪烁）

---

## 7. 文件变更清单

| 路径 | 变更类型 | 行数估计 |
|---|---|---|
| [subs/models.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/subs/models.py) | 修改 | +25 行 |
| [core/align_engine.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/core/align_engine.py) | 修改 | +35 行 |
| [workers/align_worker.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/workers/align_worker.py) | 修改 | +25 行 |
| [ui/subs_editor.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/subs_editor.py) | 修改 | +90 行 |
| [ui/waveform_view.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/waveform_view.py) | 修改 | +120 行 |
| [ui/commands.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/commands.py) | **新建** | ~250 行 |
| [ui/main_window.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/main_window.py) | 修改 | +60 行 |
| [core/audio_io.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/core/audio_io.py) | 修改 | +12 行 |
| [core/app_config.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/core/app_config.py) | 修改 | +3 行（注释） |
| [tests/test_ui_phase4_v2_smoke.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/tests/test_ui_phase4_v2_smoke.py) | **新建** | ~120 行 |

**合计**：2 个新文件 + 8 处修改，约 740 行净增。
