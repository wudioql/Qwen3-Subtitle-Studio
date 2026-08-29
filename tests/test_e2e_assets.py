"""三文件 E2E 参考资源与纯评估逻辑（不加载模型/FFmpeg）。"""
from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401

import pytest

pytestmark = pytest.mark.logic


def _case_e2e_three_assets_are_consistent_and_perfect_self_eval():
    from e2e import e2e_short_talk as e2e

    lines, reference = e2e.load_reference_assets()
    assert [path.name for path in (e2e.TEST_MP4, e2e.TEST_TXT, e2e.TEST_ASS)] == [
        "test-short-talk.mp4",
        "test-short-talk.txt",
        "test-short-talk.ass",
    ]
    assert len(lines) == len(reference.sentences) == 6
    assert all(sentence.words for sentence in reference.sentences)

    text = e2e.evaluate_text(reference, lines)
    timestamps = e2e.evaluate_timestamps(reference, reference)
    assert text["cer"] == 0 and text["ok"]
    assert timestamps["match_coverage"] == 1.0
    assert timestamps["start_error_p95_ms"] == 0
    assert timestamps["end_error_p95_ms"] == 0
    assert timestamps["ok"]


def _case_e2e_reference_exports_all_eleven_outputs(monkeypatch, tmp_path):
    from e2e import e2e_short_talk as e2e

    _lines, reference = e2e.load_reference_assets()
    monkeypatch.setattr(e2e, "EXPORT_ROOT", tmp_path)
    files = e2e.export_all(reference, "reference")
    assert len(files) == 11
    assert set(files) == {
        "sentence.srt", "sentence.vtt", "sentence.ass", "standard.lrc",
        "word.srt", "word.vtt", "word.split.ass", "word.t.ass",
        "enhanced.lrc", "karaoke.ass", "karaoke.applied.ass",
    }
    applied = (tmp_path / "reference" / "karaoke.applied.ass").read_text(encoding="utf-8")
    assert ",karaoke,{\\kf" in applied and ",fx," in applied


def _case_e2e_backend_cli_defaults_to_both():
    from e2e import e2e_short_talk as e2e

    assert e2e.parse_args([]).backend == "both"
    assert e2e.parse_args(["--backend", "qwen"]).backend == "qwen"
    assert e2e.parse_args(["--backend", "mms", "--skip-exports"]).skip_exports


def test_e2e_assets_pack(monkeypatch, tmp_path):
    """test_e2e_assets_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_e2e_three_assets_are_consistent_and_perfect_self_eval()
    _case_e2e_reference_exports_all_eleven_outputs(monkeypatch=monkeypatch, tmp_path=tmp_path)
    _case_e2e_backend_cli_defaults_to_both()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
