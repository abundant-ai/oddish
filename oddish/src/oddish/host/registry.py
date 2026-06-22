"""Name → HostAdapter resolution."""

from __future__ import annotations

from oddish.host.adapters import (
    DockerHostAdapter,
    ModalHostAdapter,
    UvicornHostAdapter,
)
from oddish.host.ports import HostAdapter

_ADAPTERS: dict[str, HostAdapter] = {
    "uvicorn": UvicornHostAdapter(),
    "docker": DockerHostAdapter(),
    "modal": ModalHostAdapter(),
}


def get_host_adapter(name: str | None) -> HostAdapter | None:
    """Resolve a host adapter by name (case-insensitive); None if unknown."""
    if not name:
        return None
    return _ADAPTERS.get(name.lower())
