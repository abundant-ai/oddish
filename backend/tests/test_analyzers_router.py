"""HTTP-level tests for the analyzers router (DB-backed).

Run with backend env sourced and the analyzers/experiments tables present:

set -a && source .env && set +a && uv run pytest tests/test_analyzers_router.py

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
from oddish.db.models import AnalyzerModel, analyzer_experiments


@pytest_asyncio.fixture
async def org_id():
    oid = f"org_rpt_{uuid.uuid4().hex[:8]}"
    yield oid
    async with get_session() as session:
        analyzer_ids = (
            await session.execute(
                AnalyzerModel.__table__.select()
                .with_only_columns(AnalyzerModel.id)
                .where(AnalyzerModel.org_id == oid)
            )
        ).scalars().all()
        if analyzer_ids:
            await session.execute(
                analyzer_experiments.delete().where(
                    analyzer_experiments.c.analyzer_id.in_(analyzer_ids)
                )
            )
        await session.execute(
            AnalyzerModel.__table__.delete().where(AnalyzerModel.org_id == oid)
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
async def test_create_lists_and_gets_analyzer(client, experiment_id, monkeypatch):
    # Stub enqueue so no dispatcher is needed.
    import oddish.core.analyzers as analyzers_core

    async def _noop(session, *, analyzer_id, org_id):
        return None

    monkeypatch.setattr(analyzers_core, "_enqueue_analyzer_worker_job", _noop)

    resp = await client.post(
        "/analyzers",
        json={"name": "Q3", "experiment_ids": [experiment_id]},
    )
    assert resp.status_code == 200, resp.text
    analyzer_id = resp.json()["id"]
    assert resp.json()["status"] == "pending"
    assert resp.json()["experiment_ids"] == [experiment_id]

    listed = await client.get("/analyzers")
    assert listed.status_code == 200, listed.text
    assert any(r["id"] == analyzer_id for r in listed.json())

    got = await client.get(f"/analyzers/{analyzer_id}")
    assert got.status_code == 200, got.text
    assert got.json()["name"] == "Q3"
    assert got.json()["experiment_ids"] == [experiment_id]


@pytest.mark.asyncio
async def test_experiment_options_lists_org_experiments(client, experiment_id):
    resp = await client.get("/analyzers/experiment-options")
    assert resp.status_code == 200, resp.text
    assert any(opt["id"] == experiment_id for opt in resp.json())


@pytest_asyncio.fixture
async def analyzer_id(org_id, experiment_id):
    async with get_session() as session:
        analyzer = AnalyzerModel(name="rollup-target", org_id=org_id)
        session.add(analyzer)
        await session.flush()
        await session.execute(
            analyzer_experiments.insert().values(
                analyzer_id=analyzer.id, experiment_id=experiment_id
            )
        )
        await session.commit()
        return analyzer.id


_UNSET = object()


async def _set_findings(analyzer_id, findings, models_by_task=_UNSET):
    # Defaults to {}, but passing None must reach the column as SQL NULL -- that
    # legacy row (findings persisted before analyzers_006 added the roster) is
    # exactly what test_rollup_unknown_total_when_roster_null covers.
    roster = {} if models_by_task is _UNSET else models_by_task
    async with get_session() as session:
        await session.execute(
            AnalyzerModel.__table__.update()
            .where(AnalyzerModel.id == analyzer_id)
            .values(findings=findings, models_by_task=roster)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_rollup_404_when_findings_null(client, analyzer_id):
    """NULL = analyzed before findings existed. Distinct from 'no failures'."""
    resp = await client.get(f"/analyzers/{analyzer_id}/rollup")
    assert resp.status_code == 404, resp.text
    assert "before findings" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_rollup_200_empty_lanes_when_no_failures(client, analyzer_id):
    await _set_findings(analyzer_id, [])
    resp = await client.get(f"/analyzers/{analyzer_id}/rollup")
    assert resp.status_code == 200, resp.text
    assert resp.json()["good_failures"]["groups"] == []


@pytest.mark.asyncio
async def test_rollup_returns_lanes(client, analyzer_id):
    finding = {
        "trial_id": "t1",
        "bucket": "good",
        "subcategory": "3b",
        "evidence_quote": "q",
        "step_ids": [1],
        "root_cause": "rc",
        "headroom_signal": "",
        "trajectory_link": "/tasks/task/probe/t1",
        "model": "claude-opus-4-8",
        "classification": "GOOD_FAILURE",
        "subtype": "Logic Error",
        "task_id": "task",
        "task_path": "tasks/task",
    }
    await _set_findings(
        analyzer_id, [finding], models_by_task={"task": ["claude-opus-4-8"]}
    )
    resp = await client.get(f"/analyzers/{analyzer_id}/rollup")
    assert resp.status_code == 200, resp.text
    assert resp.json()["good_failures"]["groups"][0]["gaps"][0]["gap"] == "Logic Error"


@pytest.mark.asyncio
async def test_rollup_unknown_total_when_roster_null(client, analyzer_id):
    """An analyzer with findings but no persisted roster (ran before
    analyzers_006) still serves its lanes; the 1a denominator reports unknown.
    Sending it through as an empty roster would claim models_total: 0 -- "no
    model ran this task" -- about a task every model may have run."""
    finding = {
        "trial_id": "t1",
        "bucket": "bad",
        "subcategory": "1a",
        "evidence_quote": "q",
        "step_ids": [1],
        "root_cause": "rc",
        "headroom_signal": "",
        "trajectory_link": "/tasks/task/probe/t1",
        "model": "claude-opus-4-8",
        "classification": "BAD_FAILURE",
        "subtype": "Ambiguous Requirements",
        "task_id": "task",
        "task_path": "tasks/task",
    }
    await _set_findings(analyzer_id, [finding], models_by_task=None)
    resp = await client.get(f"/analyzers/{analyzer_id}/rollup")
    assert resp.status_code == 200, resp.text
    group = resp.json()["task_construction"]["groups"][0]
    assert group["models_total"] is None
    assert group["models_hit"] == ["claude-opus-4-8"]


@pytest.mark.asyncio
async def test_delete_analyzer_removes_it(client, experiment_id, monkeypatch):
    import oddish.core.analyzers as analyzers_core

    async def _noop(session, *, analyzer_id, org_id):
        return None

    monkeypatch.setattr(analyzers_core, "_enqueue_analyzer_worker_job", _noop)

    resp = await client.post(
        "/analyzers",
        json={"name": "to-delete", "experiment_ids": [experiment_id]},
    )
    analyzer_id = resp.json()["id"]

    deleted = await client.delete(f"/analyzers/{analyzer_id}")
    assert deleted.status_code == 200, deleted.text

    got = await client.get(f"/analyzers/{analyzer_id}")
    assert got.status_code == 404
