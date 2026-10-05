"""tests/test_cloud_asr.py — 云端 ASR 客户端与免费模型清单服务（纯逻辑，零网络）。

覆盖：
- 响应归一化：多 schema 容错、Diarize 说话人前缀剥离、``language`` 返回字符串
  ``"None"``（不是 JSON null）的实测陷阱；
- 错误分类：402 配额 / 401 鉴权 / 429 限流 / 5xx / 400 各自落到正确异常类型，
  **402 绝不重试**是硬要求（重试只会再吃一次同样的 402）；
- 清单合并：三方名单冲突时的判定优先级（账本 > 定价页），守住「页面标免费
  不等于真的免费」这条用真金白银换来的结论；
- 缓存策略：有缓存不联网 / 无缓存才抓 / 可强制刷新；
- 反回归：云端转写路径**必须**完全不触碰 ``model_manager``——一旦有人把它拉回来，
  省显存的目标就当场作废了，这条用源码断言钉死。

归属边界：本文件只收**云端职责**的用例（响应归一化 / 错误分类 / 模型语种与清单 /
中英混说补丁 / Diarize segments / 编码与进度探测）。纯切句器行为（域名点、缩写、
数值点、词边界硬切、CJK 逐字基线）的被测函数都在 ``core.asr_engine``，本地后端
走的是同一条路径，故按被测模块归属放在 ``tests/test_punctuation.py``——即便它们
当初是由云端实测暴露出来的。

全程不发真实网络请求，也不会因此花掉任何一分钱。
"""

from __future__ import annotations

import inspect
import pathlib
import re
import subprocess
import sys

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

pytestmark = pytest.mark.logic

from core import cloud_models
from core.app_config import CloudASRPreferences, Preferences, _dict_to_obj
from core.asr_engine import (
    TranscribeConfig,
    _cloud_transcribe_text,
    _text_only_to_sentences,
)
from core.cloud_asr import (
    DEFAULT_CLOUD_MODEL,
    OFFICIAL_LANGUAGES,
    VERIFIED_FREE_MODELS,
    CloudASRAuthError,
    CloudASRConfig,
    CloudASRLanguageError,
    CloudASRNetworkError,
    CloudASRParamError,
    CloudASRQuotaError,
    CloudASRRateLimitError,
    CloudASRResult,
    LANG_SHORT_TO_FULL,
    UploadPayload,
    _classify_error,
    _finalize_language,
    build_multipart,
    infer_language_from_text,
    languages_for_model,
    normalize_language,
    parse_response,
    probe_models,
    split_zh_en_sentence_language,
    split_mixed_script_tail,
    strip_speaker_prefixes,
    supports_zh_en_sentence_split,
)
from core.cloud_asr import CloudSegment

# ── 假定价页行：刻意混入「付费 TTS」与「非语音类的免费 LLM」，验证过滤 ─────
_ROWS = [
    {"id": "XingChenAGI/XingChenGSR-V1.0", "category": "audio",
     "category_label": "语音", "is_free": True, "prices": []},
    {"id": "Qwen/Qwen3-ASR-1.7B", "category": "audio",
     "category_label": "语音", "is_free": True, "prices": []},
    {"id": "brand/NewFreeASR", "category": "audio",
     "category_label": "语音", "is_free": True, "prices": []},
    {"id": "fnlp/MOSS-TTSD-v0.5", "category": "audio",
     "category_label": "语音", "is_free": False, "prices": ["0.05"]},
    {"id": "vendor/CheapTextLLM", "category": "text",
     "category_label": "对话", "is_free": True, "prices": []},
]


# ══════════════════════════════════════════════════════════
# core.cloud_asr：响应归一化
# ══════════════════════════════════════════════════════════

def test_strip_speaker_prefixes():
    # Diarize 的 "1: " 前缀会被 _split_text_by_punct 切成 2 字垃圾句，必须剥离
    assert strip_speaker_prefixes("1: 你好，世界。") == "你好，世界。"
    assert strip_speaker_prefixes("12：第二段。") == "第二段。"
    assert strip_speaker_prefixes("1: 甲。\n2: 乙。") == "甲。\n乙。"
    assert strip_speaker_prefixes("没有前缀。") == "没有前缀。"
    assert strip_speaker_prefixes("") == ""


def test_normalize_language_handles_string_none():
    """实测陷阱：判定不出语言时返回**字符串** "None"，不是 JSON null。"""
    assert normalize_language("Chinese") == "Chinese"
    assert normalize_language("None") == ""
    assert normalize_language("null") == ""
    assert normalize_language("unknown") == ""
    assert normalize_language("") == ""
    assert normalize_language(None) == ""
    assert normalize_language("zh") == "Chinese"          # 某些模型可能返回短码
    assert normalize_language("Klingon") == "Klingon"     # 未知语言原样透传


def test_parse_response_multiple_schemas():
    # Diarize：带 segments + 说话人前缀，但**没有** language
    res = parse_response(
        {"text": "1: 你好，世界。", "duration": 3.4, "usage": {"seconds": 3.0},
         "segments": [{"start": 0.0, "end": 1.2, "text": "你好，世界。", "speaker": "1"}]},
        model="XingChenAGI/XingChenASR-Diarize-V3.0", trace_id="t1",
    )
    assert res.text == "你好，世界。"
    assert "1:" not in res.text
    assert res.language == ""
    assert res.usage_seconds == 3.0
    assert res.duration_sec == 3.4
    assert res.segments[0].speaker == "1"

    # SenseVoice：唯一返回 language 的那一类
    res2 = parse_response({"text": "你好", "language": "Chinese"},
                          model="FunAudioLLM/SenseVoiceSmall", trace_id="t2")
    assert res2.language == "Chinese"

    # 缺失字段 / 脏字段不应让整条链路崩溃，只会退化成默认值
    res3 = parse_response({"text": "x"}, model="whatever", trace_id="t3")
    assert res3.usage_seconds == 0.0 and res3.segments == []


def test_build_multipart_shape():
    body, ctype = build_multipart(
        {"model": "m/v"},
        UploadPayload(b"PCM", "audio.ogg", "audio/ogg", "opus"),
    )
    assert "multipart/form-data; boundary=" in ctype
    assert b'name="model"' in body and b"m/v" in body
    assert b'name="file"' in body and b'filename="audio.ogg"' in body
    assert body.endswith(b"--\r\n")


# ══════════════════════════════════════════════════════════
# core.cloud_asr：错误分类
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "status,expected",
    [
        (402, CloudASRQuotaError),
        (401, CloudASRAuthError),
        (403, CloudASRAuthError),
        (429, CloudASRRateLimitError),
        (500, CloudASRNetworkError),
        (400, CloudASRParamError),
        (404, CloudASRParamError),
    ],
)
def test_classify_error_maps_status(status, expected):
    err = _classify_error(status, '{"error": {"message": "boom"}}', "m")
    assert isinstance(err, expected)
    assert "boom" in str(err)


def test_classify_error_ok_on_200():
    assert _classify_error(200, "{}", "m") is None


def test_quota_error_is_actionable_in_chinese():
    """402 必须给出「充值 / 切回本地」两条出路——没有可编程的余额查询接口可依赖。"""
    err = _classify_error(402, "", "m")
    assert "402" in str(err)
    assert "充值" in str(err) and "本地" in str(err)


def test_resolved_api_key_prefers_config_then_env(monkeypatch):
    monkeypatch.delenv("QSS_SILICONFLOW_API_KEY", raising=False)
    with pytest.raises(CloudASRAuthError):
        CloudASRConfig().resolved_api_key()

    monkeypatch.setenv("QSS_SILICONFLOW_API_KEY", "sk-env")
    assert CloudASRConfig().resolved_api_key() == "sk-env"
    # 显式配置优先于环境变量
    assert CloudASRConfig(api_key="sk-cfg").resolved_api_key() == "sk-cfg"


def test_probe_models_without_key_does_no_request(monkeypatch):
    monkeypatch.delenv("QSS_SILICONFLOW_API_KEY", raising=False)
    res = probe_models()
    assert res.ok is False and "未配置" in res.message
    assert res.model_count == 0


