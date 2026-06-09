"""Local unit test for the preview seed: building the full schema then
seeding yields a consistent, deterministic, convergent preview DB.

The schema is built with ``Base.metadata.create_all`` -- oddish and backend
models register on the same ``DeclarativeBase`` -- rather than replaying the
Alembic chain. The real pipeline only ever applies Alembic incrementally onto
a branch whose schema Supabase already cloned, never from an empty database,
so a from-scratch chain replay is not a supported path.

Run against an empty Postgres by setting ``ODDISH_DATABASE_URL`` (the test
builds the schema itself); it skips otherwise. The deploy-path gate is the
real seed step in the prepare-preview-database job, which seeds the actual
branch -- if the seed breaks against the real schema, that job fails."""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import models  # noqa: F401  registers the cloud tables on the shared Base
import preview_seed
from oddish.db.models import Base

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]


@pytest.fixture(autouse=True)
def _clerk_org(monkeypatch):
    monkeypatch.setenv("ODDISH_PREVIEW_CLERK_ORG_ID", "org_seedtest")


async def _count(engine, sql):
    async with engine.connect() as c:
        return (await c.execute(text(sql))).scalar_one()


async def test_seed_populates_and_is_idempotent_and_convergent():
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        await preview_seed.seed(engine)
        assert await _count(engine, "select count(*) from organizations where id like 'seed-%'") == 1
        assert await _count(engine, "select count(*) from organizations where clerk_org_id = 'org_seedtest'") == 1
        assert await _count(engine, "select count(*) from tasks where id='seed-task-a' and current_version_id='seed-task-a-v2'") == 1
        assert await _count(engine, "select count(*) from task_experiments where task_id like 'seed-%' and deleted_at is not null") >= 1

        tasks_before = await _count(engine, "select count(*) from tasks where id like 'seed-%'")
        await preview_seed.seed(engine)  # idempotent
        assert await _count(engine, "select count(*) from tasks where id like 'seed-%'") == tasks_before

        orig = preview_seed._fixtures  # convergent
        def _edited():
            f = orig()
            f["organizations"][0]["name"] = "Edited Org"
            return f
        preview_seed._fixtures = _edited  # type: ignore[assignment]
        try:
            await preview_seed.seed(engine)
        finally:
            preview_seed._fixtures = orig  # type: ignore[assignment]
        async with engine.connect() as c:
            name = (await c.execute(text("select name from organizations where id='seed-org'"))).scalar_one()
        assert name == "Edited Org"
    finally:
        await engine.dispose()
