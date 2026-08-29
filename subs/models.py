"""subs.models — 统一内部字幕数据模型

纯数据类，无外部依赖。core 层和 ui 层、subs 层内部都引用这一套模型。

层级结构：
    SubtitleProject
    └── sentences: list[Sentence]
        └── words: list[WordTimestamp]    # 对齐后才有字/词级

时间单位统一使用 **秒（float）**，保留 3 位小数足以覆盖毫秒级精度。
导出时再按格式要求转成毫秒/厘秒/时间码字符串。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────
# 基础数据类
# ─────────────────────────────────────────────────────────────

PROJECT_SCHEMA_VERSION = 1
_MAX_PROJECT_SENTENCES = 1_000_000
# 防畸形/DoS 的安全上限（放宽取值，正常工程远达不到）
_MAX_WORDS_PER_SENTENCE = 200_000
_MAX_SENTENCE_TEXT_LEN = 200_000
_MAX_PROJECT_JSON_BYTES = 200 * 1024 * 1024


def _finite_float(value, field_name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是数字（不能是布尔）")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数字") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field_name} 必须是有限数字")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field_name} 不能小于 {minimum}")
    return result


def _parse_bool(value, field_name: str) -> bool:
    """布尔规整（打开旧工程时迁移语义，修复 ``bool(\"false\")==True`` 一类反序列化缺陷）。

    接受：真布尔；字符串 ``\"true\"/\"false\"``（大小写不敏感，旧/手改文件常见）。
    其它类型 → ValueError。保存侧永远写真布尔，故规整只发生在读取路径。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "true":
            return True
        if v == "false":
            return False
    raise ValueError(f"{field_name} 必须是布尔")


def _validate_project_dict(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("工程 JSON 顶层必须是对象")
    version = data.get("schema_version", PROJECT_SCHEMA_VERSION)
    try:
        version = int(version)
    except (TypeError, ValueError) as exc:
        raise ValueError("schema_version 必须是整数") from exc
    if version != PROJECT_SCHEMA_VERSION:
        raise ValueError(
            f"不支持的工程 schema_version={version}；当前仅支持 {PROJECT_SCHEMA_VERSION}"
        )

    sentences = data.get("sentences", [])
    if not isinstance(sentences, list):
        raise ValueError("sentences 必须是数组")
    if len(sentences) > _MAX_PROJECT_SENTENCES:
        raise ValueError(f"sentences 数量超过安全上限 {_MAX_PROJECT_SENTENCES}")

    for key in ("audio_path", "source_language"):
        if not isinstance(data.get(key, ""), str):
            raise ValueError(f"{key} 必须是字符串")
    for key in ("source_media_path", "video_path"):
        if data.get(key) is not None and not isinstance(data.get(key), str):
            raise ValueError(f"{key} 必须是字符串或 null")
    # 跨机复现三件套：可选 dict（或 null）
    for key in ("ass_style_data", "karaoke_template_data", "export_settings"):
        if data.get(key) is not None and not isinstance(data.get(key), dict):
            raise ValueError(f"{key} 必须是对象或 null")

    _finite_float(data.get("media_duration", 0.0), "media_duration", minimum=0.0)
    sample_rate = data.get("sample_rate", 16000)
    try:
        sample_rate = int(sample_rate)
    except (TypeError, ValueError) as exc:
        raise ValueError("sample_rate 必须是整数") from exc
    if not 1 <= sample_rate <= 384000:
        raise ValueError("sample_rate 必须在 1..384000")
    _finite_float(data.get("created_at", time.time()), "created_at", minimum=0.0)

    seen_sids: set[int] = set()
    for index, sentence in enumerate(sentences):
        prefix = f"sentences[{index}]"
        if not isinstance(sentence, dict):
            raise ValueError(f"{prefix} 必须是对象")
        for key in ("text", "language", "speaker", "ass_style", "ass_extra_tags"):
            if not isinstance(sentence.get(key, ""), str):
                raise ValueError(f"{prefix}.{key} 必须是字符串")
        start = _finite_float(sentence.get("start_time", 0.0), f"{prefix}.start_time", minimum=0.0)
        end = _finite_float(sentence.get("end_time", 0.0), f"{prefix}.end_time", minimum=0.0)
        if end < start:
            raise ValueError(f"{prefix}.end_time 不能早于 start_time")

        sid_raw = sentence.get("sid")
        if sid_raw is not None:
            try:
                sid = int(sid_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{prefix}.sid 必须是正整数") from exc
            if sid <= 0 or sid in seen_sids:
                raise ValueError(f"{prefix}.sid 必须为唯一正整数（当前 {sid}）")
            seen_sids.add(sid)

        text_len = len(sentence.get("text", "") or "")
        if text_len > _MAX_SENTENCE_TEXT_LEN:
            raise ValueError(
                f"{prefix}.text 长度 {text_len} 超过安全上限 {_MAX_SENTENCE_TEXT_LEN}"
            )

        words = sentence.get("words", [])
        if not isinstance(words, list):
            raise ValueError(f"{prefix}.words 必须是数组")
        if len(words) > _MAX_WORDS_PER_SENTENCE:
            raise ValueError(
                f"{prefix}.words 数量 {len(words)} 超过安全上限 {_MAX_WORDS_PER_SENTENCE}"
            )
        prev_word_start: float | None = None
        for word_index, word in enumerate(words):
            wp = f"{prefix}.words[{word_index}]"
            if not isinstance(word, dict):
                raise ValueError(f"{wp} 必须是对象")
            for key in ("text", "language", "speaker"):
                if not isinstance(word.get(key, ""), str):
                    raise ValueError(f"{wp}.{key} 必须是字符串")
            word_start = _finite_float(word.get("start_time", 0.0), f"{wp}.start_time", minimum=0.0)
            word_end = _finite_float(word.get("end_time", 0.0), f"{wp}.end_time", minimum=0.0)
            if word_end < word_start:
                raise ValueError(f"{wp}.end_time 不能早于 start_time")
            # 句内字时间单调不降（畸形文件守卫；不校验「落界」——字级编辑可不回写句界）
            if prev_word_start is not None and word_start < prev_word_start:
                raise ValueError(f"{wp}.start_time 早于前一字（句内字时间必须单调）")
            prev_word_start = word_start


class _SentenceIdCounter:
    """进程内单调递增的 Sentence sid 分配器。

    注意：加载外部项目（from_dict 会透传文件里的旧 sid）后，
    必须把分配器推进到「已用最大值」之后（reserve），否则分配器从 1 重新发号，
    后续新建的 Sentence 会与加载进来的句子撞 sid——而 Undo/Redo 全靠 sid 定位句子。
    """

    def __init__(self) -> None:
        self._next = 1

    def next(self) -> int:
        sid = self._next
        self._next += 1
        return sid

    def reserve(self, used_sid: int) -> None:
        """声明 used_sid 已被占用，把发号起点推到其后（只前进不后退）。"""
        if used_sid >= self._next:
            self._next = used_sid + 1


_SENTENCE_ID_COUNTER = _SentenceIdCounter()


def _next_sentence_id() -> int:
    """给新 Sentence 分配全局唯一 id（进程内单调递增，从 1 起）。"""
    return _SENTENCE_ID_COUNTER.next()


@dataclass
class WordTimestamp:
    """字/词级时间戳（对齐后才有）。"""
    text: str
    start_time: float           # 秒
    end_time: float             # 秒
    language: str = ""          # 该字/词的语言（如 zh / en / ja）
    # 说话人标签：仅数据契约与部分导出格式透传；**UI 未产品化**（无编辑入口），
    # 勿当作已支持多说话人识别/标注。
    speaker: str = ""
    # Phase 4 v3: 标点标记
    #   True = 此 word 是「ASR 文本里的标点」被 _merge_punct_into_words 回填生成的
    #   用途：导出时跳过逐字动效（k-tag / \t / 逐字 SRT/VTT 的「当前字」高亮），
    #         但仍参与 start_time/end_time 计算（让标点也消耗时间、显示在文本中）
    is_punct: bool = False

    @property
    def duration(self) -> float:
        return round(max(0.0, self.end_time - self.start_time), 3)


@dataclass
class Sentence:
    """句级字幕条目（ASR 句子级输出 + 对齐后聚合 + 手动校对后的基本单位）。"""
    text: str
    start_time: float           # 秒
    end_time: float             # 秒
    words: List[WordTimestamp] = field(default_factory=list)
    language: str = ""          # 该句语言
    # 说话人标签：仅数据契约与 ASS/LRC 等导出透传；**UI 未产品化**，无编辑入口。
    speaker: str = ""

    # 用于导出 / 渲染时的元数据
    ass_style: str = ""         # ASS Style 名称（空则用默认）
    ass_extra_tags: str = ""    # 行内 ASS 标签（不含 {}，用于临时编辑态）

    # 脏标记：用户改过（文本/start/end/拆分/合并/拖边界），等待重对齐
    # is_dirty=True 的句：
    #   - 在 align_project 里重跑；新结果成功前保留旧 words（事务提交）
    #   - 在 SubtitleProject.dirty_indices() 里被列出
    #   - 在 UI 表格行号列底色变黄（视觉反馈）
    is_dirty: bool = False

    # 锁定标记：用户手动确认并锁定的句，无论何种对齐（修改重对齐/全文重对齐/整项目对齐）
    # 均严格跳过并保护，防止手动精修成果被 AI 覆写
    is_locked: bool = False

    # 稳定唯一 id：Undo/Redo 命令以「对象身份」而非「值相等」定位句子，
    # 避免存在文本/时间完全相同的重复句时误删/误恢复。
    sid: int = field(default_factory=_next_sentence_id)

    # 是否有真实时间戳。True=字幕/对齐后带 start/end；False=纯文本导入（无时间戳，UI 显示为空）
    timed: bool = True

    @property
    def duration(self) -> float:
        return round(max(0.0, self.end_time - self.start_time), 3)

    def has_word_level(self) -> bool:
        return bool(self.words)

    def word_count(self) -> int:
        """返回有效字/词数（排除 is_punct 的标点 word）。"""
        return sum(1 for w in self.words if not w.is_punct)

    def fix_times_from_words(self) -> None:
        """当 words 齐全时，将句级 start/end 对齐到词级的首尾。

        对齐管线、导入还原和 UI 字级命令均可调用；现役交互要求句界与最外层
        word/标点界绑定。句级手柄反向同步外层 word（见 ui.commands）。
        """
        if not self.words:
            return
        self.start_time = self.words[0].start_time
        self.end_time = self.words[-1].end_time


@dataclass
class SubtitleProject:
    """项目根对象：一个媒体 + 若干字幕句。"""
    audio_path: str = ""                      # 已提取的 16kHz mono WAV 路径（空=无媒体，纯字幕模式）
    source_media_path: Optional[str] = None   # 原始媒体路径（音/视频源文件，区别于提取后的 audio_path）
    video_path: Optional[str] = None          # 原始视频路径（如有）
    media_duration: float = 0.0               # 媒体总时长（秒）
    sentences: List[Sentence] = field(default_factory=list)
    sample_rate: int = 16000
    source_language: str = ""                 # 源语言（如 zh / en / ja / auto）
    created_at: float = field(default_factory=time.time)

    # ── 工程级可复现设置（跨机复现三件套，可选；None = 未随工程保存，用全局偏好）──
    # 均为「原始 dict 透传」：subs 层不 import ass_style / karaoke_template，
    # 由 UI 层负责 AssStylePrefs/KaraokeTemplatePrefs/WordHighlightStyle ↔ dict 转换。
    ass_style_data: Optional[Dict[str, Any]] = None          # ASS 文字样式（AssStylePrefs.to_dict()）
    karaoke_template_data: Optional[Dict[str, Any]] = None   # 卡拉OK模板（KaraokeTemplatePrefs.to_dict()）
    export_settings: Optional[Dict[str, Any]] = None         # 导出设置（k_tag_mode + 逐字高亮）

    # ── 便捷方法 ────────────────────────────────────────
    def __len__(self) -> int:
        return len(self.sentences)

    def sort(self) -> None:
        """按 start_time 升序排序所有句子和内部词级。"""
        self.sentences.sort(key=lambda s: s.start_time)
        for s in self.sentences:
            if s.words:
                s.words.sort(key=lambda w: w.start_time)

    def has_word_level(self) -> bool:
        return any(s.has_word_level() for s in self.sentences)

    def find_sentence_at(self, t: float) -> Optional[int]:
        """返回包含时间 t（秒）的句索引；无则返回 None。

        按 start_time 升序的句子里，返回首个满足 start_time <= t <= end_time 的句；
        若 t 落在所有句之前/之后，返回距离最近的句索引（或 None）。
        """
        if not self.sentences:
            return None
        # 精确命中
        for i, s in enumerate(self.sentences):
            if s.start_time <= t <= s.end_time:
                return i
        # 兜底：最近的一句
        best = None
        best_gap: Optional[float] = None
        for i, s in enumerate(self.sentences):
            if t < s.start_time:
                gap = s.start_time - t
            elif t > s.end_time:
                gap = t - s.end_time
            else:
                return i
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best = i
        return best

    def all_words(self) -> List[WordTimestamp]:
        """按时间顺序聚合所有词级。"""
        out: List[WordTimestamp] = []
        for s in sorted(self.sentences, key=lambda x: x.start_time):
            out.extend(s.words)
        return out

    # ── 脏标记与锁定（dirty / lock tracking）─────────────────
    def dirty_indices(self) -> List[int]:
        """返回所有 is_dirty=True 的句索引（按 sentences 列表顺序）。"""
        return [i for i, s in enumerate(self.sentences) if s.is_dirty]

    def alignable_dirty_indices(self) -> List[int]:
        """返回待对齐的脏句索引（排除已锁定的句子）。"""
        return [i for i, s in enumerate(self.sentences) if s.is_dirty and not s.is_locked]

    def locked_indices(self) -> List[int]:
        """返回所有 is_locked=True 的句索引。"""
        return [i for i, s in enumerate(self.sentences) if s.is_locked]

    def mark_dirty(self, idx: int) -> None:
        """把指定句标为脏（被用户改过、待重对齐）。越界静默忽略。"""
        if 0 <= idx < len(self.sentences):
            self.sentences[idx].is_dirty = True

    def mark_clean(self, idx: int) -> None:
        """把指定句标为干净（清除脏标记，固定编辑效果）。越界静默忽略。"""
        if 0 <= idx < len(self.sentences):
            self.sentences[idx].is_dirty = False

    def mark_locked(self, idx: int, locked: bool = True) -> None:
        """设置指定句的锁定状态。越界静默忽略。"""
        if 0 <= idx < len(self.sentences):
            self.sentences[idx].is_locked = bool(locked)

    def toggle_lock(self, idx: int) -> bool:
        """切换指定句的锁定状态并返回新状态。越界返回 False。"""
        if 0 <= idx < len(self.sentences):
            self.sentences[idx].is_locked = not self.sentences[idx].is_locked
            return self.sentences[idx].is_locked
        return False

    def clear_dirty(self) -> None:
        """清空所有句的脏标记。align_dirty_only 完成后调用。"""
        for s in self.sentences:
            s.is_dirty = False

    def dirty_count(self) -> int:
        """脏句数量（性能 O(n) 一次；调用方如频繁查可缓存）。"""
        return sum(1 for s in self.sentences if s.is_dirty)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["schema_version"] = PROJECT_SCHEMA_VERSION
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "SubtitleProject":
        _validate_project_dict(d)
        raw_sentences = d.get("sentences", [])
        # 先把文件里显式存在的 sid 全部「占坑」（分配器只前进），
        # 再给缺 sid 的旧文件句子发新号——否则新发的号可能撞上文件中靠后句子的显式 sid。
        for s in raw_sentences:
            if isinstance(s, dict) and s.get("sid") is not None:
                try:
                    _SENTENCE_ID_COUNTER.reserve(int(s["sid"]))
                except (TypeError, ValueError):
                    pass
        sentences = [
            Sentence(
                text=s.get("text", ""),
                start_time=float(s.get("start_time", 0.0)),
                end_time=float(s.get("end_time", 0.0)),
                words=[
                    WordTimestamp(
                        text=w.get("text", ""),
                        start_time=float(w.get("start_time", 0.0)),
                        end_time=float(w.get("end_time", 0.0)),
                        language=w.get("language", ""),
                        speaker=w.get("speaker", ""),
                        is_punct=_parse_bool(w.get("is_punct", False), "is_punct"),   # Phase 4 v3: 透传标点标记
                    )
                    for w in s.get("words", [])
                ],
                language=s.get("language", ""),
                speaker=s.get("speaker", ""),
                ass_style=s.get("ass_style", ""),
                ass_extra_tags=s.get("ass_extra_tags", ""),
                is_dirty=_parse_bool(s.get("is_dirty", False), "is_dirty"),    # Phase 4 v2: 透传脏标记
                is_locked=_parse_bool(s.get("is_locked", False), "is_locked"),  # 透传锁定标记
                # M1: 透传唯一 id；旧文件无 sid 则新分配（此刻已安全：显式 sid 均已 reserve）
                sid=int(s["sid"]) if s.get("sid") is not None else _next_sentence_id(),
                timed=_parse_bool(s.get("timed", True), "timed"),            # 是否有真实时间戳
            )
            for s in raw_sentences
        ]
        return cls(
            audio_path=str(d.get("audio_path", "")),
            source_media_path=d.get("source_media_path"),
            video_path=d.get("video_path"),
            media_duration=float(d.get("media_duration", 0.0)),
            sentences=sentences,
            sample_rate=int(d.get("sample_rate", 16000)),
            source_language=d.get("source_language", ""),
            created_at=float(d.get("created_at", time.time())),
            ass_style_data=d.get("ass_style_data"),
            karaoke_template_data=d.get("karaoke_template_data"),
            export_settings=d.get("export_settings"),
        )

    def save_json(self, path: str | Path) -> None:
        """同目录临时文件 + fsync + os.replace，避免崩溃留下半截工程。"""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        _validate_project_dict(data)
        data = _relativize_media_paths(data, destination.parent)
        text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, destination)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @classmethod
    def load_json(cls, path: str | Path) -> "SubtitleProject":
        p = Path(path)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if size > _MAX_PROJECT_JSON_BYTES:
            raise ValueError(
                f"工程文件 {size} 字节超过安全上限 {_MAX_PROJECT_JSON_BYTES} 字节"
            )
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw = _resolve_media_paths(raw, p.parent)
        return cls.from_dict(raw)


# ─────────────────────────────────────────────────────────────
# 辅助构造函数
# ─────────────────────────────────────────────────────────────
def make_sentences_from_raw(
    texts: List[str],
    starts: List[float],
    ends: List[float],
    *,
    language: str = "",
) -> List[Sentence]:
    """快速构造 Sentence 列表（无词级）。"""
    if not (len(texts) == len(starts) == len(ends)):
        raise ValueError("texts / starts / ends 长度必须一致")
    return [
        Sentence(text=t, start_time=s, end_time=e, language=language)
        for t, s, e in zip(texts, starts, ends)
    ]


# ══════════════════════════════════════════════════════════════
# 媒体路径相对化 / 解析（跨机复现：工程整体搬运时媒体路径随工程目录解析）
# ══════════════════════════════════════════════════════════════
_MEDIA_PATH_KEYS = ("source_media_path", "audio_path", "video_path")


def _is_absolute_path(value: str) -> bool:
    """跨 OS 识别绝对路径：本机绝对，或 Windows 盘符绝对（``C:\\...``/``C:/...``），
    或 POSIX 根绝对（``/...``）。

    动机：Windows 盘符路径在非 Windows 平台 ``Path.is_absolute()`` 返回 False（反之亦然），
    会被 ``_resolve_media_paths`` 误当相对路径拼上工程目录；用 ``PureWindowsPath`` /
    ``PurePosixPath`` 兜底识别，让这类路径在保存/打开时都原样保留。
    """
    return (
        Path(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PurePosixPath(value).is_absolute()
    )


def _relativize_media_paths(data: dict, project_dir: Path) -> dict:
    """保存前把可相对化的媒体绝对路径转为相对路径（便于工程整体搬运、跨机复现）。

    - 绝对路径且相对工程目录可表示（不以 ``..`` 开头）→ 主字段存相对路径（统一正斜杠）；
    - 原绝对路径存入 ``media_path_hints``，供打开时回退与提示；
    - 无法相对化（跨盘符/相对工程目录之外）→ 保持绝对路径原样。
    - 非 Windows 平台遇到 Windows 盘符绝对路径 → 原样保留（本机 ``relpath`` 不理解盘符，
      可能把 ``C:\\proj\\song.mp4`` 相对成 ``song.mp4``，属错误转化）。
    """
    hints: Dict[str, str] = {}
    for key in _MEDIA_PATH_KEYS:
        val = data.get(key)
        if not val or not isinstance(val, str):
            continue
        path = Path(val)
        if not _is_absolute_path(val):
            continue
        if os.name != "nt" and PureWindowsPath(val).is_absolute():
            continue  # 非 Windows 平台：Windows 盘符路径无法正确相对化，保持原样
        try:
            rel = os.path.relpath(str(path), str(project_dir))
        except ValueError:  # Windows 跨盘符
            continue
        if rel and not rel.startswith(".."):
            data[key] = rel.replace("\\", "/")   # 统一正斜杠，跨 OS 可读
            hints[key] = val
    if hints:
        data["media_path_hints"] = hints
    return data


def _resolve_media_paths(raw: dict, project_dir: Path) -> dict:
    """打开时把相对媒体路径解析回绝对路径（读宽松）。

    - 相对路径 → 先按工程目录解析；解析到的文件不存在 → 回退 ``media_path_hints``
      里的原绝对路径（若其存在）；两者都无效 → 解析为「工程目录下的绝对路径」
      （保留真实位置，UI 层据此准确判断「媒体缺失」并提示重新关联）。
    - 绝对路径（本机绝对 / Windows 盘符绝对 / POSIX 根绝对）→ 原样保留
      （旧工程 / 无法相对化的媒体）。
    """
    hints = raw.get("media_path_hints")
    hints = hints if isinstance(hints, dict) else {}   # 畸形字段（非 dict）按无 hint 处理
    for key in _MEDIA_PATH_KEYS:
        val = raw.get(key)
        if not val or not isinstance(val, str):
            continue
        path = Path(val)
        if _is_absolute_path(val):
            continue
        resolved = project_dir / path
        if resolved.is_file():
            raw[key] = str(resolved)
            continue
        hint = hints.get(key)
        if isinstance(hint, str) and hint and Path(hint).is_file():
            raw[key] = hint
        else:
            raw[key] = str(resolved)   # 工程目录下的绝对路径（文件缺失，UI 提示重关联）
    return raw