# ══════════════════════════════════════════════════════════
# core.cloud_models：三方名单合并
# ══════════════════════════════════════════════════════════

def test_build_model_infos_merges_bill_evidence_over_pricing_page():
    models = cloud_models.build_model_infos(_ROWS)
    by_id = {m.id: m for m in models}

    # 付费的 TTS 与非语音类都不该出现
    assert "fnlp/MOSS-TTSD-v0.5" not in by_id
    assert "vendor/CheapTextLLM" not in by_id
    assert len(models) == 3

    # 定 price 页标免费，但账本说它收费 → 以账本为准
    qwen = by_id["Qwen/Qwen3-ASR-1.7B"]
    assert qwen.state == cloud_models.STATE_KNOWN_PAID
    assert qwen.state_label == "已知收费"
    assert "¥" not in qwen.note        # 金额会过期，UI 里不该写死
    assert "cloud.siliconflow.cn" in qwen.note   # 但必须指路到官方查价

    # 账单实证零扣费 → 已实证免费，且带实测能力摘要
    ok = by_id["XingChenAGI/XingChenGSR-V1.0"]
    assert ok.state == cloud_models.STATE_VERIFIED_FREE
    assert "标点" in ok.capability

    # 页面标免费但从未实证 → 明确标注「未实测」并提示自行查证
    new = by_id["brand/NewFreeASR"]
    assert new.state == cloud_models.STATE_UNVERIFIED
    assert new.page_free is True
    assert "cloud.siliconflow.cn" in new.note


def test_cache_roundtrip(tmp_path):
    cache = tmp_path / "sf.json"
    cloud_models.save_cache(_ROWS, cache)
    data = cloud_models.load_cache(cache)
    assert data is not None
    assert [r["id"] for r in data["rows"]] == [r["id"] for r in _ROWS]
    assert data["source"] == cloud_models.PRICING_URL


def test_load_cache_returns_none_when_missing(tmp_path):
    assert cloud_models.load_cache(tmp_path / "nope.json") is None


def test_uses_cache_without_network(tmp_path, monkeypatch):
    """有缓存就必须走缓存——这是 UI 打开设置页不卡顿的前提。"""
    cache = tmp_path / "sf.json"
    cloud_models.save_cache(_ROWS, cache)
    monkeypatch.setattr(cloud_models, "CACHE_PATH", cache)

    def boom(*_a, **_k):
        raise AssertionError("有缓存时不该联网")

    monkeypatch.setattr(cloud_models, "fetch_pricing", boom)
    snap = cloud_models.list_asr_models()
    assert snap.source == "cache"
    assert len(snap.models) == 3
    assert snap.error == ""


def test_refreshes_automatically_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_models, "CACHE_PATH", tmp_path / "sf.json")
    monkeypatch.setattr(cloud_models, "fetch_pricing", lambda *a, **k: "<html/>")
    monkeypatch.setattr(cloud_models, "parse_pricing", lambda html: _ROWS)
    snap = cloud_models.list_asr_models()
    assert snap.source == "live"
    assert len(snap.models) == 3
    # 抓完要落盘，下次打开才有缓存可用
    assert cloud_models.load_cache(tmp_path / "sf.json") is not None


def test_force_refresh_ignores_cache(tmp_path, monkeypatch):
    cache = tmp_path / "sf.json"
    cloud_models.save_cache([_ROWS[0]], cache)
    monkeypatch.setattr(cloud_models, "CACHE_PATH", cache)
    monkeypatch.setattr(cloud_models, "fetch_pricing", lambda *a, **k: "<html/>")
    monkeypatch.setattr(cloud_models, "parse_pricing", lambda html: _ROWS)
    snap = cloud_models.list_asr_models(force_refresh=True)
    assert snap.source == "live"
    assert len(snap.models) == 3      # 拿的是新抓的全量，不是缓存里那 1 条


def test_network_failure_degrades_to_empty_with_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_models, "CACHE_PATH", tmp_path / "sf.json")
    monkeypatch.setattr(cloud_models, "fetch_pricing",
                        lambda *a, **k: (_ for _ in ()).throw(cloud_models.SFCatalogError("断网")))
    snap = cloud_models.list_asr_models()
    assert snap.models == [] and "断网" in snap.error


# ══════════════════════════════════════════════════════════
# 偏好与引擎接线
# ══════════════════════════════════════════════════════════

def test_cloud_prefs_defaults_and_kwargs():
    cp = CloudASRPreferences()
    assert cp.model == DEFAULT_CLOUD_MODEL
    assert cp.codec == "opus"
    assert cp.accumulated_seconds == 0.0
    kwargs = cp.to_transcribe_config_kwargs()
    assert kwargs["model"] == DEFAULT_CLOUD_MODEL
    # kwargs 必须能直接喂给 CloudASRConfig（键名对得上才好维护）
    CloudASRConfig(**kwargs)


def test_old_preferences_without_cloud_section_still_loads():
    """旧的 preferences.json 没有 cloud_asr 字段：应回退默认值而不是炸掉。"""
    prefs = _dict_to_obj({"asr": {"source_language": "zh"}}, Preferences, _root=True)
    assert prefs.cloud_asr.model == DEFAULT_CLOUD_MODEL
    assert prefs.asr.asr_backend == "local"
    assert prefs.asr.source_language == "zh"


def test_transcribe_config_defaults_to_local():
    cfg = TranscribeConfig()
    assert cfg.asr_backend == "local"
    assert cfg.cloud_asr is None


def test_cloud_path_must_not_touch_model_manager():
    """反回归：云端存在的唯一理由就是省显存。

    只要有人在这里面 reintroduce 了 ``using_asr`` 或 ``model_manager``，
    本地 1.7B 就会被拉进显存，整个功能的意义当场归零——所以用源码断言钉住。
    """
    src = inspect.getsource(_cloud_transcribe_text)
    assert "using_asr" not in src
    assert "model_manager" not in src


# ═══════════════════════════════════════════════════════════════
# 语言支持（2026-10-04 用中/日/粤/英四段素材实测得出，不是抄官方文案）
# ═══════════════════════════════════════════════════════════════

_ZH_TEXT = "青瓷色的风掠过指尖，金线牡丹在呼吸间流转。"
_EN_TEXT = "Politics and the English language from Wikipedia"
_JA_TEXT = "いくつかの懸案があることはご承知の通りです"
_KO_TEXT = "안녕하세요 여러분"
_RU_TEXT = "Привет мир"


def test_declared_languages_stay_inside_aligner_domain():
    """所有声明的语种都必须落在对齐器的 11 种内。

    声明一个对齐器兑现不了的语种，等于给用户开一张空头支票。
    """
    from core.language_utils import LANG_SHORT_TO_FULL

    for mid, facts in VERIFIED_FREE_MODELS.items():
        assert facts.languages, f"{mid} 未声明可用语种"
        for code in facts.languages:
            assert code in LANG_SHORT_TO_FULL, f"{mid} 声明了 {code}，对齐器不支持"
    for mid, codes in OFFICIAL_LANGUAGES.items():
        for code in codes:
            assert code in LANG_SHORT_TO_FULL, f"{mid} 官方语种 {code} 越界"


def test_only_sensevoice_covers_japanese_among_free_models():
    """日语这条实证结论必须钉住——它决定了默认模型选谁。

    实测：XingChen 全系对日语素材输出的是「这我不知道你在说什么」「我Um.」这类垃圾，
    只有 SenseVoiceSmall 能正确转写。若将来某天重测发现变了，改数据同时也要改默认模型。
    """
    ja_capable = [mid for mid, f in VERIFIED_FREE_MODELS.items() if "ja" in f.languages]
    assert ja_capable == ["FunAudioLLM/SenseVoiceSmall"]

    for mid, f in VERIFIED_FREE_MODELS.items():
        if mid.startswith("XingChenAGI/"):
            assert "ja" not in f.languages, f"{mid} 不应宣称支持日语"


