# THIRD_PARTY_NOTICES — 第三方组件、模型与资产授权清单

> 本文件是工程级授权信息记录，**不构成法律意见**；最终合规判断请由具备资质的人复核。
>
> **范围必须区分**：根目录 `LICENSE` 只授权本项目自有源代码，许可证为 GPL-3.0；GPL 本身允许商业使用。当前“个人 / 非商业”定位来自默认运行组合中的额外约束：PySide6-Fluent-Widgets 上游双许可说明要求商业项目购买商业许可，默认 MMS 权重为 CC-BY-NC-4.0。商业部署必须分别解决这些第三方授权，不能把限制错误归因于 GPL。

## 1. 代码依赖（Python 包）

| 组件 | 版本约束 | 用途 | 许可证 |
|---|---|---|---|
| Python | 3.12 | 运行时 | PSF License |
| PyTorch | 2.13+cu130 | 推理框架 | BSD-3-Clause |
| flash-attn | 2.8.3 | 高效注意力 | BSD-3-Clause |
| transformers | >=5.13,<6 | Qwen3-ASR/Aligner 原生 API | Apache-2.0 |
| accelerate | >=1.0,<2 | 推理优化 | Apache-2.0 |
| onnxruntime / onnxruntime-gpu | >=1.27,<2 | ONNX 推理（人声分离/MMS；默认 CPU，目标机 GPU 覆盖） | MIT |
| PySide6 | >=6.8,<7 | GUI 框架 | LGPL-3.0（完整版 = Essentials + Addons） |
| PySide6-Fluent-Widgets | >=1.8,<2 | Fluent UI 组件 | 上游双许可：其说明将非商业用途置于 GPL-3.0，商业用途要求商业许可 |
| pyqtgraph | >=0.13,<1 | 波形渲染 | MIT |
| python-mpv | >=1.0,<2 | 可选：libmpv ctypes 绑定（真 ASS 预览） | 随底层 libmpv：GPL-2.0-or-later 或 LGPL-2.1-or-later（取决于 libmpv 构建） |
| librosa | >=0.10,<1 | 音频分析 | ISC |
| soundfile | >=0.12,<1 | WAV 读写 | BSD-3-Clause |
| numpy | >=1.26,<3 | 数值计算 | BSD-3-Clause |
| uroman | >=1.3,<2 | 罗马化 | Apache-2.0 |
| pykakasi | 可选 | 日语汉字读音 | GPL-3.0 |
| nagisa | 可选（ja） | 日文分词 | MIT |
| soynlp | 可选（ko） | 韩文分词 | MIT |
| packaging | >=24,<27 | 版本解析（探针） | BSD-2-Clause / Apache-2.0 |
| pytest / ruff | 开发依赖 | 测试/静态检查 | MIT |

> 证据入口： [PySide6-Fluent-Widgets PyPI](https://pypi.org/project/PySide6-Fluent-Widgets/) 明示双许可和商业许可要求。这里记录的是上游额外授权政策，不应表述为“GPL 自身禁止商业”。

## 2. 模型权重（运行时下载，不入库）

| 模型 | 用途 | 来源 | 授权 |
|---|---|---|---|
| Qwen3-ASR-1.7B-hf | 语音识别 | Qwen / ModelScope / HF | Apache-2.0（Qwen 官方） |
| Qwen3-ForcedAligner-0.6B-hf | 口语对齐 | Qwen / ModelScope / HF | Apache-2.0（Qwen 官方） |
| mms-300m-1130-forced-aligner-ONNX | 歌词对齐（ONNX 转换） | onnx-community（上游 MahmoudAshraf/mms-300m-1130-forced-aligner） | **CC-BY-NC-4.0**；默认权重不得用于商业用途 |
| Kim_Vocal_2.onnx | 人声分离（MDX-Net） | Politrees/UVR_resources（HF） | MIT（以原仓库元数据为准；使用前请复核） |

> 模型证据入口：Qwen 模型卡标注 Apache-2.0；[MMS 上游模型卡](https://huggingface.co/MahmoudAshraf/mms-300m-1130-forced-aligner) 元数据标注 `cc-by-nc-4.0`。ONNX 转换不会自动消除上游权重许可。

## 3. 工具与外部二进制

| 组件 | 用途 | 授权 |
|---|---|---|
| FFmpeg（推荐 BtbN **GPL static** 版，winget：`BtbN.FFmpeg.GPL`） | 抽音/转码；项目当前不提供硬字幕烧录 | GPL-3.0（BtbN 构建）；项目**不捆绑**，由使用者自行安装 |
| libmpv-2.dll（mpv.net，或 mpv 官方渠道列出的 shinchiro v3 dev 构建） | 可选：真 ASS 预览（已接线，自动启用） | LGPL/GPL 组件组合取决于具体构建；由使用者自行安装，不进入源码替换包 |

## 4. 测试与示例资产

| 资产 | 说明 | 授权 |
|---|---|---|
| test-short-talk.mp4 | 13.76s 中文短语音：音频由 **VoxCPM2** 合成，视频为**格式工厂**直接转出的无画面视频 | 自研/自生成（合成音频 + 无画面视频），随仓库以 GPLv3 分发 |
| test-short-talk.txt | 歌词/文本真值 | 同左 |
| test-short-talk.ass | 句/字级时间真值——由本项目用 **Qwen3-ForcedAligner-0.6B** 对齐生成 | 同左（本项目产物） |
| test-short-talk.qss.json | 示例工程（媒体路径已相对化，跨机可打开；audio_path 留空，打开时自动重提取） | 同左 |
| assets/icon.png、icon.ico | 应用图标（深色圆角矩形 + 声波/字幕条/播放键）——AI 生成原创图，无第三方版权素材 | 自研/自生成，随仓库以 GPLv3 分发 |

## 5. 商业部署边界

当前默认工程不能仅凭根目录 GPL-3.0 就宣称“可直接商业部署”。至少需要：

1. 向 PySide6-Fluent-Widgets 上游取得适用的商业许可，或替换该 GUI 组件并完成代码复核；
2. 替换默认 CC-BY-NC-4.0 MMS 权重，或取得权利人额外授权；
3. 按实际取得的 `libmpv-2.dll` 构建核对 LGPL/GPL 组合及动态分发义务；
4. 对模型、FFmpeg、图标/示例资产和最终安装包重新生成许可证清单与 SBOM。

## 6. 许可证证据留档建议

- 逐项在 `pip show <pkg>` / 包元数据中核对 `License` 字段并留档；
- 模型下载时保留 Model Card / 仓库 LICENSE 快照；
- 分发前产出 SBOM（如 `pip freeze` + 模型清单 + 哈希）。
