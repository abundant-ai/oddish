"""In-process dispatcher: one ``asyncio.Task`` per spawned worker.

The portable analog of Modal's container fan-out, used by the standalone
server and as the default off-Modal control plane. Each spawned worker runs
exactly one job via ``run_single_worker_job`` (the same provider-agnostic
entrypoint the Modal worker drains), so behavior converges on a single code
path. The core poll loop (``oddish.dispatch.cycle``) decides *how many* workers
to spawn per tick; this dispatcher only knows *how to run one*.

In-process handles are not durable across a control-plane restart, so
``recover`` is a no-op (``None``) and reclamation falls back to ``worker_jobs``
lease expiry + re-claim (design spec §14.3).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from oddish.dispatch.ports import Dispatcher, WorkerHandle

logger = logging.getLogger(__name__)

RunJob = Callable[..., Awaitable[bool]]
AcquireSlot = Callable[..., Awaitable["int | None"]]
ReleaseSlot = Callable[..., Awaitable[None]]


async def _default_run_job(queue_key: str, **kwargs: Any) -> bool:
    # Imported lazily so importing this module never pulls in the DB/runner
    # stack (keeps the dispatch package import-light, like the runtime port).
    from oddish.workers.queue.worker_job_single_job import run_single_worker_job

    return await run_single_worker_job(queue_key, **kwargs)


async def _default_acquire_slot(
    *, queue_key: str, limit: int, worker_id: str, lease_seconds: int
) -> int | None:
    from oddish.workers.queue.slots import acquire_queue_slot

    return await acquire_queue_slot(
        queue_key=queue_key,
        limit=limit,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )


async def _default_release_slot(*, queue_key: str, slot: int, worker_id: str) -> None:
    from oddish.workers.queue.slots import release_queue_slot

    await release_queue_slot(queue_key=queue_key, slot=slot, worker_id=worker_id)


class InProcessDispatcher:
    name = "inprocess"

    def __init__(
        self,
        *,
        run_job: RunJob | None = None,
        worker_id_prefix: str = "inproc",
        acquire_slot: AcquireSlot | None = None,
        release_slot: ReleaseSlot | None = None,
    ) -> None:
        self._run_job: RunJob = run_job or _default_run_job
        self._acquire_slot: AcquireSlot = acquire_slot or _default_acquire_slot
        self._release_slot: ReleaseSlot = release_slot or _default_release_slot
        self._worker_id_prefix = worker_id_prefix
        self._tasks: dict[str, asyncio.Task] = {}
        self._counter = 0

    async def spawn(self, *, spawn_plan: Sequence[str]) -> Sequence[WorkerHandle]:
        return await self.spawn_units(
            spawn_units=[(queue_key, "default", "default") for queue_key in spawn_plan]
        )

    async def spawn_units(
        self, *, spawn_units: Sequence[tuple[str, str, str]]
    ) -> Sequence[WorkerHandle]:
        handles: list[WorkerHandle] = []
        for queue_key, harbor_variant_id, execution_lane in spawn_units:
            self._counter += 1
            worker_id = f"{self._worker_id_prefix}-{queue_key}-{self._counter}"
            task = asyncio.create_task(
                self._safe_run(
                    queue_key,
                    worker_id,
                    harbor_variant_id=harbor_variant_id,
                    execution_lane=execution_lane,
                ),
                name=f"worker-{queue_key}",
            )
            self._tasks[worker_id] = task
            handles.append(
                WorkerHandle(
                    provider=self.name,
                    queue_key=queue_key,
                    id=worker_id,
                    provisional=False,
                    metadata={
                        "harbor_variant_id": harbor_variant_id,
                        "execution_lane": execution_lane,
                    },
                )
            )
        return handles

    async def _safe_run(
        self,
        queue_key: str,
        worker_id: str,
        *,
        harbor_variant_id: str,
        execution_lane: str,
    ) -> None:
        from oddish.core.model_concurrency import load_effective_model_concurrency_limit
        from oddish.config import settings
        from oddish.workers.queue.queue_manager import DEFAULT_SLOT_LEASE_SECONDS
        from oddish.workers.queue.sandbox_capacity import (
            SANDBOX_CAPACITY_LEASE_SECONDS,
            acquire_sandbox_capacity_lease,
            release_sandbox_capacity_lease,
        )

        capacity_slot: int | None = None
        release_capacity_lease = True
        try:
            if execution_lane == "ec2_trial":
                capacity_slot = await acquire_sandbox_capacity_lease(
                    provider="ec2",
                    limit=settings.ec2_max_concurrent_instances,
                    worker_id=worker_id,
                    lease_seconds=SANDBOX_CAPACITY_LEASE_SECONDS,
                )
                if capacity_slot is None:
                    return
            limit = await load_effective_model_concurrency_limit(queue_key)
            if limit <= 0:
                return
            # Hold a queue_slots lease for the run, mirroring the Modal /
            # off-Modal container workers (run_assigned_queue_worker). Without it,
            # multiple in-process asyncio workers for one queue_key could run past
            # the per-queue_key concurrency limit in the dispatch cycle's
            # spawn->claim race window.
            slot = await self._acquire_slot(
                queue_key=queue_key,
                limit=limit,
                worker_id=worker_id,
                lease_seconds=DEFAULT_SLOT_LEASE_SECONDS,
            )
            if slot is None:
                return  # no free slot this tick; the cycle re-spawns next tick
            try:
                # Image-agnostic workers still claim the exact dispatch tuple;
                # they simply do not require a variant-specific container image.
                run_kwargs: dict[str, Any] = {
                    "worker_id": worker_id,
                    "queue_slot": slot,
                    "harbor_variant_id": harbor_variant_id,
                }
                if execution_lane == "ec2_trial":
                    run_kwargs.update(
                        execution_lane=execution_lane,
                        capacity_provider="ec2",
                        capacity_slot=capacity_slot,
                    )
                    release_capacity_lease = False
                await self._run_job(queue_key, **run_kwargs)
                release_capacity_lease = True
            finally:
                await self._release_slot(
                    queue_key=queue_key, slot=slot, worker_id=worker_id
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - worker errors must not crash the loop
            logger.warning("in-process worker %s failed: %r", worker_id, exc)
        finally:
            if capacity_slot is not None and release_capacity_lease:
                await release_sandbox_capacity_lease(
                    provider="ec2", slot=capacity_slot, worker_id=worker_id
                )

    async def check_active(
        self, handles: Sequence[WorkerHandle]
    ) -> AsyncIterator[WorkerHandle]:
        for handle in handles:
            task = self._tasks.get(handle.id)
            if task is not None and not task.done():
                yield handle

    async def cancel(self, handles: Sequence[WorkerHandle]) -> int:
        cancelled = 0
        for handle in handles:
            task = self._tasks.get(handle.id)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                cancelled += 1
            self._tasks.pop(handle.id, None)
        return cancelled

    async def recover(self, serialized: Mapping[str, Any]) -> WorkerHandle | None:
        return None


_: Dispatcher = InProcessDispatcher()  # structural conformance check at import
