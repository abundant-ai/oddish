"""Capability negotiation + the cloud-environment default.

The negotiation iterates ``ordered_backends()`` (cheap-first) and returns the
first backend whose capabilities satisfy the requirements: plain CPU →
Daytona, GPU → Thunder when the deployment enables it (otherwise Modal),
private registry → Modal. A GPU request also carries the task's acceptable
``gpu_types`` (``None`` = any), so a backend that only launches named
accelerators (Thunder) is skipped for a task it would reject at launch.
``default_cloud_environment`` is the facade the backend cloud policy calls; the
CLI sends what the task needs (``requires_gpu``, ``gpu_types``,
``registry_auth``) and leaves this choice to the deployment."""

from __future__ import annotations

from collections.abc import Sequence

from harbor.models.environment_type import EnvironmentType

from oddish.runtime.ports import ExecutionBackend
from oddish.runtime.registry import ordered_backends


class NoEligibleBackendError(RuntimeError):
    """Raised when no registered backend satisfies the required capabilities."""


def allowed_cloud_environments() -> frozenset[EnvironmentType]:
    return frozenset(EnvironmentType(b.name) for b in ordered_backends())


def select_backend(
    *,
    requires_gpu: bool = False,
    requires_private_registry: bool = False,
    requires_tpu: bool = False,
    gpu_types: Sequence[str] | None = None,
) -> ExecutionBackend:
    for backend in ordered_backends():
        caps = backend.capabilities()
        if requires_gpu and (caps.gpu is None or not caps.gpu.serves(gpu_types)):
            continue
        if requires_private_registry and not caps.private_registry_pull:
            continue
        if requires_tpu and caps.tpu is None:
            continue
        return backend
    raise NoEligibleBackendError(
        f"No backend supports requires_gpu={requires_gpu}, "
        f"gpu_types={list(gpu_types) if gpu_types else None}, "
        f"requires_private_registry={requires_private_registry}, "
        f"requires_tpu={requires_tpu}"
    )


def default_cloud_environment(
    *,
    requires_gpu: bool = False,
    requires_private_registry: bool = False,
    requires_tpu: bool = False,
    gpu_types: Sequence[str] | None = None,
) -> EnvironmentType:
    """The cloud default via capability negotiation: TPU → GKE, GPU → Thunder
    when enabled and the task names one accelerator it offers (else Modal),
    private-registry pull → Modal, else Daytona."""
    return EnvironmentType(
        select_backend(
            requires_gpu=requires_gpu,
            requires_private_registry=requires_private_registry,
            requires_tpu=requires_tpu,
            gpu_types=gpu_types,
        ).name
    )
