"""core.asr_engine.splitting —— 切句的文本层：标点判定、域名/缩写/小数点保护、硬切。

只做「把一段文本/片段切成块」：不认识 ``TranscribeConfig``，也不构造 ``Sentence``
（那是 ``.sentences`` 的事）。本地与云端两条路径共用本模块——云端只出文本，
切句逻辑完全相同，所以修这里两端一起受益。
"""

from __future__ import annotations

import re as _re

from subs.models import WordTimestamp


# ═══════════════════════════════════════════════════════════════
# 切句文本层：正则特征 → 标点判定（域名/缩写/小数点保护）→ 硬切 → 按标点切分
# ═══════════════════════════════════════════════════════════════


# Qwen3-ASR 原生输出会带中英日韩全套标点（句末强标点 + 句中弱标点 + 空白），所以这一组 regex 越全越好
# 强句末：必须切分（中文/日韩的 。！？!？;；\n + 英文 !?）
#   英文 . 不放强句末（缩写 e.g. / U.S. 会被误切）→ 放弱句中按 follow 切
_SENT_END_PUNCT_RE = _re.compile(r"([。！？!?；;\n])")
# 弱句中：可能切分（**仅逗号 / 冒号 / 句号 / 叹号 / 问号 / 分号**——避免破坏列表项并列）
# 顿号「、」永远是词内并列，**绝不切**。
_WEAK_PUNCT_RE = _re.compile(r"([，,:：;.。！!?；;])")
# 「任意可切分标点」 → ASR 文本里能识别的标点
_HAS_ANY_PUNCT_RE = _re.compile(r"[，。！？!?；;：:,\.\s]")
#: 「含文字」判定：**排除数字**，只认各语种文字与表情区。
#: 用于识别 ``1946,`` 这类**无文字碎片**——语种判定不统计数字，它们必然判不出，
#: 需继承前一句的语种（否则英文句里的 ``1946,`` 会被贴上中文标签）。
_WORD_CHAR_RE = _re.compile(
    r"[A-Za-zÀ-ɏͰ-῿Ⰰ-퟿"
    r"぀-ヿ㐀-䶿一-鿿가-힯\U00020000-\U0002ffff]"
)
#: 中日韩文字（用于「这句是否以 CJK 为主导」的判断）
_CJK_TEXT_RE = _re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
#: 拉丁字母（英文等）
_LATIN_TEXT_RE = _re.compile(r"[A-Za-zÀ-ɏ]")


def _has_any_punct(text: str) -> bool:
    """任意可切分标点（句中 + 句末 + 空白）都算「有标点」→ 优先走 ASR 标点切分。"""
    return bool(text) and bool(_HAS_ANY_PUNCT_RE.search(text))


def _seg_chars(seg: list[WordTimestamp]) -> int:
    return sum(len(w.text) for w in seg)


def _seg_dur(seg: list[WordTimestamp]) -> float:
    if not seg:
        return 0.0
    return seg[-1].end_time - seg[0].start_time


def _split_segment_overflow(
    seg: list[WordTimestamp],
    *,
    max_chars: int,
    max_sec: float,
) -> list[list[WordTimestamp]]:
    """把一个 word segment 按 max_chars / max_sec 切成若干不溢出的子段（字级边界安全切）。

    规则：
      - max_chars > 0：单段字数不超过 max_chars
      - max_sec > 0：单段时长不超过 max_sec
      - 两个条件**任一超限就切**；都=0 不切
      - 边界永远切在两个 word 之间（不会把一个字/词切成两半）
    """
    if not seg:
        return []
    if (max_chars <= 0 or _seg_chars(seg) <= max_chars) and (
        max_sec <= 0 or _seg_dur(seg) <= max_sec
    ):
        return [list(seg)]

    out: list[list[WordTimestamp]] = []
    cur: list[WordTimestamp] = []
    cur_chars = 0
    cur_start = 0.0
    first = True
    for w in seg:
        # 评估：把 w 放进 cur 后是否会超限
        next_chars = cur_chars + len(w.text)
        next_start = cur_start if cur else w.start_time
        next_end = w.end_time
        next_dur = next_end - next_start
        chars_ok = (max_chars <= 0) or (next_chars <= max_chars)
        dur_ok = (max_sec <= 0) or (next_dur <= max_sec)
        # cur 非空 + (加了 w 超限) → 先把 cur 收走
        if (not first) and (not chars_ok or not dur_ok):
            out.append(list(cur))
            cur = [w]
            cur_chars = len(w.text)
            cur_start = w.start_time
        else:
            cur.append(w)
            cur_chars = next_chars
            if first:
                cur_start = w.start_time
                first = False
    if cur:
        out.append(list(cur))
    return out


