"""Server-side request idempotency for ``POST /tasks/sweep``.

Exercises the backend ``submission_idempotency`` migration and the dedup /
replay / TTL / lock policy in ``create_task_sweep_core``. Runs against the
dedicated Postgres in ``ODDISH_DATABASE_URL`` (see the worktree setup); skipped
when that is unset.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import models  # noqa: F401  registers SubmissionIdempotency on the shared Base
from oddish.db.models import Base

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set")

BACKEND_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Migration round-trip
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


async def _reset_and_create_all() -> None:
    """Rebuild the full current schema from the ORM models.

    The backend migration chain's first step requires the OSS oddish ``tasks``
    table to exist, and the from-scratch oddish chain carries unrelated seeding
    drift, so we materialize the present-day schema directly and exercise just
    this migration's up/down SQL against it.
    """
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _relation_exists(name: str) -> bool:
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT to_regclass(:n)"), {"n": f"public.{name}"}
            )
            return result.scalar() is not None
    finally:
        await engine.dispose()


def test_migration_applies_and_downgrades_cleanly() -> None:
    """``downgrade -1`` drops the table + unique index, and re-applying recreates
    them cleanly (the migration's real up/down SQL run via Alembic)."""
    cfg = _alembic_config()
    asyncio.run(_reset_and_create_all())
    command.stamp(cfg, "head")

    # downgrade -1 runs this migration's downgrade().
    command.downgrade(cfg, "-1")
    assert not asyncio.run(_relation_exists("submission_idempotency"))
    assert not asyncio.run(_relation_exists("uq_submission_idempotency_org_route_key"))
    # Reversing one step leaves the preceding schema intact.
    assert asyncio.run(_relation_exists("organizations"))

    # upgrade head runs this migration's upgrade() -> table + unique index back.
    command.upgrade(cfg, "head")
    assert asyncio.run(_relation_exists("submission_idempotency"))
    assert asyncio.run(_relation_exists("uq_submission_idempotency_org_route_key"))
