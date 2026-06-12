"""Task status must list ALL live experiments a task belongs to (ABT-645).

The task <-> experiment relation is many-to-many; the response keeps the
singular experiment_* fields (the primary pick) for compatibility and adds
``experiments`` with every live link, excluding soft-deleted memberships.
Runs against an empty Postgres via ``ODDISH_DATABASE_URL``; skips otherwise.
"""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.core.endpoints import get_task_status_core
from oddish.db.models import Base

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]


async def test_task_status_lists_all_live_experiments():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.execute(text("drop schema public cascade"))
            await c.execute(text("create schema public"))
            await c.run_sync(Base.metadata.create_all)
            stmts = """
                insert into organizations (id,name,slug,plan,settings,is_active,created_at,updated_at)
                values ('org1','O','o','free','{}'::jsonb,true,now(),now());
                insert into experiments (id,name,org_id,is_public,created_at,updated_at)
                values ('exp-a','Alpha','org1',false,now(),now()),
                       ('exp-b','Beta','org1',false,now(),now()),
                       ('exp-gone','Gone','org1',false,now(),now());
                insert into tasks (id,name,org_id,"user",priority,status,task_path,tags,run_analysis,run_probe,created_at,updated_at)
                values ('t1','task','org1','u','LOW','COMPLETED','p','{}'::jsonb,false,false,now(),now());
                insert into task_experiments (task_id,experiment_id,created_at,deleted_at)
                values ('t1','exp-a',now() - interval '2 hour',null),
                       ('t1','exp-b',now() - interval '1 hour',null),
                       ('t1','exp-gone',now(),now());
            """
            for stmt in stmts.split(";"):
                if stmt.strip():
                    await c.execute(text(stmt))

        async with maker() as session:
            resp = await get_task_status_core(
                session, task_id="t1", include_trials=False, org_id="org1"
            )

        listed = [(e.id, e.name) for e in resp.experiments]
        assert ("exp-a", "Alpha") in listed
        assert ("exp-b", "Beta") in listed
        assert all(e.id != "exp-gone" for e in resp.experiments)  # soft-deleted link
        assert len(listed) == 2
        # singular primary fields stay populated for compatibility
        assert resp.experiment_id in {"exp-a", "exp-b"}
        assert resp.experiment_name in {"Alpha", "Beta"}
    finally:
        await engine.dispose()
