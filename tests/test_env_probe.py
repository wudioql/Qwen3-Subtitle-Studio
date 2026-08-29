"""tests/test_env_probe.py — 环境探针纯逻辑：模型可选顺序与 CLI 参数契约。"""

from __future__ import annotations

from _bootstrap import PROJECT_ROOT  # noqa: F401  (直跑三件套：sys.path / Qt 离屏 / 偏好隔离)

import pytest

pytestmark = pytest.mark.logic


def test_env_probe_model_requirement_and_cli(tmp_path):
    from tools import env_check_native_api as probe

    missing = tmp_path / "missing-model"
    assert probe._check_local_model(missing, "test", required=False)
    assert not probe._check_local_model(missing, "test", required=True)

    args = probe.parse_args(["--require-models", "--strict-target"])
    assert args.require_models and args.strict_target
