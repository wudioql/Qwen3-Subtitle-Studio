"""core.cloud_models — SiliconFlow 可用模型清单服务（UI 与 CLI 的唯一真源）。

为什么不是「价格接口」
----------------------
SiliconFlow **没有**任何可编程的价格/余额查询接口，这是本模块存在的前提：

* ``GET /v1/models`` 返回 ``id / object / created / owned_by``，**不含价格字段**。
* ``GET /v1/user/info`` 已 **410 下线**。
* ``/v1/billing`` ``/v1/usage`` ``/v1/credits`` 等一律 404。

唯一可机器解析的公开价格来源是定价页 https://www.siliconflow.cn/pricing
（SSR HTML，每行 DOM id 形如 ``pricing-row-audio-123``）。称它「来源」而非
「真源」，是因为它的标签**会撒谎**。

三条名单的分工（核心设计）
--------------------------
本模块把三方信息合并成带 ``state`` 的 :class:`ModelInfo`，UI 据此渲染徽标：

1. **定价页**（本次抓取 + 磁盘缓存）——回答「现在有哪些模型被标注为免费」。
2. :data:`core.cloud_asr.VERIFIED_FREE_MODELS` —— **账单实证零扣费**的硬事实。
3. :data:`core.cloud_asr.PAID_MODELS` —— **账单实证收费**的硬事实，优先级最高。

三者冲突时以账本为准（``PAID_MODELS`` > ``VERIFIED_FREE_MODELS`` > 定价页），
这样即便定价页把 ``Qwen/Qwen3-ASR-1.7B`` 标成「免费」，它也会被判为
``known_paid`` 并在 UI 带上收费徽标——选择权留给用户，但知情权不让步。

缓存策略
--------
* 缓存落在 ``.config/cloud_models.json``（随 ``QSS_CONFIG_DIR`` 重定向）。
* **有缓存用缓存**（不联网、不阻塞），**无缓存才自动抓取**，同时也支持手动强制刷新。
* 超过 ``REFRESH_HINT_DAYS`` 标记待复核，超过 ``STALE_LIMIT_DAYS`` 判定陈旧——都只是提示，不会自作主张改行为。

零新增依赖：只用标准库（urllib / re / html）。
"""
from __future__ import annotations

import html as htmlmod
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .app_config import CONFIG_DIR
from .cloud_asr import (
    MODEL_PRICING_URL,
    OFFICIAL_LANGUAGES,
    PAID_MODELS,
    VERIFIED_FREE_MODELS,
    VERIFIED_ON,
    ModelFacts,
)

logger = logging.getLogger(__name__)

__all__ = [
    "STATE_VERIFIED_FREE",
    "STATE_KNOWN_PAID",
    "STATE_UNVERIFIED",
    "CatalogSnapshot",
    "ModelInfo",
    "SFCatalogError",
    "PRICING_URL",
    "MODELS_URL",
    "MODEL_PRICING_URL",
    "CACHE_PATH",
    "REFRESH_HINT_DAYS",
    "STALE_LIMIT_DAYS",
    "parse_pricing",
    "build_model_infos",
    "load_cache",
    "save_cache",
    "refresh_now",
    "list_asr_models",
    "describe_facts",
]


# ────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────

PRICING_URL = "https://www.siliconflow.cn/pricing"

#: 模型列表端点（OpenAI 兼容）。**不含价格字段**，仅用于「模型名是否有效」的零成本探活。
MODELS_URL = "https://api.siliconflow.cn/v1/models"

#: 缓存文件。随 app_config.CONFIG_DIR（即 QSS_CONFIG_DIR）重定向，便于测试隔离。
CACHE_PATH: Path = CONFIG_DIR / "cloud_models.json"

#: 定价页内容会随官方调价变化；超过这个天数提示复核，超过下者判定陈旧。
REFRESH_HINT_DAYS = 30
STALE_LIMIT_DAYS = 90

