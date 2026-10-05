"""SiliconFlow 模型清单探针（CLI）—— ``core.cloud_models`` 的命令行薄包装。

历史与现状
----------
抓取 / 解析 / 合并的真源已下沉到 :mod:`core.cloud_models`（UI 与 CLI 共用），
本文件只保留命令行参数与打印排版，**不再复制任何解析逻辑**——否则定价页
改版时要改两处。

名称说明：原名 ``sf_free_models.py`` 只提「免费模型」，但 ``--asr`` 实际按 UI 口径
输出**全部** ASR 候选并带实证状态徽标，名字与行为已不符，故随 ``core.sf_models``
一并改名为 ``cloud_models_cli.py``（同一域统一用 ``cloud_`` 前缀）。

背景（为什么需要这个工具）
--------------------------
官方 **没有**任何可编程的价格/余额查询接口：

* ``GET /v1/models`` 只返回 ``id / object / created / owned_by``，**不含价格字段**。
* ``GET /v1/user/info`` 已 **410 下线**。
* ``/v1/billing`` ``/v1/usage`` ``/v1/credits`` 等一律 404。

唯一可机器解析的公开价格来源是定价页 https://www.siliconflow.cn/pricing
（SSR HTML）。称它「来源」而非「真源」——它的标签会撒谎：

2026-10-04 实测：``Qwen/Qwen3-ASR-1.7B`` 在定价页标"免费"，实际调用在一个
从未充值的账户上产生了 ¥0.0002 扣费。因此产出的免费清单只是**提示性输入**，
真实是否免费以账单为准（见 ``core.cloud_asr`` 的 ``VERIFIED_FREE_MODELS``）。

用法
----
    python tools/cloud_models_cli.py                列出全部模型及免费标记
    python tools/cloud_models_cli.py --category audio
                                                  只看语音类
    python tools/cloud_models_cli.py --free-only    只看免费模型
    python tools/cloud_models_cli.py --write-whitelist
                                                  把免费清单写入缓存 JSON
    python tools/cloud_models_cli.py --cross-check  额外用 Key 交叉比对 /v1/models
    python tools/cloud_models_cli.py --asr          按 UI 口径输出 ASR 候选（含实证状态）

注意
----
本脚本**不发起任何会产生费用的请求**（只 GET 静态页面，可选 GET /v1/models）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.cloud_models import (  # noqa: E402  - sys.path 处理必须先于项目导入
    CACHE_PATH,
    MODELS_URL,
    build_model_infos,
    fetch_pricing,
    parse_pricing,
    save_cache,
)

SF_API_KEY_ENVS = ("SF_API_KEY", "QSS_SILICONFLOW_API_KEY")


def fetch_live_models() -> list[str] | None:
    """用 API Key 拉取 /v1/models 的 id 列表；无 key 或失败返回 None。"""
    import urllib.request

    key = ""
    for env in SF_API_KEY_ENVS:
        key = os.environ.get(env) or ""
        if key:
            break
    if not key:
        return None
    try:
        req = urllib.request.Request(MODELS_URL, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=40) as resp:  # noqa: S310 - 固定 https
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        return [str(item.get("id", "")) for item in payload.get("data", [])]
    except Exception as exc:  # 交叉校验失败不影响主流程
        print(f"  [warn] /v1/models 交叉校验失败：{exc}", file=sys.stderr)
        return None


def render(rows: list[dict], *, live_ids: list[str] | None) -> None:
    print(f"\n{'模型 ID':52s} {'类别':6s} {'计费':12s}")
    print("-" * 78)
    for r in rows:
        if r["is_free"]:
            billing = "免费"
        elif r["prices"]:
            billing = "¥ " + " / ".join(r["prices"])
        else:
            billing = "?"
        mark = ""
        if live_ids is not None:
            mark = "  [+]" if r["id"] not in live_ids else "  [ok]"
        print(f"{r['id']:52s} {r['category_label']:6s} {billing:12s}{mark}")


def render_asr(models) -> None:
    print(f"\n{'模型 ID':52s} {'状态':12s} 实测能力")
    print("-" * 100)
    for m in models:
        print(f"{m.id:52s} {m.state_label:12s} {m.capability or '—'}")
        if m.note:
            print(f"    └ {m.note}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="拉取 SiliconFlow 定价页，输出模型清单（默认全部；--free-only 只看免费），"
                    "并与账单实证账本合并出状态徽标。")
    ap.add_argument("--category", help="只看某一类：text / audio / image / video ...")
    ap.add_argument("--free-only", action="store_true", help="只显示免费模型")
    ap.add_argument("--asr", action="store_true", help="按 UI 口径输出 ASR 候选（含账本实证状态）")
    ap.add_argument("--cross-check", action="store_true", help="与 /v1/models 交叉比对")
    ap.add_argument("--write-whitelist", action="store_true", help="写入免费清单缓存 JSON")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args(argv)

    print("[1/3] 抓取定价页")
    try:
        html_text = fetch_pricing()
    except RuntimeError as exc:
        print(f"  失败：{exc}", file=sys.stderr)
        return 2
    print(f"      OK，{len(html_text)} 字符")

    print("[2/3] 解析价格行")
    rows = parse_pricing(html_text)
    if args.category:
        rows = [r for r in rows if r["category"] == args.category]
    if args.free_only:
        rows = [r for r in rows if r["is_free"]]
    print(f"      OK，{len(rows)} 行")

    live_ids = fetch_live_models() if args.cross_check else None
    state = "已启用" if live_ids is not None else f"未启用（需 {' / '.join(SF_API_KEY_ENVS)}）"
    print(f"[3/3] 交叉校验：{state}")

    if args.asr:
        models = build_model_infos(rows, category=args.category or "audio")
        if args.json:
            print(json.dumps([m.__dict__ for m in models], ensure_ascii=False, indent=2))
        else:
            render_asr(models)
            print(f"\nASR 候选 {len(models)} 个（已实证免费 "
                  f"{sum(1 for m in models if m.state == 'verified_free')} / "
                  f"已知收费 {sum(1 for m in models if m.state == 'known_paid')} / "
                  f"未实测 {sum(1 for m in models if m.state == 'unverified')}）")
        return 0

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        render(rows, live_ids=live_ids)
        free = [r["id"] for r in rows if r["is_free"]]
        print(f"\n免费模型 {len(free)} 个（共 {len(rows)} 行）")

    if args.write_whitelist:
        save_cache(rows)
        print(f"\n缓存已写入 {CACHE_PATH}")
        print("建议每 30 天重跑一次；超过 90 天 UI 会提示手动复核（也可在设置页点「刷新」）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
