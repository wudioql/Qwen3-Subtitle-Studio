"""subs.lrc_io — LRC 句级读 + 句级/增强字级写

写逻辑放 exporters.to_lrc；读 + 「写时保留元信息」放这里。

读规则（尽量宽松）：
    - 只支持句级：每行 [mm:ss.xx] 或 [mm:ss] 开头，后面是文本
    - 同一行多个时间戳（如 [00:10.00][00:20.00]副歌）会拆成多条 Sentence
    - 支持 [ar:xxx] / [ti:xxx] / [al:xxx] / [by:xxx] 等元信息，不生成句子，
      写回时（如果用 write_lrc_file 会保留在 LRC 头部）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .exporters import to_lrc
from .models import Sentence, SubtitleProject
from .time_fmt import parse_lrc_time


# ─────────────────────────────────────────────────────────────
# 读
# ─────────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"\[([^\[\]]{1,32})\]")


@dataclass
class LrcMeta:
    """LRC 元信息（ID 标签）。空即不存在。"""

    ti: str = ""  # 标题
    ar: str = ""  # 艺术家
    al: str = ""  # 专辑
    by: str = ""  # 歌词作者
    offset_ms: int = 0  # 整体偏移（毫秒）；+ 所有时间 += offset/1000
    extra: Dict[str, str] = field(default_factory=dict)

    def has_any(self) -> bool:
        return bool(self.ti or self.ar or self.al or self.by or self.offset_ms or self.extra)


def _parse_meta_tag(content: str, meta: LrcMeta) -> bool:
    """如果内容是元信息 tag（不是时间），写入 meta 并返回 True。

    判定规则：
        - 必须包含「1 个 :」
        - 冒号前 key 必须纯字母/下划线（2~6 字符）；不得纯数字（否则是 mm:ss 时间）
        - 允许特殊 key: offset（值是数字）
    """
    if ":" not in content:
        return False
    key, _, val = content.partition(":")
    key = key.strip().lower()
    val = val.strip()
    if not key:
        return False
    # 防止「00:00.54」被当 meta（key="00" 是数字）
    if key.isdigit():
        return False
    # 防止「0:05」之类被当 meta（key 只有一位数字的也不允许）
    if len(key) <= 1 and key.isdigit():
        return False
    # key 必须是字母开头的标识符（常见 LRC 元信息都符合）
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    if key == "ti":
        meta.ti = val
    elif key == "ar":
        meta.ar = val
    elif key == "al":
        meta.al = val
    elif key == "by":
        meta.by = val
    elif key == "offset":
        try:
            meta.offset_ms = int(float(val))
        except ValueError:
            pass
    else:
        meta.extra[key] = val
    return True


def parse_lrc_text(
    text: str,
    *,
    language: str = "",
    speaker: str = "",
) -> Tuple[List[Sentence], LrcMeta]:
    """把 LRC 文本解析成 Sentence 列表 + LrcMeta。**只恢复句级，不还原增强字级**。

    现役 `<start>字` 与旧 `<start,end>字` 都会剥掉时间包，纯文本保留在 sentence.text；
    如果要恢复字级，用户应先导出 JSON 或用 to_lrc(enhanced=True) 重新生成。
    """
    # 去掉现役单开始时间 <mm:ss.xx>，并兼容旧双时间 <start,end>；只恢复句级。
    inline_time = r"\d{1,3}:\d{2}(?:[\.:]\d{1,3})?"
    text_no_enh = re.sub(
        rf"<(?:{inline_time})(?:,(?:{inline_time}))?>",
        "",
        text,
    )
    meta = LrcMeta()
    raw_entries: list[tuple[float, str]] = []

    for raw_line in text_no_enh.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        # 连续 [tag] + 1 段文本
        text_start = 0
        times_in_line: list[float] = []
        for m in _TAG_RE.finditer(line):
            content = m.group(1)
            # 元信息 tag
            if _parse_meta_tag(content, meta):
                text_start = m.end()
                continue
            # 时间 tag
            try:
                t = parse_lrc_time(f"[{content}]")
                times_in_line.append(t)
            except ValueError:
                # 不是合法时间 tag，跳过
                pass
            text_start = m.end()
        if not times_in_line:
            continue
        line_text = line[text_start:].strip()
        if not line_text:
            continue
        for t in times_in_line:
            raw_entries.append((t, line_text))

    if not raw_entries:
        return [], meta

    raw_entries.sort(key=lambda x: x[0])
    # 偏移
    if meta.offset_ms != 0:
        off_s = -meta.offset_ms / 1000.0  # 约定：offset>0 表示整体向前；对时间戳做"减去偏移量"等价
        raw_entries = [(t + off_s, txt) for t, txt in raw_entries]
        raw_entries.sort(key=lambda x: x[0])

    sentences: list[Sentence] = []
    for i, (t, txt) in enumerate(raw_entries):
        # end = 下一条 start；最后一条 end = start + 合理默认（4s 或文本长度估算）
        if i + 1 < len(raw_entries):
            end = raw_entries[i + 1][0]
            # 相邻如果相等或负差，给个下限
            if end <= t:
                end = t + max(1.0, min(6.0, 0.05 * len(txt)))
        else:
            end = t + max(2.0, min(8.0, 0.06 * len(txt)))
        sentences.append(
            Sentence(
                text=txt,
                start_time=max(0.0, t),
                end_time=max(max(0.0, t) + 0.1, end),
                language=language,
                speaker=speaker,
            )
        )
    return sentences, meta


def read_lrc_file(
    path: str | Path,
    *,
    language: str = "",
    speaker: str = "",
    encoding: str = "utf-8",
) -> Tuple[List[Sentence], LrcMeta]:
    p = Path(path)
    return parse_lrc_text(
        p.read_text(encoding=encoding, errors="replace"),
        language=language,
        speaker=speaker,
    )


def read_lrc_to_project(
    path: str | Path,
    *,
    media_duration: float = 0.0,
    sample_rate: int = 16000,
    audio_path: str = "",
    video_path: Optional[str] = None,
    language: str = "",
    source_language: str = "",
    encoding: str = "utf-8",
) -> Tuple[SubtitleProject, LrcMeta]:
    sentences, meta = read_lrc_file(
        path, language=language, encoding=encoding
    )
    if not audio_path:
        audio_path = str(Path(path).with_suffix(".wav"))
    dur = media_duration
    if dur <= 0 and sentences:
        dur = sentences[-1].end_time
    proj = SubtitleProject(
        audio_path=audio_path,
        video_path=video_path,
        media_duration=dur,
        sentences=sentences,
        sample_rate=sample_rate,
        source_language=source_language or language,
    )
    return proj, meta


# ─────────────────────────────────────────────────────────────
# 写（保留元信息）
# ─────────────────────────────────────────────────────────────
def _format_meta_header(meta: LrcMeta) -> list[str]:
    lines: list[str] = []
    if meta.ti:
        lines.append(f"[ti:{meta.ti}]")
    if meta.ar:
        lines.append(f"[ar:{meta.ar}]")
    if meta.al:
        lines.append(f"[al:{meta.al}]")
    if meta.by:
        lines.append(f"[by:{meta.by}]")
    if meta.offset_ms:
        lines.append(f"[offset:{meta.offset_ms}]")
    for k, v in meta.extra.items():
        lines.append(f"[{k}:{v}]")
    return lines


def write_lrc_text(
    project: SubtitleProject,
    *,
    enhanced: bool = True,
    meta: Optional[LrcMeta] = None,
) -> str:
    """写 LRC，允许把读出来的 LrcMeta 放到文件最头部。"""
    body = to_lrc(project, enhanced=enhanced).rstrip("\n")
    if not meta or not meta.has_any():
        return body + "\n"
    head = "\n".join(_format_meta_header(meta))
    return head + ("\n" if body else "") + body + ("\n" if body else "")


def write_lrc_file(
    project: SubtitleProject,
    path: str | Path,
    *,
    enhanced: bool = True,
    meta: Optional[LrcMeta] = None,
    encoding: str = "utf-8",
) -> None:
    Path(path).write_text(
        write_lrc_text(project, enhanced=enhanced, meta=meta),
        encoding=encoding,
    )
