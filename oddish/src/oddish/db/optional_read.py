"""Reads that tolerate a table this deploy has not migrated yet."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import TypeVar

from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db.connection import is_read_session
from oddish.db.pg_errors import is_missing_table

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def read_optional_table(
    session: AsyncSession,
    read: Callable[[], Awaitable[T]],
    *,
    table: str,
    degraded: str,
) -> T | None:
    """Run ``read`` against ``table``, which this deploy may not have migrated yet.

    Code deploys and migrations land independently, so a reader behind a new
    table has a window where the table does not exist. Returns ``None`` in
    that one case (the caller degrades; ``degraded`` says how, in the warning)
    and re-raises every other error: catching all ``ProgrammingError`` would
    hide a broken query behind the same fallback.

    Under ``get_session`` the read runs inside a SAVEPOINT so the failed
    statement rolls back on its own and the caller's transaction stays
    usable. Under ``get_read_session`` every statement already owns its
    implicit transaction, PostgreSQL rejects SAVEPOINT there, and nothing
    needs protecting.
    """
    guard = nullcontext() if is_read_session(session) else session.begin_nested()
    try:
        async with guard:
            return await read()
    except ProgrammingError as exc:
        if not is_missing_table(exc):
            raise
        logger.warning(
            "%s unavailable (schema not migrated yet); %s",
            table,
            degraded,
            exc_info=True,
        )
        return None
