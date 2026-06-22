"""Name → ExecutionBackend resolution + cheap-first ordering.

``ordered_backends()`` returns Daytona before Modal so capability negotiation
(routing.py) picks the cheap CPU backend by default and only escalates to
Modal when a capability (GPU, private-registry pull) requires it."""

from __future__ import annotations

from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.modal import ModalBackend
from oddish.runtime.ports import ExecutionBackend

# Singleton instances; backends are stateless w.r.t. trial dispatch.
_MODAL = ModalBackend()
_DAYTONA = DaytonaBackend()

REGISTERED_BACKENDS: dict[str, ExecutionBackend] = {
    _DAYTONA.name: _DAYTONA,
    _MODAL.name: _MODAL,
}


def get_backend(name: str | None) -> ExecutionBackend | None:
    """Resolve a backend by provider name (case-insensitive); None if unknown."""
    if not name:
        return None
    return REGISTERED_BACKENDS.get(name.lower())


def ordered_backends() -> list[ExecutionBackend]:
    """Backends in cheap-first order: Daytona (CPU) then Modal (GPU/private)."""
    return [_DAYTONA, _MODAL]
