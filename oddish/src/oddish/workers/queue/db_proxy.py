"""Pluggable transport for the worker hot-path DB ops.

Library code calls ``get_db_proxy().claim_job(...)`` etc. The default
is ``DirectDBProxy`` which opens a fresh asyncpg connection per call
(legacy behaviour). The backend layer registers a Modal-backed proxy
that routes through a centralised pool, cutting per-worker connection
count under concurrent bursts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Protocol

import asyncpg

from oddish.config import settings
from oddish.workers.queue.queries import (
    ClaimedJobRow,
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


__all__ = [
    "ClaimedJobRow",
    "DBProxy",
    "DirectDBProxy",
    "get_db_proxy",
    "set_db_proxy",
]


class DBProxy(Protocol):
    async def claim_job(
        self,
        *,
        queue_key: str,
        worker_id: str,
        queue_slot: int | None,
        modal_function_call_id: str | None,
    ) -> ClaimedJobRow | None: ...

    async def heartbeat(
        self,
        *,
        job_id: str,
        pending_failure_count: int,
        pending_last_error: str | None,
    ) -> None: ...

    async def record_success(
        self, *, job_id: str, summary_json: str | None
    ) -> None: ...

    async def record_retry(
        self,
        *,
        job_id: str,
        error_message: str | None,
        retry_at: datetime | None,
        mirror_to_trials: bool,
        subject_id: str | None,
    ) -> None: ...

    async def record_failure(
        self, *, job_id: str, error_message: str | None
    ) -> None: ...

    async def ensure_slots(self, *, queue_key: str, limit: int) -> None: ...

    async def acquire_slot(
        self,
        *,
        queue_key: str,
        limit: int,
        worker_id: str,
        lease_seconds: int,
    ) -> int | None: ...

    async def release_slot(
        self, *, queue_key: str, slot: int, worker_id: str
    ) -> None: ...

    async def cleanup_stale_slots(self) -> int: ...


@asynccontextmanager
async def _direct_connection() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(
        settings.asyncpg_url,
        statement_cache_size=0,
        server_settings=settings.asyncpg_server_settings(),
    )
    try:
        yield conn
    finally:
        await conn.close()


class DirectDBProxy:
    """One direct asyncpg connection per call. Used in tests and as the
    fallback when no transport is registered."""

    async def claim_job(self, **kwargs) -> ClaimedJobRow | None:
        async with _direct_connection() as conn:
            return await claim_job(conn, **kwargs)

    async def heartbeat(self, **kwargs) -> None:
        async with _direct_connection() as conn:
            await heartbeat(conn, **kwargs)

    async def record_success(self, **kwargs) -> None:
        async with _direct_connection() as conn:
            await record_success(conn, **kwargs)

    async def record_retry(self, **kwargs) -> None:
        async with _direct_connection() as conn:
            await record_retry(conn, **kwargs)

    async def record_failure(self, **kwargs) -> None:
        async with _direct_connection() as conn:
            await record_failure(conn, **kwargs)

    async def ensure_slots(self, **kwargs) -> None:
        async with _direct_connection() as conn:
            await ensure_slots(conn, **kwargs)

    async def acquire_slot(self, **kwargs) -> int | None:
        async with _direct_connection() as conn:
            return await acquire_slot(conn, **kwargs)

    async def release_slot(self, **kwargs) -> None:
        async with _direct_connection() as conn:
            await release_slot(conn, **kwargs)

    async def cleanup_stale_slots(self) -> int:
        async with _direct_connection() as conn:
            return await cleanup_stale_slots(conn)


_proxy: DBProxy = DirectDBProxy()


def set_db_proxy(proxy: DBProxy) -> None:
    global _proxy
    _proxy = proxy


def get_db_proxy() -> DBProxy:
    return _proxy