# ─────────────────────────────────────────────────────────────
# 标点切分 + 标点 word 注入（核心修复）
# ─────────────────────────────────────────────────────────────

_DOMAIN_CHAR_RE = _re.compile(r"[0-9A-Za-z_\-]")
#: 汉字 / 假名 / 谚文（用于「点后紧跟 CJK → 必是中文里的小数点」判断）
_CJK_CHAR_RE = _re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
#: 常见顶级域片段——出现它们几乎可以确定这是域名而非句子边界。
_DOMAIN_TLD_HINTS = (
    "com", "org", "net", "edu", "gov", "int", "mil", "io", "ai", "co", "cn",
    "uk", "jp", "de", "fr", "info", "biz", "dev", "app", "me", "tv", "us",
    "html", "htm", "php", "aspx", "wiki",
)
#: 英文常见缩写（后面跟的「.」不是句号）
_ABBREV_HINTS = (
    "e", "i", "eg", "ie", "etc", "vs", "cf", "al", "fig", "no", "vol", "pp",
    "mr", "mrs", "ms", "dr", "prof", "st", "inc", "ltd", "co", "corp",
    "approx", "dept", "est", "min", "max", "sec",
)


def _next_word(text: str, pos: int) -> str:
    """取 ``pos`` 之后跳过空白得到的下一个词的**首字符**（取不到返回空串）。

    用于判断缩写收尾点后是否仍是「一句话的开头」——``e.g. The`` 里The
    首字母大写，而 ``sentence. another`` 里another 是小写。
    """
    m = _re.match(r"\s*([0-9A-Za-z])", text[pos:])
    return m.group(1) if m else ""


def _is_domain_dot(text: str, pos: int) -> bool:
    """判断 ``text[pos]`` 这个「.」是否属于域名 / 缩写内部（是则不该切句）。

    英文句号被降级成弱标点是为了不切碎 ``e.g.`` / ``U.S.``，但代价是
    **URL 会被切碎**：``en.wikipedia.org`` → ``en.`` + ``wikipedia.org.``。
    这里补上缺的那一半判断。

    判定采取「向后看为主」——域名点后面紧跟字母且能读出已知 TLD / 缩写片段，
    就认定它属于同一个 token。这也顺带避免了 ``...org.Politics`` 这种
    「句号后紧跟大写无空格」的粘连问题（点后是数字或小写才可能是真句号）。
    """
    n = len(text)
    # 向前看：必须紧邻域名字符（否则如「好。」不在此列）
    if pos == 0 or not _DOMAIN_CHAR_RE.fullmatch(text[pos - 1]):
        return False

    # 向后扫一个 token（字母数字/-/_）——同时保留**原始大小写**，
    # 因为「点后紧跟大写」是句号漏空格的可靠信号（sentence.Another），
    # 一旦lower() 就会把它误判成域名。
    j = pos + 1
    while j < n and _DOMAIN_CHAR_RE.fullmatch(text[j]):
        j += 1
    raw_token = text[pos + 1:j]
    token = raw_token.lower()

    # 向前扫一个 token
    k = pos - 1
    while k >= 0 and _DOMAIN_CHAR_RE.fullmatch(text[k]):
        k -= 1
    head = text[k + 1:pos].lower()

    # --- 情况 A：本点是 token 的**收尾**，后面是空白/下一个词/结尾 ---
    # 典型：``e.g. The`` / ``U.S. government`` / ``example.com.`` 里的最后一个点。
    # 这些点绝不能切，否则缩写和域名都会被腰斩。
    prev = text[pos - 1].lower()
    if not token:
        if prev in _DOMAIN_TLD_HINTS:
            return True
        if prev in _ABBREV_HINTS:
            # 缩写收尾（``e.g.``/ ``Dr.``）——但要排除「这是普通单词的句号」。
            # 判据：缩写后面紧跟的下一个词**首字母大写或数字**，说明它仍在
            # 一句话的开头；而 ``sentence. Another`` 后面是普通小写词。
            nxt_word = _next_word(text, j)
            if prev == "e" or prev == "i" or prev in ("eg", "ie"):
                # 单字母缩写（``e.g.`` / ``i.e.``）的收尾点。
                #
                # 2026-10-04 修正：这里原先无条件return True，导致**任何**以 e/i
                # 结尾的普通英文单词的句号都被当成缩写点而吞掉句界
                # ——实测 ``...West Germanic language.它到底该怎么切分`` 里的
                # ``language.`` 命中本分支，整句没被切开；英文因此与后面的中文
                # 粘成一块（``counts["han"] > 0``），语种退回 Chinese，
                # 再被24 字中文字幕上限从单词中间劈开（``Po|litics``）。
                #
                # 真正的``e.g.``/ ``i.e.`` 有个硬特征：本点前面那个「e/i」本身
                # 就是点分缩写的一员，即它的**前一个字符也是点**（``i.e.`` 里的
                # e前面是 .）。而 ``language.`` 的 e 前面是 g，据此即可区分。
                dotted = k >= 0 and text[k] == "."
                if dotted:
                    return True
                # 非点分 → 按普通单词句号处理，继续走下面的常规判据。
            if nxt_word and (nxt_word[0].isupper() or nxt_word[0].isdigit()):
                return True
            return False
        # ``U.S.`` 这种「单字母 + 点」连续结构的收尾
        if head.isalpha() and len(head) <= 2 and head.islower():
            return True
        return False

    # --- 情况 B：本点后面还有 token 字符（域名的中间点，如 example**.com**.path）---
    # **两侧都是数字**时该点必是数值内部，绝不是句界：小数（``3.5``）、
    # 千分位（``1,234.5``）、百分数（``0.8%``）、版本号（``2.1``）。
    # 这条必须**在下面的 nxt 分支之前**判定——``0.8%`` 的 nxt 是 ``%``，
    # 会在那里被当成「非域名点」直接 return False，导致「误差 0.8%」从中间腰斩。
    if (head.isdigit() and token.isdigit()):
        return True

    nxt = text[j] if j < n else ""
    if nxt and nxt not in ". \t\n":
        # 中文语境里「点后紧跟汉字」只可能是小数点/数量片段（`3.5万`、`2.5亿`），
        # 绝不可能是英文句界——英文句号后必是空白、大写字母或标点。
        # 不挡的话「售价 3.5 万元」会被从 `3.5` 处腰斩。
        if _CJK_CHAR_RE.fullmatch(nxt):
            return True
        # 例句「www.example.com/path」→ com 后面是 /，仍算域名内
        if nxt != "/":
            return False

    if token in _DOMAIN_TLD_HINTS or token in _ABBREV_HINTS:
        return True
    if head in _DOMAIN_TLD_HINTS or head in _ABBREV_HINTS:
        return True
    # 裸「字母.数字.字母」只有在两侧都**非大写开头**时才当域名：
    # ``example.com`` / ``3.14159`` 属于此列；
    # 而 ``sentence.Another`` / ``...org.Politics`` 是句号后漏了空格，必须切开。
    if (head and token and head.isalnum() and token.isalnum()
            and not head[0].isupper() and not raw_token[0].isupper()):
        return True
    return False


