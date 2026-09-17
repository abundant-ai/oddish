from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

run_module = importlib.import_module("oddish.cli.run")


def test_hosted_passthrough_import_contract_allows_harbor_without_thunder() -> None:
    class PublicHarborEnvironmentType:
        MODAL = object()
        DAYTONA = object()
        EC2 = object()
        GKE = object()

    environments = run_module._hosted_passthrough_environments(
        PublicHarborEnvironmentType
    )

    assert environments == {
        PublicHarborEnvironmentType.MODAL,
        PublicHarborEnvironmentType.DAYTONA,
        PublicHarborEnvironmentType.EC2,
        PublicHarborEnvironmentType.GKE,
    }


def test_thunder_is_hosted_passthrough_when_harbor_exposes_it() -> None:
    assert EnvironmentType.THUNDER in run_module._HOSTED_PASSTHROUGH_ENVIRONMENTS


def test_hosted_normalization_preserves_explicit_thunder() -> None:
    assert (
        run_module._normalize_hosted_environment(
            EnvironmentType.THUNDER, is_modal_api=True
        )
        is EnvironmentType.THUNDER
    )


def test_hosted_normalization_still_coerces_local_only_environment() -> None:
    assert (
        run_module._normalize_hosted_environment(
            EnvironmentType.DOCKER, is_modal_api=True
        )
        is EnvironmentType.MODAL
    )


def test_cli_leaves_gpu_backend_choice_to_the_deployment() -> None:
    # The CLI cannot know whether the target deployment registered Thunder or
    # whether Modal must serve a private-registry pull, so it names no GPU
    # backend; the hosted policy negotiates from ``requires_gpu``. The CLI's
    # own ODDISH_THUNDER_ENABLED must not leak into that decision either way.
    code = """
from oddish.cli.run import _default_cloud_environment_for_task, _task_requires_gpu
print(_default_cloud_environment_for_task(None, override_gpus=1))
print(_task_requires_gpu(None, override_gpus=1))
"""
    for thunder_enabled in ("false", "true"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "ODDISH_THUNDER_ENABLED": thunder_enabled},
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip().splitlines()[-2:] == ["None", "True"]


def test_sweep_payload_carries_the_gpu_requirement_only_when_present() -> None:
    from oddish.cli.api import build_sweep_payload

    common = dict(
        task_id="t", configs=[{"agent": "nop"}], environment=None, user=None,
        priority="low", experiment_id=None,
    )
    assert build_sweep_payload(**common, requires_gpu=True)["requires_gpu"] is True
    assert "requires_gpu" not in build_sweep_payload(**common)
    assert "environment" not in build_sweep_payload(**common, requires_gpu=True)
    # The acceptable types ride along with the requirement, never alone.
    typed = build_sweep_payload(**common, requires_gpu=True, gpu_types=["H100"])
    assert typed["gpu_types"] == ["H100"]
    assert "gpu_types" not in build_sweep_payload(**common, requires_gpu=True)
    assert "gpu_types" not in build_sweep_payload(**common, gpu_types=["H100"])


def test_task_gpu_types_come_from_task_toml(tmp_path) -> None:
    # Read straight from task.toml like the GPU count; absent or empty means
    # any type, and an unreadable task never blocks the submit.
    task_toml = tmp_path / "task.toml"
    task_toml.write_text('[environment]\ngpus = 1\ngpu_types = ["H100", "A100"]\n')
    assert run_module._task_config_gpu_types(tmp_path) == ["H100", "A100"]
    task_toml.write_text("[environment]\ngpus = 1\n")
    assert run_module._task_config_gpu_types(tmp_path) is None
    task_toml.write_text("[environment]\ngpus = 1\ngpu_types = []\n")
    assert run_module._task_config_gpu_types(tmp_path) is None
    task_toml.unlink()
    assert run_module._task_config_gpu_types(tmp_path) is None


def test_explicit_numinous_gpu_opt_in_keeps_its_priority(monkeypatch):
    from oddish.config import settings
    monkeypatch.setattr(settings, "numinous_enabled", True)
    monkeypatch.setattr(settings, "numinous_gpu_enabled", True)
    assert run_module._default_cloud_environment_for_task(None, override_gpus=1) is EnvironmentType.NUMINOUS
    monkeypatch.setattr(settings, "numinous_gpu_enabled", False)
    assert run_module._default_cloud_environment_for_task(None, override_gpus=1) is None
    assert run_module._default_cloud_environment_for_task(None, override_gpus=0) is EnvironmentType.NUMINOUS
