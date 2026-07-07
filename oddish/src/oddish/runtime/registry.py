"""Name → ExecutionBackend resolution + cheap-first ordering.

``ordered_backends()`` returns Daytona before Modal so capability negotiation
(routing.py) picks the cheap CPU backend by default and only escalates to
Modal when a capability (GPU, private-registry pull) requires it."""

from __future__ import annotations

from oddish.config import settings
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.gke import GkeBackend
from oddish.runtime.backends.modal import ModalBackend
from oddish.runtime.ports import ExecutionBackend

# Singleton instances; backends are stateless w.r.t. trial dispatch.
_MODAL = ModalBackend()
_DAYTONA = DaytonaBackend()

REGISTERED_BACKENDS: dict[str, ExecutionBackend] = {
    _DAYTONA.name: _DAYTONA,
    _MODAL.name: _MODAL,
}

# GKE joins only when a cluster is configured, and always AFTER Modal so
# cheap-first negotiation never hands non-TPU work to it. Installs without GKE
# config keep the exact Daytona/Modal set.
if settings.gke_cluster_name:
    _GKE = GkeBackend()
    REGISTERED_BACKENDS[_GKE.name] = _GKE


def get_backend(name: str | None) -> ExecutionBackend | None:
    """Resolve a backend by provider name (case-insensitive); None if unknown."""
    if not name:
        return None
    return REGISTERED_BACKENDS.get(name.lower())


def ordered_backends() -> list[ExecutionBackend]:
    """Backends in cheap-first order: Daytona (CPU) then Modal (GPU/private).

    Sourced from ``REGISTERED_BACKENDS`` (insertion-ordered cheap-first) so the
    resolution set and the routing order never desync."""
    return list(REGISTERED_BACKENDS.values())
