"""Real PostgreSQL approval and connection lifetime; resource/storage readers are controlled."""

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import event, select

import auth
from auth.types import AuthMethod
from auth.verification import CachedAuthData, set_cached_auth
from api.routers import deliveries, tasks, trials
from models import APIKeyScope, OrganizationModel, hash_api_key
from oddish.core.task_files import TaskFileSource
from oddish.db import engine, get_session

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("ODDISH_DATABASE_URL"),
        reason="disposable local PostgreSQL required",
    ),
]


@pytest_asyncio.fixture
async def api(monkeypatch):
    org_id = f"read_reuse_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id,
                name=org_id,
                slug=org_id,
                clerk_org_id=org_id,
                execution_enabled=True,
            )
        )
    key = f"ok_{uuid.uuid4().hex}"
    cached = CachedAuthData(
        method=AuthMethod.API_KEY, org_id=org_id, scope=APIKeyScope.FULL
    )
    set_cached_auth(f"apikey:{hash_api_key(key)}", cached)
    app = FastAPI()
    for router in (deliveries.router, tasks.router, trials.router):
        app.include_router(router)
    counts = SimpleNamespace(checkouts=0, active=0)

    def checkout(*_):
        counts.checkouts += 1
        counts.active += 1

    def checkin(*_):
        counts.active -= 1

    event.listen(engine.sync_engine, "checkout", checkout)
    event.listen(engine.sync_engine, "checkin", checkin)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
            headers={"Authorization": f"Bearer {key}"},
        ) as client:
            yield SimpleNamespace(
                client=client, org_id=org_id, counts=counts, cached=cached
            )
        assert counts.active == 0
    finally:
        event.remove(engine.sync_engine, "checkout", checkout)
        event.remove(engine.sync_engine, "checkin", checkin)
        async with get_session() as session:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@pytest.mark.parametrize(
    "module,reader,path,expected",
    [
        (
            trials,
            "get_trial_response_for_org_core",
            "/trials/trial-1",
            {"trial_id": "trial-1"},
        ),
        (
            trials,
            "get_trial_by_index_core",
            "/tasks/task-1/trials/2",
            {"task_id": "task-1", "index": 2},
        ),
        (
            tasks,
            "get_task_open_core",
            "/tasks/task-1/open?version_id=version-7",
            {"version_id": "version-7"},
        ),
        (tasks, "get_task_panel_core", "/tasks/task-1/panel?version=7", {"version": 7}),
        (tasks, "get_task_detail_core", "/tasks/task-1/detail", {"task_id": "task-1"}),
        (
            deliveries,
            "get_delivery_board_core",
            "/deliveries/delivery-1",
            {"delivery_id": "delivery-1"},
        ),
        (
            deliveries,
            "get_task_qa_history_core",
            "/tasks/task-1/qa-history",
            {"task_id": "task-1"},
        ),
    ],
)
async def test_cached_api_key_shares_one_checkout_with_route_reads(
    api, monkeypatch, module, reader, path, expected
):
    async def read(session, **kwargs):
        assert api.counts.active == 1
        assert kwargs["org_id"] == api.org_id
        assert {key: kwargs[key] for key in expected} == expected
        assert await session.scalar(select(1)) == 1
        return JSONResponse({"ok": True})

    monkeypatch.setattr(module, reader, read)
    monkeypatch.setattr(deliveries, "_fill_user_names", AsyncMock())
    response = await api.client.get(path)
    assert response.status_code == 200, response.text
    assert api.counts.checkouts == 1
    assert api.counts.active == 0


async def test_revocation_and_scope_still_block_before_resource_read(api, monkeypatch):
    reader = AsyncMock(return_value=JSONResponse({"ok": True}))
    monkeypatch.setattr(trials, "get_trial_response_for_org_core", reader)
    assert (await api.client.get("/trials/trial-1")).status_code == 200
    reader.reset_mock()
    async with get_session() as session:
        await session.execute(
            OrganizationModel.__table__.update()
            .where(OrganizationModel.id == api.org_id)
            .values(execution_enabled=False)
        )
    response = await api.client.get("/trials/trial-1")
    assert response.status_code == 403
    reader.assert_not_awaited()
    async with get_session() as session:
        await session.execute(
            OrganizationModel.__table__.update()
            .where(OrganizationModel.id == api.org_id)
            .values(execution_enabled=True)
        )
    api.cached.scope = APIKeyScope.READ
    board_reader = AsyncMock()
    monkeypatch.setattr(deliveries, "get_delivery_board_core", board_reader)
    assert (await api.client.get("/deliveries/delivery-1")).status_code == 403
    board_reader.assert_not_awaited()
    assert api.counts.active == 0


