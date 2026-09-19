"""Name → ExecutionBackend resolution + the ordered routing set.

``ordered_backends()`` returns Daytona before the opt-in Thunder and EC2
backends, Modal, and Archil, so capability negotiation keeps Daytona as the
default CPU backend and escalates to the first GPU-capable backend only when a
capability requires it: Thunder on deployments that enable it, otherwise
Modal."""

from __future__ import annotations

from oddish.config import settings
from oddish.runtime.backends.archil import ArchilBackend
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.ec2 import Ec2Backend
from oddish.runtime.backends.gke import GkeBackend
from oddish.runtime.backends.modal import ModalBackend
from oddish.runtime.backends.numinous import NuminousBackend
from oddish.runtime.backends.thunder import ThunderBackend
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

# Thunder follows Daytona in the ordered registry so enabling it cannot
# replace the CPU default, and precedes Modal so that on deployments that
# enable it, cheap-first negotiation hands GPU work to Thunder.
if settings.thunder_enabled:
    _THUNDER = ThunderBackend()
    REGISTERED_BACKENDS[_THUNDER.name] = _THUNDER

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
    """All registered backends in stable policy/display order."""
    return list(REGISTERED_BACKENDS.values())
