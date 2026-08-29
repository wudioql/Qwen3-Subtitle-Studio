"""subs.subtitle_io — 字幕/纯文本导入（SRT / VTT / LRC / 纯文本 → Sentence 列表）

用于「导入字幕 / 纯文本」：
- 有时间戳的文件（SRT/VTT/LRC）→ 解析出带 start/end 的句子（timed=True）
- 纯文本（.txt）→ 按非空行拆句，timed=False（无时间戳，UI 显示为空，待手动对齐）
- 纯文本可指定每句时长占位（用 media_duration 均分），但不写死时间戳语义
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import List, Tuple

from .ass_utils import decode_ass_text
from .models import Sentence, WordTimestamp
from .time_fmt import parse_srt_time, parse_vtt_time, parse_ass_time
from .lrc_io import parse_lrc_text

# 支持的导入后缀
_SUBTITLE_EXT = {".srt", ".vtt", ".lrc", ".txt", ".ass"}


def is_subtitle_file(path: str) -> bool:
    """是否为受支持的字幕/文本文件（按后缀）。"""
    return Path(path).suffix.lower() in _SUBTITLE_EXT


def _strip_tags(text: str) -> str:
    """去行内标签并还原 HTML 实体，使本项目导出的特殊正文可读回。"""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _is_cjk_edge_char(ch: str) -> bool:
    """交界字符是否为 CJK 系（汉字/假名/谚文/CJK 标点/全角），用于多行 cue 拼接判定。"""
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF   # CJK 统一表意（含扩展 A）
        or 0x3040 <= o <= 0x30FF                          # 平/片假名
        or 0xAC00 <= o <= 0xD7AF                          # 韩语音节
        or 0x3000 <= o <= 0x303F                          # CJK 标点（。「」、等）
        or 0xFF00 <= o <= 0xFFEF                          # 全角 ASCII/半角假名
    )


def _join_cue_lines(lines: List[str]) -> str:
    """多行 cue 文本拼接。

    旧实现恒按 " " 连接——多行中/日/韩字幕的行间会凭空多出一个可见空格。
    规则：交界处任一端字符是 CJK 系 → 直接拼接（东亚排版行间无空格）；
    两端都是拉丁/数字 → 补一个单词空格（保持英文多行 cue 的可读性）。
    """
    out = ""
    for ln in lines:
        if not ln:
            continue
        if out and not (_is_cjk_edge_char(out[-1]) or _is_cjk_edge_char(ln[0])):
            out += " "
        out += ln
    return out


def _parse_srt(text: str) -> List[Tuple[float, float, str]]:
    """解析 SRT → [(start, end, text), ...]。"""
    text = text.lstrip("\ufeff")
    blocks = re.split(r"\n\s*\n", text.strip())
    out: List[Tuple[float, float, str]] = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        # 跳过序号行
        first = lines[0].lstrip("\ufeff")
        if first.isdigit():
            lines = lines[1:]
        if not lines:
            continue
        # 时间码行
        if "-->" not in lines[0]:
            continue
        try:
            start_s, end_s = lines[0].split("-->")[:2]
            start = parse_srt_time(start_s.strip())
            end = parse_srt_time(end_s.strip())
        except (ValueError, IndexError):
            continue
        body = _join_cue_lines(lines[1:]).strip()
        body = _strip_tags(body)
        if body:
            out.append((start, end, body))
    return out


def _parse_vtt(text: str) -> List[Tuple[float, float, str]]:
    """解析 WebVTT → [(start, end, text), ...]。"""
    lines = text.splitlines()
    # 跳过头部 "WEBVTT" 及空行/样式
    out: List[Tuple[float, float, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if "-->" not in line:
            i += 1
            continue
        try:
            start_s, end_s = line.split("-->")[:2]
            start = parse_vtt_time(start_s.strip())
            end = parse_vtt_time(end_s.strip())
        except (ValueError, IndexError):
            i += 1
            continue
        i += 1
        body_lines = []
        while i < n and lines[i].strip():
            body_lines.append(lines[i].strip())
            i += 1
        body = _join_cue_lines(body_lines).strip()
        body = _strip_tags(body)
        if body:
            out.append((start, end, body))
        # 跳过块间空行
        while i < n and not lines[i].strip():
            i += 1
    return out


# ─────────────────────────────────────────────────────────────
# ASS 导入（句级 + k-tag 字级）
# ─────────────────────────────────────────────────────────────

def _strip_ass_override_tags(text: str) -> str:
    """去 override 并解码 ASS 正文转义；保留用户字面花括号/反斜杠。"""
    return decode_ass_text(text)


# k-tag 可与其它 override 共处一个块，且外部 ASS 可能使用大写标签。
_K_TAG_RE = re.compile(r"\{[^}]*?\\(?:kf|ko|k)(\d+)[^}]*\}", re.IGNORECASE)


def _parse_ass(text: str) -> List[Tuple[float, float, str, List[WordTimestamp]]]:
    """解析 ASS [Events] → [(start, end, plain_text, words), ...]。

    - 句级：words 为空列表
    - k-tag 字级：words 包含从 k/kf/ko 标签还原的 WordTimestamp
      若某行无 k-tag，则 words=[]，退化为句级
    - Comment 行跳过
    """
    in_events = False
    format_map: dict[str, int] = {}
    text_idx = 9  # ASS 标准格式 Text 在第 10 列（index 9）
    entries: List[Tuple[float, float, str, List[WordTimestamp]]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        # 段标记（大小写不敏感）
        lower = line.lower()
        if lower == "[events]":
            in_events = True
            continue
        if lower.startswith("[") and lower.endswith("]"):
            if in_events:
                break  # 离开 [Events] 段
            continue
        if not in_events:
            continue

        # Format 行
        if lower.startswith("format:"):
            fields_str = line.split(":", 1)[1].strip()
            fields = [f.strip().lower() for f in fields_str.split(",")]
            format_map = {name: idx for idx, name in enumerate(fields)}
            text_idx = format_map.get("text", 9)
            continue

        # 只处理 Dialogue，跳过 Comment
        if not lower.startswith("dialogue:"):
            continue

        # 解析 Dialogue 行
        content = line.split(":", 1)[1].strip()
        parts = content.split(",")
        if len(parts) <= text_idx:
            continue

        start_str = parts[format_map.get("start", 1)].strip()
        end_str = parts[format_map.get("end", 2)].strip()
        raw_text = ",".join(parts[text_idx:]).strip()

        try:
            start = parse_ass_time(start_str)
            end = parse_ass_time(end_str)
        except ValueError:
            continue

        # 尝试 k-tag 字级解析
        words = _parse_ass_karaoke_words(raw_text, start, end)

        # 句级纯文本（剥 override tags）
        plain_text = _strip_ass_override_tags(raw_text)
        if not plain_text:
            continue

        entries.append((start, end, plain_text, words))

    return entries


def _parse_ass_karaoke_words(
    raw_text: str, line_start: float, _line_end: float
) -> List[WordTimestamp]:
    """从 ASS Dialogue Text 中解析 k/kf/ko 标签 → WordTimestamp 列表。

    无 k-tag 时返回空列表（调用方应退化为句级）。
    （`_line_end` 当前未使用：k-tag 累计时长天然收敛于行末，参数保留仅为调用点语义可读。）

    算法：
      1. 用正则找到所有 {\\k\\d+} / {\\kf\\d+} / {\\ko\\d+} 标签
      2. 每个标签的时长（厘秒）是后续字的显示持续时间
      3. 标签到下一个标签之间的文本（剥掉嵌套 override）即为该字可见文本
      4. 字 start = line_start + 累计前序时长；字 end = 字 start + 本字时长
    """
    k_matches = list(_K_TAG_RE.finditer(raw_text))
    if not k_matches:
        return []

    words: List[WordTimestamp] = []
    cursor_cs = 0  # 累计厘秒（从 line_start 起）

    for i, match in enumerate(k_matches):
        duration_cs = int(match.group(1))
        # 当前 k-tag 到下一个 k-tag 之间的文本
        text_start = match.end()
        if i + 1 < len(k_matches):
            text_end = k_matches[i + 1].start()
        else:
            text_end = len(raw_text)
        visible = raw_text[text_start:text_end]

        # 剥掉嵌套的 override tags + 换行转换
        visible_clean = _strip_ass_override_tags(visible)
        if not visible_clean:
            # 零时长或空文本的 k-tag 仍消耗时长但不生成 word
            cursor_cs += duration_cs
            continue

        word_start = line_start + cursor_cs / 100.0
        cursor_cs += duration_cs
        word_end = line_start + cursor_cs / 100.0

        words.append(WordTimestamp(
            text=visible_clean,
            start_time=round(word_start, 3),
            end_time=round(word_end, 3),
        ))

    return words


def _plain_text_sentences(text: str, media_duration: float = 0.0) -> List[Sentence]:
    """纯文本 → 无时间戳句子（timed=False）。

    若给了 media_duration，用均分占位 start/end（仅作排序/预览，语义仍是「未对齐」）。
    """
    out: List[Sentence] = []
    texts: List[str] = []
    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        # .txt 是显式纯文本合同：数字年份、编号和包含“-->”的歌词都必须保留。
        texts.append(ln)
    if media_duration > 0 and texts:
        step = media_duration / len(texts)
        for i, t in enumerate(texts):
            out.append(Sentence(
                text=t,
                start_time=i * step,
                end_time=(i + 1) * step,
                language="",
                is_dirty=True,
                timed=False,
            ))
    else:
        for t in texts:
            out.append(Sentence(text=t, start_time=0.0, end_time=0.0,
                                language="", is_dirty=True, timed=False))
    return out


def _read_file_text_with_encoding(path: Path) -> str:
    """自适应编码链读取文本（utf-8-sig -> utf-8 -> gb18030 -> gbk -> big5 -> replace 兜底）。"""
    raw_bytes = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5"):
        try:
            return raw_bytes.decode(enc).lstrip("\ufeff")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("utf-8", errors="replace").lstrip("\ufeff")


def parse_subtitle_or_text(
    path: str | Path,
    *,
    media_duration: float = 0.0,
    language: str = "",
) -> List[Sentence]:
    """按扩展名解析字幕/文本文件 → Sentence 列表。

    - .srt / .vtt：带时间戳（timed=True）
    - .lrc：带时间戳（timed=True，用 read_lrc 的句级）
    - .txt / 其他：纯文本，timed=False
    """
    p = Path(path)
    raw = _read_file_text_with_encoding(p)
    ext = p.suffix.lower()
    entries: List[Tuple[float, float, str]] = []

    if ext == ".srt":
        entries = _parse_srt(raw)
    elif ext == ".vtt":
        entries = _parse_vtt(raw)
    elif ext == ".lrc":
        sents, _meta = parse_lrc_text(raw, language=language)
        for sentence in sents:
            sentence.is_dirty = True
        return sents  # 外部导入统一标脏；原时间仍保留
    elif ext == ".ass":
        return _build_sentences_from_ass(raw, language=language)
    else:
        return _plain_text_sentences(raw, media_duration=media_duration)

    # 有时间的字幕
    entries.sort(key=lambda x: x[0])
    return [
        Sentence(text=t, start_time=start, end_time=end,
                 language=language, is_dirty=True, timed=True)
        for (start, end, t) in entries
    ]


def _build_sentences_from_ass(
    raw: str, *, language: str = ""
) -> List[Sentence]:
    """从 ASS 文本构建 Sentence 列表。

    自动检测 k-tag：有 k-tag 的 Dialogue 行会还原字级 WordTimestamp；
    无 k-tag 的行退化为句级（words=[]）。
    """
    entries = _parse_ass(raw)
    if not entries:
        return []
    entries.sort(key=lambda x: x[0])
    sentences: List[Sentence] = []
    for start, end, plain_text, words in entries:
        s = Sentence(
            text=plain_text,
            start_time=start,
            end_time=end,
            language=language,
            is_dirty=True,
            timed=True,
        )
        if words:
            s.words = words
        sentences.append(s)
    return sentences


def load_subtitle_to_sentences(
    path: str | Path,
    *,
    media_duration: float = 0.0,
    language: str = "",
) -> List[Sentence]:
    """导入字幕/文本文件 → Sentence 列表（便捷入口）。"""
    return parse_subtitle_or_text(path, media_duration=media_duration, language=language)


__all__ = [
    "is_subtitle_file",
    "parse_subtitle_or_text",
    "load_subtitle_to_sentences",
]