def test_gsr_v1_is_chinese_only_because_it_translates():
    """GSR-V1.0 听得懂英文却把英文译成中文，所以英文不算它「可用」。

    这是本轮实测最反直觉的一条：官方口径明明写了「中+英」，但转写结果是中文，
    强制对齐器会因为「音频说英语、文本是中文」而对不上轴。

    混合素材复验：中文→英文→中文 26.7s 输入下，GSR 输出汉字 83 / 拉丁 8，
    英文段被整段译成「来自维基百科的英语政治与英语内容…」，而 Ultra / V3.2 /
    SenseVoice 都完整保留了英文原文。
    """
    gsr = VERIFIED_FREE_MODELS["XingChenAGI/XingChenGSR-V1.0"]
    assert gsr.languages == ("zh",)
    assert not gsr.has_language


def test_gsr_and_ultra_must_not_be_grouped_together():
    """反回归：Ultra **不是** GSR 那类生成式模型，不能把它的语种集也砍成中文。

    官方文案很容易让人误判——GSR 说「融合大语言模型做语义优化」，Ultra 说
    「高可读语义转写 / 自动精简口语冗余」，读起来像同一类。但实测判据是
    「输出是否与音频同语言」：

    - GSR  英文音频 → 中文译文（汉字 37 / 拉丁 8）→ 跨语言改写，仅中文
    - Ultra 英文音频 → 英文原文（汉字 0 / 拉丁 107）→ 不改写，中英可用
    - Ultra 的中文错字（金线→惊现、未干→味甘）与 V3.2 **完全相同**，
      说明那是识别错误而非「语义规整」；若真在做语义规整，不可能和不规整的
      V3.2 错得一模一样。

    误把 Ultra 砍成仅中文，等于白扔一个免费的中英可用模型。
    """
    ultra = VERIFIED_FREE_MODELS["XingChenAGI/XingChenASR-V3.2-Ultra"]
    assert "en" in ultra.languages, "Ultra 实测输出英文原文，不该被降级为仅中文"
    assert "ja" not in ultra.languages, "Ultra 实测日语输出垃圾，不该标为可用"
    # GSR 与 Ultra 的语种集必须不同：前者仅中文，后者含英文
    gsr = VERIFIED_FREE_MODELS["XingChenAGI/XingChenGSR-V1.0"]
    assert gsr.languages != ultra.languages


def test_default_model_covers_the_core_language_need():
    """默认模型必须覆盖 中/英/日/粤——否则用户不选就会踩坑。"""
    langs = languages_for_model(DEFAULT_CLOUD_MODEL)
    for need in ("zh", "en", "ja", "yue"):
        assert need in langs, f"默认模型 {DEFAULT_CLOUD_MODEL} 缺 {need}"
    assert VERIFIED_FREE_MODELS[DEFAULT_CLOUD_MODEL].has_language


def test_infer_language_by_decisive_scripts():
    """假名/谚文/西里尔是独占脚本，出现即可定语言。"""
    pool = ("zh", "en", "yue", "ja", "ko", "ru")
    assert infer_language_from_text(_JA_TEXT, pool) == "ja"
    assert infer_language_from_text(_KO_TEXT, pool) == "ko"
    assert infer_language_from_text(_RU_TEXT, pool) == "ru"


def test_infer_language_han_versus_latin():
    """汉字与拉丁字母 100% 可分——这正是 XingChen 系 auto 能跑通的原因。"""
    assert infer_language_from_text(_ZH_TEXT, ("zh", "en")) == "zh"
    assert infer_language_from_text(_EN_TEXT, ("zh", "en")) == "en"


def test_infer_language_mixed_counts_as_chinese():
    """用户规则：中英混杂一律当中文，不做「谁多谁少」的加权。

    混排文本交给中文对齐器即可——对齐器要的是「这条时间轴属于哪种语言」，
    而不是逐字溯源。若改成按数量判定，同样的文本会因比例波动在两种语言间摇摆。
    """
    mixed = "这个 feature 的实现用到了 MCP protocol"
    assert infer_language_from_text(mixed, ("zh", "en")) == "zh"
    # 汉字占绝对多数
    assert infer_language_from_text("今天的会议 discuss 了 API 设计", ("zh", "en")) == "zh"
    # 粤语候选集同样按中文处理（用户拍板，不额外判断粤语）
    assert infer_language_from_text(_ZH_TEXT, ("zh", "yue")) == "zh"


def test_infer_language_refuses_when_script_is_ambiguous():
    """拉丁字母但候选集里没有 en 时必须放弃——猜错语言会让整条时间轴歪掉。"""
    # 法/德/意/葡/西同为拉丁字母，无法区分
    assert infer_language_from_text("Bonjour le monde", ("fr", "de", "it", "pt", "es")) is None
    assert infer_language_from_text("Bonjour", ("fr", "de")) is None
    # 但候选集只剩一个时，是唯一解而非猜测
    assert infer_language_from_text("Bonjour", ("fr",)) == "fr"
    # 候选集有 en 时拉丁可判英文，无需因为「多候选」而放弃
    assert infer_language_from_text("Politics and the English language",
                                    ("zh", "en", "yue")) == "en"
    # 独占脚本在宽候选集下依然能定语言（前提是该语言确实在候选集内）
    assert infer_language_from_text(_JA_TEXT, ("zh", "en", "yue", "ja")) == "ja"
    # 假名出现但候选集里没有 ja → 不猜（模型压根不支持日语，判出来也对不上）
    assert infer_language_from_text(_JA_TEXT, ("zh", "en", "yue")) is None


def test_infer_language_edge_cases_return_none():
    assert infer_language_from_text("", ("zh", "en")) is None
    assert infer_language_from_text("，，。！？123", ("zh", "en")) is None
    assert infer_language_from_text(_ZH_TEXT, ()) is None


def test_languages_for_model_source_precedence():
    """实测优先于官方口径；都没有时退到对齐器 11 种（配合多候选不猜，效果是宁可报错）。"""
    assert languages_for_model("FunAudioLLM/SenseVoiceSmall")[:1] == ("zh",)
    assert languages_for_model("Qwen/Qwen3-ASR-1.7B") == OFFICIAL_LANGUAGES["Qwen/Qwen3-ASR-1.7B"]


# ═══════════════════════════════════════════════════════════════
# 中英混说逐句补丁（用户拍板：单段单语言是模型硬限制，只做定点缓解）
# ═══════════════════════════════════════════════════════════════

_SENSEVOICE = "FunAudioLLM/SenseVoiceSmall"
_QWEN3 = "Qwen/Qwen3-ASR-1.7B"
#: XingChen 系代表（真机 GUI 测试里就是它输出「句点后无空格」「只有逗号」的形态）
_XC_MODEL = "XingChenAGI/XingChenASR-V3.2"


def test_patch_scope_follows_verified_english_passthrough():
    """补丁范围 = **实测中英混说输出英文原文**的模型全集。

    白名单不是随手写的：判据是2026-10-04 实测账本里这些模型混说时汉字 0 / 拉丁 N，
    即英文段确为原文输出。XingChen 系虽然**不返回 language**（靠三层兜底补），
    但那恰恰是本补丁存在的理由——不返回语言才需要逐句判定。

    GSR 是唯一必须排除的：它把外语音频**译成中文**再输出（实测英文素材给出
    「来自维基百科的英语政治与英语内容…」，汉字 37 / 拉丁 8），输出里根本不存在
    英文文本，给它判英文等于对着中文译文跑英文对齐器，错误更严重。
    """
    for patched in (
        _SENSEVOICE,
        _QWEN3,
        "XingChenAGI/XingChenASR-V3.2-Ultra",
        "XingChenAGI/XingChenASR-V3.2",
        "XingChenAGI/XingChenASR-Diarize-V3.0",
    ):
        assert supports_zh_en_sentence_split(patched), patched
    for not_patched in (
        "XingChenAGI/XingChenGSR-V1.0",
        "brand/Unknown",
        "",
    ):
        assert not supports_zh_en_sentence_split(not_patched)


