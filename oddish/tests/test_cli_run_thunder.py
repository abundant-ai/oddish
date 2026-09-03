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


def test_enabled_thunder_does_not_steal_implicit_cli_gpu_routing() -> None:
    code = """
from oddish.cli.run import _default_cloud_environment_for_task
print(_default_cloud_environment_for_task(None, override_gpus=1).value)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "ODDISH_THUNDER_ENABLED": "true"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "modal"
