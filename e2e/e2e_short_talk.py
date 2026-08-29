"""三文件真机 E2E：test-short-talk.mp4 / .txt / .ass。

默认验收：
1. MP4 → FFmpeg 16k mono WAV；
2. Qwen3-ASR + Qwen3 ForcedAligner；
3. 同一音频 + TXT/ASS 参考文本 → MMS-FA 全文对齐；
4. TXT 作为文字真值，ASS k-tag 作为句/字级时间真值；
5. 两后端分别导出 UI 对应的 11 种字幕产物（含应用模板后 ASS）并生成 JSON 报告。

需 Windows/CUDA/模型/FFmpeg；不进入默认 pytest。纯参考资源与评估函数由
``tests/test_e2e_assets.py`` 在无模型环境验证。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.align_engine import AlignConfig, align_full_text  # noqa: E402
from core.asr_engine import TranscribeConfig, transcribe  # noqa: E402
from core.audio_io import extract_audio, get_audio_info  # noqa: E402
from core.constants import (  # noqa: E402
    ALIGNER_MODEL_PATH,
    ASR_MODEL_PATH,
    MMS_ALIGNER_MODEL_PATH,
    PROJECT_ROOT as CONST_ROOT,
    TEMP_DIR,
)
from core.mms_aligner import get_mms_aligner  # noqa: E402
from core.model_manager import ModelManager  # noqa: E402
from core.text_utils import extract_pure_words  # noqa: E402
from subs import (  # noqa: E402
    LrcMeta,
    Sentence,
    SubtitleProject,
    WordHighlightStyle,
    WordTimestamp,
    parse_subtitle_or_text,
    to_ass,
    to_ass_karaoke,
    to_ass_karaoke_applied,
    to_lrc,
    to_srt,
    to_vtt,
    write_lrc_file,
)

assert CONST_ROOT == PROJECT_ROOT

TEST_MP4 = PROJECT_ROOT / "test-short-talk.mp4"
TEST_TXT = PROJECT_ROOT / "test-short-talk.txt"
TEST_ASS = PROJECT_ROOT / "test-short-talk.ass"
OUT_WAV = TEMP_DIR / "test-short-talk.wav"
EXPORT_ROOT = TEMP_DIR / "short-talk-exports"
REPORT_JSON = EXPORT_ROOT / "short_talk_report.json"


def _normal_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def load_reference_assets() -> tuple[list[str], SubtitleProject]:
    """加载并交叉校验 TXT 文本真值与 ASS k-tag 时间真值。"""
    missing = [path for path in (TEST_MP4, TEST_TXT, TEST_ASS) if not path.is_file()]
    if missing:
        raise FileNotFoundError("E2E 三文件缺失：" + ", ".join(str(path) for path in missing))

    text_sentences = [
        line.strip()
        for line in TEST_TXT.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    ass_sentences = parse_subtitle_or_text(TEST_ASS, language="zh")
    if not ass_sentences:
        raise ValueError("test-short-talk.ass 未解析出 Dialogue")
    if any(not sentence.words for sentence in ass_sentences):
        raise ValueError("test-short-talk.ass 必须每句都含 k-tag 字级时间")

    txt_text = _normal_text("".join(text_sentences))
    ass_text = _normal_text("".join(sentence.text for sentence in ass_sentences))
    if txt_text != ass_text:
        raise ValueError(f"TXT/ASS 文本不一致：txt={txt_text!r}, ass={ass_text!r}")

    reference = SubtitleProject(
        source_media_path=str(TEST_MP4),
        video_path=str(TEST_MP4),
        media_duration=max(sentence.end_time for sentence in ass_sentences),
        source_language="zh",
        sentences=ass_sentences,
    )
    return text_sentences, reference


def cer_and_alignment(hypothesis: str, reference: str) -> tuple[float, str]:
    matcher = difflib.SequenceMatcher(a=reference, b=hypothesis, autojunk=False)
    edits = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        edits += max(i2 - i1, j2 - j1) if tag == "replace" else (i2 - i1) + (j2 - j1)
    diff = "\n".join(
        line[:400]
        for line in difflib.unified_diff(
            [reference], [hypothesis], fromfile="ref", tofile="hyp", lineterm=""
        )
    )
    return edits / max(1, len(reference)), diff


def _expand_word_units(words: Iterable[WordTimestamp]) -> list[tuple[str, float, float]]:
    units: list[tuple[str, float, float]] = []
    for word in words:
        pure = extract_pure_words(word.text)
        if not pure:
            continue
        duration = max(0.0, float(word.end_time) - float(word.start_time))
        for index, token in enumerate(pure):
            start = float(word.start_time) + duration * index / len(pure)
            end = float(word.start_time) + duration * (index + 1) / len(pure)
            units.append((token, start, end))
    return units


def _project_units(project: SubtitleProject) -> list[tuple[str, float, float]]:
    return _expand_word_units(
        word
        for sentence in sorted(project.sentences, key=lambda item: item.start_time)
        for word in sentence.words
    )


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * max(0.0, min(1.0, q)))
    return float(ordered[index])


def evaluate_text(project: SubtitleProject, reference_lines: list[str]) -> dict:
    ref = _normal_text("".join(reference_lines))
    hyp = _normal_text("".join(sentence.text for sentence in project.sentences))
    cer, diff = cer_and_alignment(hyp, ref)
    return {
        "cer": cer,
        "hyp_char_count": len(hyp),
        "ref_char_count": len(ref),
        "hyp_sentence_count": len(project.sentences),
        "ref_sentence_count": len(reference_lines),
        "diff_excerpt": diff[:1200],
        "ok": cer < 0.25,
    }


def evaluate_timestamps(project: SubtitleProject, reference: SubtitleProject) -> dict:
    ref_units = _project_units(reference)
    hyp_units = _project_units(project)
    ref_tokens = [item[0] for item in ref_units]
    hyp_tokens = [item[0] for item in hyp_units]
    matcher = difflib.SequenceMatcher(a=ref_tokens, b=hyp_tokens, autojunk=False)

    start_errors: list[float] = []
    end_errors: list[float] = []
    matched = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(i2 - i1):
            ref_word = ref_units[i1 + offset]
            hyp_word = hyp_units[j1 + offset]
            start_errors.append(abs(hyp_word[1] - ref_word[1]))
            end_errors.append(abs(hyp_word[2] - ref_word[2]))
            matched += 1

    monotonic = True
    for sentence in project.sentences:
        previous = -1.0
        for word in sentence.words:
            if word.start_time < previous - 1e-6 or word.end_time < word.start_time:
                monotonic = False
            previous = max(previous, word.end_time)

    coverage = matched / max(1, len(ref_units))
    start_p50 = _percentile(start_errors, 0.50)
    start_p95 = _percentile(start_errors, 0.95)
    end_p50 = _percentile(end_errors, 0.50)
    end_p95 = _percentile(end_errors, 0.95)
    first_error = abs(project.sentences[0].start_time - reference.sentences[0].start_time)
    last_error = abs(project.sentences[-1].end_time - reference.sentences[-1].end_time)

    ok = (
        coverage >= 0.75
        and start_p50 is not None and start_p50 <= 0.35
        and end_p50 is not None and end_p50 <= 0.35
        and start_p95 is not None and start_p95 <= 1.0
        and end_p95 is not None and end_p95 <= 1.0
        and first_error <= 1.0
        and last_error <= 2.0
        and monotonic
    )
    return {
        "reference_word_units": len(ref_units),
        "hypothesis_word_units": len(hyp_units),
        "matched_word_units": matched,
        "match_coverage": coverage,
        "start_error_p50_ms": None if start_p50 is None else round(start_p50 * 1000),
        "start_error_p95_ms": None if start_p95 is None else round(start_p95 * 1000),
        "end_error_p50_ms": None if end_p50 is None else round(end_p50 * 1000),
        "end_error_p95_ms": None if end_p95 is None else round(end_p95 * 1000),
        "first_sentence_start_error_ms": round(first_error * 1000),
        "last_sentence_end_error_ms": round(last_error * 1000),
        "monotonic": monotonic,
        "ok": ok,
    }


def step_extract_audio() -> tuple[Path, float]:
    print("\n=== 1. MP4 → 16k mono WAV ===")
    TEMP_DIR.mkdir(exist_ok=True)
    out = extract_audio(TEST_MP4, OUT_WAV, sample_rate=16000, channels=1)
    info = get_audio_info(out)
    if info.sample_rate != 16000 or info.channels != 1 or info.num_frames <= 0:
        raise RuntimeError(f"抽音产物异常：{info}")
    print(f"[OK] {info.duration:.3f}s / {info.sample_rate}Hz / {info.channels}ch")
    return out, float(info.duration)


def run_qwen_pipeline(wav_path: Path, manager: ModelManager) -> tuple[SubtitleProject, float]:
    print("\n=== 2A. Qwen ASR + Qwen ForcedAligner ===")
    config = TranscribeConfig(
        source_language="zh",
        return_word_timestamps=True,
        align_backend="qwen",
        use_cache=True,
    )
    started = time.perf_counter()
    project = transcribe(
        wav_path,
        source_media_path=TEST_MP4,
        model_manager=manager,
        cfg=config,
    )
    elapsed = time.perf_counter() - started
    print(f"[OK] {len(project.sentences)} 句 / {sum(len(s.words) for s in project.sentences)} words / {elapsed:.2f}s")
    return project, elapsed


def run_mms_pipeline(
    wav_path: Path,
    duration: float,
    reference: SubtitleProject,
    manager: ModelManager,
) -> tuple[SubtitleProject, float]:
    print("\n=== 2B. TXT/ASS 参考文本 + MMS-FA 全文对齐 ===")
    project = SubtitleProject(
        source_media_path=str(TEST_MP4),
        video_path=str(TEST_MP4),
        audio_path=str(wav_path),
        media_duration=duration,
        source_language="zh",
        sentences=[
            Sentence(
                text=sentence.text,
                start_time=sentence.start_time,
                end_time=sentence.end_time,
                language="zh",
                is_dirty=True,
            )
            for sentence in reference.sentences
        ],
    )
    started = time.perf_counter()
    aligned = align_full_text(
        project,
        model_manager=manager,
        cfg=AlignConfig(source_language="zh", align_backend="mms"),
    )
    elapsed = time.perf_counter() - started
    print(f"[OK] {len(aligned.sentences)} 句 / {sum(len(s.words) for s in aligned.sentences)} words / {elapsed:.2f}s")
    return aligned, elapsed


def export_all(project: SubtitleProject, backend: str) -> dict:
    """导出 UI 对应的 4 个句级 + 7 个字级产物。"""
    output_dir = EXPORT_ROOT / backend
    output_dir.mkdir(parents=True, exist_ok=True)
    style = WordHighlightStyle(underline=True)
    meta = LrcMeta(ti="短语音测试", ar="test-short-talk", by="Qwen3 Subtitle Studio E2E")

    # CLI E2E 不启动 QApplication；用确定性等宽近似验证成品导出结构。
    coord_state = {"line": None, "cursor": 0, "last_index": -1}

    def e2e_coord(index, syllable, line):
        width = 64
        left = (1920 - len(line) * width) // 2
        if coord_state["line"] != line or index <= coord_state["last_index"]:
            coord_state.update(line=line, cursor=0)
        coord_state["last_index"] = index
        if not syllable:
            position = 0
        else:
            position = line.find(syllable, coord_state["cursor"])
            if position < 0:
                position = coord_state["cursor"]
            coord_state["cursor"] = position + len(syllable)
        return (left + position * width + len(syllable) * width // 2, 990, 960, 990)

    rendered = {
        "sentence.srt": to_srt(project, style, mode="per_sentence"),
        "sentence.vtt": to_vtt(project, style, mode="per_sentence"),
        "sentence.ass": to_ass(project, style, mode="per_sentence"),
        "word.srt": to_srt(project, style, mode="per_word"),
        "word.vtt": to_vtt(project, style, mode="per_word"),
        "word.split.ass": to_ass(project, style, mode="per_word", strategy="split"),
        "word.t.ass": to_ass(project, style, mode="per_word", strategy="t"),
        "enhanced.lrc": to_lrc(project, enhanced=True),
        "karaoke.ass": to_ass_karaoke(project, k_mode="kf"),
        "karaoke.applied.ass": to_ass_karaoke_applied(
            project, k_mode="kf", coord_provider=e2e_coord,
        ),
    }
    for name, text in rendered.items():
        (output_dir / name).write_text(text, encoding="utf-8", newline="\n")
    write_lrc_file(project, output_dir / "standard.lrc", enhanced=False, meta=meta)

    files = sorted(output_dir.iterdir())
    if len(files) != 11 or any(path.stat().st_size == 0 for path in files):
        raise RuntimeError(f"{backend} 导出不完整：{[path.name for path in files]}")
    report = {}
    for path in files:
        try:
            display_path = path.relative_to(PROJECT_ROOT)
        except ValueError:
            display_path = path
        report[path.name] = {
            "size_bytes": path.stat().st_size,
            "path": str(display_path),
        }
    return report


def _preflight(backends: list[str]) -> None:
    required: list[Path] = []
    if "qwen" in backends:
        required.extend((ASR_MODEL_PATH, ALIGNER_MODEL_PATH))
    if "mms" in backends:
        required.append(MMS_ALIGNER_MODEL_PATH)
    missing = [path for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("模型缺失：" + ", ".join(str(path) for path in missing))
    if "mms" in backends and not get_mms_aligner().is_available():
        raise FileNotFoundError(f"MMS 目录存在但未找到 ONNX：{MMS_ALIGNER_MODEL_PATH}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=("both", "qwen", "mms"),
        default="both",
        help="默认 both：ASR+Qwen 与参考文本+MMS 都验收",
    )
    parser.add_argument("--skip-exports", action="store_true", help="只跑识别/对齐与指标")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    reference_lines, reference = load_reference_assets()
    selected = ["qwen", "mms"] if args.backend == "both" else [args.backend]
    _preflight(selected)

    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    wav_path, duration = step_extract_audio()
    manager = ModelManager()
    reports: dict[str, dict] = {}
    try:
        if "qwen" in selected:
            project, elapsed = run_qwen_pipeline(wav_path, manager)
            reports["qwen"] = {
                "wall_seconds": elapsed,
                "rtf": elapsed / max(0.001, duration),
                "text": evaluate_text(project, reference_lines),
                "timestamps": evaluate_timestamps(project, reference),
            }
            if not args.skip_exports:
                reports["qwen"]["exports"] = export_all(project, "qwen")

        if "mms" in selected:
            project, elapsed = run_mms_pipeline(wav_path, duration, reference, manager)
            reports["mms"] = {
                "wall_seconds": elapsed,
                "rtf": elapsed / max(0.001, duration),
                "text": evaluate_text(project, reference_lines),
                "timestamps": evaluate_timestamps(project, reference),
            }
            if not args.skip_exports:
                reports["mms"]["exports"] = export_all(project, "mms")

        overall = {
            "assets": [path.name for path in (TEST_MP4, TEST_TXT, TEST_ASS)],
            "audio_duration_seconds": duration,
            "backends": reports,
        }
        REPORT_JSON.write_text(
            json.dumps(overall, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n报告：{REPORT_JSON}")

        ok = all(
            report["text"]["ok"] and report["timestamps"]["ok"]
            for report in reports.values()
        )
        print("[PASS] 三文件双后端 E2E" if ok else "[FAIL] 指标未达门禁，详见 JSON")
        return 0 if ok else 1
    finally:
        manager.cleanup()



if __name__ == "__main__":
    raise SystemExit(main())