def test_gsr_excluded_because_it_translates_english_to_chinese():
    """反回归护栏：GSR 的实测可用语种只有 zh，且混说输出会被判为「英文丢失」。

    GSR 的``languages`` 只有 ``zh``——这是它「只支持中文」的账本依据。
    若哪天有人误把它加进白名单，本测试会失败。
    """
    assert "en" not in languages_for_model("XingChenAGI/XingChenGSR-V1.0")
    mixed = "青紫色的风掠过指尖。Politics and the English language from Wikipedia, 是一段英文。"
    sents = _text_only_to_sentences(
        mixed, total_sec=26.0, cfg=TranscribeConfig(source_language="auto"),
        project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(
            s, "Chinese", "XingChenAGI/XingChenGSR-V1.0"),
    )
    assert not any(s.language == "English" for s in sents), [
        s.language for s in sents]


def test_patch_splits_english_out_of_chinese_segment():
    """整段中文 → 纯英文句切成English，含汉字句沿用Chinese。"""
    assert (split_zh_en_sentence_language(
        "Politics and the English language from Wikipedia.", "Chinese", _QWEN3
    ) == "English")
    # 中英混排（含汉字）→ 沿用整段，不切
    assert (split_zh_en_sentence_language(
        "今天的会议 discuss 了 API 设计", "Chinese", _QWEN3
    ) is None)
    assert (split_zh_en_sentence_language(_ZH_TEXT, "Chinese", _SENSEVOICE) is None)


def test_patch_never_relabels_cantonese_as_chinese():
    """**粤语保护是这条补丁的核心约束**。

    粤语对齐器是独立模型（``Cantonese``），把粤语句标成中文普通话等于整段时间轴作废。
    因此含汉字的粤语句必须原样沿用 ``Cantonese``，绝不能返回 English 也不能被改写。
    """
    yue_text = "我今日唔想講嘢，你唔好問我啦。"
    # 整段粤语 + 纯英文句 → 只有英文句被切走
    assert (split_zh_en_sentence_language(
        "This is an English sentence.", "Cantonese", _SENSEVOICE
    ) == "English")
    # 整段粤语 + 粤语句 → None（沿用 Cantonese，调用方回落整段语种）
    assert split_zh_en_sentence_language(yue_text, "Cantonese", _SENSEVOICE) is None
    # 粤语口语字「嘅/係/唔」含汉字，仍必须沿用粤语
    assert split_zh_en_sentence_language(
        "呢个嘢我哋都搞掂咗喇。", "Cantonese", _QWEN3) is None


def test_patch_ignores_non_chinese_segments():
    """整段非中/粤时不启用补丁——已是英文/日语的整段无需再切。"""
    assert (split_zh_en_sentence_language(
        "Politics and the English language.", "English", _QWEN3) is None)
    assert (split_zh_en_sentence_language(
        _JA_TEXT, "Japanese", _SENSEVOICE) is None)


def test_patch_refuses_fragments_and_other_scripts():
    """纯标点/数字碎片判不出语言；假名等独占脚本也不该被当成英文。"""
    assert split_zh_en_sentence_language("，", "Chinese", _QWEN3) is None
    assert split_zh_en_sentence_language("1946,", "Chinese", _QWEN3) is None
    assert split_zh_en_sentence_language("12345", "Chinese", _QWEN3) is None
    assert split_zh_en_sentence_language("", "Chinese", _QWEN3) is None
    assert split_zh_en_sentence_language("   ", "Cantonese", _QWEN3) is None
    # 日语混在中文整段里 → 不动（用户拍板：其它语言不判定）
    assert split_zh_en_sentence_language(_JA_TEXT, "Chinese", _SENSEVOICE) is None


def test_mixed_audio_sentences_get_english_not_shards():
    """回归：26.66s 中英混合素材的真实输出（2026-10-04 本地 Qwen3 实测）。

    修复前英文被中文字幕上限（24 字符）切碎成 9 片，且从 ``Englis|h`` 处劈开单词。
    修复后英文句整体保留并判为 English，中文句不受影响。
    """
    text = (
        "青紫色的风掠过指尖，金线牡丹在呼吸间流转。"
        "Politics and the English language from Wikipedia, the free encyclopedia."
    )
    cfg = TranscribeConfig()
    sents = _text_only_to_sentences(
        text, total_sec=26.66, cfg=cfg, project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", _QWEN3),
    )
    langs = [s.language for s in sents]
    assert "English" in langs, f"英文句应被判为 English，实际 {langs}"
    # 英文**整段不被腰斩**：修复前会被 24 字符阈值从 ``Englis|h`` 处劈开，
    # 碎片数 4 段以上；现在按标点边界切，最长的英文句含完整单词
    english = [s.text for s in sents if s.language == "English"]
    joined = "".join(english)
    assert "Politics and the English language from Wikipedia" in joined
    # 不该出现从单词中间劈开的碎片（碎片会缺少词首大写或以半个词结尾）
    assert all(not t.strip().startswith("h ") for t in english), english
    # 中文仍在，且没有被误判
    assert any(s.language == "Chinese" and "青紫色" in s.text for s in sents)


def test_xingchen_english_capable_models_also_split_english():
    """Ultra / V3.2 / Diarize 三个模型同样能把英文整句切出，且不被腰斩。

    它们 2026-10-04 实测混说时输出英文原文（汉字 0 / 拉丁 N），只是**不返回**
    ``language``——不返回语言正是本补丁存在的理由，不是阻碍。三者共用同一条
    代码路径，故用参数化一次覆盖。
    """
    text = (
        "青紫色的风掠过指尖，金线牡丹在呼吸间流转。"
        "青瓷色的风掠过指尖惊现牡丹在呼吸间流转。"
        "Politics and the English language from Wikipedia, 是一段英文。"
    )
    for model in (
        "XingChenAGI/XingChenASR-V3.2-Ultra",
        "XingChenAGI/XingChenASR-V3.2",
        "XingChenAGI/XingChenASR-Diarize-V3.0",
    ):
        sents = _text_only_to_sentences(
            text, total_sec=26.66, cfg=TranscribeConfig(),
            project_language="Chinese",
            sentence_language_fn=lambda s, m=model: split_zh_en_sentence_language(s, "Chinese", m),
        )
        langs = [s.language for s in sents]
        assert "English" in langs, f"{model}: 英文句应切出，实际 {langs}"
        english = [s.text for s in sents if s.language == "English"]
        joined = "".join(english)
        assert "Politics and the English language from Wikipedia" in joined, model
        # 修复前英文被 24 字符中文字幕上限切碎、且从 Englis|h 处腰斩
        assert all(not t.strip().startswith("h ") for t in english), (model, english)
        # 中文仍在，且未被误判：用「含汉字的句必须全判Chinese」表达，不写死句数
        # （句数随标点切分变化，写死会脆；判据是语种归属，不是切分粒度）
        han_sents = [s for s in sents if any("一" <= ch <= "鿿" for ch in s.text)]
        assert han_sents and all(s.language == "Chinese" for s in han_sents), (
            model, [(s.language, s.text) for s in han_sents])


def test_per_lang_limits_now_apply_per_sentence():
    """**顺序修复的核心断言**：per_lang 必须能按**句**语种命中。

    修复前 per_lang 只按整段语种查表，混合素材整段恒为 Chinese → 永远取不到
    ``en`` 条目，配置形同虚设（实测配了 max_chars=90 仍被切碎成 15 句）。
    """
    from core.app_config import SegmentationPrefs

    text = "青紫色的风掠过指尖。Politics and the English language from Wikipedia, free."
    cfg = TranscribeConfig(seg_prefs=SegmentationPrefs(
        enabled=True, per_lang={"en": {"max_chars": 200}}))
    sents = _text_only_to_sentences(
        text, total_sec=26.0, cfg=cfg, project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", _QWEN3),
    )
    english = [s.text for s in sents if s.language == "English"]
    joined = "".join(english)
    # 逗号是句界标点，切成 2 句是正确的；关键是没有单词被腰斩
    assert "Wikipedia" in joined and "free" in joined
    assert not any(t.strip().startswith("h ") for t in english), english
    fallback = languages_for_model("some/brand-new-model")
    assert set(fallback) == {c for c in LANG_SHORT_TO_FULL if c != "auto"}


