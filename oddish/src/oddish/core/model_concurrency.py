"""Admin-set per-queue-key concurrency limits, stored in the database.

An override row wins over the deploy-time ``ODDISH_MODEL_CONCURRENCY_OVERRIDES``
/ default settings, so a limit can be changed without a redeploy. Shaped like
the sibling ``model_concurrency_advisory`` table: hand-written migration, raw
SQL, no ORM model.
"""

import logging
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings

logger = logging.getLogger(__name__)

MAX_MODEL_CONCURRENCY = 10_000


async def get_model_concurrency_overrides(
    session: AsyncSession,
    queue_keys: Iterable[str] | None = None,
) -> dict[str, int]:
    """Stored override rows, keyed by normalized queue key. All rows when
    ``queue_keys`` is None."""
    query = "SELECT queue_key, concurrency_limit FROM model_concurrency_overrides"
    params = {}
    if queue_keys is not None:
        normalized = sorted({settings.normalize_queue_key(key) for key in queue_keys})
        if not normalized:
            return {}
        query += " WHERE queue_key = ANY(CAST(:queue_keys AS TEXT[]))"
        params = {"queue_keys": normalized}
    rows = (await session.execute(text(query), params)).all()
    return {str(row.queue_key): int(row.concurrency_limit) for row in rows}


async def get_effective_model_concurrency_limits(
    session: AsyncSession,
    queue_keys: Iterable[str],
) -> dict[str, int]:
    """Effective limit per queue key: the admin override when set, else the
    deploy-time limit.

    Keyed by the caller's spelling of each key, NOT the normalized form.
    ``build_spawn_plan`` looks limits up under the key it discovered on
    ``worker_jobs`` and reads a miss as limit 0 -- a silent, total dispatch
    stall for that queue -- so re-keying the dict here would strand it.
    """
    keys = tuple(queue_keys)
    overrides = await get_model_concurrency_overrides(session, keys)
    return {
        key: overrides.get(
            settings.normalize_queue_key(key), settings.get_model_concurrency(key)
        )
        for key in keys
    }


async def load_effective_model_concurrency_limits(
    queue_keys: Iterable[str],
) -> dict[str, int]:
    """``get_effective_model_concurrency_limits`` on its own session. Best-effort:
    an unreadable override table decays to the deploy limits, never blocks dispatch.
    """
    keys = tuple(queue_keys)
    try:
        from oddish.db import get_session

        async with get_session() as session:
            return await get_effective_model_concurrency_limits(session, keys)
    except Exception as e:  # noqa: BLE001 - override read is best-effort
        logger.warning("Concurrency overrides unavailable, using deploy limits: %s", e)
        return {key: settings.get_model_concurrency(key) for key in keys}


async def load_effective_model_concurrency_limit(queue_key: str) -> int:
    return (await load_effective_model_concurrency_limits((queue_key,)))[queue_key]


async def set_model_concurrency_override(
    session: AsyncSession,
    queue_key: str,
    limit: int | None,
) -> tuple[str, int | None]:
    """Upsert one override (clear it when ``limit`` is None). Returns the
    normalized queue key and the stored override."""
    if limit is not None and not 0 <= limit <= MAX_MODEL_CONCURRENCY:
        raise ValueError(f"limit must be between 0 and {MAX_MODEL_CONCURRENCY}")
    normalized = settings.normalize_queue_key(queue_key)
    if limit is None:
        await session.execute(
            text(
                "DELETE FROM model_concurrency_overrides WHERE queue_key = :queue_key"
            ),
            {"queue_key": normalized},
        )
    else:
        await session.execute(
            text(
                """
                INSERT INTO model_concurrency_overrides
                    (queue_key, concurrency_limit, updated_at)
                VALUES (:queue_key, :concurrency_limit, NOW())
                ON CONFLICT (queue_key) DO UPDATE
                SET concurrency_limit = EXCLUDED.concurrency_limit,
                    updated_at = NOW()
                """
            ),
            {"queue_key": normalized, "concurrency_limit": limit},
        )
    return normalized, limit
