from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings

MAX_MODEL_CONCURRENCY = 10_000


async def get_model_concurrency_overrides(
    session: AsyncSession,
    queue_keys: Iterable[str] | None = None,
) -> dict[str, int]:
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
    normalized = {settings.normalize_queue_key(key) for key in queue_keys}
    overrides = await get_model_concurrency_overrides(session, normalized)
    return {
        key: overrides.get(key, settings.get_model_concurrency(key))
        for key in normalized
    }


async def load_effective_model_concurrency_limits(
    queue_keys: Iterable[str],
) -> dict[str, int]:
    normalized = {settings.normalize_queue_key(key) for key in queue_keys}
    fallback = {key: settings.get_model_concurrency(key) for key in normalized}
    try:
        from oddish.db import get_session

        async with get_session() as session:
            return await get_effective_model_concurrency_limits(session, normalized)
    except Exception:
        return fallback


async def load_effective_model_concurrency_limit(queue_key: str) -> int:
    normalized = settings.normalize_queue_key(queue_key)
    return (await load_effective_model_concurrency_limits((normalized,)))[normalized]


async def set_model_concurrency_override(
    session: AsyncSession,
    queue_key: str,
    limit: int | None,
) -> tuple[str, int, int | None]:
    normalized = settings.normalize_queue_key(queue_key)
    if limit is not None and not 0 <= limit <= MAX_MODEL_CONCURRENCY:
        raise ValueError(f"limit must be between 0 and {MAX_MODEL_CONCURRENCY}")
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
    deploy_limit = settings.get_model_concurrency(normalized)
    return normalized, limit if limit is not None else deploy_limit, limit
