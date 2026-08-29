"""core.constants — 全局路径与常量

所有路径相对项目根目录解析，方便打包后改写。
"""

from __future__ import annotations

import os
from pathlib import Path


# ── 项目根目录（main.py 所在目录）───────────────────────────────────────
_FILE_DIR = Path(__file__).resolve().parent  # core/
PROJECT_ROOT: Path = _FILE_DIR.parent

# ── 模型与数据路径 ─────────────────────────────────────────────────────
MODEL_DIR: Path = PROJECT_ROOT / "models"
ASR_MODEL_PATH: Path = MODEL_DIR / "Qwen3-ASR-1.7B-hf"
ALIGNER_MODEL_PATH: Path = MODEL_DIR / "Qwen3-ForcedAligner-0.6B-hf"
KIM_VOCAL_MODEL_PATH: Path = MODEL_DIR / "Kim_Vocal_2.onnx"
MMS_ALIGNER_MODEL_PATH: Path = MODEL_DIR / "mms-300m-1130-forced-aligner-onnx"

# 临时目录（音频提取、分块等中间文件）。
# QSS_TEMP_DIR 环境变量可重定向（与 app_config 的 QSS_CONFIG_DIR 同法）：
# 只读安装目录/多用户/便携版/测试隔离都可用它指到可写位置。
# **不在 import 期创建目录**（库被导入不应有写盘副作用）；真正写前用 ensure_temp_dir()。
TEMP_DIR: Path = Path(os.environ["QSS_TEMP_DIR"]) if os.environ.get("QSS_TEMP_DIR") else PROJECT_ROOT / ".temp"


def ensure_temp_dir() -> Path:
    """惰性创建临时目录（parents=True，可一次建多级）并返回其路径。"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR

# 临时文件保留天数（启动清理时删除超过此天数的残留 wav / chunk 子目录）
TEMP_MAX_AGE_DAYS: int = 3

# 日志轮转上限（字节），超过则截断保留尾部
LOG_MAX_BYTES: int = 2 * 1024 * 1024   # 2 MB

# ── 音频常量 ───────────────────────────────────────────────────────────
DEFAULT_SAMPLE_RATE: int = 16000  # Qwen3-ASR / Aligner 都要求 16kHz mono
DEFAULT_CHANNELS: int = 1

# ── 模型上限（来自官方 README / Model Card）────────────────────────────
ASR_MAX_DURATION: float = 1200.0       # Qwen3-ASR 单次最大 20 分钟
ALIGNER_MAX_DURATION: float = 300.0    # ForcedAligner 单次最大 5 分钟（官方 README "up to 5 minutes"）

# 对齐器自动切块（align_full_text 遇到 >ALIGNER_MAX_DURATION 时启用）
ALIGN_CHUNK_MAX_DURATION: float = 240.0  # 单块最大 240s（留 60s 余量至硬上限 300s）
ALIGN_CHUNK_MIN_DURATION: float = 30.0   # 单块最小 30s（防碎片；30s 声学上下文够）
ALIGN_CHUNK_OVERLAP: float = 1.5         # 相邻块重叠 1.5s（对齐需比 ASR 更宽上下文）

# MMS 末字长拖音追踪：自末字元音起点起的最大拖音时长（秒）。
# 末字搜索上界 = min(下一句起点, 元音起点 + 本值)。两个锚都稳定：
#   - 下一句起点不随本句重对齐改变；
#   - 元音起点由 CTC Viterbi 按文本锚定，重复重对齐收敛到同一位置。
# 因此上界与裁剪窗长度彻底解耦——窗口给多宽都不会「给多少吃多少」，
# 重对齐幂等；同时拖音可以合法延伸出旧句界（句界随之修正，而非被钳死）。
# 歌曲长拖音常见 1~4s，6s 覆盖长 ballad 并防病态无限延伸。
MMS_TAIL_EXTEND_MAX: float = 6.0

# 单句重对齐裁剪窗的双侧扩展（秒），Qwen/MMS 通用：
# 窗口 = [max(前句尾, 句首 - 本值), min(后句头, 句尾 + 本值)]（MMS 尾侧用
# MMS_TAIL_EXTEND_MAX）。目的：句界被手动拖错（拖短）后，真实发音仍落在窗内，
# 重对齐能把首/尾字恢复到正确位置——窗口若被当前句界奴役，拖短的句界会把
# 真值挡在窗外，首字只会不变或更短、尾字被压到窗尾，永远修不回去。
# 邻句边界是稳定锚（不随本句重对齐漂移），窗口因此不产生棘轮。
ALIGN_WIN_EXTEND: float = 3.0

# MMS 单句重对齐（无前句/上下文时）的**句首**窗口前瞻（秒）。
# 与 ALIGN_WIN_EXTEND（3s，Qwen/恢复用）不同：MMS 是 CTC 强制对齐，窗内任何
# 非静音发音都会被迫分配给本句 tokens——无可靠前句锚时，3s 的前瞻会把片头
# 音乐/噪声/邻句残响错误塞给首字，句首前移漂移。0.5s 足以覆盖「句首被拖短」
# 的恢复需求，且把污染面压到最小；有前句时窗首直接锚在前句尾，不受本值影响。
MMS_HEAD_EXTEND: float = 0.5

# 句间接缝吸附阈值（秒）：单句/脏句重对齐后，尾字结束点与后句起点的间隙
# ≤ 本值时吸附到后句起点（尾字 end = 后句 start）。
# 背景：全文重对齐整段共享一张 20ms 帧网格，连唱处天然无缝；单句重对齐
# 每次按裁剪窗新建网格，锚换算 round 一次、帧数 floor 一次，尾字即使顶到
# 上界也只能停在新网格的帧边界——与锚恒差 0~20ms，与全文路径不一致。
# 25ms = 一帧(20ms) + 舍入容差；真实停顿（人类换气近百毫秒起）远大于此，
# 不受影响。与 merge_punct「句内相邻字消除间隙」同语义，作用于句间。
ALIGN_SEAM_SNAP_MAX: float = 0.025

# 注：ASR 侧不做超长自动切块（已明确否决该需求）；>ASR_MAX_DURATION 由 transcribe() 守卫报错

# ── 推理默认参数 ───────────────────────────────────────────────────────
DEFAULT_DTYPE_STR: str = "bfloat16"    # 8GB 卡原生 BF16，不量化
DEFAULT_ATTN_IMPL: str = "flash_attention_2"  # 失败则回退 sdpa

# VAD 已移除（主场景 <20min 不需要；歌曲 VAD 负收益）
