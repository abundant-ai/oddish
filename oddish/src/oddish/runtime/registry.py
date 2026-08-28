"""Name → ExecutionBackend resolution + cheap-first ordering.

``ordered_backends()`` returns Daytona before the opt-in EC2 backend, Modal,
and Archil, so capability negotiation keeps Daytona as the default CPU backend
and only escalates to Modal when a capability requires it. GKE joins last, only
when a cluster is configured, so cheap-first negotiation hands it only TPU work."""

from __future__ import annotations

from oddish.config import settings
from oddish.runtime.backends.archil import ArchilBackend
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.ec2 import Ec2Backend
from oddish.runtime.backends.gke import GkeBackend
from oddish.runtime.backends.modal import ModalBackend
from oddish.runtime.backends.numinous import NuminousBackend
from oddish.runtime.ports import ExecutionBackend

# Singleton instances; backends are stateless w.r.t. trial dispatch.
_MODAL = ModalBackend()
_DAYTONA = DaytonaBackend()
_ARCHIL = ArchilBackend()

REGISTERED_BACKENDS: dict[str, ExecutionBackend] = {}

# Numinous joins FIRST (cheapest CPU lane) when enabled, so cheap-first
# negotiation hands plain-CPU trials to it before Daytona.
if settings.numinous_enabled:
    _NUMINOUS = NuminousBackend()
    REGISTERED_BACKENDS[_NUMINOUS.name] = _NUMINOUS

REGISTERED_BACKENDS[_DAYTONA.name] = _DAYTONA

if settings.ec2_enabled:
    _EC2 = Ec2Backend()
    REGISTERED_BACKENDS[_EC2.name] = _EC2

REGISTERED_BACKENDS[_MODAL.name] = _MODAL
REGISTERED_BACKENDS[_ARCHIL.name] = _ARCHIL

# GKE joins only when a cluster is configured, and always after the other
# backends so cheap-first negotiation never hands non-TPU work to it.
if settings.gke_cluster_name:
    _GKE = GkeBackend()
    REGISTERED_BACKENDS[_GKE.name] = _GKE


def get_backend(name: str | None) -> ExecutionBackend | None:
    """Resolve a backend by provider name (case-insensitive); None if unknown."""
    if not name:
        return None
    return REGISTERED_BACKENDS.get(name.lower())


def ordered_backends() -> list[ExecutionBackend]:
    """Backends in cheap-first order: Daytona, opt-in EC2, Modal, Archil, GKE.

    Sourced from ``REGISTERED_BACKENDS`` (insertion-ordered cheap-first) so the
    resolution set and the routing order never desync."""
    return list(REGISTERED_BACKENDS.values())
