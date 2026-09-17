"""The SandboxProvider port (thin first-cut, per design spec §5.1).

Phase A scope is exactly the four oddish-side provider concerns that are
consolidated out of scattered ``if environment == …`` branches:
capability reporting, Harbor env-kwargs, hung-sandbox teardown, and Modal
debug capture. The full ``Sandbox`` lifecycle verb set (create/exec/logs/
files/stop/delete) from spec §5.1 lands with its first consumer in a later
phase; it is intentionally NOT defined here (YAGNI).
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

LatencyClass = Literal["instant", "seconds", "minutes"]


def accelerator_family(value: str) -> str:
    """Normalize a GPU name across providers: case, ``:count`` and ``!``
    suffixes, and the A100 memory variants all collapse to one family."""
    normalized = value.strip().upper().replace("_", "-").rstrip("!")
    normalized = normalized.split(":", 1)[0].rstrip("!")
    if normalized in {"A100", "A100XL", "A100-40", "A100-80", "A100-40GB", "A100-80GB"}:
        return "A100"
    return normalized


@dataclass(frozen=True)
class GpuSupport:
    """Accelerator support — a fallback-ordered list, never a bool (spec §8)."""

    accelerators: tuple[str, ...]
    max_count: int
    # True for a backend with no "any GPU" pool: it launches only a request
    # that names exactly one of its ``accelerators`` (Thunder). Every other
    # backend takes any request and validates the name itself at launch.
    requires_named_accelerator: bool = False

    def serves(self, gpu_types: Sequence[str] | None) -> bool:
        """Whether a task's acceptable GPU types (``None`` = any) fit here."""
        if not self.requires_named_accelerator:
            return True
        if not gpu_types or len(gpu_types) != 1:
            return False
        offered = {accelerator_family(name) for name in self.accelerators}
        return accelerator_family(gpu_types[0]) in offered


@dataclass(frozen=True)
class TpuSupport:
    """TPU accelerator support — the offered chip generations and the
    single-host chip ceiling (Harbor's one-pod environment is single-host)."""

    types: tuple[str, ...]
    max_chips_per_host: int


@dataclass(frozen=True)
class Capabilities:
    """Feature-detected backend capabilities (spec §5.1). Optional blocks are
    only valid when the corresponding flag is set; routing never assumes."""

    gpu: GpuSupport | None
    private_registry_pull: bool
    network_egress: Literal["deny", "allow", "configurable"]
    persistent_volumes: bool
    streaming_logs: bool
    memory_snapshot_fork: bool
    cold_start: LatencyClass
    tpu: TpuSupport | None = None


@runtime_checkable
class ExecutionBackend(Protocol):
    """Thin trial-facing provider port. Harbor still owns the sandbox
    lifecycle; a backend only supplies the oddish-side specifics."""

    name: str

    def capabilities(self) -> Capabilities: ...

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Return validated Harbor ``environment.kwargs`` for this backend.

        Backends may merge user-owned options over defaults or reject overrides
        of platform-owned settings.
        """
        ...

    async def teardown(self, external_id: str) -> bool:
        """Best-effort terminate a hung sandbox by id. Never raises; returns
        True when a terminate/delete was issued, False otherwise."""
        ...

    def capture_diagnostics(self, job_dir: Path) -> AbstractContextManager[Path | None]:
        """Context manager that captures provider SDK diagnostics for a trial
        run into ``job_dir`` (yields the log path) or is a no-op (yields None)."""
        ...
