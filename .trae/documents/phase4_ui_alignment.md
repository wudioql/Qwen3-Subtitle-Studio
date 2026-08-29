# Phase 4 UI 重构 + 句级重对齐 + ffmpeg 部署调整 实施计划

> 用户最新评估：标点维持「合并到前一字」默认行为；句级重对齐走「手动确认」路径；句级手调范围含编辑 start/end + 光标处拆分 + 合并选中；ffmpeg 改 winget 安装的 BtbN.FFmpeg.GPL static。

---

## 1. Summary

完成三件事：

1. **UI 句级手调能力落地**：在 SubsEditor 表格里编辑 start/end、按光标处拆分、合并多选；WaveformView 实现真实音频加载 + pyqtgraph 句级色块渲染 + 边界拖手柄。
2. **句级重对齐手动触发**：在 MainWindow 新增"对齐当前修改过的句"动作（Ctrl+Shift+R 已存在但未接逻辑），仅对打 dirty 标记的句跑 Aligner。
3. **ffmpeg 错误文案更新**：从「scoop install ffmpeg」改为「winget install BtbN.FFmpeg.GPL static」+ 同时保留 scoop/BtbN/gyan 三选项。

不改 ASR / 对齐核心逻辑（已通过 E2E 验证）；不改 exporters 标点处理（维持 merge_punct=True 默认）。

---

## 2. Current State Analysis

### 已就绪

- `core/asr_engine.py` `_split_text_by_punct` + `_merge_punct_into_words`（Phase 4 上半段已落地）
- `core/align_engine.py` `align_sentence` / `align_project` 双模式对齐
- `subs/converter.py` `WordHighlightStyle.merge_punct` 标点合并已就位
- `subs/exporters.py` 6 种产物全部 OK
- `workers/align_worker.py` `mode="project"` / `mode="sentences"` 两模式
- `tests/phase4_e2e_test_short_talk.py` **ALL METRICS GREEN**

### 缺口

