"""core.cloud_asr.facts —— 模型账本：哪些免费、哪些收费、各自实测支持什么语种。

**唯一真源**：设置页（``ui/settings/cloud_asr_tab.py``）与命令行
（``tools/cloud_models_cli.py``）都从这里取。``core/cloud_models.py`` 负责抓定价页
并与账本对账，三方名单冲突时优先级为
``PAID_MODELS`` > ``VERIFIED_FREE_MODELS`` > 定价页标注。
"""


from __future__ import annotations


from dataclasses import dataclass


# ═══════════════════════════════════════════════════════════════
# 模型能力清单 —— 2026-10-04 用真实账单逐条实证，不是抄页面文案
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ModelFacts:
    """单个云端模型的实测能力。全部字段来自一次真实调用的真实响应。"""

    has_language: bool = False       # 响应是否带 language 字段
    has_punctuation: bool = False    # 输出文本是否带标点（决定能否被 _split_text_by_punct 切句）
    has_segments: bool = False       # 是否带 segments 句级时间戳
    #: **本项目可用**的语种短码。判据不是「模型能不能听懂」，而是
    #: 「转写文本是否与音频同语言」——强制对齐器要求两者一致，否则对不上时间轴。
    #: 例：GSR-V1.0 听得懂英文，却把英文**译为中文文本**，故 en 不在列。
    languages: tuple[str, ...] = ()
    note: str = ""


VERIFIED_ON = "2026-10-04"


#: 控制台模型页（已过滤 speech）——**人为查证价格的唯一去处**。
MODEL_PRICING_URL = "https://cloud.siliconflow.cn/me/models?types=speech"


#: 账单实证**零扣费**的模型（同日真机调用后在费用明细中查无记录）。
#: ``languages`` 是 2026-10-04 用中/日/粤/英四段素材逐条跑出来的，不是抄官方文案。
VERIFIED_FREE_MODELS: dict[str, ModelFacts] = {
    "FunAudioLLM/SenseVoiceSmall": ModelFacts(
        has_language=True,
        has_punctuation=True,
        languages=("zh", "en", "yue", "ja", "ko"),
        note=(
            "免费模型里唯一同时支持中/英/日/韩/粤的，且会返回 language"
            "（实测为英文全名：Chinese / Japanese / Cantonese / English）。"
            "粤语保留「嘅/系」等口语原字。代价：中文有错字、会漏逗号导致并句。"
        ),
    ),
    "XingChenAGI/XingChenGSR-V1.0": ModelFacts(
        has_punctuation=True,
        languages=("zh",),
        note=(
            "仅限中文。生成式语音识别（GSR）会把外语音频译成中文文本再输出——"
            "实测同一段英文音频，输出汉字 37 / 拉丁 8，是译文不是原文，"
            "因此英文/日语都不能选它。"
            "代价换来的是中文质量最好：与人工参考稿字面相似度 1.000（逐字一致）。"
        ),
    ),
    "XingChenAGI/XingChenASR-V3.2-Ultra": ModelFacts(
        has_punctuation=True,
        languages=("zh", "en"),
        note=(
            "中/英可用：英文输出原文（汉字 0 / 拉丁 107），不做 GSR 那种跨语言改写。"
            "中文有约 2 处同音错字（每段 20~30 字量级出现 2 次），"
            "与 V3.2 的错字位置完全相同，可见那是识别错误而非语义规整。"
            "粤语会被转成普通话，日语不可用。首次调用冷启动约 19s。"
        ),
    ),
    "XingChenAGI/XingChenASR-V3.2": ModelFacts(
        has_punctuation=True,
        languages=("zh", "en", "yue"),
        note=(
            "中/英/粤可用，英文与粤语均输出原文（粤语保留「嘅/系」口语字）；日语不可用。"
            "只输出句号，逗号/问号全丢 → 中文易并句。"
        ),
    ),
    "XingChenAGI/XingChenASR-Diarize-V3.0": ModelFacts(
        has_punctuation=True,
        has_segments=True,
        languages=("zh", "en", "yue"),
        note=(
            "面向会议场景的说话人分离模型：唯一返回 segments 句级时间戳"
            "（与人工轴误差 <0.1s）；中/英/粤可用，日语不可用。"
            "text 带 '1: ' 前缀须剥离。不需要分离说话人时选它只是多花时间，无额外收益。"
        ),
    ),
}


#: 账单实证**会扣费**的模型（同日调用后在费用明细中有真实扣费记录）。
#: 刻意不写单价：金额会随官方调价过期，而「实证日期 + 查价去处」不会失真。
PAID_MODELS: dict[str, str] = {
    "Qwen/Qwen3-ASR-1.7B": (
        f"{VERIFIED_ON} 账单实证会产生真实扣费。定价页曾标「免费」，以账本为准。"
        f"本项目不缓存金额，请到控制台模型页查看实时价格：{MODEL_PRICING_URL}"
    ),
}


#: 默认模型 = SenseVoiceSmall：免费模型里唯一覆盖中/英/日/粤的，且能返回 language，
#: 于是工具栏「自动检测」真正可用（XingChen 全系返回 None，必须显式指定语言）。
#: **官方口径**的语言支持（未经本项目实测）。仅用于 UI 展示并标注来源，不作可用性承诺。
#: Qwen3-ASR 官方标称 30 语 + 22 方言，恰好覆盖对齐器全部 11 种；但它收费，
#: 本项目没对它做过语言实测，所以只能标「官方口径」而非「实测」。
OFFICIAL_LANGUAGES: dict[str, tuple[str, ...]] = {
    "Qwen/Qwen3-ASR-1.7B": ("zh", "en", "yue", "ja", "ko", "fr", "de", "it", "pt", "ru", "es"),
}


DEFAULT_CLOUD_MODEL = "FunAudioLLM/SenseVoiceSmall"


def is_verified_free(model: str) -> bool:
    """模型是否在 :data:`VERIFIED_FREE_MODELS` 账本实证清单里。"""
    return model in VERIFIED_FREE_MODELS
