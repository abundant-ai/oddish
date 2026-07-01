from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from harbor.models.environment_type import EnvironmentType

from oddish.runtime.routing import (
    NoEligibleBackendError,
    allowed_cloud_environments,
    default_cloud_environment,
    select_backend,
)


def test_allowed_cloud_environments_is_modal_and_daytona() -> None:
    assert allowed_cloud_environments() == frozenset(
        {EnvironmentType.MODAL, EnvironmentType.DAYTONA}
    )


def test_default_cloud_environment_gpu_routes_to_modal() -> None:
    assert default_cloud_environment(requires_gpu=True) == EnvironmentType.MODAL


def test_default_cloud_environment_cpu_routes_to_daytona() -> None:
    assert default_cloud_environment(requires_gpu=False) == EnvironmentType.DAYTONA


def test_select_backend_gpu_skips_daytona_picks_modal() -> None:
    assert select_backend(requires_gpu=True).name == "modal"


def test_select_backend_private_registry_picks_modal() -> None:
    # Closed-internet / private-registry need forces Modal (Daytona can't pull).
    assert select_backend(requires_private_registry=True).name == "modal"


def test_select_backend_plain_cpu_picks_daytona() -> None:
    assert select_backend().name == "daytona"


def test_select_backend_raises_when_no_eligible_backend(monkeypatch) -> None:
    # With nothing registered, negotiation has nothing to return -> fail fast.
    import oddish.runtime.routing as routing

    monkeypatch.setattr(routing, "ordered_backends", lambda: [])
    with pytest.raises(NoEligibleBackendError):
        routing.select_backend(requires_gpu=True)