| 模块 | 现状 | 缺口 |
|---|---|---|
| [ui/subs_editor.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/subs_editor.py) | 4 个工具按钮存在但 `setEnabled(False)` 无 click 槽；编辑仅触发 `text_changed` / `time_changed` signal | 拆分/合并/插入/删除无逻辑；时间编辑无校验（可能 end<start） |
| [ui/waveform_view.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/waveform_view.py) | `set_audio` 只存数据不画；`region` (LinearRegion) 已存在但未启用 | 无真实波形曲线；无句级色块；无边界拖手柄 |
| [ui/main_window.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/ui/main_window.py) | `_on_act_align_selected` 已存在但只接 `worker.sentence_aligned → editor.refresh_row` | 缺 dirty 追踪；缺拆分/合并/插入/删除的菜单/按钮接入；缺"波形加载"动作 |
| [core/audio_io.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/core/audio_io.py) | `_iter_ffmpeg_candidates` + `ensure_ffmpeg` 完整 | 错误文案推荐 scoop 优先（与用户实际部署不符） |
| [core/app_config.py](file:///d:/AI-tools/Qwen3-Subtitle-Studio/core/app_config.py) | `PathPreferences.ffmpeg_path` 已支持用户覆盖 | 无 winget / BtbN 的提示 |

---

## 3. Proposed Changes

### 3.1 `core/asr_engine.py` — `_text_only_to_sentences` 微调

`max_sentence_chars` 已存在（默认 24 = 硬切上限），已能覆盖「每句最多字数」开关。无改动。

### 3.2 `core/audio_io.py` — `ensure_ffmpeg` 错误文案

修改 `ensure_ffmpeg` 的 `RuntimeError` 推荐顺序：

**改动 1**：文案里把 BtbN（winget 安装的 GPL static 版）放最前，scoop 放第二，gyan 留作 fallback。

**改动 2**：补一句 winget 提示：
```
推荐（与你环境一致）：winget install BtbN.FFmpeg.GPL static
```

**改动 3**：探测成功日志中加版本探测（`ffmpeg -version` 第一行），便于排查 PATH 里多个 ffmpeg 时的版本混淆。

**不动**：`_iter_ffmpeg_candidates` 逻辑（PATH 枚举已能自动找到 winget 装的 ffmpeg）。

### 3.3 `core/app_config.py` — `PathPreferences.ffmpeg_path` + 探测文档

不动 dataclass 字段（已存在）。在 `app_config.py` 顶部注释里补一句：

> 用户若装了多个 ffmpeg（如 winget 装的 BtbN.FFmpeg.GPL static + IDE 自带的裁剪版），可在 `.config/preferences.json` 的 `paths.ffmpeg_path` 写绝对路径覆盖探测。

### 3.4 `subs/models.py` — `Sentence` 增 `is_dirty` 字段

**新增字段** `is_dirty: bool = False` —— 标记该句的文本/start/end 被用户改过、需要重对齐。

**新增方法** `mark_dirty()` —— 设 `is_dirty=True` 并发出 `dirty_changed` Qt Signal（可选，QObject 化后）。

**`SubtitleProject` 新增**：
- `dirty_indices() -> list[int]`
- `clear_dirty() -> None`

### 3.5 `ui/subs_editor.py` — 工具按钮接入 + 编辑校验

**改动 1** `_on_item_changed`：在 col 1/2 编辑时，校验 `start < end` 且 `0 <= start < media_duration`；不合法回退 + 状态栏告警；不自动重对齐（用户决策：手动触发）。

**改动 2** `_btn_add.clicked` `_btn_del.clicked` `_btn_split.clicked` `_btn_merge.clicked` 接 4 个槽：
- `_on_add()`：在选中行后插入空行，start=选中句 end，end=start+3s，mark_dirty
- `_on_del()`：删选中行（多选支持），mark 邻句 dirty
- `_on_split()`：在当前播放头位置拆选中句为 2 句；words 按时间切两半；mark 新两句 dirty
- `_on_merge()`：把多选 N 句合并为 1 句，text 拼接，words 保留并按 start 排序，start=min、end=max；mark 新句 dirty

**改动 3** `set_project` 时同步绑定行被选时高亮波形游标（已有 row_selected → 波形 set_playhead）。

**不改** `merge_punct` 默认值（用户决策：保留 True）。

### 3.6 `ui/waveform_view.py` — 真实音频加载 + 句级色块 + 边界拖手柄

**改动 1** `set_audio(audio_np, sample_rate)` 真实实现：
- 降采样到 ≤ 50000 采样点（pyqtgraph 渲染上限）
- 算每像素 max abs 作为曲线 y 值
- `pg.PlotCurveItem.setData` 真实画
- 缓存 `_downsampled_x`、`_downsampled_y`

**改动 2** `set_project(project)` 增：遍历 `project.sentences` 画句级色块（`pg.LinearRegionItem` 半透明 + 文本 label 显示句号 + 头几字）。

**改动 3** 新增 `set_dirty_border(idx, start_s, end_s)`：在指定句的边界画 `pg.InfiniteLine` 拖手柄，拖动后 emit `boundary_dragged(idx, edge, new_time)` Signal。

**新增 Signal**：
- `boundary_dragged = Signal(int, str, float)` —— idx, "start"|"end", new_s
- `selection_synced = Signal(int)` —— 表格选行时高亮句级色块

### 3.7 `ui/main_window.py` — 动作接入 + dirty 追踪 + 拆分/合并/重对齐

**改动 1** 新增 Signal `_mark_dirty(idx)` 由 SubsEditor → MainWindow → SubtitleProject.sentences[idx].mark_dirty()。

**改动 2** 表格行被改时（text_changed / time_changed）：
- 更新 `project.sentences[idx]` 的 text / start / end
- mark dirty
- **不**自动重对齐（用户决策：手动触发）

**改动 3** `_on_act_align_selected` 改造：
- 读 `project.dirty_indices()`
- 若空 → 状态栏提示"没有待重对齐的句"
- 若非空 → 弹 `QMessageBox.question`："将对 N 句重新对齐（这会重新计算字级时间戳，原手动拖的边界会失效），继续？"
- 确认后起 `AlignWorker(mode="sentences", indices=dirty)`
- 完成后 `project.clear_dirty()`

**改动 4** 拆分/合并按钮接入（`editor._btn_split` / `editor._btn_merge` 的 click 已接，MainWindow 这边只把 dirty 状态传导给 status bar / 顶部"待重对齐 N 句"标签）。

**改动 5** 新增工具栏动作 "📊 加载波形"，调用 `audio_io.load_audio(project.audio_path)` → `waveform.set_audio(...)` —— 默认在 open media 时自动调用。

**改动 6** 接 `waveform.boundary_dragged` → 更新 `project.sentences[idx].start` / `.end` + mark dirty + 同步表格行（不自动重对齐）。

### 3.8 `ui/player_panel.py` — 不动

播放器骨架已 OK；不涉及本次范围。

### 3.9 新增 `tests/test_ui_phase4_smoke.py` — UI 集成冒烟

只做 QApplication 实例化 + 信号连接 + 拆分/合并/重对齐入口可达性检查（不实际跑模型，依赖已有的 dirty 标志逻辑）。

### 3.10 不改动的清单（用户已决策）

- ❌ 不动 `subs/converter.py` `merge_punct_words`：维持 `merge_punct=True` 默认（用户决策）
- ❌ 不动 `_split_text_by_punct` / `_merge_punct_into_words`：上一轮已完成且 E2E 验证
- ❌ 不自动重对齐：所有重对齐走手动 Ctrl+Shift+R
- ❌ 不做「播放头处拆分」：用户没勾
- ❌ 不做 ASS 样式编辑器、设置面板、mpv 嵌入：超出本次范围

---

## 4. Assumptions & Decisions

| 决策点 | 选择 | 依据 |
|---|---|---|
| 标点 word 在字级导出时 | 合并到前一字（`merge_punct=True` 默认） | 用户答：合并到前一字（默认） |
| 句级重对齐触发方式 | 手动 Ctrl+Shift+R + 弹窗确认 | 用户答：手动改完后再手动确认全部重新对齐 |
| 改 start/end 是否自动重对齐 | 否，仅 mark dirty；改文本/拆分/合并/重对齐后 才覆盖 | 用户原话"手动主要还是改错字和分句" |
| UI 句级手调范围 | 编辑 start/end + 光标处拆分 + 合并选中 | 用户答：三项均选 |
| 频谱上拖动句级边界 | 必做（WaveformView `boundary_dragged` Signal） | 用户原话"在频谱上预览和拖动" |
| max_sentence_chars 默认值 | 保持 24 | 用户原话"可以根据设置每句最多字数来得到更贴近预期的结果"——已可调 |
| ffmpeg 部署 | BtbN.FFmpeg.GPL static（winget 装） | 用户原话 |
| 拆分后是否重对齐 | 是（mark dirty，弹窗确认后重对齐） | 与"手动主要改错字和分句"一致 |
| 合并后是否重对齐 | 是（mark dirty，弹窗确认后重对齐） | 同上 |
| dirty 标记追踪位置 | `Sentence.is_dirty` 字段 + `SubtitleProject.dirty_indices()` | 简单可序列化；不需要 Qt Signal 跨线程 |

---

## 5. Verification

按以下顺序验证（每步必须绿才能进下一步）：

### 5.1 unit 验证（无模型）

```bash
.venv/Scripts/python tests/test_ui_phase4_smoke.py
```
- QApplication 实例化 OK
- SubsEditor 4 个工具按钮 enabled 且 click 槽已连
- WaveformView `boundary_dragged` Signal 存在
- Sentence.is_dirty / mark_dirty 行为正确
- SubtitleProject.dirty_indices / clear_dirty 行为正确

### 5.2 回归验证

```bash
.venv/Scripts/python tests/phase2_import_smoke.py   # 必须 ALL PASSED
.venv/Scripts/python tests/phase3_subs_smoke.py     # 必须 ALL PASSED
.venv/Scripts/python tests/test_punct_segmentation.py  # 必须 ALL PASSED
```

### 5.3 E2E 验证

```bash
.venv/Scripts/python tests/phase4_e2e_test_short_talk.py
```
必须 `[EXIT] ALL METRICS GREEN ✔`；6 句分句 + 52 token + CER ≤ 20% + 6 种导出 OK。

### 5.4 ffmpeg 验证

```bash
python -c "from core.audio_io import ensure_ffmpeg; print(ensure_ffmpeg())"
```
应返回 winget 装的 BtbN 路径（而非 IDE 裁剪版）。

### 5.5 UI 手动验证（开发者点）

启动 `python main.py`：
1. 打开 `test-short-talk.mp4` → 波形自动加载，6 个句级色块 + 6 条文本
2. 双击第 2 句 → 播放器跳转 + 波形游标移动
3. 改第 3 句文本（漏字补一个）→ 状态栏 `待重对齐 1 句`
4. 选中第 4 句 + 第 5 句 → 点"合并选中"→ 弹"合并 N 句 → 1 句"确认框 → 状态栏 `待重对齐 1 句`
5. Ctrl+Shift+R → 弹"将对 N 句重新对齐"确认框 → 确认 → 字级时间刷新
6. 拖第 3 句波形上的开始边界 → 表格对应 cell 更新，但不自动重对齐
7. 点导出菜单 → 6 种产物落盘 + 标点合并行为与 phase4 e2e 一致

---

## 6. Out of Scope (本次明确不做)

- mpv / libmpv-2.dll 真实嵌入播放器
- ASS 样式编辑器（Aegisub Import/Export）
- 完整的偏好设置面板（只更新了 ffmpeg 文案 + 注释）
- 「播放头处拆分」动作（用户未选）
- 自动重对齐策略（用户明确要求手动）
- 跨项目的 .qsproj 持久化格式
- 多线程切分长音频（>5min 的 aligner 上限）
