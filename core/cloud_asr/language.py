"""core.cloud_asr.language —— 语言决议、脚本判定与响应归一化。

三件事：
1. 语言名归一化：服务端给全名（``Chinese``）或不给，短码兜底，两种都要能吃；
2. **逐句**判语种：ASR 只返回一个 language，中英混说时必须按句拆，否则英文段会被
   整段标成中文（判据：有汉字→zh，纯拉丁→en，独占脚本优先）；
3. 把服务端 JSON 归一化成 ``CloudASRResult``（``parse_response``，含说话人前缀剥离）。

``parse_response`` 放在这里而非 ``.client``：它的实质工作就是语言字段归一化 + 文本清洗，
与上面的判定共用同一批正则，放一起才是内聚。
"""


from __future__ import annotations


import logging
import re
from typing import Any, Optional, Sequence

from ..language_utils import LANG_SHORT_TO_FULL


from .facts import OFFICIAL_LANGUAGES, VERIFIED_FREE_MODELS


from .types import CloudASRResult, CloudSegment


logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 响应归一化
# ═══════════════════════════════════════════════════════════════

_SPEAKER_PREFIX_RE = re.compile(r"^\s*\d+\s*[:：]\s*")


def strip_speaker_prefixes(text: str) -> str:
    """剥离 Diarize 系列 ``text`` 里的 ``"1: "`` 说话人前缀。

    ``asr_engine._split_text_by_punct`` 会把 ``"1: "`` 当成独立子句（``1:`` 只有 2 字、
    冒号是弱标点且不达切分阈值），产出 ``"1: "`` 这样的垃圾句，所以必须在进分句前剥离。
    """
    if not text:
        return ""
    return "\n".join(_SPEAKER_PREFIX_RE.sub("", ln) for ln in text.splitlines()).strip()


def normalize_language(raw: str) -> str:
    """把云端返回的 language 归一化成项目内部的语言**全名**。

    SenseVoice / Qwen3 返回 ``"Chinese"`` 这类英文全名，恰好与
    ``language_utils.LANG_SHORT_TO_FULL`` 的值一致；但静音/无法判定时会返回
    **字符串** ``"None"``（不是 JSON null），必须显式识别掉。
    """
    s = str(raw or "").strip()
    if not s or s.lower() in ("none", "null", "unknown", "n/a"):
        return ""
    for short, full in LANG_SHORT_TO_FULL.items():
        if full and s.lower() == full.lower():
            return full
    if s.lower() in LANG_SHORT_TO_FULL:  # 某些模型可能返回短码
        return LANG_SHORT_TO_FULL[s.lower()] or ""
    return s


#: 字符脚本 → 它可能属于的语种短码。用于模型不返回 language 时从转写文本反推。
#: 假名/谚文/西里尔是**独占**脚本，出现即可定语言；汉字与拉丁则按用户规则直判
#: （有汉字→zh，纯拉丁→en），故这里只登记独占脚本与两个粗粒度类别。
_SCRIPT_TO_CODES: dict[str, tuple[str, ...]] = {
    "kana": ("ja",),                                  # 假名：日语独有
    "hangul": ("ko",),                                # 谚文：韩语独有
    "cyrillic": ("ru",),                              # 西里尔：俄语
    "han": ("zh",),                                   # 汉字：直判中文（含中英混杂）
    "latin": ("en",),                                 # 拉丁：直判英文
}


#: 只要出现就足以定语言的脚本（假名/谚文/西里尔不像汉字那样被多语种共用）。
_DECISIVE_SCRIPTS = ("kana", "hangul", "cyrillic")


#: 拉丁字母的**全部**候选语种。与 :data:`_SCRIPT_TO_CODES` 分开是刻意的：
#: 上面那张表服务于「有 en 就判英文」的快路径，而这里服务于「候选集里没有 en」
#: 时的唯一解判定——fr/de/it/pt/es 同为拉丁，只有剩一个时才算确定。
_LATIN_CANDIDATES = ("en", "fr", "de", "it", "pt", "es")