def _hard_split(seg: str, cut_c: int) -> list[str]:
    """超长句兜底硬切；**拉丁文本对齐词边界**，中文按字数原样切。

    为什么不能直接 ``seg[i:i + cut_c]``（2026-10-04 修正）：那是按字符数盲切，
    英文会从**单词中间**断开——实测 159 字纯英文被切成
    ``...originated in the`` / `` Anglo-Saxon peoples...``。字幕读者看到的是断词，
    对齐器拿到的也是一个不存在的「词」。这正是用户报的「单词字母硬切」。

    切点规则：向前找最近一个空白并**在空白处断**（空白本身丢弃，两段都不带
    首尾空白——留着会在字幕里显示成多余空格）。回溯距离限制在半个切分长度内；
    若切点正好落在一个超长单词内部（窗口内没有任何空白），就按字切——否则既造
    碎片词又会因``start`` 不前进而死循环。CJK 文本没有空格概念，仍按字数硬切
    （**行为一字不变**，tests/test_punctuation.py 的中文护栏守着这条）。
    """
    if cut_c <= 0 or len(seg) <= cut_c:
        return [seg]
    if _is_cjk_dominant(seg):
        return [seg[i:i + cut_c] for i in range(0, len(seg), cut_c)]

    out: list[str] = []
    start = 0
    n = len(seg)
    floor = max(1, cut_c // 2)      # 回溯窗口下限，保证 start 一定前进
    while start < n:
        end = min(start + cut_c, n)
        if end >= n:                      # 最后一段：到头即止
            piece = seg[start:end]
            if piece.strip():
                out.append(piece)
            break
        brk = -1
        for p in range(end - 1, start + floor - 1, -1):
            if seg[p].isspace():
                brk = p                   # 空白本身丢弃，下一段从 p+1 起
                break
        if brk <= start:
            brk = end                # 切点落在超长单词内部 → 按字切
        piece = seg[start:brk]
        if piece.strip():
            out.append(piece)
        start = brk + 1 if brk < end else brk
    return out or [seg]


def _is_cjk_dominant(seg: str) -> bool:
    """该片段是否以**中日韩文字**为主导（汉字/假名/谚文 多于 拉丁字母）。

    决定逗号要不要当句界用：
    - **中文系**：中文逗号 ``，`` 是真句界（「青紫色的风掠过指尖，金线牡丹在呼吸间流转。」
      读作两句），必须按原逻辑切，行为逐字不变。
    - **英文系**：逗号只是**从句边界**，比句点轻一个层级。先按句点切、
      只有超上限才从逗号切，否则英文会被逗号切碎成 `In 1946,` / `,` 这类碎片
      （真机GUI 测试发现：XingChen 输出的英文只有逗号，粒度明显偏碎）。
    """
    cjk = len(_CJK_TEXT_RE.findall(seg))
    latin = len(_LATIN_TEXT_RE.findall(seg))
    return cjk > latin


def _split_text_by_punct(
    text: str,
    *,
    min_chars_per_split: int = 4,
    strong_only: bool = False,
) -> list[str]:
    """把 ASR 原始带标点文本切句，保留标点本身。

    **两级切分**（``strong_only=True`` 启用第1 级，2026-10-04）：

    1. 第 1 级 —— 只在**强标点**处切：中文/日韩的 ``。！？；`` + 英文 ``.!?;``
       （英文句点要过 :func:`_is_domain_dot` 域名/缩写/小数点保护）。这是**语义句界**。
    2. 第 2 级 —— 追加按**弱标点**（``,`` ``:``）切，给已经超长的一句做二次细分。

    为什么要两级（真实 GUI 测试发现的缺陷）：原实现把 ``,`` 和 ``.``
    **平铺在同一次扫描里**，谁先到谁切。英文于是被逗号抢先切碎——
    ``Politics ... Wikipedia, the free encyclopedia,1946, was written ...``
    被切成 10/11/49/23/5/36 字符的碎片，``1946,`` 单独成句且因「数字不计数」
    被判成中文；更糟的是碎片长度全部< 24，``max_sentence_chars`` 那一步压根不触发，
    **「单句最大字数」对英文完全失效**（用户实测：改了设置没反应）。

    顿号「、」永远是词内并列，**绝不切**（中文里「经济、新闻」不该被劈开）。

    Returns:
        List[str]，每个元素是一段含尾部标点的完整子句，例如：
            '青紫色的风掠过指尖，'  '金线牡丹在呼吸间流转，'  '墨香未干，'  '茶烟已绕过雕花窗。'
    """
    if not text:
        return []
    # 一次性扫描：分两类标点（强/弱），记录位置 + 类型
    cuts: list[tuple[int, str]] = []  # (位置之后, 切分标点字符)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if _SENT_END_PUNCT_RE.fullmatch(ch):
            cuts.append((i + 1, ch))
            i += 1
            continue
        if strong_only and ch == ".":
            # 英文句点升为强标点（第 1 级）。域名 / 缩写 / 数值内部（`3.5`、`
            # 0.8%`）由 _is_domain_dot 挡掉，粘连漏空格（`Wikipedia.The`）则切开。
            if not _is_domain_dot(text, i):
                cuts.append((i + 1, ch))
            i += 1
            continue
        if _WEAK_PUNCT_RE.fullmatch(ch):
            if strong_only:
                # 第 1 级只看句界，逗号/冒号一律留给第 2 级
                i += 1
                continue
            # 弱标点：统计后续非标点字符数（空格跳过但不中断计数——支持英文 `,` 切句）
            j = i + 1
            follow = 0
            while j < n and not _WEAK_PUNCT_RE.fullmatch(text[j]):
                if text[j] not in " \t":
                    follow += 1
                j += 1
            # 域名/URL 内部的「.」不是句号：en.wikipedia.org 不能被切成 en. + wikipedia.org.
            if ch == "." and _is_domain_dot(text, i):
                i += 1
                continue
            if follow >= max(2, min_chars_per_split):
                cuts.append((i + 1, ch))
            i += 1
            continue
        i += 1

    if not cuts:
        return [text]

    # 边界去重（连续多个标点只取第一个的右边界作为切点）
    boundaries: list[int] = [0]
    last = 0
    for pos, _ in cuts:
        if pos > last:
            boundaries.append(pos)
            last = pos
    if last < n:
        boundaries.append(n)

    parts: list[str] = []
    for a, b in zip(boundaries, boundaries[1:]):
        seg = text[a:b]
        if seg.strip():
            parts.append(seg)
    return parts
