"""tests/test_asr_duration_guard.py — ASR 时长上限守卫（>1200s 明确报错，不做自动切块）

背景：「>1200s 长音频自动切块」已被明确否决（无此需求），超限由守卫报错。
本文件钉死守卫行为，防止守卫被静默改动或删除：
1. 时长 > 1200s → 抛 ValueError（消息指引先用 FFmpeg 外部切片），
   且**不进入**模型激活/推理阶段（守卫在 prepare_audio 之后、using_asr 之前）
2. 恰好 1200s（边界）→ 正常单次推理，守卫不误伤
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest
import torch

from core import asr_engine as ae
from core.audio_io import AudioInfo
from core.constants import ASR_MAX_DURATION

pytestmark = pytest.mark.logic


# ─────────────────────────────────────────────────────────────
# 替身（本沙盒无模型/FFmpeg；保持「缝」类型形状与真机一致）
# ─────────────────────────────────────────────────────────────

class _Inputs(dict):
    """模拟 processor 返回的 BatchFeature：可 .to() 就地返回、可按键取张量。"""

    def to(self, *_args, **_kwargs):
        return self


class _FakeProc:
    def __init__(self, text: str = "边界时长识别文本。", lang: str = "Chinese"):
        self.text = text
        self.lang = lang
        self.calls: list[dict] = []

    def apply_transcription_request(self, *, audio: str, **kwargs):
        self.calls.append({"audio": audio, **kwargs})
        return _Inputs({
            "input_ids": torch.tensor([[1, 2, 3]]),
            "attention_mask": torch.tensor([[1, 1, 1]]),
        })

    def decode(self, gen_ids, *, return_format: str = "parsed"):
        assert return_format == "parsed"
        return [{"transcription": self.text, "language": self.lang}]


class _FakeModel:
    device = "cpu"
    dtype = torch.float32

    def generate(self, **_kwargs):
        return torch.tensor([[1, 2, 3, 10, 11]])


class _FakeModelManager:
    """只实现 transcribe() 用到的 using_asr() 一个口，并记录进入次数。"""

    def __init__(self):
        self.proc = _FakeProc()
        self.model = _FakeModel()
        self.enter_count = 0

    @contextmanager
    def using_asr(self, **_kwargs):
        self.enter_count += 1
        yield (self.proc, self.model)


def _fake_audio_info(duration: float) -> AudioInfo:
    return AudioInfo(
        path=Path("fake.wav"), sample_rate=16000, channels=1,
        duration=duration, num_frames=int(duration * 16000),
    )


# ─────────────────────────────────────────────────────────────

def _case_transcribe_honors_cancel_before_work():
    from core.task_control import TaskCancelled

    cfg = ae.TranscribeConfig(cancel_cb=lambda: True)
    with pytest.raises(TaskCancelled):
        ae.transcribe("unused.wav", model_manager=_FakeModelManager(), cfg=cfg)


def _case_transcribe_rejects_over_limit_audio():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ae, "prepare_audio",
                   lambda *a, **k: (Path("fake.wav"), _fake_audio_info(1200.1)))
        mp.setattr(ae, "align_full_text",
                   lambda **k: (_ for _ in ()).throw(AssertionError("守卫后不应走到对齐")))
        mm = _FakeModelManager()

        with pytest.raises(ValueError, match="切成 ≤ 20 分钟"):
            ae.transcribe("too_long.wav", model_manager=mm, cfg=ae.TranscribeConfig())

        assert mm.enter_count == 0        # 守卫生效：未激活 ASR 模型
        assert mm.proc.calls == []        # 更未发起任何推理
    finally:
        mp.undo()
    print("test_transcribe_rejects_over_limit_audio PASSED ✔")


def _case_transcribe_boundary_exactly_max_passes():
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ae, "prepare_audio",
                   lambda *a, **k: (Path("fake.wav"), _fake_audio_info(float(ASR_MAX_DURATION))))
        mp.setattr(ae, "align_full_text", lambda project, **k: project)
        mm = _FakeModelManager()

        project = ae.transcribe(
            "cache-or-vocals.wav",
            source_media_path="original-video.mp4",
            model_manager=mm,
            cfg=ae.TranscribeConfig(),
        )

        assert project.source_media_path == "original-video.mp4"
        assert project.audio_path == "fake.wav"
        assert project.video_path == "original-video.mp4"
        assert mm.enter_count == 1            # 恰好 1200s：守卫放行，单次推理
        assert len(mm.proc.calls) == 1
        assert len(project.sentences) == 1
        assert project.media_duration == pytest.approx(ASR_MAX_DURATION)
    finally:
        mp.undo()
    print("test_transcribe_boundary_exactly_max_passes PASSED ✔")


def test_asr_duration_guard_pack():
    """test_asr_duration_guard_pack：合并 3 个场景（断言逐条保留，见各 _case_*）。"""
    _case_transcribe_honors_cancel_before_work()
    _case_transcribe_rejects_over_limit_audio()
    _case_transcribe_boundary_exactly_max_passes()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
