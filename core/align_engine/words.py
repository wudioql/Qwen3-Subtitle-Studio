"""core.align_engine.words — 语言决议 + 字级切回 + 事务提交（原语层）。

本模块位于 ``align_engine`` 分层栈的底层（``config``/``common`` 之上、``full`` /
``chunking`` 之下），被单段、切块与两阶段三条执行路径共用，**不**反向依赖它们。

- **语言决议**：一律经 ``_infer_full_language``（``cfg.source_language``(≠auto) >
  句级 > 项目）。分组键是 ``tokenizer_family``（default / ja / ko）**而非语言本身**。
- **字级切回**：对齐原语产出 → ``_attach_words_to_sentences`` 切回目标句。
- **事务提交**：先深拷贝候选句、验证非空，再原子写回；空产出 / 异常不污染原句、
  不清 dirty、不动上锁句。
"""
from __future__ import annotations

import copy
import logging
from typing import Callable, List, Optional, Sequence, Tuple

from subs.models import Sentence, SubtitleProject, WordTimestamp

from core.language_utils import tokenizer_family
from core.model_manager import ModelManager
from core.text_utils import attach_words_to_sentences as _attach_words_to_sentences
from core.text_utils import extract_pure_words

from .common import _infer_full_language, apply_seam_snaps, commit_aligned_words
from .config import AlignConfig, _report
import core.align_engine as _ae  # 符号经包查找，兼容 patch("core.align_engine.*")

logger = logging.getLogger("core.align_engine")


# ══════════════════════════════════════════════════════════════════
# 语言分段 / 句级语言
# ══════════════════════════════════════════════════════════════════

def _resolve_language_segments(
    project: SubtitleProject,
    cfg: AlignConfig,
) -> List[Tuple[str, List[Sentence]]]:
    """把有文本的句子按 **Qwen 分词器族**切成连续同族段。

    决议顺序：cfg.source_language(≠auto) > 句级语言 > 项目语言。

    分组键是 ``tokenizer_family``（default / ja / ko），**不是语言本身**：Qwen 官方
    分词只在 ja / ko 分支切到 nagisa / soynlp，其余语言（含 None）共用同一条默认
    「CJK 逐字 + 空格词」路径（实测逐字等价）。故同族连续句合并成**一次**调用不会
    改变任何分词结果——中/粤/英混排 = 1 段 = 零裁剪，行为与单语项目一致。

    元组第一元素是该段**首句**决议出的语言全名，仅用于选择分词器 / 预检 / 日志，
    **不再**用于逐句 ``word.language`` 标注（调用方按句各自回填）。
    空文本句不参与对齐（无段可归）；任一句语言无法决议 → ValueError。
    """
    segments: List[Tuple[str, List[Sentence]]] = []
    last_family: Optional[str] = None
    for i, s in enumerate(project.sentences):
        if not (s.text or "").strip():
            continue
        lang = _infer_full_language(cfg.source_language, s.language, project.source_language)
        if not lang:
            raise ValueError(
                f"无法推断第 {i+1} 句的语言（sentence.language={s.language!r}, "
                f"project.source_language={project.source_language!r}, "
                f"cfg.source_language={cfg.source_language!r}）。请显式设置句级或项目语言。"
            )
        family = tokenizer_family(lang)
        if segments and last_family == family:
            segments[-1][1].append(s)
        else:
            segments.append((lang, [s]))
            last_family = family
    return segments


def _full_language(cfg: AlignConfig, sentence: Sentence, project: SubtitleProject) -> Optional[str]:
    """逐句决议语言全名（与分段同一决议顺序）。"""
    return _infer_full_language(cfg.source_language, sentence.language, project.source_language)


def _language_setter(
    cfg: AlignConfig,
    project: SubtitleProject,
    fallback: str,
) -> Callable[[Sentence], str]:
    """返回「句 → 语言全名」函数（决议失败回落 fallback，保证标注非空）。"""
    def _get(sentence: Sentence) -> str:
        return _full_language(cfg, sentence, project) or fallback
    return _get


def _build_word_languages(
    sentences: Sequence[Sentence],
    project: SubtitleProject,
    cfg: AlignConfig,
) -> List[str]:
    """把「逐句语言」铺开成与 ``extract_pure_words(拼接文本)`` 等长的逐词语言表。

    仅供 MMS 后端使用：MMS 用逐词语言决定数字拼读表与日语 pykakasi 路由
    （``MMSAligner._romanize_word``）。拼接约定与 ``align`` 一致（空格分隔），
    故整段词数 = 各句词数之和。
    """
    langs: List[str] = []
    for s in sentences:
        n = len(extract_pure_words(s.text))
        langs.extend([_full_language(cfg, s, project) or ""] * n)
    return langs


