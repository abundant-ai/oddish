# backend/tests/cc_chat/conftest.py
import os
import pytest
import pytest_asyncio
import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.db.models import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

URL = os.environ.get("ODDISH_DATABASE_URL")

ORG = "org_cc_test"


async def reset_and_seed(engine):
    async with engine.begin() as c:
        await c.execute(text("drop schema public cascade"))
        await c.execute(text("create schema public"))
        await c.run_sync(Base.metadata.create_all)
        await c.execute(
            text(
                "insert into organizations (id, name, slug, plan, settings, is_active, created_at, updated_at) "
                "values (:id, 'CC Test', 'cc-test', 'free', '{}', true, now(), now())"
            ),
            {"id": ORG},
        )


async def seed_session(maker, *, session_id="cs_1", status="active", scope_kind="experiment", scope_id="exp_1"):
    from models import ChatSession
    async with maker() as s:
        s.add(ChatSession(
            id=session_id, org_id=ORG, user_id="u1",
            scope_kind=scope_kind, scope_id=scope_id,
            status=status, daytona_session_id="cc",
        ))
        await s.commit()


@pytest_asyncio.fixture
async def db():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    engine = create_async_engine(URL)
    await reset_and_seed(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()