def _script_counts(text: str) -> dict[str, int]:
    """按 Unicode 区段统计各类字符数量（标点/数字不计数，避免噪声干扰）。"""
    counts = {k: 0 for k in _SCRIPT_TO_CODES}
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
            counts["kana"] += 1
        elif 0x1100 <= o <= 0x11FF or 0xAC00 <= o <= 0xD7AF:
            counts["hangul"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF:
            counts["han"] += 1
        elif ch.isalpha() and o < 0x0300:      # 拉丁字母（含带变音符的 é/ü/ã），排除希腊字母
            counts["latin"] += 1
    return counts


def infer_language_from_text(text: str, candidates: Sequence[str]) -> Optional[str]:
    """从转写文本反推语种——**只在候选集内**判定，判不出就老实返回 None。

    这是「模型不返回 language + 用户选了 auto」时的第二层兜底。

    中英判定规则（用户拍板，刻意从简）：**文本里只要有汉字就算中文**，中英混杂
    也算中文；纯拉丁字母才算英文。理由是强制对齐器需要的是「这条时间轴属于哪种
    语言」而非逐字溯源——中文轨配中文模型即可，混排里的英文单词不影响对齐。
    纯英文则必须判 en，否则拉丁文本会被当成中文送去对齐。

    假名/谚文/西里尔是独占脚本，出现即定语言（SenseVoice 靠它们区分日/韩/俄）。

    仍会返回 None 的唯一情形：拉丁字母但候选集里**没有 en**（如 fr/de/it/pt/es
    互相无法区分）——此时宁可让用户显式指定，也不猜一个可能错的语言去污染时间轴。
    同理，候选集里没有 zh 时出现汉字也不猜。

    返回短码；无法确定时返回 ``None``（调用方应据此报错而不是硬编一个默认值）。
    """
    if not text or not candidates:
        return None
    counts = _script_counts(text)
    if not any(counts.values()):
        return None
    pool = set(candidates)

    # 独占脚本优先：假名 / 谚文 / 西里尔不会被其它语种共用
    for name in _DECISIVE_SCRIPTS:
        if counts[name]:
            hit = [c for c in candidates if c in _SCRIPT_TO_CODES[name]]
            return hit[0] if len(hit) == 1 else None

    # 有汉字 → 中文（用户规则：中英混杂也归中文，不做「谁多谁少」的加权）
    if counts["han"] and "zh" in pool:
        return "zh"
    # 纯拉丁：候选集有 en 就判英文（英文素材在本项目里是最常见的非中场景）
    if counts["latin"] and "en" in pool:
        return "en"
    # 候选集里没有 en 时才走保守路径：拉丁对应多种罗曼/日耳曼语，唯一解才算，
    # 多解则放弃（猜错语言会让整条时间轴歪掉）
    latin_pool = [c for c in candidates if c in _LATIN_CANDIDATES]
    if len(latin_pool) == 1:
        return latin_pool[0]
    return None


#: 允许「整段中/粤→ 逐句切出英文」的模型（用户拍板的适用范围）。
#:
#: 纳入门槛只有一条：**2026-10-04 实测中英混说时输出英文原文**（汉字 0 / 拉丁 N），
#: 而不是「理论上应该能推出来什么」。实测账本见 :data:`VERIFIED_FREE_MODELS` 的 ``languages``。
#:
#: - SenseVoiceSmall：纯英文与混说均输出原文；免费模型里唯一覆盖中/英/日/粤的。
#: - Qwen3-ASR-1.7B：本地默认模型，行为与云端同源。
#: - Ultra / V3.2 / Diarize：XingChen 系虽**不返回 language**（靠兜底链补），
#:   但混说时英文段确为原文输出，切出必然正确 —— 不返回语言恰恰是本补丁存在的理由。
#:   Diarize 另有收益：``segments`` 已由服务端按语句切好，逐句判定直接作用于
#:   服务端边界（实测中英混说 26.7s 给出「中文 3 段 + 英文 1 段 + 杂音 1 段」），
#:   比标点切句更准，见 :func:`~core.asr_engine._sentences_from_segments`。
#:
#: **唯一刻意排除**：``XingChenAGI/XingChenGSR-V1.0`` —— 生成式语音识别会把外语音频
#: **译成中文文本**再输出（实测英文素材输出「来自维基百科的英语政治与英语内容…」，
#: 汉字 37 / 拉丁 8）。它的输出里根本不存在英文文本，切英文等于把中文译文送去英文
#: 对齐器，必错且会污染时间轴。粤语同理：GSR 会转成普通话书面语。
#:
#: 不做全语种泛化：其它语言混入属**已知缺陷**，宁可不判也不猜一个可能错的语言。
_MIXED_ZH_EN_PATCH_MODELS: frozenset[str] = frozenset({
    "FunAudioLLM/SenseVoiceSmall",
    "Qwen/Qwen3-ASR-1.7B",
    "XingChenAGI/XingChenASR-V3.2-Ultra",
    "XingChenAGI/XingChenASR-V3.2",
    "XingChenAGI/XingChenASR-Diarize-V3.0",
})


def supports_zh_en_sentence_split(model: str) -> bool:
    """该模型是否启用「整段中/粤→ 逐句切出英文」补丁。见 :data:`_MIXED_ZH_EN_PATCH_MODELS`。"""
    return model in _MIXED_ZH_EN_PATCH_MODELS


#: 整段语种为这些语言时，**同样**启用「逐句切出英文」（用户 2026-10-07 拍板）。
#:
#: 为什么日/韩要单列一档而不是并入中/粤那一档：日语句子天然含汉字（Kanji），
#: 「日本語勉強中」实测是 ``han=6 / kana=0`` —— 连假名都没有。若沿用中/粤的
#: 「含汉字 → 沿用整段语种」判据，日整段里的英文句会被汉字拖累而被漏判。
#: 所以日/韩走一条**独立分支**：只挡独占脚本（假名/谚文/西里尔），不挡汉字。
#:
#: 明确**不在**本档内（用户拍板：中/粤里一般不混日韩，日/韩里一般不混中粤）：
#: - 中/粤 ↔ 日/韩 互判：不做。宁可漏判也不猜错语种污染时间轴。
#: - 俄语等其它语言：不做，同上。
_JA_KO_TO_EN: frozenset[str] = frozenset({
    LANG_SHORT_TO_FULL["ja"],
    LANG_SHORT_TO_FULL["ko"],
})


def split_zh_en_sentence_language(
    text: str,
    project_lang: str,
    model: str,
) -> Optional[str]:
    """中英混说补丁：**只**把「整段中/粤」里的纯英文句切出来判成英文，其余原样沿用。

    为什么不复用 :func:`infer_language_from_text`：那个函数的候选集来自模型语种表，
    粤语整段若按「有汉字→zh」判定，粤语句会被**误标成中文普通话**——而粤语对齐器
    是独立的（``Cantonese``），标错等于整段时间轴作废。本函数因此把「整段语种」
    当作**权威值**继承下去，只在识别出「纯拉丁、无任何汉字」时才改判 en。

    用户拍板的范围（刻意从简，属**已知缺陷**的定点缓解，不是通用方案）：

    - 单段单语言是 ASR 模型的**硬性限制**（本地/云端 Qwen3、SenseVoice 实测均返回
      单一 language），理论上的其它语言混入**不做判定**——只处理「整段语言→英文」
      这一种方向（用户 2026-10-07 拍板：中/粤里一般不混日韩，反向同理）。
    - 整段为中/粤时，**纯英文**句（无汉字、无假名/谚文/西里尔）切出为 ``English``；
      含汉字的句子一律沿用整段语种（粤语因此不会被误标为中文）。
    - 整段为日/韩时（见 :data:`_JA_KO_TO_EN`）**同样**切出英文句，但判据不同：
      不挡汉字（日语 Kanji 与汉字同码区），只挡假名/谚文/西里尔。
    - 仅对 :data:`_MIXED_ZH_EN_PATCH_MODELS` 里的模型生效；其它模型返回 ``None``，
      调用方据此沿用整段语种（粤语需求可手动选对齐模型，或直接不用这些模型）。

    Args:
        text: 单句文本（已由标点切句切好）。
        project_lang: 整段语种**全名**（如 ``"Chinese"`` / ``"Cantonese"``）。
        model: 模型 id，用于查补丁适用范围。

    Returns:
        判定为英文时返回 ``"English"``；其余情况返回 ``None``（=沿用整段语种）。
    """
    if not supports_zh_en_sentence_split(model):
        return None
    if not text or not text.strip():
        return None
    counts = _script_counts(text)
    counts_latin = counts["latin"]
    counts_kana = counts["kana"]
    counts_hangul = counts["hangul"]
    counts_cyrillic = counts["cyrillic"]
    # 整段是中/粤 → 切英文；整段是日/韩 → 也切英文（用户拍板，见 _JA_KO_TO_EN）。
    if project_lang in _JA_KO_TO_EN:
        # 日语句子天然含汉字（Kanji）：「日本語勉強中」是 han=6 / kana=0，
        # 连假名都不一定有。因此这里**不能**沿用中/粤的「含汉字→沿用整段」判据，
        # 只能挡下仍带本地独占脚本的句子——那些必定不是英文。
        # 韩语同理（谚文独占，无歧义）。
        if counts_kana or counts_hangul or counts_cyrillic:
            return None
        if counts_latin:
            return LANG_SHORT_TO_FULL["en"]
        return None
    # 只在「整段是中文或粤语」时才切英文
    if project_lang not in (LANG_SHORT_TO_FULL["zh"], LANG_SHORT_TO_FULL["yue"]):
        return None
    if counts["han"] or counts_kana or counts_hangul or counts_cyrillic:
        return None          # 含汉字/其它独占脚本 → 沿用整段语种（保护粤语不被误标）
    if counts_latin:
        return LANG_SHORT_TO_FULL["en"]
    return None              # 纯标点/数字碎片 → 沿用整段语种


# 黏连块里「纯拉丁尾巴」的最短长度。低于此值视为「中文里夹了几个英文词」，
# 不切——宁可漏判也不误伤粤语（用户对粤语零容忍，见 split_zh_en_sentence_language）。
_MIN_LATIN_TAIL_CHARS = 20


# 末字汉字的定位正则。判据与 :func:`_script_counts` 的 Unicode 区段保持一致
# （含扩展 A的 0x3400-0x4DBF），避免两处对「什么算汉字」的理解分叉。
_HAN_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF]")


