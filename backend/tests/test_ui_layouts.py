"""Exercise preference HTTP routes against a disposable local PostgreSQL schema."""

import importlib.util
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.routers import ui_layouts
from auth import AuthContext, AuthMethod, require_auth

PATH = "/users/me/ui-layouts/experiment.trial-drawer"


@pytest_asyncio.fixture
async def preference_api(monkeypatch):
    url = os.environ.get("UI_LAYOUT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("UI_LAYOUT_TEST_DATABASE_URL must name a disposable local database")
    schema = f"ui_layout_test_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    migration_path = (
        Path(__file__).parents[1] / "alembic/versions/user_ui_layouts_001.py"
    )
    spec = importlib.util.spec_from_file_location("layout_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(conn, action):
        with Operations.context(MigrationContext.configure(conn)):
            action()

    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)"))
        await conn.execute(
            text(
                "INSERT INTO users VALUES ('alice-org-a'), ('bob-org-a'), ('alice-org-b')"
            )
        )
        await conn.run_sync(migrate, migration.upgrade)

    @asynccontextmanager
    async def read_session():
        async with async_sessionmaker(
            engine.execution_options(isolation_level="AUTOCOMMIT")
        )() as session:
            yield session

    @asynccontextmanager
    async def write_session():
        async with async_sessionmaker(engine).begin() as session:
            yield session

    monkeypatch.setattr(ui_layouts, "get_read_session", read_session)
    monkeypatch.setattr(ui_layouts, "get_session", write_session)
    app = FastAPI()
    app.include_router(ui_layouts.router)
    auth = AuthContext(
        method=AuthMethod.CLERK_JWT, user_id="alice-org-a", org_id="org-a"
    )
    app.dependency_overrides[require_auth] = lambda: auth
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, auth, engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(migrate, migration.downgrade)
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_roundtrip_replacement_and_account_isolation(preference_api):
    client, auth, engine = preference_api
    defaults = ui_layouts.TrialDrawerLayout().model_dump()
    assert (await client.get(PATH)).json() == defaults
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM user_ui_layouts")) == 0
    saved = {
        **defaults,
        "preferredWidthPx": 1250,
        "taskPanePercent": 63,
        "maximized": True,
        "showTrial": False,
    }
    assert (await client.put(PATH, json=saved)).status_code == 200
    for user_id in ("bob-org-a", "alice-org-b"):
        auth.user_id = user_id
        assert (await client.get(PATH)).json() == defaults
        assert (
            await client.put(PATH, json={**defaults, "preferredWidthPx": 800})
        ).status_code == 200
    auth.user_id = "alice-org-a"
    assert (await client.get(PATH)).json() == saved
    saved["preferredWidthPx"] = 1000
    assert (await client.put(PATH, json=saved)).status_code == 200
    assert (await client.get(PATH)).json() == saved
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM user_ui_layouts")) == 3


@pytest.mark.asyncio
async def test_rejects_keys_anonymous_invalid_layouts_and_unknown_layout_keys(
    preference_api,
):
    client, auth, _ = preference_api
    for method in (AuthMethod.API_KEY, AuthMethod.ANONYMOUS):
        auth.method = method
        assert (await client.get(PATH)).status_code == 403
        assert (await client.put(PATH, json={})).status_code == 403
    auth.method = AuthMethod.CLERK_JWT
    auth.user_id = None
    assert (await client.get(PATH)).status_code == 403
    auth.user_id = "alice-org-a"
    for patch in (
        {"version": 2},
        {"preferredWidthPx": 200},
        {"preferredWidthPx": 20000},
        {"taskPanePercent": 0},
        {"taskPanePercent": 100},
        {"showTask": False, "showTrial": False},
        {"maximized": "true"},
        {"user_id": "bob-org-a"},
    ):
        assert (await client.put(PATH, json=patch)).status_code == 422
    assert (await client.put(PATH + "-unknown", json={})).status_code == 422
    assert (
        await client.get(PATH)
    ).json() == ui_layouts.TrialDrawerLayout().model_dump()
