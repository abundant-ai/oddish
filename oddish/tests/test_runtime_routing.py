from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dataclasses

import pytest

from harbor.models.environment_type import EnvironmentType

from oddish.runtime.ports import Capabilities, TpuSupport
from oddish.runtime.routing import (
    NoEligibleBackendError,
    allowed_cloud_environments,
    default_cloud_environment,
    select_backend,
)


class _StubTpuBackend:
    """Minimal TPU-capable backend for routing tests (no SDK, no registry)."""

    name = "gke"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            tpu=TpuSupport(types=("v5e", "v6e"), max_chips_per_host=8),
            private_registry_pull=False,
            network_egress="allow",
            persistent_volumes=False,
            streaming_logs=False,
            memory_snapshot_fork=False,
            cold_start="minutes",
        )


def test_allowed_cloud_environments_is_modal_and_daytona() -> None:
    assert allowed_cloud_environments() == frozenset(
        {EnvironmentType.MODAL, EnvironmentType.DAYTONA}
    )


def test_default_cloud_environment_gpu_routes_to_daytona() -> None:
    assert default_cloud_environment(requires_gpu=True) == EnvironmentType.DAYTONA


def test_default_cloud_environment_cpu_routes_to_daytona() -> None:
    assert default_cloud_environment(requires_gpu=False) == EnvironmentType.DAYTONA


def test_select_backend_gpu_picks_daytona() -> None:
    assert select_backend(requires_gpu=True).name == "daytona"


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


def test_capabilities_tpu_defaults_none() -> None:
    # A backend that omits the TPU block reports no TPU support.
    caps = Capabilities(
        gpu=None,
        private_registry_pull=False,
        network_egress="allow",
        persistent_volumes=False,
        streaming_logs=False,
        memory_snapshot_fork=False,
        cold_start="seconds",
    )
    assert caps.tpu is None


def test_tpu_support_is_frozen() -> None:
    tpu = TpuSupport(types=("v5e", "v6e"), max_chips_per_host=8)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tpu.max_chips_per_host = 4  # type: ignore[misc]


def test_requires_tpu_skips_non_tpu_backends(monkeypatch) -> None:
    # The default backends (Daytona, Modal) have no TPU support, so a TPU
    # requirement finds nothing eligible rather than falling back to CPU/GPU.
    import oddish.runtime.routing as routing
    from oddish.runtime.backends.daytona import DaytonaBackend
    from oddish.runtime.backends.modal import ModalBackend

    monkeypatch.setattr(
        routing, "ordered_backends", lambda: [DaytonaBackend(), ModalBackend()]
    )
    with pytest.raises(NoEligibleBackendError):
        routing.select_backend(requires_tpu=True)


def test_requires_tpu_selects_tpu_capable_backend(monkeypatch) -> None:
    import oddish.runtime.routing as routing
    from oddish.runtime.backends.daytona import DaytonaBackend
    from oddish.runtime.backends.modal import ModalBackend

    monkeypatch.setattr(
        routing,
        "ordered_backends",
        lambda: [DaytonaBackend(), ModalBackend(), _StubTpuBackend()],
    )
    assert routing.select_backend(requires_tpu=True).name == "gke"


def test_default_cloud_environment_tpu_routes_to_tpu_backend(monkeypatch) -> None:
    import oddish.runtime.routing as routing
    from oddish.runtime.backends.daytona import DaytonaBackend
    from oddish.runtime.backends.modal import ModalBackend

    monkeypatch.setattr(
        routing,
        "ordered_backends",
        lambda: [DaytonaBackend(), ModalBackend(), _StubTpuBackend()],
    )
    assert routing.default_cloud_environment(requires_tpu=True) == EnvironmentType.GKE


def test_requires_tpu_does_not_disturb_cpu_and_gpu_defaults(monkeypatch) -> None:
    # A TPU-capable backend registered last must never steal plain CPU or GPU
    # work from the cheaper backends ahead of it.
    import oddish.runtime.routing as routing
    from oddish.runtime.backends.daytona import DaytonaBackend
    from oddish.runtime.backends.modal import ModalBackend

    monkeypatch.setattr(
        routing,
        "ordered_backends",
        lambda: [DaytonaBackend(), ModalBackend(), _StubTpuBackend()],
    )
    assert routing.select_backend().name == "daytona"
    assert routing.select_backend(requires_gpu=True).name == "daytona"
