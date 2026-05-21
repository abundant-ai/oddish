"""Modal-hosted DB proxy: bounded asyncpg pool, callable from workers.

Replaces the per-worker ``asyncpg.connect`` pattern in the hot path.
A handful of replicas, each holding a small pool, serve every worker
in the fleet -- so 1000 concurrent nop workers no longer translate to
1000+ concurrent Postgres connections.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg
import modal

from oddish.config import settings
from oddish.workers.queue.db_proxy import ClaimedJobRow, DBProxy, set_db_proxy
from oddish.workers.queue.queries import (
    acquire_slot,
    claim_job,
    cleanup_stale_slots,
    ensure_slots,
    heartbeat,
    record_failure,
    record_retry,
    record_success,
    release_slot,
)

from modal_app import (
    DB_PROXY_POOL_SIZE,
    DB_PROXY_REPLICAS,
    app,
    image,
    runtime_secrets,
)


@app.cls(
    image=image,
    secrets=runtime_secrets,
    min_containers=DB_PROXY_REPLICAS,
    max_containers=DB_PROXY_REPLICAS,
    timeout=86400,
)
class DBProxyService:
    pool: asyncpg.Pool | None = None

    @modal.enter()
    async def _open(self) -> None:
        self.pool = await asyncpg.create_pool(
            settings.asyncpg_url,
            min_size=2,
            max_size=DB_PROXY_POOL_SIZE,
            statement_cache_size=0,
            server_settings=settings.asyncpg_server_settings(),
        )

    @modal.exit()
    async def _close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    @modal.method()
    async def claim_job(
        self,
        *,
        queue_key: str,
        worker_id: str,
        queue_slot: int | None,
        modal_function_call_id: str | None,
    ) -> ClaimedJobRow | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            return await claim_job(
                conn,
                queue_key=queue_key,
                worker_id=worker_id,
                queue_slot=queue_slot,
                modal_function_call_id=modal_function_call_id,
            )

    @modal.method()
    async def heartbeat(
        self,
        *,
        job_id: str,
        pending_failure_count: int,
        pending_last_error: str | None,
    ) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await heartbeat(
                conn,
                job_id=job_id,
                pending_failure_count=pending_failure_count,
                pending_last_error=pending_last_error,
            )

    @modal.method()
    async def record_success(self, *, job_id: str, summary_json: str | None) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await record_success(conn, job_id=job_id, summary_json=summary_json)

    @modal.method()
    async def record_retry(
        self,
        *,
        job_id: str,
        error_message: str | None,
        retry_at: datetime | None,
        mirror_to_trials: bool,
        subject_id: str | None,
    ) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await record_retry(
                conn,
                job_id=job_id,
                error_message=error_message,
                retry_at=retry_at,
                mirror_to_trials=mirror_to_trials,
                subject_id=subject_id,
            )

    @modal.method()
    async def record_failure(self, *, job_id: str, error_message: str | None) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await record_failure(conn, job_id=job_id, error_message=error_message)

    @modal.method()
    async def ensure_slots(self, *, queue_key: str, limit: int) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await ensure_slots(conn, queue_key=queue_key, limit=limit)

    @modal.method()
    async def acquire_slot(
        self,
        *,
        queue_key: str,
        limit: int,
        worker_id: str,
        lease_seconds: int,
    ) -> int | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            return await acquire_slot(
                conn,
                queue_key=queue_key,
                limit=limit,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    @modal.method()
    async def release_slot(
        self, *, queue_key: str, slot: int, worker_id: str
    ) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await release_slot(
                conn, queue_key=queue_key, slot=slot, worker_id=worker_id
            )

    @modal.method()
    async def cleanup_stale_slots(self) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            return await cleanup_stale_slots(conn)


class ModalDBProxy(DBProxy):
    """Client side: routes ``DBProxy`` calls to ``DBProxyService`` over Modal."""

    def __init__(self) -> None:
        self._svc = DBProxyService()

    async def claim_job(self, **kwargs: Any) -> ClaimedJobRow | None:
        return await self._svc.claim_job.remote.aio(**kwargs)

    async def heartbeat(self, **kwargs: Any) -> None:
        await self._svc.heartbeat.remote.aio(**kwargs)

    async def record_success(self, **kwargs: Any) -> None:
        await self._svc.record_success.remote.aio(**kwargs)

    async def record_retry(self, **kwargs: Any) -> None:
        await self._svc.record_retry.remote.aio(**kwargs)

    async def record_failure(self, **kwargs: Any) -> None:
        await self._svc.record_failure.remote.aio(**kwargs)

    async def ensure_slots(self, **kwargs: Any) -> None:
        await self._svc.ensure_slots.remote.aio(**kwargs)

    async def acquire_slot(self, **kwargs: Any) -> int | None:
        return await self._svc.acquire_slot.remote.aio(**kwargs)

    async def release_slot(self, **kwargs: Any) -> None:
        await self._svc.release_slot.remote.aio(**kwargs)

    async def cleanup_stale_slots(self) -> int:
        return await self._svc.cleanup_stale_slots.remote.aio()


def install_modal_db_proxy() -> ModalDBProxy:
    proxy = ModalDBProxy()
    set_db_proxy(proxy)
    return proxy
