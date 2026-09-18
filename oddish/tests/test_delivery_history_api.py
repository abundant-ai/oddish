"""Standalone-server inventory route over real HTTP against local PostgreSQL."""

from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import TaskModel, utcnow


@pytest_asyncio.fixture
async def history_api(monkeypatch):
    import oddish.db.connection as connection
    import oddish.server as server
    import oddish.server.deliveries as routes

    async with connection.engine.connect() as conn:
        transaction = await conn.begin()

        @asynccontextmanager
        async def get_session():
            async with AsyncSession(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session

        for module in (server, routes):
            monkeypatch.setattr(module, "get_session", get_session)
            monkeypatch.setattr(module, "get_read_session", get_session)
        async with get_session() as session:
            live = TaskModel(
                id="task-1", name="new-name", task_path="s3://t/new-name", user="tester"
            )
            retired = TaskModel(
                id="task-old",
                name="retired-name",
                task_path="s3://t/retired",
                user="tester",
                deleted_at=utcnow(),
            )
            session.add_all([live, retired])
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.api), base_url="http://test"
        ) as client:
            try:
                yield client
            finally:
                await transaction.rollback()


@pytest.mark.asyncio
async def test_inventory_lists_live_and_retired_tasks_with_the_local_label(history_api):
    response = await history_api.get("/deliveries/task-inventory")
    assert response.status_code == 200, response.text
    inventory = response.json()
    assert inventory["org_id"] == "local"
    by_id = {task["id"]: task for task in inventory["tasks"]}
    assert (
        by_id["task-1"]["org_id"] == "local" and by_id["task-1"]["retired_at"] is None
    )
    assert by_id["task-old"]["retired_at"] is not None