STATE_VERIFIED_FREE = "verified_free"   # 账单实证零扣费
STATE_KNOWN_PAID = "known_paid"         # 账单实证收费
STATE_UNVERIFIED = "unverified"         # 仅定价页标注免费，无账本证据

STATE_LABELS = {
    STATE_VERIFIED_FREE: "已实证免费",
    STATE_KNOWN_PAID: "已知收费",
    STATE_UNVERIFIED: "未实测",
}

_CATEGORY_LABELS = {
    "text": "对话",
    "audio": "语音",
    "image": "生图",
    "video": "视频",
    "rerank": "重排",
    "embedding": "向量",
}

_ROW_RE = re.compile(r'<div id="pricing-row-([a-z]+)-')
_TARGET_RE = re.compile(r'target=([^"&]+)"')
_PRICE_RE = re.compile(r"¥\s*([\d.]+)")
_FREE_MARK = "免费"

#: 语音类里混着 TTS（语音合成）模型，它们不能做转写。当前两个 TTS 恰好是付费的，
#: 本就进不了「页面免费」清单；这里保留关键字防御，防止未来出现免费 TTS 被误列。
#: 注意别用 "voice"——SenseVoiceSmall（ASR）会被误伤。
_ASR_EXCLUDE_HINTS = ("tts", "cosyvoice", "fish-speech")


class SFCatalogError(RuntimeError):
    """抓取或解析定价页失败。UI 应据此降级展示而非崩溃。"""


# ────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────

@dataclass
class ModelInfo:
    """一个候选模型的合并视图（定价页 + 账本实证）。"""

    id: str = ""
    category: str = "audio"
    state: str = STATE_UNVERIFIED
    note: str = ""                      # 给 UI 展示的说明
    capability: str = ""                # 实测能力摘要（如「标点 ✓ · 句级时间戳 ✗」）
    #: 可用语种的**短码**（短码 → 中文显示名由 ui.languages 负责，core 不碰 UI 文案）。
    #: 空列表 = 尚未掌握，UI 应显示「未知」而不是「不支持」。
    languages: list[str] = field(default_factory=list)
    #: 语种清单的来历：``"verified"`` 本项目实测 / ``"official"`` 官方口径 / ``""`` 未知。
    #: UI 必须把两者区分显示——把官方口径说成实测，等于替平台背书。
    languages_source: str = ""
    page_free: bool = False             # 定价页是否标注免费
    prices: list[str] = field(default_factory=list)

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def is_known_paid(self) -> bool:
        return self.state == STATE_KNOWN_PAID

    def badge(self) -> str:
        """下拉项前缀徽标——用纯文本而非 emoji，避免字体缺失时变豆腐块。"""
        if self.state == STATE_VERIFIED_FREE:
            return "[已实证免费]"
        if self.state == STATE_KNOWN_PAID:
            return "[已知收费]"
        return "[未实测]"

    def display_price(self) -> str:
        if self.prices:
            return "¥ " + " / ".join(self.prices)
        return "页面标免费" if self.page_free else "?"


@dataclass
class CatalogSnapshot:
    """一次清单查询的结果 + 它的来历（UI 需要据此显示来源与陈旧提示）。"""

    models: list[ModelInfo] = field(default_factory=list)
    source: str = "cache"               # "cache" | "live"
    snapshot_date: str = ""
    age_days: int = -1                  # -1 = 未知
    stale: bool = False                 # 超过 STALE_LIMIT_DAYS
    hint: str = ""                      # 给用户的提示（"建议复核" 等）
    error: str = ""                     # 抓取失败原因（有缓存降级时不为空）


# ────────────────────────────────────────────────────────────────
# 抓取与解析
# ────────────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _strip_tags(chunk: str) -> str:
    return htmlmod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk))).strip()


