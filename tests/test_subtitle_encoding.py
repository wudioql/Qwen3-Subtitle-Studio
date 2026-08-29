"""tests.test_subtitle_encoding — 测试字幕文件编码自适应嗅探

验证：
1. UTF-8 带 BOM 的 SRT 正常解析
2. GBK / GB18030 编码的 SRT 正常解析，无乱码
3. 标准 UTF-8 的 LRC / VTT 正常解析
"""

import tempfile
from pathlib import Path

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

from subs.subtitle_io import parse_subtitle_or_text
import pytest

pytestmark = pytest.mark.logic


def _case_utf8_bom_srt():
    content = "\ufeff1\n00:00:01,000 --> 00:00:03,000\n你好，世界！\n"
    with tempfile.NamedTemporaryFile("wb", suffix=".srt", delete=False) as f:
        f.write(content.encode("utf-8-sig"))
        tmp_path = Path(f.name)
    try:
        sents = parse_subtitle_or_text(tmp_path)
        assert len(sents) == 1
        assert sents[0].text == "你好，世界！"
        assert abs(sents[0].start_time - 1.0) < 1e-3
        print("test_utf8_bom_srt OK ✔")
    finally:
        tmp_path.unlink()


def _case_gb18030_srt():
    content = "1\n00:00:02,500 --> 00:00:05,000\n这是一个GBK编码的中文字幕\n"
    with tempfile.NamedTemporaryFile("wb", suffix=".srt", delete=False) as f:
        f.write(content.encode("gb18030"))
        tmp_path = Path(f.name)
    try:
        sents = parse_subtitle_or_text(tmp_path)
        assert len(sents) == 1
        assert sents[0].text == "这是一个GBK编码的中文字幕"
        assert abs(sents[0].start_time - 2.5) < 1e-3
        print("test_gb18030_srt OK ✔")
    finally:
        tmp_path.unlink()


# ═════════════ SRT/VTT 多行 cue 拼接（CJK 行界无空格 / 拉丁保留空格） ═════════════

from subs.subtitle_io import _parse_srt, _parse_vtt  # noqa: E402


def _case_multiline_cue_join_rules():
    """SRT/VTT 多行 cue：CJK 行界无空格、拉丁行界保留空格。"""
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n青紫色的风\n掠过指尖\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nhello brave\nnew world\n"
    )
    entries = _parse_srt(srt)
    assert entries[0][2] == "青紫色的风掠过指尖"
    assert entries[1][2] == "hello brave new world"

    vtt = (
        "WEBVTT\n\n"
        "00:01.000 --> 00:02.000\n桜の花が\n咲きました\n\n"
        "00:03.000 --> 00:04.000\nsay it\nout loud\n"
    )
    ventries = _parse_vtt(vtt)
    assert ventries[0][2] == "桜の花が咲きました"
    assert ventries[1][2] == "say it out loud"


def _case_import_dirty_contract_and_plain_text_preservation(tmp_path):
    """所有外部导入统一标脏；显式 TXT 不得丢数字或箭头行。"""
    fixtures = {
        "sample.txt": "2024\nA --> B\n",
        "sample.srt": "1\n00:00:01,000 --> 00:00:02,000\nSRT line\n",
        "sample.vtt": "WEBVTT\n\n00:01.000 --> 00:02.000\nVTT line\n",
        "sample.lrc": "[00:01.00]LRC line\n",
        "sample.ass": (
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\an5\\KF50}ASS line\n"
        ),
    }
    parsed = {}
    for name, content in fixtures.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        parsed[name] = parse_subtitle_or_text(path, media_duration=4.0)
        assert parsed[name] and all(sentence.is_dirty for sentence in parsed[name])

    assert [sentence.text for sentence in parsed["sample.txt"]] == ["2024", "A --> B"]
    assert parsed["sample.ass"][0].text == "ASS line"
    assert parsed["sample.ass"][0].words[0].text == "ASS line"


def test_encoding_detect_pack():
    """test_encoding_detect_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_utf8_bom_srt()
    _case_gb18030_srt()


def test_encoding_cues_pack(tmp_path):
    """test_encoding_cues_pack：合并 2 个场景（断言逐条保留，见各 _case_*）。"""
    _case_multiline_cue_join_rules()
    _case_import_dirty_contract_and_plain_text_preservation(tmp_path=tmp_path)

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
