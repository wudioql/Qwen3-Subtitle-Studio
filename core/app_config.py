"""core.app_config — 持久化偏好设置 / 默认配置（手动调参的入口）。

所有可调参数（ASR / Align / 导出样式 / 路径覆盖）都通过 Preferences 暴露：
    1) load_preferences() 读 JSON，不存在则用 DEFAULT_PREFERENCES 存一份模板
    2) save_preferences(p) 回写 JSON（保留未知字段，不丢失用户手加的 key）
    3) 手动调参：直接编辑 JSON 或后续 UI 设置面板改完调用 save_preferences()

项目规则：动态对象（progress_cb 回调、ModelManager 引用）**不能**进 Preferences；
        只允许可 JSON 序列化的基础类型 (str / int / float / bool / list / dict / None)。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields as _dc_fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, get_type_hints
from .constants import (
    ALIGNER_MODEL_PATH,
    ASR_MODEL_PATH,
    PROJECT_ROOT,
)
from subs.ass_style import AssStylePrefs

logger = logging.getLogger(__name__)

# QSS_CONFIG_DIR 环境变量可重定向配置目录：测试套件（见 tests/conftest.py）用它把
# 偏好读写隔离到临时目录，避免改写用户真实的 .config/preferences.json；
# 未设置时使用项目根 .config/。
CONFIG_DIR: Path = Path(os.environ["QSS_CONFIG_DIR"]) if os.environ.get("QSS_CONFIG_DIR") else PROJECT_ROOT / ".config"
# 不在 import 期创建目录；真正读写偏好时由 save_preferences 的
# `p.parent.mkdir(parents=True, exist_ok=True)` 惰性创建（多级）。
DEFAULT_PREFERENCES_PATH: Path = CONFIG_DIR / "preferences.json"

# 偏好内存缓存（按文件路径分键 → 已解析的 Preferences）。
# load_preferences 返回 deepcopy 副本；save_preferences 成功后刷新。
_PREFS_CACHE: Dict[str, Preferences] = {}


def _prefs_cache_key(path: Optional[str | Path]) -> str:
    return str(Path(path) if path else DEFAULT_PREFERENCES_PATH)


# ────────────────────────────────────────────────────────────────
# 子项配置：ASR / Align / UI / 路径
# ────────────────────────────────────────────────────────────────

@dataclass
class ASRPreferences:
    """TranscribeConfig 的持久化镜像（不含 progress_cb 动态字段）。"""
    source_language: str = "auto"
    return_word_timestamps: bool = True
    extract_vocals: bool = False         # 前置轻量级人声分离 (Kim_Vocal_2)
    context: str = ""
    max_new_tokens: int = 512
    use_cache: bool = True
    fallback_min_sentence_sec: float = 2.0
    fallback_max_sentence_sec: float = 15.0
    # 分句三段式：标点切 → 短句合(min) → 超长切(max)
    min_sentence_chars: int = 4
    min_sentence_sec: float = 0.3
    max_sentence_chars: int = 24         # ← 用户调「单句最大字数」的位置（0=不限制）
    max_sentence_sec: float = 8.0        # ← 用户调「单句最大时长(秒)」（0=不限制）
    align_pad_before: float = 0.12   # 与 align_pad_after 对称（声学上下文，产出钳回句界）
    align_pad_after: float = 0.12


@dataclass
class AlignPreferences:
    """AlignConfig 的持久化镜像（不含 progress_cb 动态字段）。

    注：无 source_language 字段——对齐语言按「句级语言 → 项目语言」决议
    （align_engine 内统一），不走偏好，工具栏「识别语言」只作用于 ASR；
    旧 preferences.json 中的残留 key 由宽松反序列化自动忽略。
    align_backend 由主工具栏维护（唯一写入口）。
    """
    align_backend: str = "qwen"          # "qwen" (口语/播客) | "mms" (歌曲/歌词长拖音)
    subchunk_min_chars: int = 6
    pad_before: float = 0.12         # 与 pad_after 对称（声学上下文，产出钳回句界）
    pad_after: float = 0.12


@dataclass
class StylePreferences:
    """WordHighlightStyle 4 样式开关 + ASS 额外 override tag；导出默认勾选。"""
    bold: bool = False
    italic: bool = False
    underline: bool = True           # PotPlayer 逐字默认下划线（与 usable-subtitle-sample 一致）
    strike: bool = False
    ass_extra_tags: str = ""         # 例如：\fad(200,200)\blur0.6（不含外层大括号）
    ass_highlight_color: str = "#FFD54F"  # 非 k-tag ASS 当前字颜色（用户可见的基础高亮）


@dataclass
class ExportPreferences:
    """导出相关的持久化项。

    default_dir：最近一次成功导出的目录（跨会话兜底）。优先级低于当前媒体目录：
        载入媒体后导出建议目录始终跟随媒体；仅在无媒体路径时使用此值。
    k_tag_mode：标准 ASS 的 k-tag 类型（kf 扫过 / k 切换 / ko 去描边；旧值 km 由 UI 迁移为 k）。

    注：旧版本的每格式勾选项（srt/vtt/ass_split/ass_t/lrc_enhanced/lrc_standard/
    ass_karaoke/ass_strategy/lrc_enhanced_mode）已随「每种产物独立按钮」的导出面板
    删除；load_preferences 反序列化宽松，旧 preferences.json 中的这些残留 key 会被
    自动忽略，无需迁移。
    """
    default_dir: str = ""            # 空 = 尚无跨会话记忆
    k_tag_mode: str = "kf"


@dataclass
class PathPreferences:
    """路径覆盖（留空 = 代码自动选）。"""
    ffmpeg_path: str = ""            # 空 = ensure_ffmpeg 自动选
    asr_model_path: str = ""         # 空 = constants.ASR_MODEL_PATH
    aligner_model_path: str = ""     # 空 = constants.ALIGNER_MODEL_PATH
    vocal_model_path: str = ""       # 空 = constants.KIM_VOCAL_MODEL_PATH
    mms_aligner_model_path: str = "" # 空 = constants.MMS_ALIGNER_MODEL_PATH


@dataclass
class KaraokeTemplatePreferences:
    """Aegisub 卡拉OK模板（k-tag ASS 自带 Automation 模板）的持久化镜像。

    与 AssStylePreferences 同法：内部 dict 镜像 subs.karaoke_template
    .KaraokeTemplatePrefs，UI 层 to_prefs()/apply() 转换。空 data = 默认模板。
    """
    data: Dict[str, Any] = field(default_factory=dict)

    def to_prefs(self):
        from subs.karaoke_template import KaraokeTemplatePrefs, default_karaoke_templates
        if not self.data:
            return default_karaoke_templates()
        return KaraokeTemplatePrefs.from_dict(self.data)

    def apply(self, prefs) -> None:
        self.data = prefs.to_dict()


@dataclass
class AssStylePreferences:
    """ASS 文字样式（字体/字号/颜色/描边/阴影/对齐/边距）。

    内部用 dict 镜像 subs.ass_style.AssStylePrefs，避免 dataclass 嵌套序列化复杂度。
    UI 层用 AssStylePrefs.from_dict(prefs.ass_style.data) 读取/编辑后回写。
    """
    data: Dict[str, Any] = field(default_factory=lambda: AssStylePrefs().to_dict())

    def to_style(self) -> AssStylePrefs:
        return AssStylePrefs.from_dict(self.data)

    def apply(self, style: AssStylePrefs) -> None:
        self.data = style.to_dict()


@dataclass
class SegmentationPrefs:
    """分句偏好：按语言分别给字数/时长上限（**默认不限**）。

    字段：
        enabled: 总开关。False → 所有 per_lang 全视为 0（不限制），保留 ASR 标点切分原貌。
        per_lang: 短语言码 → {"max_chars": int, "max_duration_sec": float}
            0 / 缺省字段 = 不限制。
            例：
                "zh": {"max_chars": 16, "max_duration_sec": 8.0}
                "en": {"max_chars": 12, "max_duration_sec": 6.0}
                "ja": {"max_chars": 20, "max_duration_sec": 8.0}
                "ko": {"max_chars": 16, "max_duration_sec": 8.0}

    注：硬切兜底只在 ASR 文本含标点 / 长段场景下生效，永远**不会切分一个 word 的中间**。
        手动拆分 / 合并仍由 UI 完成。
    """
    enabled: bool = False                       # 默认关闭 → 不硬限
    per_lang: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def get_limits(self, lang_short: str) -> tuple[int, float]:
        """返回 (max_chars, max_duration_sec) 用于指定语言。

        未启用 / 缺语言 / 缺字段 → (0, 0.0) 即不限制。
        """
        if not self.enabled:
            return 0, 0.0
        d = self.per_lang.get(lang_short) or {}
        return (
            int(d.get("max_chars", 0) or 0),
            float(d.get("max_duration_sec", 0.0) or 0.0),
        )


@dataclass
class Preferences:
    """根偏好设置对象。"""
    # version 仅为普通格式标记（历史编号，恒为 1）：不做版本校验、不实现迁移——
    # 读取走「宽松反序列化 + 递归保留未知字段」，新增字段以类默认值兜底，无需靠
    # version 区分新旧。与工程文件 schema（subs.models.PROJECT_SCHEMA_VERSION）同理。
    version: int = 1
    asr: ASRPreferences = field(default_factory=ASRPreferences)
    align: AlignPreferences = field(default_factory=AlignPreferences)
    style: StylePreferences = field(default_factory=StylePreferences)
    export: ExportPreferences = field(default_factory=ExportPreferences)
    paths: PathPreferences = field(default_factory=PathPreferences)
    ass_style: AssStylePreferences = field(default_factory=AssStylePreferences)
    karaoke_template: KaraokeTemplatePreferences = field(default_factory=KaraokeTemplatePreferences)
    segmentation: SegmentationPrefs = field(default_factory=SegmentationPrefs)   # Phase 4 v3
    # UI 偏好（原经 extra 顶层字段透传；升格为正式字段，读取直接走 prefs.ui_theme /
    # prefs.player_preview_mode。旧 preferences.json 里它们本就是顶层键，加载时自动
    # 落入字段，无需迁移。）
    ui_theme: str = "dark"                    # "light" | "dark"（与 ui.themes._DEFAULT_THEME 一致）
    player_preview_mode: str = "sentence"     # 预览字幕模式 key（见 ui.subtitle_overlay.PREVIEW_MODES）
    extra: Dict[str, Any] = field(default_factory=dict)  # 用户自定义扩展，不做 schema 校验


# ────────────────────────────────────────────────────────────────
# 转 config 辅助（Preferences ↔ TranscribeConfig / AlignConfig）
# ────────────────────────────────────────────────────────────────

def apply_asr_prefs(
    cfg: object,
    prefs: ASRPreferences,
) -> None:
    """把 ASRPreferences 写到 TranscribeConfig（原地修改）。progress_cb 不受影响。"""
    # TranscribeConfig 字段名与 ASRPreferences 1:1 对应
    for k, v in asdict(prefs).items():
        if hasattr(cfg, k):
            try:
                setattr(cfg, k, v)
            except Exception:
                logger.warning("[prefs] 无法写入 TranscribeConfig.%s = %r", k, v)


def apply_align_prefs(
    cfg: object,
    prefs: AlignPreferences,
) -> None:
    """把 AlignPreferences 写到 AlignConfig（原地修改）。progress_cb 不受影响。

    与 apply_asr_prefs 对称：AlignConfig 字段名与 AlignPreferences 1:1 对应，
    用户在 preferences.json 手调的 subchunk_min_chars / pad_before / pad_after
    经由此函数注入对齐配置（source_language / align_backend 由调用方按
    工具栏即时选择覆盖，见 ui.workflow_controller）。
    """
    for k, v in asdict(prefs).items():
        if hasattr(cfg, k):
            try:
                setattr(cfg, k, v)
            except Exception:
                logger.warning("[prefs] 无法写入 AlignConfig.%s = %r", k, v)


def resolved_paths(prefs: PathPreferences) -> Dict[str, str]:
    """返回最终生效的路径（覆盖 or 默认常量）。"""
    from .constants import KIM_VOCAL_MODEL_PATH, MMS_ALIGNER_MODEL_PATH
    return {
        "ffmpeg_path": prefs.ffmpeg_path or "",
        "asr_model_path": prefs.asr_model_path or str(ASR_MODEL_PATH),
        "aligner_model_path": prefs.aligner_model_path or str(ALIGNER_MODEL_PATH),
        "vocal_model_path": prefs.vocal_model_path or str(KIM_VOCAL_MODEL_PATH),
        "mms_aligner_model_path": prefs.mms_aligner_model_path or str(MMS_ALIGNER_MODEL_PATH),
    }


# ────────────────────────────────────────────────────────────────
# 反序列化 / 序列化
# ────────────────────────────────────────────────────────────────

def _obj_to_dict(obj: Any) -> Dict[str, Any]:
    """dataclass → dict；遇到子 dataclass 递归；保留 list/dict/scalar/None。

    嵌套 dataclass 反序列化时收进 ``_extra`` 的「未声明字段」在此合并回输出，
    实现未知字段的递归保留（load→save 不丢）。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj  # type: ignore[return-value]
    if isinstance(obj, (list, tuple)):
        return [_obj_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _obj_to_dict(v) for k, v in obj.items()}
    # dataclass
    if hasattr(obj, "__dataclass_fields__"):
        out: Dict[str, Any] = {}
        for k in obj.__dataclass_fields__.keys():
            out[k] = _obj_to_dict(getattr(obj, k))
        extra = getattr(obj, "_extra", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                out.setdefault(str(k), _obj_to_dict(v))
        return out
    # 其他不可 JSON 化的直接转 str（保护）
    return str(obj)


def _dict_to_obj(d: Any, cls: Any, *, _root: bool = False) -> Any:
    """把 dict 填进 dataclass cls；缺字段用类默认值；多余字段**递归保留**（宽松反序列化）。

    - 嵌套 dataclass（非 root）：未声明字段收进 ``obj._extra``，序列化时由
      ``_obj_to_dict`` 合并回输出——实现未知字段 load→save 不丢。
    - root（Preferences 自身）：不收集 ``_extra``，顶层未知字段由
      ``load_preferences`` 的 ``extra`` 字段逻辑统一处理（避免双份）。
    兼容 PEP 563（from __future__ import annotations）：字段 type 是字符串时用 get_type_hints 解析。
    """
    if d is None:
        return cls()
    if not isinstance(d, dict):
        logger.warning("[prefs] 期望 %s 为 dict，实际 %s，回退默认", cls.__name__, type(d).__name__)
        return cls()
    if not is_dataclass(cls):
        return d
    try:
        type_hints: Dict[str, Any] = get_type_hints(cls)
    except Exception:
        type_hints = {}
    dc_fields = {f.name: f for f in _dc_fields(cls)}
    kwargs: Dict[str, Any] = {}
    for name, f in dc_fields.items():
        if name not in d:
            continue
        raw = d[name]
        # 字段真实类型：优先从 type_hints（resolve 过的真实 type）拿；否则回退 f.type。
        # 不做 eval 解析字符串类型（注入隐患）；拿不到真实类型就按原始值直收。
        if name in type_hints:
            real_cls = type_hints[name]
        else:
            real_cls = f.type
            if isinstance(real_cls, str) and raw is not None:
                # 字符串注解且 hints 解析失败：不做 eval，按原始值直收（宽松反序列化语义不变）
                real_cls = type(raw)
        if isinstance(real_cls, type) and is_dataclass(real_cls):
            kwargs[name] = _dict_to_obj(raw, real_cls)
        # Dict[str, Any] / list / scalar：原样
        else:
            kwargs[name] = raw
    try:
        obj = cls(**kwargs)
        if not _root:
            extra = {k: v for k, v in d.items() if k not in dc_fields}
            if extra:
                obj._extra = extra  # type: ignore[attr-defined]
        return obj
    except TypeError as e:
        logger.warning("[prefs] %s 构造失败（%s），回退默认值", cls.__name__, e)
        return cls()


def _mirror_align_pads(prefs: Preferences) -> Preferences:
    """ASR 直通对齐与重对齐共用同一对 pad 语义。

    真源：``prefs.align.pad_before/after``（设置页只编辑这一对）。
    ``prefs.asr.align_pad_*`` 为镜像字段，供 TranscribeConfig 1:1 apply。
    若用户手改 JSON 只动一侧，以 align 为准覆盖 asr 镜像，避免两侧漂移。
    """
    prefs.asr.align_pad_before = float(prefs.align.pad_before)
    prefs.asr.align_pad_after = float(prefs.align.pad_after)
    return prefs


def load_preferences(path: Optional[str | Path] = None) -> Preferences:
    """读取偏好设置；文件不存在则写一份 DEFAULT_PREFERENCES 并返回它。

    性能：进程内缓存已解析的 Preferences（按文件路径分键），避免 UI 高频动作
    （切主题/导出/打开媒体/切预览模式等约 30 处调用点）每次都读盘 + JSON 解析。
    返回**隔离副本**（deepcopy），调用方原地修改不会污染缓存；`save_preferences`
    写盘成功后会刷新对应缓存项。跨进程的外部文件改动不会自动失效——需要时可
    调 ``invalidate_preferences_cache()`` 强制重读。
    """
    p = Path(path) if path else DEFAULT_PREFERENCES_PATH
    key = _prefs_cache_key(p)
    cached = _PREFS_CACHE.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    if not p.exists():
        default = Preferences()
        try:
            save_preferences(default, p)
            logger.info("[prefs] 已生成默认偏好模板: %s", p)
        except Exception:
            logger.exception("[prefs] 写默认偏好失败，内存默认值继续")
        return copy.deepcopy(_mirror_align_pads(default))
    try:
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        logger.exception("[prefs] 读 %s 失败，回退默认", p)
        return _mirror_align_pads(Preferences())
    prefs = _dict_to_obj(raw, Preferences, _root=True)
    # raw 里不在 schema 的字段全部塞进 prefs.extra（宽松保留）
    known_top = set(getattr(Preferences, "__dataclass_fields__", {}).keys())
    if isinstance(raw, dict):
        extras = {k: v for k, v in raw.items() if k not in known_top}
        if extras and not prefs.extra:
            prefs.extra = extras
        elif extras:
            prefs.extra.update(extras)
    prefs = _mirror_align_pads(prefs)
    _PREFS_CACHE[key] = copy.deepcopy(prefs)
    return copy.deepcopy(prefs)


def save_preferences(prefs: Preferences, path: Optional[str | Path] = None) -> Path:
    """保存偏好；返回实际写入路径。格式：UTF-8 / 缩进 2 空格 / 末尾换行。"""
    p = Path(path) if path else DEFAULT_PREFERENCES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    _mirror_align_pads(prefs)
    data = _obj_to_dict(prefs)
    # extra 要展开到顶层（如果 prefs.extra 有东西），方便用户直接编辑 JSON
    if isinstance(data, dict) and "extra" in data:
        extra_data = data.pop("extra") or {}
        for k, v in extra_data.items():
            data.setdefault(k, v)
    tmp_path: Path | None = None
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent)
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.exception("[prefs] 写 %s 失败", p)
        raise
    # 写盘成功才刷新缓存（存隔离副本，避免调用方后续原地修改污染缓存）
    _PREFS_CACHE[_prefs_cache_key(p)] = copy.deepcopy(prefs)
    return p


def invalidate_preferences_cache(path: Optional[str | Path] = None) -> None:
    """清除偏好内存缓存。``path`` 为 None 时全清，否则只清指定文件对应项。

    用于"外部已改动 preferences.json、需要强制重读"的场景（进程内 save 无需调用，
    会自动刷新）。也供测试隔离不同用例的缓存状态。
    """
    if path is None:
        _PREFS_CACHE.clear()
    else:
        _PREFS_CACHE.pop(_prefs_cache_key(path), None)


__all__: List[str] = [
    "ASRPreferences",
    "AlignPreferences",
    "StylePreferences",
    "ExportPreferences",
    "PathPreferences",
    "AssStylePreferences",
    "KaraokeTemplatePreferences",
    "SegmentationPrefs",   # Phase 4 v3
    "Preferences",
    "apply_asr_prefs",
    "apply_align_prefs",
    "resolved_paths",
    "load_preferences",
    "save_preferences",
    "invalidate_preferences_cache",
    "CONFIG_DIR",
    "DEFAULT_PREFERENCES_PATH",
]