def fetch_pricing(timeout: float = 40.0, tries: int = 3) -> str:
    """GET 定价页 HTML。只读静态页，**不会产生任何费用**。失败抛 SFCatalogError。"""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                PRICING_URL,
                headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定 https
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # 网络探测必须容错重试
            last = exc
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise SFCatalogError(f"抓取定价页失败：{last}")


def parse_pricing(html_text: str) -> list[dict[str, Any]]:
    """从定价页 HTML 解析出每个模型的价目行。

    每行 DOM 是 ``<div id="pricing-row-{category}-{no}" ...>``，行内的
    ``target=<urlencoded model id>`` 是最可靠的模型 ID 来源。
    """
    matches = list(_ROW_RE.finditer(html_text))
    rows: list[dict[str, Any]] = []
    for idx, m in enumerate(matches):
        # 下一个同构锚点即本行终点（最后一行吃到文末）
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(html_text)
        chunk = html_text[m.start():end]

        raw_id = _TARGET_RE.search(chunk)
        if not raw_id:
            continue
        model_id = urllib.parse.unquote(htmlmod.unescape(raw_id.group(1))).strip()
        if not model_id:
            continue

        text = _strip_tags(chunk)
        prices = _PRICE_RE.findall(text)
        cat = m.group(1)
        rows.append({
            "id": model_id,
            "category": cat,
            "category_label": _CATEGORY_LABELS.get(cat, cat),
            # 「含免费字样且没有任何 ¥ 数字」才算页面判定免费
            "is_free": _FREE_MARK in text and not prices,
            "prices": prices,
        })
    return rows


def _is_asr_candidate(model_id: str) -> bool:
    low = model_id.lower()
    return not any(h in low for h in _ASR_EXCLUDE_HINTS)


def describe_facts(model_id: str, facts: ModelFacts) -> str:
    """把实测能力翻译成人话标签（无facts时为空串）。"""
    if not facts:
        return ""
    parts = []
    parts.append("标点 " + ("✓" if facts.has_punctuation else "✗"))
    parts.append("返回语言 " + ("✓" if facts.has_language else "✗"))
    parts.append("句级时间戳 " + ("✓" if facts.has_segments else "✗"))
    return " · ".join(parts)


def build_model_infos(rows: list[dict[str, Any]], *, category: str = "audio") -> list[ModelInfo]:
    """把价目行合并三方信息，产出按 ID 排序的模型视图。

    仅保留**页面标注免费**的模型（这是用户的「只能选标注免费的」诉求），
    但会为每个模型标注它是否在账本里被实证过——不再由本模块替用户做取舍。
    """
    out: list[ModelInfo] = []
    for r in rows:
        if category and r.get("category") != category:
            continue
        mid = str(r.get("id") or "")
        if not mid or not _is_asr_candidate(mid):
            continue
        if not r.get("is_free"):
            continue

        facts = VERIFIED_FREE_MODELS.get(mid)
        if mid in PAID_MODELS:
            state = STATE_KNOWN_PAID
            # 刻意不写明单价：数字会随官方调价过期，而「实证日期 + 查价去处」不会失真。
            note = (
                f"账单实证会产生扣费（{VERIFIED_ON} 实测到真实扣费记录）。"
                "单价可能随官方调价变动，本项目不缓存金额——"
                f"请到控制台模型页查看实时价格：{MODEL_PRICING_URL}"
            )
        elif facts is not None:
            state, note = STATE_VERIFIED_FREE, facts.note
        else:
            state = STATE_UNVERIFIED
            note = (
                "定价页标注为免费，但尚无账单实证：既没证据显示它收费，也没证据显示它真的免费。"
                "定价页的「免费」标签出现过名不副实的先例，"
                f"首次使用前请先到 {MODEL_PRICING_URL} 核对价格，"
                "并用短音频试跑一次后再查账单确认。"
            )
        official = OFFICIAL_LANGUAGES.get(mid, ())
        langs = list(facts.languages) if facts else list(official)
        out.append(ModelInfo(
            id=mid,
            category=r.get("category", "audio"),
            state=state,
            note=note,
            capability=describe_facts(mid, facts),
            languages=langs,
            languages_source="verified" if facts else ("official" if official else ""),
            page_free=True,
            prices=list(r.get("prices") or []),
        ))
    out.sort(key=lambda m: m.id)
    return out


