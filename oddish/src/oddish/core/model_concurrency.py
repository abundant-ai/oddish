"""Database-backed per-queue concurrency overrides."""

import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import settings

logger = logging.getLogger(__name__)

MAX_MODEL_CONCURRENCY = 10_000


async def get_model_concurrency_overrides(
    session: AsyncSession,
    queue_keys: Iterable[str] | None = None,
) -> dict[str, int]:
    """Return stored overrides by normalized queue key."""
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
    """Return override or deploy limits, keyed as supplied by the caller."""
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
    """Load effective limits, failing closed when overrides cannot be read."""
    keys = tuple(queue_keys)
    try:
        from oddish.db import get_session

        async with get_session() as session:
            return await get_effective_model_concurrency_limits(session, keys)
    except Exception as e:  # noqa: BLE001 - an unknown override must stop dispatch
        logger.warning("Concurrency overrides unavailable; dispatch disabled: %s", e)
        return dict.fromkeys(keys, 0)


async def load_effective_model_concurrency_limit(queue_key: str) -> int:
    return (await load_effective_model_concurrency_limits((queue_key,)))[queue_key]


async def set_model_concurrency_override(
    session: AsyncSession,
    queue_key: str,
    limit: int | None,
    *,
    actor_user_id: str,
    actor_api_key_id: str | None = None,
) -> str:
    """Upsert or clear an override and return its normalized queue key."""
    if limit is not None and not 0 <= limit <= MAX_MODEL_CONCURRENCY:
        raise ValueError(f"limit must be between 0 and {MAX_MODEL_CONCURRENCY}")
    if not actor_user_id.strip():
        raise ValueError("actor_user_id is required")
    normalized = settings.normalize_queue_key(queue_key)
    params = {"queue_key": normalized}
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:queue_key, 0))"),
        params,
    )
    old_limit = (
        await session.execute(
            text(
                "SELECT concurrency_limit FROM model_concurrency_overrides "
                "WHERE queue_key = :queue_key"
            ),
            params,
        )
    ).scalar_one_or_none()
    if old_limit == limit:
        return normalized

    if limit is None:
        await session.execute(
            text(
                "DELETE FROM model_concurrency_overrides WHERE queue_key = :queue_key"
            ),
            params,
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
    await session.execute(
        text(
            """
            INSERT INTO model_concurrency_audit
                (queue_key, old_override_limit, new_override_limit,
                 old_effective_limit, new_effective_limit,
                 actor_user_id, actor_api_key_id)
            VALUES
                (:queue_key, :old_override_limit, :new_override_limit,
                 :old_effective_limit, :new_effective_limit,
                 :actor_user_id, :actor_api_key_id)
            """
        ),
        {
            "queue_key": normalized,
            "old_override_limit": old_limit,
            "new_override_limit": limit,
            "old_effective_limit": (
                old_limit if old_limit is not None else deploy_limit
            ),
            "new_effective_limit": limit if limit is not None else deploy_limit,
            "actor_user_id": actor_user_id,
            "actor_api_key_id": actor_api_key_id,
        },
    )
    return normalized


async def list_model_concurrency_audit(
    session: AsyncSession,
    *,
    queue_key: str | None = None,
    before_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, object] = {"limit": limit}
    if queue_key is not None:
        clauses.append("queue_key = :queue_key")
        params["queue_key"] = settings.normalize_queue_key(queue_key)
    if before_id is not None:
        clauses.append("id < :before_id")
        params["before_id"] = before_id
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    result = await session.execute(
        text(
            f"""
            SELECT id, queue_key, old_override_limit, new_override_limit,
                   old_effective_limit, new_effective_limit,
                   actor_user_id, actor_api_key_id, changed_at
            FROM model_concurrency_audit
            {where}
            ORDER BY id DESC
            LIMIT :limit
            """
        ),
        params,
    )
    return [dict(row) for row in result.mappings().all()]