@pytest.mark.parametrize(
    "path,storage",
    [
        (
            "/tasks/task-1/files?version=7&recursive=0&prefix=environment&limit=100&cursor=page-2&inline=false&presign=false",
            "list_task_files_s3",
        ),
        (
            "/tasks/task-1/files/instruction.md?version=7&max_bytes=100",
            "get_task_file_content_s3",
        ),
    ],
)
async def test_file_reads_release_connection_before_waiting_on_storage(
    api, monkeypatch, path, storage
):
    async def source(session, **kwargs):
        assert kwargs == {"task_id": "task-1", "org_id": api.org_id, "version": 7}
        assert await session.scalar(select(1)) == 1
        return TaskFileSource(
            7,
            "tasks/task-1/v7/",
            "tasks/task-1/v7-files/.oddish-manifest.json",
            "hash-v7",
        )

    entered, release = asyncio.Event(), asyncio.Event()

    async def read_storage(**kwargs):
        assert api.counts.active == 0
        assert kwargs["version"] == 7
        if storage == "list_task_files_s3":
            assert kwargs["cursor"] == "page-2"
            assert kwargs["limit"] == 100
            assert kwargs["recursive"] is False
        entered.set()
        await release.wait()
        return {"content": "historical file", "cursor": "page-3"}

    monkeypatch.setattr(tasks, "resolve_task_file_source", source)
    monkeypatch.setattr(tasks, storage, read_storage)
    pending = asyncio.create_task(api.client.get(path))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert api.counts.active == 0
        assert api.counts.checkouts == 1
    finally:
        release.set()
        response = await pending
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "failure", [HTTPException(404, "Not found"), RuntimeError("read failed")]
)
async def test_read_errors_release_the_connection(api, monkeypatch, failure):
    monkeypatch.setattr(
        trials, "get_trial_response_for_org_core", AsyncMock(side_effect=failure)
    )
    if isinstance(failure, HTTPException):
        assert (await api.client.get("/trials/missing")).status_code == 404
    else:
        with pytest.raises(RuntimeError, match="read failed"):
            await api.client.get("/trials/missing")
    assert api.counts.active == 0


async def test_database_failure_does_not_authorize_a_cached_key(api, monkeypatch):
    reader = AsyncMock()
    monkeypatch.setattr(trials, "get_trial_response_for_org_core", reader)
    monkeypatch.setattr(
        auth, "require_execution_org", AsyncMock(side_effect=ConnectionError("offline"))
    )
    with pytest.raises(ConnectionError, match="offline"):
        await api.client.get("/trials/trial-1")
    reader.assert_not_awaited()
    assert api.counts.active == 0


@pytest.mark.parametrize("by_index", [False, True], ids=["by-id", "by-index"])
async def test_versioned_trial_detail_cold_and_warm_query_counts(api, by_index):
    """Count actual route reads, including approval, with no mocked core reader."""
    from oddish.db import (
        ExperimentModel,
        TaskModel,
        TaskVersionModel,
        TrialModel,
        TrialStatus,
        VerdictStatus,
    )

    task_id = f"trial_budget_{uuid.uuid4().hex[:8]}"
    trial_id = f"{task_id}-0"
    async with get_session() as session:
        session.add(ExperimentModel(id=task_id, name=task_id, org_id=api.org_id))
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                task_path="/tmp/task",
                org_id=api.org_id,
                user="test",
            )
        )
        await session.flush()
        session.add(
            TaskVersionModel(
                id=f"{task_id}-v1",
                task_id=task_id,
                version=1,
                task_path="/tmp/v1",
                pre_trial={"items": [], "cost_usd": 0.25},
                pre_trial_status=VerdictStatus.SUCCESS,
            )
        )
        await session.flush()
        session.add(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task_id,
                experiment_id=task_id,
                task_version_id=f"{task_id}-v1",
                org_id=api.org_id,
                agent="claude-code",
                provider="anthropic",
                queue_key="anthropic/test",
                status=TrialStatus.SUCCESS,
            )
        )
    statements = []

    def record(_conn, _cursor, statement, *_):
        statements.append(statement)

    path = f"/tasks/{task_id}/trials/0" if by_index else f"/trials/{trial_id}"
    event.listen(engine.sync_engine, "after_cursor_execute", record)
    try:
        api.counts.checkouts = 0
        cold = await api.client.get(path)
        assert cold.status_code == 200, cold.text
        assert len(statements) == 5, statements
        assert api.counts.checkouts == 1
        statements.clear()
        warm = await api.client.get(path)
        assert warm.status_code == 200, warm.text
        assert len(statements) == 4, statements
        assert api.counts.checkouts == 2
        assert warm.json() == cold.json()
        assert cold.json()["pre_trial_status"] == "success"
        assert cold.json()["pre_trial_cost_usd"] == 0.25
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", record)
        async with get_session() as session:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.id == trial_id)
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(ExperimentModel.id == task_id)
            )
