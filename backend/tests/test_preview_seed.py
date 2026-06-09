"""Gate: applying both Alembic stacks then seeding yields a consistent,
deterministic, convergent preview DB. Requires MIGRATED_DB_URL pointing
at a Postgres where both stacks are already at head (CI provides it)."""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import preview_seed

URL = os.environ.get("MIGRATED_DB_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="MIGRATED_DB_URL not set"),
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
