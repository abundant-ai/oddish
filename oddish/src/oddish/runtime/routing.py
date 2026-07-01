"""Capability negotiation + the cloud-environment default.

The negotiation reproduces today's outcome (GPU/private-registry → Modal,
plain CPU → Daytona) by iterating ``ordered_backends()`` (cheap-first) and
returning the first backend whose capabilities satisfy the requirements.
``default_cloud_environment`` is the behavior-preserving facade the CLI and
the backend cloud policy call."""

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
) -> ExecutionBackend:
    for backend in ordered_backends():
        caps = backend.capabilities()
        if requires_gpu and caps.gpu is None:
            continue
        if requires_private_registry and not caps.private_registry_pull:
            continue
        return backend
    raise NoEligibleBackendError(
        f"No backend supports requires_gpu={requires_gpu}, "
        f"requires_private_registry={requires_private_registry}"
    )


def default_cloud_environment(*, requires_gpu: bool) -> EnvironmentType:
    """The cloud default: GPU → Modal, else Daytona (via capability negotiation)."""
    return EnvironmentType(select_backend(requires_gpu=requires_gpu).name)
