"""Per-user auto-probe default: a user with run_probe_default=True opts their
NEW task submissions into run_probe without passing --run-probe.

Runs against an empty Postgres via ``ODDISH_DATABASE_URL``; skips otherwise.
"""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models  # noqa: F401  registers cloud tables on the shared Base
from auth.types import AuthContext, AuthMethod
from api.routers.tasks import _apply_user_run_probe_default
from oddish.db.models import Base
from oddish.schemas import AgentModelPair, TaskSweepSubmission

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]


async def _setup(session_maker):
    async with session_maker() as s:
        await s.execute(
            text(
                "insert into organizations (id,name,slug,plan,settings,is_active,created_at,updated_at) "
                "values ('org1','O','o','free','{}'::jsonb,true,now(),now())"
            )
        )
        await s.execute(
            text(
                "insert into users (id,org_id,role,email,is_active,run_probe_default,created_at,updated_at) "
                "values ('u-on','org1','member','on@x.io',true,true,now(),now()),"
                "       ('u-off','org1','member','off@x.io',true,false,now(),now())"
            )
        )
        await s.commit()


def _sub(run_probe: bool = False) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="t1",
        run_probe=run_probe,
        configs=[AgentModelPair(agent="nop")],
    )


def _auth(user_id: str) -> AuthContext:
    return AuthContext(method=AuthMethod.API_KEY, org_id="org1", user_id=user_id)


async def test_default_on_user_flips_run_probe():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.execute(text("drop schema public cascade"))
            await c.execute(text("create schema public"))
            await c.run_sync(Base.metadata.create_all)
        await _setup(maker)

        async with maker() as session:
            sub = _sub()
            await _apply_user_run_probe_default(session, sub, _auth("u-on"))
            assert sub.run_probe is True  # default-on user opts in

            sub_off = _sub()
            await _apply_user_run_probe_default(session, sub_off, _auth("u-off"))
            assert sub_off.run_probe is False  # default-off user unchanged
    finally:
        await engine.dispose()


async def test_explicit_run_probe_true_is_untouched():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as c:
            await c.execute(text("drop schema public cascade"))
            await c.execute(text("create schema public"))
            await c.run_sync(Base.metadata.create_all)
        await _setup(maker)

        async with maker() as session:
            sub = _sub(run_probe=True)
            # default-off user, but explicit True must stay True (no downgrade).
            await _apply_user_run_probe_default(session, sub, _auth("u-off"))
            assert sub.run_probe is True
    finally:
        await engine.dispose()
