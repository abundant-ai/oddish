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


def test_cli_gpu_default_is_thunder_regardless_of_local_thunder_setting() -> None:
    # The CLI's own ODDISH_THUNDER_ENABLED is irrelevant: the hosted API owns
    # the policy check, so the client routes GPU work to Thunder either way.
    code = """
from oddish.cli.run import _default_cloud_environment_for_task
print(_default_cloud_environment_for_task(None, override_gpus=1).value)
"""
    for thunder_enabled in ("false", "true"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "ODDISH_THUNDER_ENABLED": thunder_enabled},
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip().splitlines()[-1] == "thunder"


def test_cli_gpu_default_falls_back_to_modal_without_thunder_member(monkeypatch):
    # Public Harbor releases may lack the fork-only THUNDER member.
    class PublicHarborEnvironmentType:
        MODAL = object()
        DAYTONA = object()
        GKE = object()
        NUMINOUS = object()

    monkeypatch.setattr(run_module, "EnvironmentType", PublicHarborEnvironmentType)
    assert (
        run_module._default_cloud_environment_for_task(None, override_gpus=1)
        is PublicHarborEnvironmentType.MODAL
    )


def test_explicit_numinous_gpu_opt_in_keeps_its_priority(monkeypatch):
    from oddish.config import settings
    monkeypatch.setattr(settings, "numinous_enabled", True)
    monkeypatch.setattr(settings, "numinous_gpu_enabled", True)
    assert run_module._default_cloud_environment_for_task(None, override_gpus=1) is EnvironmentType.NUMINOUS
    monkeypatch.setattr(settings, "numinous_gpu_enabled", False)
    assert run_module._default_cloud_environment_for_task(None, override_gpus=1) is EnvironmentType.THUNDER
    assert run_module._default_cloud_environment_for_task(None, override_gpus=0) is EnvironmentType.NUMINOUS
