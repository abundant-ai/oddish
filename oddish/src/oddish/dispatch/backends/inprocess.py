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


async def _default_run_job(queue_key: str, **kwargs: Any) -> bool:
    # Imported lazily so importing this module never pulls in the DB/runner
    # stack (keeps the dispatch package import-light, like the runtime port).
    from oddish.workers.queue.worker_job_single_job import run_single_worker_job

    return await run_single_worker_job(queue_key, **kwargs)


class InProcessDispatcher:
    name = "inprocess"

    def __init__(
        self,
        *,
        run_job: RunJob | None = None,
        worker_id_prefix: str = "inproc",
    ) -> None:
        self._run_job: RunJob = run_job or _default_run_job
        self._worker_id_prefix = worker_id_prefix
        self._tasks: dict[str, asyncio.Task] = {}
        self._counter = 0

    async def spawn(self, *, spawn_plan: Sequence[str]) -> Sequence[WorkerHandle]:
        handles: list[WorkerHandle] = []
        for queue_key in spawn_plan:
            self._counter += 1
            worker_id = f"{self._worker_id_prefix}-{queue_key}-{self._counter}"
            task = asyncio.create_task(
                self._safe_run(queue_key, worker_id),
                name=f"worker-{queue_key}",
            )
            self._tasks[worker_id] = task
            handles.append(
                WorkerHandle(
                    provider=self.name,
                    queue_key=queue_key,
                    id=worker_id,
                    provisional=False,
                )
            )
        return handles

    async def _safe_run(self, queue_key: str, worker_id: str) -> None:
        try:
            await self._run_job(queue_key, worker_id=worker_id, queue_slot=0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - worker errors must not crash the loop
            logger.warning("in-process worker %s failed: %r", worker_id, exc)

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
