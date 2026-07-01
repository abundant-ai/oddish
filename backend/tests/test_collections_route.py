"""HTTP-level tests for POST /experiments/collections.

Exercises the route, auth gating, and name validation against the real
local Postgres. Run with the backend env sourced:

    set -a && source .env && set +a && uv run pytest tests/test_collections_route.py -v
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from models import APIKeyScope, OrganizationModel
from oddish.core.api_keys import create_api_key
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
    task_experiments,
)


# ---------------------------------------------------------------------------
# Teardown helper
# ---------------------------------------------------------------------------


async def _cleanup(
    *,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    experiment_ids: list[str] | None = None,
    api_key_ids: list[str] | None = None,
    org_ids: list[str] | None = None,
) -> None:
    from oddish.db.models import APIKeyModel
    from oddish.db import experiment_trials

    async with get_session() as session:
        if experiment_ids:
            await session.execute(
                experiment_trials.delete().where(
                    experiment_trials.c.experiment_id.in_(experiment_ids)
                )
            )
        if trial_ids:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.id.in_(trial_ids))
            )
        if task_ids:
            await session.execute(
                task_experiments.delete().where(
                    task_experiments.c.task_id.in_(task_ids)
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id.in_(task_ids))
            )
        if experiment_ids:
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id.in_(experiment_ids)
                )
            )
        if api_key_ids:
            await session.execute(
                APIKeyModel.__table__.delete().where(APIKeyModel.id.in_(api_key_ids))
            )
        if org_ids:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id.in_(org_ids)
                )
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    return create_app()


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def seed_org_with_trials():
    """Seed an org with a task, an experiment, two trials, and a TASKS-scope key.

    Yields (org_id, trial_id_1, trial_id_2, raw_api_key). Tears itself down.
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_col_{suffix}"
    experiment_id = f"exp_col_{suffix}"
    task_id = f"task_col_{suffix}"
    trial_id_1 = f"trial_col_1_{suffix}"
    trial_id_2 = f"trial_col_2_{suffix}"

    api_key_model = None
    created_experiment_ids: list[str] = [experiment_id]

    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=f"Test Org {suffix}", slug=f"test-org-{suffix}"
            )
        )
        session.add(
            ExperimentModel(id=experiment_id, name=f"col-test-{suffix}", org_id=org_id)
        )
        session.add(
            TaskModel(
                id=task_id,
                name=f"col-task-{suffix}",
                user="test",
                task_path="/tmp/fake",
                org_id=org_id,
            )
        )
        await session.flush()
        await session.execute(
            task_experiments.insert().values(
                task_id=task_id,
                experiment_id=experiment_id,
                deleted_at=None,
            )
        )
        for trial_id in (trial_id_1, trial_id_2):
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    agent="claude-code",
                    provider="anthropic",
                    model="anthropic/claude-sonnet-4-6",
                    queue_key=f"test-col-{trial_id}",
                    status=TrialStatus.QUEUED,
                    origin=TrialOrigin.ODDISH,
                    is_probe=False,
                )
            )
        api_key_model, raw_key = create_api_key(
            org_id=org_id, name=f"test-key-{suffix}", scope=APIKeyScope.TASKS
        )
        session.add(api_key_model)

    try:
        yield org_id, trial_id_1, trial_id_2, raw_key, created_experiment_ids
    finally:
        await _cleanup(
            trial_ids=[trial_id_1, trial_id_2],
            task_ids=[task_id],
            experiment_ids=created_experiment_ids,
            api_key_ids=[api_key_model.id] if api_key_model else None,
            org_ids=[org_id],
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_collection_route(client, seed_org_with_trials):
    _org_id, t1, t2, raw_key, created_experiment_ids = seed_org_with_trials

    resp = await client.post(
        "/experiments/collections",
        json={"name": "my collection", "trial_ids": [t1, t2]},
        headers={"Authorization": f"Bearer {raw_key}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["trials_linked"] == 2
    assert body["name"] == "my collection"
    created_experiment_ids.append(body["id"])


@pytest.mark.asyncio
async def test_create_collection_rejects_unknown_trial(client, seed_org_with_trials):
    _org_id, t1, _t2, raw_key, _created_experiment_ids = seed_org_with_trials

    resp = await client.post(
        "/experiments/collections",
        json={"name": "c", "trial_ids": [t1, "nope"]},
        headers={"Authorization": f"Bearer {raw_key}"},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_collection_rejects_empty_name(client, seed_org_with_trials):
    _org_id, t1, t2, raw_key, _created_experiment_ids = seed_org_with_trials

    resp = await client.post(
        "/experiments/collections",
        json={"name": "   ", "trial_ids": [t1, t2]},
        headers={"Authorization": f"Bearer {raw_key}"},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_collection_requires_auth(client):
    resp = await client.post(
        "/experiments/collections",
        json={"name": "x", "trial_ids": ["a"]},
    )
    assert resp.status_code in (401, 403)
