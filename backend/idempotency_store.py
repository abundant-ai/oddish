"""Postgres-backed idempotency store for the cloud backend.

Implements ``oddish.core.idempotency.IdempotencyStore`` over the
``submission_idempotency`` table and is injected into ``create_task_sweep_core``
so the open-source ``oddish`` core stays free of backend imports. All methods
operate on the request's own session/transaction, so the recorded key and the
work it guards commit (or roll back) together.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import SubmissionIdempotency
from oddish.core.idempotency import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    StoredIdempotencyRecord,
)


class SubmissionIdempotencyStore:
    """Idempotency record store bound to one request's session/transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, org_id: str, route: str, key_hash: str
    ) -> StoredIdempotencyRecord | None:
        result = await self._session.execute(
            select(SubmissionIdempotency).where(
                SubmissionIdempotency.org_id == org_id,
                SubmissionIdempotency.route == route,
                SubmissionIdempotency.key_hash == key_hash,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return StoredIdempotencyRecord(
            request_hash=row.request_hash,
            status=row.status,
            response_json=row.response_json,
            expires_at=row.expires_at,
        )

    async def begin(
        self,
        org_id: str,
        route: str,
        key_hash: str,
        request_hash: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        record = SubmissionIdempotency(
            org_id=org_id,
            route=route,
            key_hash=key_hash,
            request_hash=request_hash,
            status=STATUS_IN_PROGRESS,
            response_json=None,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        # A SAVEPOINT lets a unique-constraint collision roll back just this
        # insert while leaving the outer transaction usable to inspect the
        # winning row.
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
            return True
        except IntegrityError:
            return False

    async def complete(
        self, org_id: str, route: str, key_hash: str, response_json: dict
    ) -> None:
        await self._session.execute(
            update(SubmissionIdempotency)
            .where(
                SubmissionIdempotency.org_id == org_id,
                SubmissionIdempotency.route == route,
                SubmissionIdempotency.key_hash == key_hash,
            )
            .values(status=STATUS_COMPLETED, response_json=response_json)
        )

    async def discard(
        self, org_id: str, route: str, key_hash: str, now: datetime
    ) -> None:
        # Delete only the expired row: a concurrent retry may have already
        # replaced it with a fresh (non-expired) row, which must survive.
        await self._session.execute(
            delete(SubmissionIdempotency).where(
                SubmissionIdempotency.org_id == org_id,
                SubmissionIdempotency.route == route,
                SubmissionIdempotency.key_hash == key_hash,
                SubmissionIdempotency.expires_at <= now,
            )
        )