def test_english_uses_its_own_char_limit_by_default():
    """回归：英文默认阈值必须独立于中文字幕阈值。

    ``max_sentence_chars=24`` 是按**汉字**定的。修复语种判定后若仍沿用它，
    英文照样被从单词中间劈开（``Englis|h``）——实测 26.66s 混合素材英文碎成 7 片。
    正确行为：英文走语言默认上限（84 字符 ≈ 12~15 词），中文仍走 24。
    """
    text = (
        "青紫色的风掠过指尖，金线牡丹在呼吸间流转。"
        "Politics and the English language from Wikipedia, the free encyclopedia at "
        "en.wikipedia.org."
    )
    sents = _text_only_to_sentences(
        text, total_sec=26.66, cfg=TranscribeConfig(), project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", _QWEN3),
    )
    english = [s.text for s in sents if s.language == "English"]
    assert len(english) == 2, f"英文应按标点切成 2 句，实际 {len(english)}: {english}"
    # 域名完整、无单词腰斩
    assert any("en.wikipedia.org" in t for t in english), english
    assert not any(t.strip().startswith("h ") or t.strip().startswith("t en.") for t in english)
    # 中文那句仍是 2 句（未被英文阈值放大）
    chinese = [s.text for s in sents if s.language == "Chinese"]
    assert len(chinese) == 2, chinese


def _mk_result(text: str = "", language: str = "") -> CloudASRResult:
    return CloudASRResult(text=text, raw_text=text, language=language)


def test_finalize_language_layer1_model_returned_wins():
    """第 1 层：模型自己返回的语言优先级最高（它就是刚听过的那段音频）。"""
    cfg = CloudASRConfig(model="FunAudioLLM/SenseVoiceSmall", source_language="auto")
    res = _finalize_language(_mk_result(_JA_TEXT, "Japanese"), cfg)
    assert res.language == "Japanese"


def test_finalize_language_layer2_explicit_user_choice():
    """第 2 层：用户显式指定。即便文本是中文，指定 en 就该按 en 走。"""
    cfg = CloudASRConfig(model="XingChenAGI/XingChenGSR-V1.0", source_language="en")
    res = _finalize_language(_mk_result(_ZH_TEXT), cfg)
    assert res.language == "English"


def test_finalize_language_layer3_infers_from_transcript():
    """第 3 层：模型不返回语言且用户选 auto 时，从转写文本反推。

    这是让 XingChen 系的 auto 从「必定报错」变成「能跑通」的关键一层。
    """
    cfg = CloudASRConfig(model="XingChenAGI/XingChenGSR-V1.0", source_language="auto")
    res = _finalize_language(_mk_result(_ZH_TEXT), cfg)
    assert res.language == "Chinese"


def test_finalize_language_raises_when_unresolvable():
    """三层都失败时才报错，且要说出该模型到底支持什么——否则用户无从下手。"""
    cfg = CloudASRConfig(
        model="XingChenAGI/XingChenGSR-V1.0",
        source_language="auto", require_language=True,
    )
    with pytest.raises(CloudASRLanguageError) as ei:
        _finalize_language(_mk_result(_EN_TEXT), cfg)
    assert "GSR-V1.0" in str(ei.value)
    assert "zh" in str(ei.value)


def test_finalize_language_silent_when_not_required():
    """require_language=False 用于探测场景：拿不到语言也不该中断。"""
    cfg = CloudASRConfig(
        model="XingChenAGI/XingChenGSR-V1.0",
        source_language="auto", require_language=False,
    )
    assert _finalize_language(_mk_result(_EN_TEXT), cfg).language == ""


# ══════════════════════════════════════════════════════════════════════
# 两级切分（2026-10-04 真机 GUI 测试发现的问题 2/3/4）
# ══════════════════════════════════════════════════════════════════════

#: XingChen 系实测：整段中文 + 一长句英文，英文里夹了数字碎片
_XC_MIXED_NUM = (
    "青紫色的风掠过指尖，金线牡丹在呼吸间流转。"
    "Politics and the English language from Wikipedia, the free encyclopedia,"
    "1946, was written by a political context."
)


def test_english_comma_split_only_when_over_limit():
    """**问题 3 核心断言**：英文先按句点切，**超上限**才从逗号切。

    修复前 `,` 和 `.` 被平铺在同一优先级扫描里，英文在句点处没被切开就先被逗号
    切碎，且碎片长度全部 < 24 使得 `max_sentence_chars` 压根不触发。
    """
    sents = _text_only_to_sentences(
        _XC_MIXED_NUM, total_sec=26.0, cfg=TranscribeConfig(), project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", _XC_MODEL),
    )
    langs = [s.language for s in sents]
    # 英文必须被切出来，且不能有被判成中文的碎片
    assert "English" in langs, langs
    assert "Chinese" in langs, langs
    # 所有后半段的英文内容都必须是 English（关键：`1946,` 不能判成 Chinese）
    en_part = [s for s in sents if s.language == "English"]
    assert en_part, langs
    # 数字碎片继承前句语种 → 不再出现「只有数字却标中文」的句
    for s in sents:
        if not s.text.strip(" ,.:;!?").strip():
            continue
        has_word = any(ch.isalpha() for ch in s.text)
        if not has_word:
            assert s.language != "Chinese", f"数字碎片被判成中文：{s.text!r}"


def test_numeric_fragment_inherits_previous_sentence_language():
    """**问题 3 兜底**：纯数字/标点碎片继承**前一句**语种，而不是回落整段。"""
    sents = _text_only_to_sentences(
        "Politics and the English language from Wikipedia,1946, was written by a "
        "political context that changed after the war ended completely.",
        total_sec=20.0, cfg=TranscribeConfig(), project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", _XC_MODEL),
    )
    numeric = [s for s in sents if not any(ch.isalpha() for ch in s.text)]
    for s in numeric:
        assert s.language == "English", f"{s.text!r} 应继承英文，实际 {s.language}"


def test_glued_period_english_is_split_into_whole_sentences():
    """**问题 4（用户确认为 A 类）**：XingChen 英文句点后无空格，必须按整句切开。"""
    sents = _text_only_to_sentences(
        "青紫色的风掠过指尖。"
        "Politics and the English language from Wikipedia.The free encyclopedia."
        "In 1946,the political context changed.",
        total_sec=26.0, cfg=TranscribeConfig(), project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", _XC_MODEL),
    )
    texts = [s.text for s in sents]
    assert "Politics and the English language from Wikipedia." in texts, texts
    assert "The free encyclopedia." in texts, texts
    # 全部英文句都应是 English，不该出现中文标签
    assert all(s.language == "English" for s in sents[1:]), \
        [(s.language, s.text) for s in sents]


def test_max_chars_setting_now_actually_affects_english():
    """**问题 2 的实质**：分语言上限必须真正影响英文切分，且双向都生效。

    修复前逗号碎片长度都 < 24，硬切那一步压根不触发，用户改「单句最大字数」
    对英文毫无反应——这就是他观察到的「设置很奇怪、判定不讲道理」。

    同时锁定一条**刻意的例外**：全局 ``max_sentence_chars`` **不**作用于逐句改判
    出来的英文句（它走 :data:`_LATIN_SCRIPT_MAX_CHARS`=84 这个语言默认）。
    原因：24 是中文字幕阈值，英文按字符计长 ≈ 1.5 个单词，直接套用会把单词腰斩
    ——那正是本轮最初要修的 bug。控制英文必须用「分语言 → English」这一项。
    """
    from core.app_config import SegmentationPrefs

    text = ("青紫色的风掠过指尖。"
            "Politics and the English language from Wikipedia is a free encyclopedia "
            "that anyone can edit and it has a rather long tail indeed.")

    def count(cfg):
        return _text_only_to_sentences(
            text, total_sec=26.0, cfg=cfg, project_language="Chinese",
            sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", _XC_MODEL),
        )

    # 收紧 → 切得更碎；放宽 → 整句保留。两者都必须真的生效。
    def with_en(limit: int):
        return count(TranscribeConfig(
            seg_prefs=SegmentationPrefs(enabled=True, per_lang={"en": {"max_chars": limit}})))

    loose, tight = with_en(200), with_en(40)
    assert max(len(s.text) for s in loose if s.language == "English") == 130
    # 2026-10-04：硬切改为**对齐词边界**（`_hard_split`），因此上限是「不超过」
    # 而非「恰好等于」——切点回退到最近的空白，实际长度会略小于上限（此处 39 ≤ 40）。
    # 断言用区间而非等值，正是为了锁住「不超上限」这个真正的契约；
    # 若哪天改回盲切，这里会重新变成 40 并提醒我们复核。
    tight_max = max(len(s.text) for s in tight if s.language == "English")
    assert 0 < tight_max <= 40, tight_max
    # 收紧确实切得更碎（这条才是本测试的主张）
    assert len(loose) < len(tight), (len(loose), len(tight))
    # 词边界护栏：英文句首尾不得有多余空白（`_hard_split` 切点丢弃空白本身）
    for sents in (loose, tight):
        for s in sents:
            if s.language == "English":
                assert s.text == s.text.strip(), f"英文句首尾有多余空白：{s.text!r}"
    # 刻意的例外：全局值对英文不生效（英文走语言默认 84），避免用中文阈值腰斩单词。
    # 同样按「不超过」断言（词边界对齐后实际长度会略小于上限）。
    for global_c in (20, 24, 200):
        sents = count(TranscribeConfig(max_sentence_chars=global_c))
        en_max = max(len(s.text) for s in sents if s.language == "English")
        assert 0 < en_max <= 84, (global_c, en_max)