# ══════════════════════════════════════════════════════════════════
# 字级切回与事务提交（单段 / 切块 / 两阶段共用）
# ══════════════════════════════════════════════════════════════════

def _qwen_words(
    audio_np, sr, text: str, lang: str, offset: float, model_manager: ModelManager,
) -> List[WordTimestamp]:
    """Qwen 对齐原语 → 全局时间的 WordTimestamp 列表。"""
    items = _ae.align_sentence_raw((audio_np, sr), text, lang, model_manager=model_manager)
    if not items:
        return []
    return [
        WordTimestamp(
            text=it["text"],
            start_time=round(it["start_time"] + offset, 3),
            end_time=round(it["end_time"] + offset, 3),
            language=lang,
        )
        for it in items
    ]


def _assign_words(
    target_sents: Sequence[Sentence],
    raw_words: List[WordTimestamp],
) -> List[Tuple[Sentence, Sentence]]:
    """把全量字级切回目标句，返回 [(原句, 候选句)]（候选句已挂 words，尚未提交）。

    候选句是深拷贝：空产出/异常不会污染原句。
    """
    if not raw_words:
        return []
    candidates = [copy.deepcopy(s) for s in target_sents]
    for candidate in candidates:
        candidate.words = []
    assigned = _attach_words_to_sentences(candidates, raw_words)
    return list(zip(target_sents, assigned))


def _commit_one(
    original: Sentence,
    candidate: Sentence,
    *,
    language_of: Callable[[Sentence], str],
    context: str,
) -> bool:
    """事务式提交单句候选（按句回填 word.language）。返回是否提交成功。"""
    if original.is_locked or not candidate.words:
        return False
    lang = language_of(original)
    for w in candidate.words:
        w.language = lang
    if not commit_aligned_words(candidate, list(candidate.words)):
        logger.warning("[Align] %s 的句 sid=%d 未得到字级，保留原状", context, original.sid)
        return False
    original.words = copy.deepcopy(candidate.words)
    original.start_time = candidate.start_time
    original.end_time = candidate.end_time
    original.timed = candidate.timed
    original.is_dirty = False
    return True


def _commit_span(
    target_sents: Sequence[Sentence],
    raw_words: List[WordTimestamp],
    *,
    language_of: Callable[[Sentence], str],
    context: str,
) -> set[int]:
    """切回 + 事务式提交一组句子，返回成功提交的 sid 集合。"""
    committed: set[int] = set()
    for original, candidate in _assign_words(target_sents, raw_words):
        if _commit_one(original, candidate, language_of=language_of, context=context):
            committed.add(original.sid)
    return committed


# ══════════════════════════════════════════════════════════════════
# 收尾 / 族判定
# ══════════════════════════════════════════════════════════════════

def _finish_full_text(
    project: SubtitleProject,
    cfg: AlignConfig,
    committed_sids: set[int],
    *,
    total_steps: int,
    log_label: str,
) -> SubtitleProject:
    """收尾：清空文本句脏标记 → 排序 → 接缝吸附 → 日志/进度。"""
    for sentence in project.sentences:
        if not sentence.is_locked and not (sentence.text or "").strip():
            sentence.is_dirty = False

    if not committed_sids:
        logger.warning("[Align] full-text 对齐没有提交任何新字级，项目保持原状")

    project.sort()
    apply_seam_snaps(project, sentence_sids=committed_sids)
    _report(cfg, total_steps, total_steps, "完成")
    logger.info(
        "[Align] %s 完成：%d 句成功提交",
        log_label, len(committed_sids),
    )
    return project


def _dominant_family(segments: List[Tuple[str, List[Sentence]]]) -> str:
    """多族冲突时的主导族：优先 default（覆盖语言最广），否则取首段的族。"""
    if any(tokenizer_family(lang) == "default" for lang, _ in segments):
        return "default"
    return tokenizer_family(segments[0][0])


def _conflict_segments(
    segments: List[Tuple[str, List[Sentence]]],
    dominant_family: str,
) -> List[Tuple[str, List[Sentence]]]:
    """取与主导族不同的段（即两阶段里需要精修的 ja / ko 段）。"""
    return [(lang, sents) for lang, sents in segments if tokenizer_family(lang) != dominant_family]
