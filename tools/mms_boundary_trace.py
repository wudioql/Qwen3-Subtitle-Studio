"""tools/mms_boundary_trace.py — MMS-FA 字级边界诊断（**只读**）

用途：打印每个字的 raw FA 边界、尾音扫描锚点、上界由谁决定（``bound_source``），
用于回答「这个字的 end 为什么停在这里」。

⚠️ **必须选对音频源**：生产开启人声提取时，对齐器吃到的是**人声轨**而不是混音
（``ui/project_controller.py``：16k wav / 人声分离 wav = ``audio_path``）。在混音上诊断
会把伴奏当成发音证据，结论会反过来——用 ``--audio vocals`` 复现生产输入。

**必须走上下文对齐**：MMS 是 CTC 强制对齐，单句孤对齐时句尾的伴奏/后句开口没有
文本可分配，会被硬塞给末字（实测某句末字 end 因此多出 9 秒）。所以本工具调用
``align_with_context``，窗口与生产 ``align_sentence`` 一致（head = 前句起点，
tail = 后句终点；无邻句时用 MMS_HEAD_EXTEND / MMS_TAIL_EXTEND_MAX）。

边界说明：
- 只调 ``MMSAligner``，不碰 Qwen3-ForcedAligner、ASR、共用后处理、导出与工程 JSON。
- 输出的是 **raw FA 结果**（merge / sanitize / seam snap 尚未施加）。
- 只读媒体与工程文件；除非显式 ``--json``，不写任何文件。

示例：
    python tools/mms_boundary_trace.py --project test-long-sing.qss.json --sentences 2,4,8
    python tools/mms_boundary_trace.py --media song.mp4 --text "九霄云外探迷踪" --start 7 --end 15
    # 生产等价（开启人声提取）：对齐器只看到人声，伴奏不提供声学证据
    python tools/mms_boundary_trace.py --project song.qss.json --audio vocals
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402

from core.constants import MMS_HEAD_EXTEND, MMS_TAIL_EXTEND_MAX  # noqa: E402
from core.mms_aligner import (  # noqa: E402
    _SAMPLE_RATE,
    MMSAligner,
    get_mms_aligner,
)
from core.text_utils import extract_pure_words  # noqa: E402

# 模型目录优先用仓库内自带权重，避免被用户偏好里的其他路径带偏。
_LOCAL_MODEL_DIR = _ROOT / "models" / "mms-300m-1130-forced-aligner-ONNX"


def _load_project(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_audio(media: Path, want_sr: int = _SAMPLE_RATE) -> np.ndarray:
    """任意音/视频 → 16k mono float32。

    容器格式（mp4/mkv…）soundfile 读不了，必须走 FFmpeg 提取；WAV/FLAC 直接读，
    避免为诊断样本重复解码整段视频。
    """
    from core.audio_io import extract_audio, load_audio

    src = media
    if media.suffix.lower() not in (".wav", ".flac"):
        src = extract_audio(media, sample_rate=want_sr, channels=1)
    audio, _ = load_audio(src, target_sr=want_sr)
    return np.asarray(audio, dtype=np.float32)


def _resolve_vocals(media: Path, explicit: Optional[Path]) -> Path:
    """取人声分离轨。优先显式路径，其次生产同一套缓存命名（不另造路径）。"""
    from core.vocal_separator import extract_vocals_to_wav, vocals_cache_path

    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"--vocals-wav 不存在：{explicit}")
        return explicit

    try:
        cached = vocals_cache_path(media)
    except OSError as exc:
        raise SystemExit(f"源媒体不可访问：{media}（{exc}）") from exc
    if cached.is_file():
        print(f"[audio] 复用生产人声缓存：{cached}")
        return cached

    out = _ROOT / ".pytest_tmp_vocals" / f"vocals_{media.stem}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[audio] 未找到人声缓存，现场分离 → {out}（首次约十几秒）")
    try:
        return extract_vocals_to_wav(media, out, allow_fallback=False)
    except Exception as exc:  # noqa: BLE001 — 诊断工具，失败即如实退出
        raise SystemExit(f"人声分离失败：{exc}") from exc



def _slice(audio: np.ndarray, start: float, end: float) -> np.ndarray:
    lo = max(0, int(round(start * _SAMPLE_RATE)))
    hi = min(len(audio), int(round(end * _SAMPLE_RATE)))
    return audio[lo:hi]


def _align_cached(
    aligner: MMSAligner,
    emit_cache: Dict[str, np.ndarray],
    use_context: bool,
    **kwargs,
):
    """跑一次对齐，但复用同一窗口的 emissions（A/B 必须喂同一份声学结果）。"""
    original = aligner._generate_emissions_chunked

    def wrapped(audio_np):
        if "e" not in emit_cache:
            emit_cache["e"] = original(audio_np)
        return emit_cache["e"]

    aligner._generate_emissions_chunked = wrapped  # type: ignore[method-assign]
    try:
        if use_context:
            return aligner.align_with_context(**kwargs)
        return aligner.align(**kwargs)
    finally:
        aligner._generate_emissions_chunked = original  # type: ignore[method-assign]


def _run_case(
    aligner: MMSAligner,
    emit_cache: Dict[str, np.ndarray],
    clip: np.ndarray,
    case: Dict,
) -> Tuple[list, List[dict]]:
    """跑一个 case，返回 (words, 本句的 trace 记录)。"""
    records: List[dict] = []
    prev_text = case["prev_text"]
    next_text = case["next_text"]
    use_context = bool(prev_text or next_text)
    common = dict(
        audio=(clip, _SAMPLE_RATE),
        language=case["language"],
        offset_sec=case["offset"],
        boundary_trace=records.append,
    )
    if use_context:
        words = _align_cached(
            aligner, emit_cache, True,
            prev_text=prev_text, text=case["text"], next_text=next_text,
            prev_language=case["prev_language"], next_language=case["next_language"],
            **common,
        )
    else:
        words = _align_cached(
            aligner, emit_cache, False,
            text=case["text"], tail_limit_sec=case["tail_limit"], **common,
        )

    # trace 覆盖 prev+本句+next 的全部字，按生产切片约定只取本句那段
    lo = len(extract_pure_words(prev_text))
    hi = lo + len(extract_pure_words(case["text"]))
    return words, records[lo:hi]


def _fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.3f}"


def _print_table(label: str, recs: List[dict]) -> None:
    print(f"\n== {label} ==")
    print(f"{'#':>3} {'字':<4} {'s_f':>5} {'锚':>5} {'上界':>5} {'扫描终':>6} "
          f"{'start':>7} {'end':>7} {'决定者':<12} {'哨兵':>5}")
    for r in recs:
        onset = "—" if r["onset"] is None else str(r["onset"])
        print(f"{r['unit_index']:>3} {r['text']:<4} {r['s_f']:>5} "
              f"{r['tail_scan_start']:>5} {r['next_s_f']:>5} {r['e_f_scan']:>6} "
              f"{_fmt(r['start_time']):>7} {_fmt(r['end_time']):>7} "
              f"{r['bound_source']:<12} {onset:>5}")


def _build_cases(args) -> Tuple[Path, List[dict]]:
    if args.media:
        if not args.text.strip():
            raise SystemExit("--media 模式必须给 --text")
        end = args.end if args.end is not None else args.start + 30.0
        head = max(0.0, args.start - MMS_HEAD_EXTEND)
        return args.media, [{
            "label": f"media[{args.start:.2f}-{end:.2f}]",
            "text": args.text,
            "prev_text": "",
            "next_text": "",
            "window": (head, end),
            "offset": head,
            "tail_limit": end,
            "language": args.language,
            "prev_language": None,
            "next_language": None,
        }]

    proj = _load_project(args.project)
    media_path = Path(proj.get("source_media_path") or proj.get("video_path") or "")
    if not media_path.exists():
        raise SystemExit(f"工程里的媒体路径不存在：{media_path}（可先用 --media 指定）")
    sents = proj.get("sentences") or []
    pick = (
        [int(x) for x in args.sentences.split(",") if x.strip()]
        if args.sentences else list(range(len(sents)))
    )
    cases = []
    for idx in pick:
        if not 0 <= idx < len(sents):
            raise SystemExit(f"句下标越界：{idx}（共 {len(sents)} 句）")
        s = sents[idx]
        prev_s = sents[idx - 1] if idx > 0 else None
        next_s = sents[idx + 1] if idx + 1 < len(sents) else None
        # 与生产 align_sentence 同构：有邻句 → 窗覆盖邻句（邻句文本吸收自己的发音）
        head = float(prev_s["start_time"]) if prev_s else s["start_time"] - MMS_HEAD_EXTEND
        tail = float(next_s["end_time"]) if next_s else s["end_time"] + MMS_TAIL_EXTEND_MAX
        if args.win == "tight":
            # 备选：只覆盖本句 + 前瞻（≈ 生产的「孤对齐」分支），用于观察无邻句文本时
            # 末字是否被伴奏吃掉。默认不用——孤对齐会制造假「吃音」。
            head = s["start_time"] - MMS_HEAD_EXTEND
            tail = s["end_time"] + MMS_TAIL_EXTEND_MAX
            if next_s:
                tail = min(tail, float(next_s["start_time"]))
        use_ctx = args.win == "ctx"
        cases.append({
            "label": f"句{idx}: {s['text'][:18]}",
            "text": s["text"],
            "prev_text": ((prev_s or {}).get("text") or "") if use_ctx else "",
            "next_text": ((next_s or {}).get("text") or "") if use_ctx else "",
            "window": (head, tail),
            "offset": head,
            "tail_limit": next_s["start_time"] if next_s else None,
            "language": s.get("language") or proj.get("source_language") or args.language,
            "prev_language": (prev_s or {}).get("language"),
            "next_language": (next_s or {}).get("language"),
        })
    return media_path, cases


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="MMS-FA 字级边界诊断 / A-B 锚点反事实重放（只读，不改工程）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--project", type=Path, help="工程 JSON（.qss.json），按句号取文本与句界")
    src.add_argument("--media", type=Path, help="直接给媒体文件（配合 --text/--start/--end）")

    ap.add_argument("--sentences", default="", help="句下标（0 起，逗号分隔；默认全部）")
    ap.add_argument("--win", choices=["ctx", "tight"], default="ctx",
                    help="ctx=窗覆盖邻句并带邻句文本（默认，同生产）；tight=只覆盖本句+前瞻")
    ap.add_argument("--text", default="", help="--media 模式下的对齐文本")
    ap.add_argument("--start", type=float, default=0.0, help="--media 模式的窗口起点（秒）")
    ap.add_argument("--end", type=float, default=None, help="--media 模式的窗口终点（秒）")
    ap.add_argument("--language", default="zh", help="语言（默认 zh）")
    ap.add_argument("--json", type=Path, default=None, help="把结果写成 JSON（可选）")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"],
                    help="ORT 执行设备（默认 cpu：诊断不受 GPU 显存状态影响）")
    ap.add_argument("--audio", choices=["mix", "vocals"], default="mix",
                    help="喂给对齐器的音频源。**生产在开启人声提取时喂 vocals**，"
                         "此时伴奏不参与声学证据，A/B 结论可能完全不同")
    ap.add_argument("--vocals-wav", type=Path, default=None,
                    help="直接指定人声轨（默认复用生产缓存，缺失则现场分离）")
    args = ap.parse_args(argv)

    media_path, cases = _build_cases(args)

    aligner = get_mms_aligner(
        model_dir=_LOCAL_MODEL_DIR if _LOCAL_MODEL_DIR.exists() else None,
        device=args.device,
    )
    audio = _load_audio(
        _resolve_vocals(media_path, args.vocals_wav) if args.audio == "vocals" else media_path
    )
    print(f"[audio] 源 = {args.audio}"
          + (f"（{args.vocals_wav}）" if args.audio == "vocals" and args.vocals_wav else ""))

    all_out: List[dict] = []

    for case in cases:
        clip = _slice(audio, *case["window"])
        if clip.size == 0:
            print(f"!! {case['label']}：窗口为空，跳过")
            continue
        emit_cache: Dict[str, np.ndarray] = {}   # 每个窗口一份
        words_a, recs_a = _run_case(aligner, emit_cache, clip, case)
        if not recs_a:
            print(f"!! {case['label']}：未产生字，跳过")
            continue
        _print_table(case["label"], recs_a)
        entry = {
            "label": case["label"],
            "text": case["text"],
            "window": list(case["window"]),
            "words_a": [[w.text, w.start_time, w.end_time] for w in words_a],
            "trace_a": recs_a,
        }

        all_out.append(entry)

    if args.json:
        args.json.write_text(
            json.dumps(all_out, ensure_ascii=False, indent=1), encoding="utf-8",
        )
        print(f"\n已写：{args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