# ══════════════════════════════════════════════════════════════════════
# 服务端 segments 优先路径（Diarize-V3.0 是唯一返回 segments 的模型）
# ══════════════════════════════════════════════════════════════════════

#: Diarize-V3.0 在 26.7s 中英混合素材上的真实返回（2026-10-04 实测）
_DIARIZE_SEGMENTS = [
    CloudSegment(0.48, 3.0, "青瓷色的风掠过指尖", "1"),
    CloudSegment(2.92, 10.0, "惊现牡丹在呼吸，间流转墨香味甘茶烟已绕过雕花窗。", "1"),
    CloudSegment(10.16, 13.26, "今夜的月色可愿与我共采一段流。", "1"),
    CloudSegment(14.56, 20.319, "Politics and the english language from wikipedia", "2"),
]


def test_segments_take_priority_over_punctuation_split():
    """有segments 时直接用服务端边界，不走标点切句。"""
    sents = _text_only_to_sentences(
        "", total_sec=26.7, cfg=TranscribeConfig(), project_language="Chinese",
        segments=_DIARIZE_SEGMENTS,
    )
    assert len(sents) == len(_DIARIZE_SEGMENTS)
    # 时间戳是服务端真值，不是字符权重占位
    assert sents[0].start_time == 0.48
    assert sents[0].end_time == 3.0
    # 说话人标签要透传（Diarize 存在的意义就在这里）
    assert [s.speaker for s in sents] == ["1", "1", "1", "2"]


def test_segments_get_per_sentence_language():
    """Diarize 自己按语言分段，但项目级 language 仍是单一中文→必须逐句判定。"""
    allowed = languages_for_model("XingChenAGI/XingChenASR-Diarize-V3.0")

    def fn(s: str):
        return LANG_SHORT_TO_FULL.get(infer_language_from_text(s, allowed) or "") or None

    sents = _text_only_to_sentences(
        "", total_sec=26.7, cfg=TranscribeConfig(), project_language="Chinese",
        sentence_language_fn=fn, segments=_DIARIZE_SEGMENTS,
    )
    assert [s.language for s in sents] == [
        "Chinese", "Chinese", "Chinese", "English",
    ]


def test_no_segments_falls_back_to_punctuation_exactly():
    """不传 segments（本地路径 / 其它云端模型）时行为与改动前完全一致。"""
    zh = "青瓷色的风掠过指尖，惊现牡丹在呼吸间流转。"
    sents = _text_only_to_sentences(
        zh, total_sec=10.0, cfg=TranscribeConfig(), project_language="Chinese",
    )
    assert [s.text for s in sents] == ["青瓷色的风掠过指尖，", "惊现牡丹在呼吸间流转。"]
    assert {s.language for s in sents} == {"Chinese"}


def test_empty_segments_list_falls_back_too():
    """空列表必须回落到标点切句，不能产出零句。"""
    sents = _text_only_to_sentences(
        "青瓷色的风掠过指尖，惊现牡丹在呼吸间流转。",
        total_sec=10.0, cfg=TranscribeConfig(), project_language="Chinese",
        segments=[],
    )
    assert len(sents) == 2


def test_cloud_path_returns_segments_to_caller(monkeypatch):
    """反回归：云端路径必须把 segments 带回主流程，否则 Diarize 白接。

    原先靠 ``inspect.getsource`` 断言源码里出现 ``result.segments``——那是字符串
    匹配：改个局部变量名就假红，而把真实值丢掉、只在别处留个字面量却可能假绿。
    改成行为断言：喂一个带 segments 的假响应，看返回值三元组里到底有没有它。
    """
    import core.cloud_asr as ca

    segments = [
        CloudSegment(0.0, 1.0, "甲。", "1"),
        CloudSegment(1.0, 2.0, "乙。", "2"),
    ]
    fake = CloudASRResult(
        text="甲。乙。", raw_text="甲。乙。", language="Chinese",
        usage_seconds=2.0, trace_id="t-seg", segments=segments,
    )
    monkeypatch.setattr(ca, "transcribe_cloud", lambda *a, **k: fake)

    cfg = TranscribeConfig(
        cloud_asr=CloudASRConfig(
            model="XingChenAGI/XingChenASR-Diarize-V3.0", source_language="zh"),
    )
    text, language, got = _cloud_transcribe_text(None, pathlib.Path("nope.wav"), cfg)

    assert (text, language) == ("甲。乙。", "Chinese")
    assert got == segments, "segments 未原样带回主流程"
    # 顺手钉住同一函数里的用量台账回写（官方无用量查询接口，只能客户端自己记）
    assert cfg.cloud_asr.observed_usage_seconds == 2.0


# ══════════════════════════════════════════════════════════════════════
# 回归：英文句界丢失（GUI 真机实测，2026-10-04）
# ══════════════════════════════════════════════════════════════════════


def test_english_sentence_between_chinese_is_detected_as_english():
    """端到端：中英混说里夹在中文之间的**纯英文句**必须被判English。

    这是上面那个 bug 的**下游可观测后果**——句界恢复后，英文句才能被
    ``split_zh_en_sentence_language`` 切出来，送去英文对齐器而不是中文对齐器。
    """
    text = ("今天我们来聊聊英文识别的问题。"
            "Politics and the English language is a West Germanic language."
            "它到底该怎么切分才正确。")
    sents = _text_only_to_sentences(
        text, total_sec=26.0, cfg=TranscribeConfig(), project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", _XC_MODEL),
    )
    assert [s.language for s in sents] == ["Chinese", "English", "Chinese"], \
        [(s.language, s.text) for s in sents]
    # 英文那句必须完整保留，不得被 24 字中文阈值腰斩
    assert sents[1].text == "Politics and the English language is a West Germanic language."


# ── 中英黏连块（2026-10-04 真机实测）────────────────────────────────────
# SenseVoice 在 26.7s 中英混合素材上把「中文句尾 + 英文句首」转写成一个**无标点
# 的黏连块**：`今夜的月色…一段流光politics and the English language from Wikipedia…`。
# 该块含汉字 → 旧实现直接沿用整段语种 → 整块 144 字符全被标Chinese，
# 英文部分送了中文对齐器（日志实证：`11 句 / 1 语言段`）。

_MIXED_TAIL = (
    "今夜的月色可愿与我共采一段流光"
    "politics and the English language from Wikipedia the free encyclopedia@"
)


def test_mixed_script_tail_is_split_into_chinese_and_english():
    """黏连块必须切成「中文头 + 英文尾」两段，英文尾判为 English。"""
    parts = split_mixed_script_tail(_MIXED_TAIL, "Chinese", "FunAudioLLM/SenseVoiceSmall")
    assert parts is not None, "中英黏连块应被切分"
    head, tail = parts
    assert head == "今夜的月色可愿与我共采一段流光"
    assert tail == (
        "politics and the English language from Wikipedia the free encyclopedia@"
    )
    # 切分必须无损
    assert head + tail == _MIXED_TAIL
    assert split_zh_en_sentence_language(
        tail, "Chinese", "FunAudioLLM/SenseVoiceSmall",
    ) == "English"


