"""The SandboxProvider port (thin first-cut, per design spec §5.1).

Phase A scope is exactly the four oddish-side provider concerns that are
consolidated out of scattered ``if environment == …`` branches:
capability reporting, Harbor env-kwargs, hung-sandbox teardown, and Modal
debug capture. The full ``Sandbox`` lifecycle verb set (create/exec/logs/
files/stop/delete) from spec §5.1 lands with its first consumer (cc_chat)
in a later phase; it is intentionally NOT defined here (YAGNI).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

LatencyClass = Literal["instant", "seconds", "minutes"]


@dataclass(frozen=True)
class GpuSupport:
    """Accelerator support — a fallback-ordered list, never a bool (spec §8)."""

    accelerators: tuple[str, ...]
    max_count: int


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


@runtime_checkable
class ExecutionBackend(Protocol):
    """Thin trial-facing provider port. Harbor still owns the sandbox
    lifecycle; a backend only supplies the oddish-side specifics."""

    name: str

    def capabilities(self) -> Capabilities: ...

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Return the Harbor ``environment.kwargs`` for this backend, merging
        provider defaults under the caller's kwargs (caller wins)."""
        ...

    async def teardown(self, external_id: str) -> bool:
        """Best-effort terminate a hung sandbox by id. Never raises; returns
        True when a terminate/delete was issued, False otherwise."""
        ...

    def capture_diagnostics(self, job_dir: Path) -> AbstractContextManager[Path | None]:
        """Context manager that captures provider SDK diagnostics for a trial
        run into ``job_dir`` (yields the log path) or is a no-op (yields None)."""
        ...