# ────────────────────────────────────────────────────────────────
# 缓存读写
# ────────────────────────────────────────────────────────────────

def load_cache(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """读缓存；不存在/损坏返回 None（不抛，便于 UI 走「自动刷新」分支）。"""
    p = Path(path) if path else CACHE_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    return data


def save_cache(rows: list[dict[str, Any]], path: Optional[Path] = None) -> Path:
    """写缓存（原子替换，失败不影响调用方继续用内存结果）。"""
    p = Path(path) if path else CACHE_PATH
    payload = {
        "source": PRICING_URL,
        "snapshot_date": datetime.now(timezone.utc).astimezone().date().isoformat(),
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "verified_on": VERIFIED_ON,
        "rows": rows,
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    except Exception:
        logger.exception("[sf-models] 写缓存失败 %s", p)
    return p


def _age_days(snapshot_date: str, fetched_at: str) -> int:
    """算缓存年龄（天）。两个时间戳任一可用即可；都不可解析返回 -1。"""
    for raw in (fetched_at, snapshot_date):
        if not raw:
            continue
        try:
            then = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - then).days)
    return -1


# ────────────────────────────────────────────────────────────────
# 对外主接口
# ────────────────────────────────────────────────────────────────

def refresh_now(path: Optional[Path] = None) -> list[ModelInfo]:
    """立即抓取定价页 → 写缓存 → 返回模型清单。**阻塞**，UI 请放到后台线程。"""
    rows = parse_pricing(fetch_pricing())
    save_cache(rows, path)
    return build_model_infos(rows)


def list_asr_models(
    *,
    force_refresh: bool = False,
    path: Optional[Path] = None,
) -> CatalogSnapshot:
    """拿到 ASR 候选模型清单，并按「有缓存用缓存 / 无缓存自动刷新」策略决定来源。

    * ``force_refresh=True``：必定联网（手动刷新按钮）。
    * 有缓存：直接返回缓存内容，并据年龄给出提示，**不联网**。
    * 无缓存：自动抓取；失败时返回空清单 + ``error``。
    """
    if force_refresh:
        try:
            return CatalogSnapshot(models=refresh_now(path), source="live")
        except Exception as exc:
            snap = _from_cache(path, hint="")
            if snap.models:                       # 刷新失败但旧缓存可用 → 降级
                snap.error = str(exc)
                return snap
            return CatalogSnapshot(error=str(exc))

    snap = _from_cache(path, hint="")
    if snap.models:
        return snap
    try:
        return CatalogSnapshot(models=refresh_now(path), source="live")
    except Exception as exc:
        return CatalogSnapshot(error=str(exc))


def _from_cache(path: Optional[Path], hint: str) -> CatalogSnapshot:
    data = load_cache(path)
    if not data:
        return CatalogSnapshot(hint=hint)
    rows = data.get("rows") or []
    models = build_model_infos(rows)
    if not models:
        return CatalogSnapshot(hint=hint)
    age = _age_days(str(data.get("snapshot_date") or ""), str(data.get("fetched_at") or ""))
    notes = [hint] if hint else []
    if age >= STALE_LIMIT_DAYS:
        notes.append(f"清单已超过 {age} 天未更新，建议手动刷新并核对定价页。")
    elif age >= REFRESH_HINT_DAYS:
        notes.append(f"清单已 {age} 天未刷新。")
    return CatalogSnapshot(
        models=models,
        source="cache",
        snapshot_date=str(data.get("snapshot_date") or ""),
        age_days=age,
        stale=age >= STALE_LIMIT_DAYS,
        hint=" ".join(n for n in notes if n),
    )