def test_pure_latin_sentence_needs_no_tail_split():
    """纯拉丁句（无汉字）不需要再切——原``English`` 判定路径已覆盖。"""
    text = "politics and the English language from Wikipedia"
    assert split_mixed_script_tail(
        text, "Chinese", "FunAudioLLM/SenseVoiceSmall",
    ) is None
    assert split_zh_en_sentence_language(
        text, "Chinese", "FunAudioLLM/SenseVoiceSmall",
    ) == "English"


@pytest.mark.parametrize("text", [
    # 粤语整句：绝不能被切开，更不能被标成 English
    "我嘅广东话讲得唔係好，但係听得明嘅",
    "茶烟已绕过雕花窗",
    # 粤语/中文里夹一两个英文词：尾巴不够长 → 沿用整段语种
    "我嘅 English 好正",
    "我嘅 English 几好",
    # 假名 / 谚文 / 西里尔尾巴：不是拉丁 → 不切
    "中文尾巴こんにちは",
    "中文尾巴안녕하세요",
])
def test_tail_split_never_breaks_cantonese_or_short_english(text):
    """**粤语防误伤护栏**：只有「足够长的纯拉丁尾巴」才切，其余一律沿用整段语种。"""
    assert split_mixed_script_tail(
        text, "Chinese", "FunAudioLLM/SenseVoiceSmall",
    ) is None
    assert split_zh_en_sentence_language(
        text, "Chinese", "FunAudioLLM/SenseVoiceSmall",
    ) is None, "含汉字的文本绝不能被判为 English"


def test_tail_split_respects_project_language_and_model_scope():
    """整段非中/粤、或模型不在补丁范围 → 不切。"""
    assert split_mixed_script_tail(_MIXED_TAIL, "Japanese", "FunAudioLLM/SenseVoiceSmall") is None
    assert split_mixed_script_tail(_MIXED_TAIL, "English", "FunAudioLLM/SenseVoiceSmall") is None
    assert split_mixed_script_tail(
        _MIXED_TAIL, "Chinese", "some/unlisted-model",
    ) is None


# 完整的真机转写文本（据SRT 反推，167 字）。第2 句与第 3 句之间**没有标点**。
_GLUE_RAW = (
    "青紫色的风掠过指尖，金线牡丹在呼吸间流转。"
    "墨香未干，茶烟已绕过雕花窗，"
    "今夜的月色可愿与我共采一段流光"
    "politics and the English language from Wikipedia the free "
    "encyclopedia@Wikiediaorg politics in the language1946 is."
)


def _glue_sentences(*, with_fix: bool):
    from core.asr_engine import TranscribeConfig, _text_only_to_sentences
    model = "FunAudioLLM/SenseVoiceSmall"
    return _text_only_to_sentences(
        _GLUE_RAW, total_sec=26.66, cfg=TranscribeConfig(),
        project_language="Chinese",
        sentence_language_fn=lambda s: split_zh_en_sentence_language(s, "Chinese", model),
        glue_model_id=model if with_fix else None,
    )


def test_glued_block_end_to_end_yields_two_language_segments():
    """端到端：黏连块必须让英文部分拿到 English 标签（原来整块被标 Chinese）。"""
    sents = _glue_sentences(with_fix=True)
    langs = [s.language for s in sents]
    assert langs.count("English") >= 1, f"英文句未拿到 English 标签：{langs}"
    # 语种序列必须是「中文在前、英文在后」的干净两段，不能交错
    assert langs == sorted(langs, key=lambda x: 0 if x == "Chinese" else 1), langs
    # 英文句确实包含那段英文原文
    en_text = "".join(s.text for s in sents if s.language == "English")
    assert "politics and the English language" in en_text


def test_glued_block_fix_keeps_cjk_comma_split_intact():
    """反回归：中文头**不能**丢掉中文逗号切分。

    引入黏连块拆分时实测抓到的自伤：黏连块整体含大量拉丁 → ``_is_cjk_dominant``
    判False；若把中文头原样放行，中文逗号切分会整个丢失，
    ``墨香未干，茶烟已绕过雕花窗，`` 会粘成 31 字再被硬切在「共|采」之间。
    """
    sents = _glue_sentences(with_fix=True)
    zh_texts = [s.text for s in sents if s.language == "Chinese"]
    assert "墨香未干，" in zh_texts, zh_texts
    assert "茶烟已绕过雕花窗，" in zh_texts, zh_texts
    #中文头不得出现被硬切在词中间的痕迹
    assert not any(t.endswith("共") for t in zh_texts), zh_texts


def test_glued_block_fix_is_lossless():
    """切分必须无损：所有句子拼回原始文本（忽略空白）。"""
    sents = _glue_sentences(with_fix=True)
    joined = "".join(s.text for s in sents)
    assert re.sub(r"\s+", "", joined) == re.sub(r"\s+", "", _GLUE_RAW)


def test_glued_block_fix_is_off_without_model_id():
    """不传模型 id（本地路径）→ 不做黏连拆分，行为与改动前一致。"""
    before = _glue_sentences(with_fix=False)
    assert all(s.language == "Chinese" for s in before), \
        "未启用补丁时不得改判语种"


# ── 模型说明文案：不得夹带测试素材的具体文字（2026-10-04 用户实测反馈）──
# 这些字句（青瓷/金线/惊现/未干/味甘/茶烟/流光）来自本项目的四段测试素材。
# 测试代码里引用它们是对的（那本来就是测试数据），但 ``ModelInfo.note`` 会
# 被设置页原样显示给用户——「线上模型说明里写着金线变成惊现」显然不合逻辑。

_TEST_MATERIAL_WORDS = (
    "青瓷", "金线", "惊现", "未干", "味甘", "墨香", "茶烟", "流光", "青紫",
)


def test_model_notes_do_not_leak_test_material_words():
    """所有免费模型的 ``note`` 不得包含测试素材的具体字词。"""
    offenders = {
        model_id: sorted(
            w for w in _TEST_MATERIAL_WORDS
            if w in (facts.note or "")
        )
        for model_id, facts in VERIFIED_FREE_MODELS.items()
    }
    offenders = {m: ws for m, ws in offenders.items() if ws}
    assert not offenders, f"模型说明夹带了测试素材文字：{offenders}"


# ── 阶段文案与编码进度（2026-10-05 用户真机反馈「等待过长疑似卡顿」）──
#
# 实测依据（.temp/app.log，非猜测）：
#   00:49:07,734  [cloud-asr] 上传 opus 93.2KB → model=SenseVoiceSmall
#   00:49:39,972  [cloud-asr] 完成：170 字 / usage=27.0s
# 「上传 …KB」这行打出时**编码已经做完了**，其后32.2 秒全是云端推理。
# 而旧实现在编码前只发一次 ``progress_cb(0, 0, "编码音频并准备上传…")``，
# 之后整个 HTTP 等待期间不再上报——用户于是盯着一句**已经过时的**文案
# 看完整个推理过程，理所当然地以为卡死了。
#
# 契约：① 云端推理期间必须有**如实描述该阶段**的文案；
#      ② 编码阶段按 FFmpeg 真实 out_time 上报百分比（不伪造）。