_LATIN_RE = re.compile(r"[A-Za-z]")


_KANA_RE = re.compile(r"[\u3040-\u30FF\u31F0-\u31FF]")


_HANGUL_RE = re.compile(r"[\u1100-\u11FF\uAC00-\uD7AF]")


_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def split_mixed_script_tail(
    text: str,
    project_lang: str,
    model: str,
) -> Optional[tuple[str, str]]:
    """把「中文句尾 + 英文句首」的**黏连块**切成 ``(中文头, 英文尾)``。

    为什么需要这个函数（2026-10-04 真机实测）：:func:`split_zh_en_sentence_language`
    遇到**含汉字**的文本一律返回 ``None``（保护粤语不被误标），这在「一句一语种」
    的素材上完全正确。但SenseVoice 在中英混合素材上会吐出**无标点的黏连块**::

        今夜的月色可愿与我共采一段流光politics and the English language from…

    这块144 字符里既有汉字又有拉丁，按「含汉字→沿用整段语种」的规则会**整块标成
    Chinese**，于是英文部分被送去中文对齐器。实测日志：``11 句 / 1 语言段``——
    逐句判定形同虚设。

    切分判据刻意保守到近乎苛刻——**只看末字汉字之后那段尾巴**：
    尾巴须无假名/谚文/西里尔、纯长度 ≥ 20 字符。否则一律返回 ``None``，
    调用方沿用原有行为。这样粤语（「我嘅 English 好正」）、纯中文、纯英文
    全部不受影响，只有真正的大段英文才会被切出来。

    与 :func:`split_zh_en_sentence_language` 的分工：那个函数回答「这一句是什么
    语言」，这个函数回答「这一句内部是否藏着另一种语言的整段」。

    Args:
        text: 待检查的文本片段（通常是强标点切出的一整块）。
        project_lang: 整段语种**全名**。
        model: 模型 id，用于查补丁适用范围。

    Returns:
        ``(head, tail)``；不满足切分条件时返回 ``None``。
        ``head + tail`` 恒等于 ``text``（无损，调用方无需担心丢字符）。
    """
    if not supports_zh_en_sentence_split(model):
        return None
    if project_lang not in (LANG_SHORT_TO_FULL["zh"], LANG_SHORT_TO_FULL["yue"]):
        return None
    if not text or not text.strip():
        return None

    han_positions = [m.start() for m in _HAN_RE.finditer(text)]
    if not han_positions:
        return None              # 无汉字 → 交给 split_zh_en_sentence_language 整句判定
    tail = text[han_positions[-1] + 1:]
    if not tail.strip():
        return None
    if _KANA_RE.search(tail) or _HANGUL_RE.search(tail) or _CYRILLIC_RE.search(tail):
        return None              # 尾巴是日/韩/俄文 → 不是英文
    if len(tail.strip()) < _MIN_LATIN_TAIL_CHARS:
        return None              # 尾巴太短 → 是「中文里夹几个英文词」，不切
    if not _LATIN_RE.search(tail):
        return None              # 无任何拉丁字母（纯数字/标点）
    return text[:han_positions[-1] + 1], tail


