"""ASS 字段与正文的安全编码/解码工具。

ASS Dialogue 以逗号分字段，正文还把 ``{...}`` 视为 override、``\\N`` 视为
换行。用户正文与生成标签必须分流：正文先 escape，只有项目明确的标签载荷可
作为 markup 拼接。
"""
from __future__ import annotations


def sanitize_ass_field(value: object, *, fallback: str = "") -> str:
    """清洗 Style/Name/Font 等逗号字段；用全角逗号保留可见语义。"""
    text = str(value or fallback)
    text = text.replace("\r", " ").replace("\n", " ").replace(",", "，")
    return text.strip() or fallback


def sanitize_ass_tag_payload(value: object) -> str:
    """清洗不含外层花括号的显式 override 载荷，防止换行/闭合标签逃逸。"""
    return (
        str(value or "")
        .replace("\r", "")
        .replace("\n", "")
        .replace("{", "")
        .replace("}", "")
    )


def escape_ass_text(value: object) -> str:
    """把用户可见正文编码为 ASS Text；不产生任何可执行 override。"""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    # 先保护原始反斜杠/花括号，再把真实换行编码成唯一允许的控制序列。
    text = text.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}")
    return text.replace("\n", r"\N")


def _is_escaped(text: str, index: int) -> bool:
    slashes = 0
    pos = index - 1
    while pos >= 0 and text[pos] == "\\":
        slashes += 1
        pos -= 1
    return bool(slashes % 2)


def strip_ass_override_tags(text: str) -> str:
    """删除未转义的 ``{...}`` override；保留 ``\\{`` / ``\\}`` 字面花括号。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "{" and not _is_escaped(text, i):
            j = i + 1
            while j < len(text):
                if text[j] == "}" and not _is_escaped(text, j):
                    break
                j += 1
            if j < len(text):
                i = j + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def unescape_ass_text(text: str) -> str:
    """解析 ASS 正文转义，不把 ``\\\\N`` 误当换行。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 >= len(text):
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "\\":
            out.append("\\")
        elif nxt == "N":
            out.append("\n")
        elif nxt == "n":
            out.append(" ")
        elif nxt == "h":
            out.append("\u00a0")
        elif nxt in "{}":
            out.append(nxt)
        else:
            out.extend(("\\", nxt))
        i += 2
    return "".join(out)


def decode_ass_text(text: str) -> str:
    """ASS Text → 去 override 的可见正文。"""
    return unescape_ass_text(strip_ass_override_tags(text)).strip()


__all__ = [
    "sanitize_ass_field",
    "sanitize_ass_tag_payload",
    "escape_ass_text",
    "strip_ass_override_tags",
    "unescape_ass_text",
    "decode_ass_text",
]
