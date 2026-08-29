"""core.align_engine.common — 裁剪 / 接缝吸附 / 语言 / 分词依赖。"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Tuple

import numpy as np

from subs.models import Sentence, SubtitleProject, WordTimestamp

from core.constants import ALIGN_SEAM_SNAP_MAX
from core.language_utils import LANG_SHORT_TO_FULL, resolve_language as _resolve_language
from core.text_utils import merge_punct_into_words, sanitize_word_timestamps


logger = logging.getLogger("core.align_engine")

def _crop_audio(
    audio_np: np.ndarray, sr: int,
    start_sec: float, end_sec: float,
    pad_before: float = 0.0, pad_after: float = 0.0,
) -> Tuple[np.ndarray, float, float]:
    total_n = audio_np.shape[0]
    s_idx = max(0, int(round((start_sec - pad_before) * sr)))
    e_idx = min(total_n, int(round((end_sec + pad_after) * sr)))
    if e_idx <= s_idx:
        return np.zeros((0,), dtype=audio_np.dtype), start_sec, end_sec
    return audio_np[s_idx:e_idx], s_idx / sr, e_idx / sr


def snap_tail_to_next_start(sentence: Sentence, next_start: Optional[float]) -> None:
    """句间接缝吸附（原地修改）：尾字结束点与后句起点的间隙 ≤ ALIGN_SEAM_SNAP_MAX
    时，把尾字 end（连同句尾）吸附到后句起点。

    动机：
    - 单句/脏句重对齐按裁剪窗新建 20ms 帧网格，尾字常停在帧边界，与后句
      起点恒差 0~20ms；
    - 全文重对齐若**按语言分段**，每段各自一张网格，**段与段接缝**同样会
      出现 ≤25ms 空隙（并非「整文件一张网格就天然无缝」）。
    真实停顿（人类换气近百毫秒起）远大于阈值，误吸附风险可忽略。
    须在 ``fix_times_from_words`` 之后调用。
    """
    if next_start is None or not sentence.words:
        return
    tail = sentence.words[-1]
    gap = float(next_start) - float(tail.end_time)
    if 0.0 < gap <= ALIGN_SEAM_SNAP_MAX:
        tail.end_time = round(float(next_start), 3)
        sentence.end_time = tail.end_time


def snap_next_start_to_prev_end(nxt: Sentence, prev_end: Optional[float]) -> bool:
    """后句首不超前句尾（与前句尾吸附后句首**对称**的接缝收尾）。

    小重叠（0 < prev_end - nxt.start_time ≤ ALIGN_SEAM_SNAP_MAX，即后句首
    超前句尾不到一帧容差）时，把后句整体右移，使其首字 start = 前句尾——
    表现层修正「句首漂进前句范围」的残余重叠，不改变句内结构。

    - 后句锁定或无字级不改；
    - 与 ``snap_tail_to_next_start`` 互补：小间隙走前者（前句尾→后句首），
      小重叠走本函数（后句首→前句尾），两者互斥且幂等；
    - 返回是否发生吸附。
    """
    if prev_end is None or nxt.is_locked or not nxt.words:
        return False
    overlap = float(prev_end) - float(nxt.start_time)
    if not (0.0 < overlap <= ALIGN_SEAM_SNAP_MAX):
        return False
    shift = round(overlap, 3)
    nxt.start_time = round(float(prev_end), 3)
    for w in nxt.words:
        w.start_time = round(float(w.start_time) + shift, 3)
        w.end_time = round(float(w.end_time) + shift, 3)
    nxt.end_time = nxt.words[-1].end_time
    return True


def commit_aligned_words(
    sentence: Sentence,
    words: List[WordTimestamp],
    *,
    next_start: Optional[float] = None,
) -> bool:
    """验证最小成功条件后一次性提交单句对齐结果。

    空结果不触碰原 ``words/start/end/timed/dirty``，调用方可安全保留旧对齐
    供用户查看或重试；只有生成出可用字级后才清 dirty。
    """
    if not words:
        return False
    merged = merge_punct_into_words(sentence.text, words)
    sanitized = sanitize_word_timestamps(merged)
    if not sanitized:
        return False
    sentence.words = sanitized
    sentence.fix_times_from_words()
    snap_tail_to_next_start(sentence, next_start)
    sentence.timed = True
    sentence.is_dirty = False
    return True


def apply_seam_snaps(
    project: SubtitleProject,
    *,
    sentence_sids: Optional[set[int]] = None,
) -> int:
    """对项目内相邻句做接缝吸附（按当前 ``sentences`` 顺序）。

    - 跳过锁定句（不改其字级/句尾）；
    - 未锁定句可吸附到后句起点（后句是否锁定无关）；
    - ``sentence_sids`` 非空时只修改本轮成功提交的句子，避免失败句的旧
      时间戳被收尾步骤意外改写；None 表示处理全部可用句；
    - 返回实际发生吸附的句数。

    调用方应在字级已写好、``fix_times_from_words`` 已跑完、且句序已按
    时间排好（``project.sort()``）之后调用。
    """
    sents = project.sentences
    n = 0
    for i, s in enumerate(sents):
        nxt = sents[i + 1] if i + 1 < len(sents) else None
        if nxt is None:
            continue
        # ① 前句尾 → 后句首（小间隙吸附）
        if (not s.is_locked) and s.words and (
            sentence_sids is None or s.sid in sentence_sids
        ):
            before = float(s.words[-1].end_time)
            snap_tail_to_next_start(s, float(nxt.start_time))
            if float(s.words[-1].end_time) != before:
                n += 1
        # ② 后句首 → 前句尾（小重叠吸附，与前句锁定无关、只看后句是否可改）
        if (sentence_sids is None or nxt.sid in sentence_sids) and \
                snap_next_start_to_prev_end(nxt, float(s.end_time)):
            n += 1
    if n:
        logger.info("[Align] 句间接缝吸附：%d 处（阈值 ≤%.0fms）", n, ALIGN_SEAM_SNAP_MAX * 1000)
    return n


# ══════════════════════════════════════════════════════════════════
# Qwen 后端官方分词硬依赖的「开工前」预检
#
# 上游 processor 对 ja/ko 缺 nagisa/soynlp 时抛 ImportError：full 模式异常能弹框，
# 但 project/dirty/sentences 模式逐句 try/except 会把这种**系统性必败**吞成
# 「日志一行 + 静默无产出」（用户看到「完成」却没有任何字级）。统一在各入口
# 开工前 fail-fast，错误信息直接给出安装命令与替代后端。
# ══════════════════════════════════════════════════════════════════

_SEGMENTER_DEPS: dict[str, Tuple[str, str, str]] = {
    # 语言全名 → (包名, 安装命令, 中文名)
    "Japanese": ("nagisa", "pip install nagisa", "日语"),
    "Korean": ("soynlp", "pip install soynlp", "韩语"),
}


def check_segmenter_dependency(language_full: str) -> Optional[str]:
    """Qwen 后端分词依赖检查：缺失 → 返回可读提示（含安装命令）；就绪/不需要 → None。

    仅覆盖 Qwen 后端官方分词链路（ja→nagisa / ko→soynlp）；MMS-FA 后端不经过
    这两个包（ja 读音依赖 pykakasi，缺失时自行回退 uroman），不要在 MMS 路径误用。
    """
    dep = _SEGMENTER_DEPS.get((language_full or "").strip())
    if dep is None:
        return None
    import importlib.util
    if importlib.util.find_spec(dep[0]) is None:
        return (
            f"{language_full}（{dep[2]}）对齐需要分词包 {dep[0]}"
            f"（Qwen3-ForcedAligner 官方分词链路），当前环境未安装。\n"
            f"　安装命令：{dep[1]}\n"
            f"　或切换对齐后端为 MMS-FA（歌词长拖音后端不依赖该包）。"
        )
    return None


def preflight_segmenter_deps(
    languages_full: Iterable[str],
    *,
    backend: str = "qwen",
) -> None:
    """开工前统一预检：任一目标语言缺分词包 → 立即 RuntimeError（fail-fast，全模式响亮报错）。"""
    if (backend or "qwen").lower() == "mms":
        return
    msgs: List[str] = []
    seen: set[str] = set()
    for lang in languages_full:
        if not lang or lang in seen:
            continue
        seen.add(lang)
        msg = check_segmenter_dependency(lang)
        if msg:
            msgs.append(msg)
    if msgs:
        raise RuntimeError(
            "对齐所需的分词依赖缺失（未安装将无法产出字级时间戳）：\n" + "\n".join(msgs)
        )


def _infer_full_language(short: str, hint_from_sentence: str, hint_from_project: str) -> Optional[str]:
    if short and short.lower() != "auto":
        r = _resolve_language(short)
        if r:
            return r
    for hint in (hint_from_sentence, hint_from_project):
        if not hint:
            continue
        for val in set(LANG_SHORT_TO_FULL.values()):
            if val is None:
                continue
            if hint.lower() == val.lower() or hint.lower().startswith(val.lower()):
                return val
        if hint.lower() in LANG_SHORT_TO_FULL and LANG_SHORT_TO_FULL[hint.lower()] is not None:
            return LANG_SHORT_TO_FULL[hint.lower()]
    return None
