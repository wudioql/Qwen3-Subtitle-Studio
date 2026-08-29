"""Qwen3 Subtitle Studio 环境探针：默认不要求权重、不实例化完整模型。

用法：
    python tools/env_check_native_api.py
        基础依赖 + transformers 原生类/API；模型目录不存在时明确 SKIP。
    python tools/env_check_native_api.py --require-models
        额外要求本地模型配置/Processor 可加载（仍不加载 safetensors）。
    python tools/env_check_native_api.py --strict-target --require-models
        Windows 目标机发布前检查：Python 3.12、CUDA、flash-attn、FFmpeg、模型。
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import os
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
os.environ["PATH"] = str(_PROJECT_ROOT) + os.pathsep + os.environ.get("PATH", "")

from core.constants import ALIGNER_MODEL_PATH, ASR_MODEL_PATH  # noqa: E402


def section(title: str) -> None:
    print(f"\n{'=' * 64}\n  {title}\n{'=' * 64}")


def result(ok: bool, success: str, failure: str) -> bool:
    print(f"  {'✅' if ok else '❌'} {success if ok else failure}")
    return ok


def note(text: str) -> None:
    print(f"  ℹ️  {text}")


def _import_required_modules() -> bool:
    required = {
        "torch": "PyTorch",
        "transformers": "Transformers",
        "accelerate": "Accelerate",
        "numpy": "NumPy",
        "soundfile": "SoundFile",
        "librosa": "librosa",
        "onnxruntime": "ONNX Runtime",
        "uroman": "uroman",
        "PySide6": "PySide6",
        "qfluentwidgets": "PySide6-Fluent-Widgets",
        "pyqtgraph": "pyqtgraph",
    }
    ok = True
    for module_name, label in required.items():
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "")
            print(f"  ✅ {label}{f' {version}' if version else ''}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  ❌ {label} 导入失败：{type(exc).__name__}: {exc}")
    return ok


def _check_native_transformers_api() -> bool:
    from packaging.version import Version
    import transformers
    from transformers import (
        AutoModelForMultimodalLM,
        AutoModelForTokenClassification,
        AutoProcessor,
    )

    ok = result(
        Version(transformers.__version__) >= Version("5.13.0"),
        f"transformers {transformers.__version__} ≥ 5.13",
        f"transformers {transformers.__version__} < 5.13",
    )
    _ = (AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor)

    expected_classes = (
        "Qwen3ASRConfig",
        "Qwen3ASRProcessor",
        "Qwen3ASRForConditionalGeneration",
        "Qwen3ASRForTokenClassification",
    )
    classes = {}
    for name in expected_classes:
        try:
            classes[name] = getattr(transformers, name)
            print(f"  ✅ transformers.{name}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  ❌ transformers.{name} 不可用：{exc}")

    processor_cls = classes.get("Qwen3ASRProcessor")
    if processor_cls is not None:
        for method in (
            "apply_transcription_request",
            "prepare_forced_aligner_inputs",
            "decode",
            "decode_forced_alignment",
        ):
            ok &= result(
                hasattr(processor_cls, method),
                f"Qwen3ASRProcessor.{method} 可用",
                f"Qwen3ASRProcessor 缺少 {method}",
            )
        if hasattr(processor_cls, "decode"):
            signature = inspect.signature(processor_cls.decode)
            ok &= result(
                "return_format" in signature.parameters,
                "Processor.decode 支持 return_format",
                "Processor.decode 缺少 return_format",
            )
    return ok


def _check_local_model(path: Path, label: str, *, required: bool) -> bool:
    if not path.is_dir():
        if required:
            print(f"  ❌ {label} 模型目录不存在：{path}")
            return False
        print(f"  ⏭️  {label} 模型未下载，跳过本地配置/Processor：{path}")
        return True

    try:
        from transformers import AutoConfig, AutoProcessor

        config = AutoConfig.from_pretrained(str(path), local_files_only=True)
        architectures = list(getattr(config, "architectures", None) or [])
        if not architectures:
            raise ValueError("config.architectures 为空")
        processor = AutoProcessor.from_pretrained(str(path), local_files_only=True)
        print(f"  ✅ {label} config={architectures}；Processor={type(processor).__name__}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ {label} 本地配置/Processor 检查失败：{type(exc).__name__}: {exc}")
        return False


def _check_target_runtime(*, strict: bool) -> bool:
    ok = True
    py_ok = sys.version_info[:2] == (3, 12)
    if strict:
        ok &= result(sys.platform == "win32", "Windows 目标系统", f"目标要求 Windows，当前 {sys.platform}")
        ok &= result(py_ok, "Python 3.12", f"目标环境要求 Python 3.12，当前 {sys.version.split()[0]}")
    else:
        note(f"Python {sys.version.split()[0]}（--strict-target 时要求 3.12）")

    try:
        import torch

        cuda_ok = bool(torch.cuda.is_available())
        cuda_text = f"torch={torch.__version__}, CUDA={cuda_ok}"
    except Exception as exc:  # noqa: BLE001
        cuda_ok = False
        cuda_text = f"torch 导入失败：{exc}"
    if strict:
        ok &= result(cuda_ok, cuda_text, f"目标 CUDA 不可用：{cuda_text}")
    else:
        note(cuda_text)

    flash_ok = importlib.util.find_spec("flash_attn") is not None
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    if strict:
        ok &= result(flash_ok, "flash_attn 已安装", "flash_attn 未安装")
        ok &= result(ffmpeg_ok, "FFmpeg 在 PATH", "FFmpeg 不在 PATH")
    else:
        note(f"flash_attn={'可用' if flash_ok else '未安装'}；FFmpeg={'可用' if ffmpeg_ok else '未找到'}")

    forbidden = [name for name in ("qwen_asr", "qwen_audio") if importlib.util.find_spec(name)]
    ok &= result(
        not forbidden,
        "未发现 qwen-asr/qwen-audio 冲突包",
        f"发现冲突包：{', '.join(forbidden)}；请卸载",
    )
    return ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-models", action="store_true", help="模型目录缺失时视为失败")
    parser.add_argument("--strict-target", action="store_true", help="启用 Windows 目标运行时严格门禁")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ok = True

    section("1. Python 依赖导入")
    ok &= _import_required_modules()

    section("2. transformers 原生 Qwen3-ASR/Aligner API（零权重实例化）")
    try:
        ok &= _check_native_transformers_api()
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  ❌ 原生 API 检查失败：{type(exc).__name__}: {exc}")

    section("3. 本地模型配置（可选）")
    ok &= _check_local_model(ASR_MODEL_PATH, "ASR", required=args.require_models)
    ok &= _check_local_model(ALIGNER_MODEL_PATH, "Aligner", required=args.require_models)

    section("4. 目标运行时")
    ok &= _check_target_runtime(strict=args.strict_target)

    section("总结")
    if ok:
        print("  🎉 环境探针通过；未执行任何完整模型实例化或权重加载。")
        return 0
    print("  ⚠️  环境探针未通过，请处理上方 ❌ 项。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
