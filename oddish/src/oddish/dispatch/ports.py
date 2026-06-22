"""The ``Dispatcher`` / ``WorkerScaler`` port (design spec §5.2).

Formalizes the Modal-vs-in-process seam. The lean job contract is copied from
the Snakemake ``RemoteExecutor`` (``run_job`` + ``check_active_jobs`` +
``cancel_jobs``); the **poll loop, spawn-plan math, and per-poll caps stay in
the core** (``oddish.dispatch.cycle``). A ``Dispatcher`` only knows *how to run
N workers* and *how to observe / cancel / reattach them*.

Handle identity is **worker-written, not spawner-returned** (load-bearing, see
§5.2/§11): on Modal the ``spawn.aio()`` return is discarded and the worker
self-reports ``current_function_call_id()`` into its claimed ``worker_jobs``
row. So ``spawn`` returns **provisional** handles whose ``id`` may be empty;
``cancel``/``recover`` resolve the durable handle from persisted state, never an
in-memory value the spawner held. Backends with synchronous ids (a Kubernetes
Job name) populate ``id`` eagerly and set ``provisional=False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class WorkerHandle:
    """A serializable reference to a spawned worker.

    ``provider`` names the dispatcher backend ("modal" | "inprocess" |
    "docker" | "k8s"); ``queue_key`` is the queue the worker drains; ``id`` is
    the durable, backend-specific identifier used to reattach or cancel (a
    Modal function-call id, a Docker container id, a Kubernetes Job name). When
    ``provisional`` is True the ``id`` is not yet authoritative (the worker has
    not self-reported it).
    """

    provider: str
    queue_key: str
    id: str = ""
    provisional: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def serialize(self) -> dict:
        return {
            "provider": self.provider,
            "queue_key": self.queue_key,
            "id": self.id,
            "provisional": self.provisional,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def deserialize(cls, data: Mapping[str, Any]) -> "WorkerHandle":
        return cls(
            provider=str(data["provider"]),
            queue_key=str(data["queue_key"]),
            id=str(data.get("id", "")),
            provisional=bool(data.get("provisional", True)),
            metadata=dict(data.get("metadata") or {}),
        )


@runtime_checkable
class Dispatcher(Protocol):
    """Launch / observe / cancel / reattach a fleet of single-job workers."""

    name: str

    async def spawn(self, *, spawn_plan: Sequence[str]) -> Sequence[WorkerHandle]:
        """Ensure one worker per ``queue_key`` in ``spawn_plan``.

        Each worker drains exactly one job (or batches per
        ``WORKER_BATCH_BUDGET_SECONDS``) via ``drain_worker_jobs``. Returned
        handles may be provisional — see the module docstring.
        """
        ...

    def check_active(
        self, handles: Sequence[WorkerHandle]
    ) -> AsyncIterator[WorkerHandle]:
        """Async-generator yielding the handles that are still running."""
        ...

    async def cancel(self, handles: Sequence[WorkerHandle]) -> int:
        """Terminate in-flight workers; return the count actually cancelled."""
        ...

    async def recover(self, serialized: Mapping[str, Any]) -> WorkerHandle | None:
        """Reattach a handle after a control-plane restart (Nomad RecoverTask).

        Best-effort: ephemeral scalers (in-process, Docker) may return ``None``
        and rely on lease expiry + re-claim instead of durable reattach.
        """
        ...