def languages_for_model(model: str) -> tuple[str, ...]:
    """该模型**本项目可用**的语种短码：实测优先，其次官方口径，都没有则退回对齐器 11 种。

    第三档是刻意的保守选择——对陌生模型按「什么都可能」给候选，配合
    :func:`infer_language_from_text` 的「多候选不猜」规则，效果是宁可报错也不乱猜。
    """
    facts = VERIFIED_FREE_MODELS.get(model)
    if facts is not None and facts.languages:
        return facts.languages
    official = OFFICIAL_LANGUAGES.get(model)
    if official:
        return official
    return tuple(c for c in LANG_SHORT_TO_FULL if c != "auto")


def parse_response(payload: dict[str, Any], *, model: str, trace_id: str) -> CloudASRResult:
    """把服务端 JSON 归一化成 :class:`CloudASRResult`（字段随模型而异）。"""
    raw_text = str(payload.get("text") or "").strip()
    # 只有 Diarize 系列会在 text 上带 "1: " 说话人前缀（实测），其余模型透传原文本。
    # 这里原本还有一行 ``if not cleaned and raw_text: cleaned = strip(...)`` 作「兜底」，
    # 但它在任何路径下都不可能改变结果：非 Diarize 时 cleaned 就是 raw_text（空则两者
    # 皆空，条件不成立）；Diarize 时重剥一次前缀得到的仍是同一个值。已删除。
    cleaned = strip_speaker_prefixes(raw_text) if model == "XingChenAGI/XingChenASR-Diarize-V3.0" \
        else raw_text

    usage = payload.get("usage") or {}
    try:
        usage_sec = float(usage.get("seconds", 0) or 0)
    except (TypeError, ValueError):
        usage_sec = 0.0
    try:
        duration = float(payload.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0

    segments: list[CloudSegment] = []
    for item in payload.get("segments") or []:
        if not isinstance(item, dict):
            continue
        try:
            segments.append(CloudSegment(
                start=float(item.get("start", 0) or 0),
                end=float(item.get("end", 0) or 0),
                text=str(item.get("text") or "").strip(),
                speaker=str(item.get("speaker") or "").strip(),
            ))
        except (TypeError, ValueError):
            logger.warning("[cloud-asr] 跳过结构异常的 segment：%r", item)

    return CloudASRResult(
        text=cleaned,
        raw_text=raw_text,
        language=normalize_language(payload.get("language")),
        duration_sec=duration,
        usage_seconds=usage_sec,
        trace_id=trace_id,
        segments=segments,
    )
