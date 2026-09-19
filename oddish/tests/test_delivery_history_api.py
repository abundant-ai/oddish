"""Standalone-server history routes over real HTTP against local PostgreSQL."""

from contextlib import asynccontextmanager
import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from test_delivery_backfill import bundle

from oddish.core.ingest.delivery_backfill import build_plan
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
async def test_inventory_then_preview_then_apply_over_http(history_api):
    client = history_api
    response = await client.get("/deliveries/task-inventory")
    assert response.status_code == 200, response.text
    inventory = response.json()
    assert inventory["org_id"] == "local"
    by_id = {task["id"]: task for task in inventory["tasks"]}
    assert (
        by_id["task-1"]["org_id"] == "local" and by_id["task-1"]["retired_at"] is None
    )
    assert by_id["task-old"]["retired_at"] is not None

    plan = build_plan(bundle(), org_id="local", inventory=inventory)
    files = {
        "plan": ("plan.json", json.dumps(plan).encode(), "application/json"),
        "inventory": (
            "inventory.json",
            json.dumps(inventory).encode(),
            "application/json",
        ),
    }
    response = await client.post("/deliveries/history-imports", files=files)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert (preview["mode"], preview["outcome"]) == ("preview", "previewed")
    assert preview["summary"]["delivery_history"]["created"] == 1

    response = await client.post(
        "/deliveries/history-imports", files=files, data={"apply": "true"}
    )
    assert response.status_code == 200, response.text
    applied = response.json()
    assert (applied["mode"], applied["outcome"]) == ("apply", "applied")

    response = await client.get("/deliveries/history-imports")
    assert [r["outcome"] for r in response.json()] == ["applied", "previewed"]

    # A plan for another organization is rejected with a receipt, not a 500.
    foreign_inventory = {
        **inventory,
        "org_id": "org-x",
        "tasks": [{**task, "org_id": "org-x"} for task in inventory["tasks"]],
    }
    foreign = build_plan(bundle(), org_id="org-x", inventory=foreign_inventory)
    response = await client.post(
        "/deliveries/history-imports",
        files={
            **files,
            "plan": ("plan.json", json.dumps(foreign).encode(), "application/json"),
        },
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "rejected"

    response = await client.post(
        "/deliveries/history-imports",
        files={**files, "plan": ("plan.json", b"not json", "application/json")},
    )
    assert response.status_code == 422
