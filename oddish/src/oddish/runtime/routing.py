"""Capability negotiation + the cloud-environment default.

The negotiation iterates ``ordered_backends()`` (cheap-first) and returns the
first backend whose capabilities satisfy the requirements: plain CPU →
Daytona, GPU → Thunder when the deployment enables it (otherwise Modal),
private registry → Modal. ``default_cloud_environment`` is the facade the
backend cloud policy calls; the CLI sends what the task needs (``requires_gpu``,
``registry_auth``) and leaves this choice to the deployment."""

from __future__ import annotations

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
) -> ExecutionBackend:
    for backend in ordered_backends():
        caps = backend.capabilities()
        if requires_gpu and caps.gpu is None:
            continue
        if requires_private_registry and not caps.private_registry_pull:
            continue
        if requires_tpu and caps.tpu is None:
            continue
        return backend
    raise NoEligibleBackendError(
        f"No backend supports requires_gpu={requires_gpu}, "
        f"requires_private_registry={requires_private_registry}, "
        f"requires_tpu={requires_tpu}"
    )


def default_cloud_environment(
    *,
    requires_gpu: bool = False,
    requires_private_registry: bool = False,
    requires_tpu: bool = False,
) -> EnvironmentType:
    """The cloud default via capability negotiation: TPU → GKE, GPU → Thunder
    when enabled (else Modal), private-registry pull → Modal, else Daytona."""
    return EnvironmentType(
        select_backend(
            requires_gpu=requires_gpu,
            requires_private_registry=requires_private_registry,
            requires_tpu=requires_tpu,
        ).name
    )
