from __future__ import annotations

import importlib
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

run_module = importlib.import_module("oddish.cli.run")


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


def test_allowed_cloud_environments_includes_archil() -> None:
    assert allowed_cloud_environments() == frozenset(
        {EnvironmentType.MODAL, EnvironmentType.DAYTONA, EnvironmentType.ARCHIL}
    )


def test_archil_is_a_hosted_passthrough_environment() -> None:
    assert EnvironmentType.ARCHIL in run_module._HOSTED_PASSTHROUGH_ENVIRONMENTS


def test_default_cloud_environment_gpu_routes_to_modal_without_thunder() -> None:
    # Thunder is not registered in the test environment, so Modal remains the
    # only GPU-capable backend in the default registry.
    assert default_cloud_environment(requires_gpu=True) == EnvironmentType.MODAL


def test_registered_thunder_wins_gpu_negotiation_but_not_cpu_or_registry(
    monkeypatch,
) -> None:
    # The registry places Thunder between Daytona and Modal; cheap-first
    # negotiation must hand GPU work to it while plain CPU stays on Daytona
    # and private-registry pulls (which Thunder lacks) still go to Modal.
    import oddish.runtime.routing as routing
    from oddish.runtime.backends.daytona import DaytonaBackend
    from oddish.runtime.backends.modal import ModalBackend
    from oddish.runtime.backends.thunder import ThunderBackend

    monkeypatch.setattr(
        routing,
        "ordered_backends",
        lambda: [DaytonaBackend(), ThunderBackend(), ModalBackend()],
    )
    assert routing.select_backend().name == "daytona"
    # Thunder launches only a task naming exactly one accelerator it offers
    # (Harbor's Thunder environment rejects anything else at construction),
    # so those are the GPU requests it wins; A100 memory variants count as A100.
    for gpu_types in (["H100"], ["a100-80gb"], ["A6000"]):
        assert (
            routing.select_backend(requires_gpu=True, gpu_types=gpu_types).name
            == "thunder"
        )
    # "Any GPU", several acceptable types, or one Thunder lacks go to Modal,
    # which takes any request and validates the name itself at launch.
    for gpu_types in (None, [], ["H100", "A100"], ["L4"]):
        assert (
            routing.select_backend(requires_gpu=True, gpu_types=gpu_types).name
            == "modal"
        )
    assert routing.select_backend(requires_private_registry=True).name == "modal"
    assert (
        routing.default_cloud_environment(requires_gpu=True, gpu_types=["H100"])
        == EnvironmentType.THUNDER
    )
    # A GPU task that must pull from a private registry cannot run on Thunder.
    assert (
        routing.default_cloud_environment(
            requires_gpu=True, gpu_types=["H100"], requires_private_registry=True
        )
        == EnvironmentType.MODAL
    )


def test_gpu_support_serves_any_request_unless_it_needs_a_named_accelerator() -> None:
    from oddish.runtime.ports import GpuSupport

    open_pool = GpuSupport(accelerators=("H100",), max_count=8)
    named_only = GpuSupport(
        accelerators=("A100", "H100"), max_count=8, requires_named_accelerator=True
    )
    for gpu_types in (None, [], ["L4"], ["H100", "L4"]):
        assert open_pool.serves(gpu_types)
    assert named_only.serves(["h100"])
    assert named_only.serves(["A100-40GB"])
    for gpu_types in (None, [], ["L4"], ["H100", "A100"]):
        assert not named_only.serves(gpu_types)


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
    # Non-TPU backends must not satisfy a TPU requirement by falling back to
    # CPU or GPU.
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
    assert routing.select_backend(requires_gpu=True).name == "modal"
