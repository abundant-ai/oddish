from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import utcnow
from pg_errors import is_undefined_table_error


def unavailable(exc: ProgrammingError) -> HTTPException:
    if not is_undefined_table_error(exc):
        raise exc
    return HTTPException(
        503,
        "Cost exclusions are not available yet (schema is still migrating). "
        "Try again shortly.",
    )


async def soft_delete(
    session: AsyncSession, model, row_id: str, not_found_detail: str
) -> None:
    result = await session.scalars(select(model).where(model.id == row_id))
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    row.deleted_at = utcnow()
    await session.commit()
