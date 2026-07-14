"""HTTP-level tests for the reports router (DB-backed).

Run with backend env sourced and the reports/experiments tables present:

set -a && source .env && set +a && uv run pytest tests/test_reports_router.py

Uses an async httpx client (ASGITransport) rather than the sync
``fastapi.testclient.TestClient``: the sync client drives the app from a
throwaway event loop per call, which conflicts with the asyncpg connection
pool bound to pytest-asyncio's session loop by the async fixtures below
("attached to a different loop"). Staying fully async keeps everything on
one loop, matching the rest of the DB-backed test suite (see test_skills.py).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import APIKeyScope, AuthContext, AuthMethod, require_auth
from oddish.db import ExperimentModel, get_session
from oddish.db.models import ReportModel, report_experiments


@pytest_asyncio.fixture
async def org_id():
    oid = f"org_rpt_{uuid.uuid4().hex[:8]}"
    yield oid
    async with get_session() as session:
        report_ids = (
            await session.execute(
                ReportModel.__table__.select()
                .with_only_columns(ReportModel.id)
                .where(ReportModel.org_id == oid)
            )
        ).scalars().all()
        if report_ids:
            await session.execute(
                report_experiments.delete().where(
                    report_experiments.c.report_id.in_(report_ids)
                )
            )
        await session.execute(
            ReportModel.__table__.delete().where(ReportModel.org_id == oid)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(ExperimentModel.org_id == oid)
        )
        await session.commit()


@pytest_asyncio.fixture
async def experiment_id(org_id):
    async with get_session() as session:
        exp = ExperimentModel(name="exp-1", org_id=org_id)
        session.add(exp)
        await session.commit()
        await session.refresh(exp)
        return exp.id


@pytest_asyncio.fixture
async def client(org_id):
    fake_auth = AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=org_id,
        user_id="user_1",
        scope=APIKeyScope.FULL,
    )

    async def _fake_require_auth() -> AuthContext:
        return fake_auth

    app = create_app()
    app.dependency_overrides[require_auth] = _fake_require_auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_create_lists_and_gets_report(client, experiment_id, monkeypatch):
    # Stub enqueue so no dispatcher is needed.
    import oddish.core.reports as reports_core

    async def _noop(session, *, report_id, org_id):
        return None

    monkeypatch.setattr(reports_core, "_enqueue_report_worker_job", _noop)

    resp = await client.post(
        "/reports",
        json={"name": "Q3", "experiment_ids": [experiment_id]},
    )
    assert resp.status_code == 200, resp.text
    report_id = resp.json()["id"]
    assert resp.json()["status"] == "pending"
    assert resp.json()["experiment_ids"] == [experiment_id]

    listed = await client.get("/reports")
    assert listed.status_code == 200, listed.text
    assert any(r["id"] == report_id for r in listed.json())

    got = await client.get(f"/reports/{report_id}")
    assert got.status_code == 200, got.text
    assert got.json()["name"] == "Q3"
    assert got.json()["experiment_ids"] == [experiment_id]


@pytest.mark.asyncio
async def test_experiment_options_lists_org_experiments(client, experiment_id):
    resp = await client.get("/reports/experiment-options")
    assert resp.status_code == 200, resp.text
    assert any(opt["id"] == experiment_id for opt in resp.json())


@pytest.mark.asyncio
async def test_delete_report_removes_it(client, experiment_id, monkeypatch):
    import oddish.core.reports as reports_core

    async def _noop(session, *, report_id, org_id):
        return None

    monkeypatch.setattr(reports_core, "_enqueue_report_worker_job", _noop)

    resp = await client.post(
        "/reports",
        json={"name": "to-delete", "experiment_ids": [experiment_id]},
    )
    report_id = resp.json()["id"]

    deleted = await client.delete(f"/reports/{report_id}")
    assert deleted.status_code == 200, deleted.text

    got = await client.get(f"/reports/{report_id}")
    assert got.status_code == 404
