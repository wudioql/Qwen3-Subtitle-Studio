"""依赖声明、授权口径与部署文档的一致性门禁（纯逻辑）。"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.logic


IMPORT_TO_DIST = {
    "PySide6": "pyside6",
    "librosa": "librosa",
    "mpv": "python-mpv",
    "numpy": "numpy",
    "onnxruntime": "onnxruntime",
    "packaging": "packaging",
    "pyqtgraph": "pyqtgraph",
    "qfluentwidgets": "pyside6-fluent-widgets",
    "soundfile": "soundfile",
    "torch": "torch",
    "transformers": "transformers",
    "uroman": "uroman",
}


def _requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)", line)
        if match:
            names.add(match.group(1).lower().replace("_", "-"))
    return names


def _direct_external_imports() -> set[str]:
    roots = ["core", "subs", "ui", "workers", "tools"]
    paths = [PROJECT_ROOT / "main.py"]
    for root in roots:
        paths.extend((PROJECT_ROOT / root).rglob("*.py"))
    imported: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
    return imported


def test_direct_runtime_imports_have_explicit_requirements():
    requirements = _requirement_names(PROJECT_ROOT / "requirements.txt")
    imported = _direct_external_imports()
    expected = {dist for module, dist in IMPORT_TO_DIST.items() if module in imported}
    missing = sorted(expected - requirements)
    assert not missing, f"项目直接 import 但 requirements 未显式声明：{missing}"
    assert "torch" in requirements
    assert "soundfile" in requirements


def test_license_and_deployment_wording_is_not_self_contradictory():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    deployment = (PROJECT_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "GPL 本身不禁止商业使用" in readme
    assert "不得再写“GPLv3 本身仅非商业”" in agents
    assert "GPL 本身允许商业使用" in notices
    assert "python-mpv" in notices and "GPL-2.0-or-later" in notices
    assert "uroman" in notices and "Apache-2.0" in notices
    assert "onnxruntime / onnxruntime-gpu" in notices and ">=1.27,<2" in notices
    assert "仅未来 mpv 嵌入需要；当前仍为占位" not in deployment
    assert "libmpv-2.dll | ~50 MB" not in deployment
    assert "用户已确认本机 `libmpv` 测试通过，可以正常使用" in deployment
    assert "当前没有明确的硬字幕烧录或 Nuitka 分发规划" in deployment