def test_transcribe_cloud_reports_distinct_stage_for_cloud_inference(monkeypatch):
    """云端推理阶段必须换文案，且换文案的时刻**不早于**真正发出 HTTP。"""
    import core.cloud_asr as ca

    events: list[tuple[str, float]] = []
    sent_at: list[float] = []

    def fake_post(endpoint, api_key, body, content_type, timeout, trace_id):
        sent_at.append(events[-1][1] if events else 0.0)
        return 200, '{"text": "测试文本"}'

    # 补丁要打在 **client 子模块** 上，不是包上：``transcribe_cloud`` 本体就在
    # ``core.cloud_asr.client`` 里，它按 ``client.__dict__`` 解析 ``_http_post`` /
    # ``encode_for_upload``。打在包命名空间只换掉一份「再导出的引用」，调用点看不见
    # ——那会让测试真的去发 HTTP / 真的去跑 ffmpeg。
    monkeypatch.setattr(ca.client, "_http_post", fake_post)
    clock = {"t": 0.0}

    def fake_encode(*args, **kwargs):
        # 模拟「编码花掉一段时间」：让编码阶段与 HTTP 时刻拉开可测的间隔。
        # 时钟由本函数推进即可——``transcribe_cloud`` 全程只读 ``time.strftime``，
        # 从不读 ``time.perf_counter``，所以这里**不需要** patch time。
        # （曾有一行 ``setattr(ca.time, "perf_counter", …)``：它从未被调用过，
        # 纯属误导，还会把全局 time 模块改掉一大截，已删。）
        clock["t"] += 5.0
        return ca.UploadPayload(b"x", "audio.ogg", "audio/ogg", "opus")

    monkeypatch.setattr(ca.client, "encode_for_upload", fake_encode)

    cfg = CloudASRConfig(api_key="sk-test", model="FunAudioLLM/SenseVoiceSmall",
                        require_language=False)
    ca.transcribe_cloud(
        b"raw", cfg=cfg,
        progress_cb=lambda d, t, s: events.append((s, clock["t"])),
    )

    texts = [s for s, _ in events]
    # 旧文案必须消失——它正是「用户以为卡住」的那一句
    assert not any("编码音频并准备上传" in s for s in texts), \
        f"仍在用已过时的阶段文案：{texts}"
    # 新文案必须出现，且点明是云端识别阶段 + 给出量级预期
    infer = [s for s in texts if "云端识别中" in s]
    assert infer, f"缺少云端推理阶段文案：{texts}"
    assert any(ch.isdigit() for s in infer for ch in s), \
        f"云端阶段文案应给出耗时量级预期（让用户知道等多久算正常）：{infer}"
    # 换文案的时刻不得早于 HTTP 真正发出（否则又变成「提前报完成」）
    infer_at = next(ts for s, ts in events if "云端识别中" in s)
    assert infer_at >= sent_at[0], (
        f"云端阶段文案早于 HTTP 发出：文案 {infer_at:.2f}s < 请求 {sent_at[0]:.2f}s"
    )


def test_encoding_stage_reports_real_progress_from_ffmpeg(monkeypatch, tmp_path):
    """编码阶段按 FFmpeg 的 ``out_time`` 上报**真实**百分比，且单调不回退。

    用假 ffmpeg 脚本模拟 ``-progress pipe:1`` 的输出，避免真跑子进程
    （真跑既依赖本机 ffmpeg 路径，耗时也随机器波动）。
    """
    import core.cloud_asr as ca
    import core.audio_io as audio_io

    # 假 ffmpeg：带 -progress 时按 ffmpeg 真实格式吐进度行并落盘；
    # 否则（时长探测）往 stderr 打印 Duration 行。
    # 用 .bat 包一层是因为 ``ffmpeg_path`` 只接受**可执行文件路径**，
    # 直接给 python.exe 会把第一个 ffmpeg 参数当成脚本名。
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(
        "import sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "dst = pathlib.Path(args[-1])\n"
        "if '-progress' in args:\n"
        "    for us in (0, 30000000, 60000000, 300000000):\n"
        "        print('out_time_us=%d' % us, flush=True)\n"
        "    dst.write_bytes(b'ogg')\n"
        "else:\n"
        "    sys.stderr.write('  Duration: 00:00:10.00, start: 0.0, bitrate: 1 kb/s\\n')\n",
        encoding="utf-8",
    )
    bat = tmp_path / "fake_ffmpeg.bat"
    bat.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    # 直接填好探测缓存：audio_io.ensure_ffmpeg 会短路返回，不会真去跑
    # 冒烟探测（那要求能真的从真实媒体抽音，假的可执行文件过不了）。
    monkeypatch.setattr(audio_io, "_FFMPEG_CHOSEN", str(bat))
    monkeypatch.setattr(audio_io, "_FFMPEG_CHOSEN_OVERRIDE",
                        audio_io._current_ffmpeg_override())
    src = tmp_path / "in.wav"
    src.write_bytes(b"wav")

    events: list[tuple[int, int, str]] = []
    payload = ca.encode_for_upload(
        src, codec="opus", ffmpeg_path=str(bat),
        progress_cb=lambda d, t, s: events.append((d, t, s)),
    )
    assert payload.codec == "opus"
    pcts = [d for d, t, _ in events if t > 0]
    assert pcts, "编码阶段未上报任何百分比（用户只能看到静止的等待）"
    assert pcts == sorted(pcts), f"百分比必须单调不回退：{pcts}"
    assert pcts[-1] > pcts[0], f"百分比应随编码推进：{pcts}"
    assert all(0 <= p <= 100 for p in pcts), pcts


def test_duration_probe_parses_ffmpeg_three_part_timestamp(monkeypatch):
    """``Duration: 00:00:18.04, start:...`` 是**三段**时间（时/分/秒）。

    反回归：曾按两段切分导致探测恒返回 0，于是百分比上报整条链路被静默关闭
    （真机表现 = 代码改了却毫无效果，极具迷惑性）。
    """
    import core.cloud_asr as ca
    import core.audio_io as audio_io

    class _FakeProc:
        returncode = 1        # ffmpeg 不给输出参数时以非 0 退出，属正常
        stderr = ("  Duration: 00:00:18.04, start: 0.000000, bitrate: 107 kb/s\n"
                  "  Stream #0:0: Audio: aac\n")

    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        # 只拦「时长探测」这一种调用（无 -progress），其余放行。
        # 注意 audio_io 与 cloud_asr **共享同一个 subprocess 模块对象**，
        # 无条件替换会连带污染 ffmpeg 冒烟探测。
        if "-progress" not in cmd and "-i" in cmd:
            return _FakeProc()
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(audio_io, "_FFMPEG_CHOSEN", "fake-ffmpeg")
    monkeypatch.setattr(audio_io, "_FFMPEG_CHOSEN_OVERRIDE",
                        audio_io._current_ffmpeg_override())
    # 直接打全局 ``subprocess`` 模块对象（audio_io 与 cloud_asr 共享同一个），
    # 不再绕道包命名空间——云模块拆包后 ``cloud_asr.subprocess`` 已不是合同的一部分。
    monkeypatch.setattr(subprocess, "run", fake_run)
    dur = ca._media_duration_sec(pathlib.Path("x.mp4"))
    assert dur == pytest.approx(18.04, abs=0.01), f"时长探测失败：{dur}"




# ── 模型说明不得出现 Markdown 加粗标记（2026-10-05 用户真机反馈）──
# 设置页的说明标签是 ``Qt.TextFormat.PlainText``，**不解释 Markdown**，
# 于是 core/cloud_asr/facts.py 里按注释习惯写的 ``**强调**`` 会被原样画到界面上。
# 已在数据源清掉现存几处，但那是逐条手改；这里同时钉住「数据源干净」
# 与「UI 边界兜底」，任一条被绕过都不会漏到用户眼前。
_MD_BOLD_RE = re.compile(r"\*\*[^*\n]+\*\*")


def test_model_notes_contain_no_markdown_bold():
    offenders = {
        model_id: _MD_BOLD_RE.findall(facts.note or "")
        for model_id, facts in VERIFIED_FREE_MODELS.items()
        if _MD_BOLD_RE.search(facts.note or "")
    }
    assert not offenders, (
        f"模型说明里残留 Markdown 加粗标记（设置页按纯文本渲染，会显示成星号）：{offenders}"
    )


def test_plain_text_strips_markdown_bold_at_ui_boundary():
    """UI 边界兜底：即便数据源漏进来，渲染前也必须压成纯文本。"""
    from ui.settings.cloud_asr_tab import plain_text

    assert plain_text("**仅限中文**。会**译成中文**") == "仅限中文。会译成中文"
    assert plain_text("没有星号") == "没有星号"
    # 未闭合的 ** 宁可原样保留，也不要静默吃掉半个词
    assert plain_text("未闭合 ** 星号") == "未闭合 ** 星号"
    assert plain_text("**") == "**"
    # 多个强调段
    assert plain_text("a**b**c**d**e") == "abcde"
