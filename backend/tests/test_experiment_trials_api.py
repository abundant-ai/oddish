"""HTTP-level tests for GET /experiments/{id}/trials.

Exercises the route, auth gating, and org scoping against the real local
Postgres. Run with the backend env sourced:

    set -a && source .env && set +a && uv run pytest tests/test_experiment_trials_api.py -v
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from models import APIKeyScope
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
from models import OrganizationModel


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

    async with get_session() as session:
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
                APIKeyModel.__table__.delete().where(
                    APIKeyModel.id.in_(api_key_ids)
                )
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
async def seed_experiment_with_trials():
    """Seed an experiment with two trials (one probe, one non-probe) and an org API key.

    Yields (experiment_id, raw_api_key).
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_et_{suffix}"
    experiment_id = f"exp_et_{suffix}"
    task_id = f"task_et_{suffix}"
    trial_real_id = f"trial_et_real_{suffix}"
    trial_probe_id = f"trial_et_probe_{suffix}"

    api_key_model = None
    raw_key = None

    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=f"Test Org {suffix}", slug=f"test-org-{suffix}"
            )
        )
        session.add(
            ExperimentModel(id=experiment_id, name=f"et-test-{suffix}", org_id=org_id)
        )
        session.add(
            TaskModel(
                id=task_id,
                name=f"et-task-{suffix}",
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
        session.add(
            TrialModel(
                id=trial_real_id,
                name=f"{task_id}-real",
                task_id=task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key="test-et",
                status=TrialStatus.QUEUED,
                origin=TrialOrigin.ODDISH,
                is_probe=False,
            )
        )
        session.add(
            TrialModel(
                id=trial_probe_id,
                name=f"{task_id}-probe",
                task_id=task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key="test-et-probe",
                status=TrialStatus.QUEUED,
                origin=TrialOrigin.ODDISH,
                is_probe=True,
            )
        )
        api_key_model, raw_key = create_api_key(
            org_id=org_id, name=f"test-key-{suffix}", scope=APIKeyScope.READ
        )
        session.add(api_key_model)

    yield experiment_id, raw_key

    await _cleanup(
        trial_ids=[trial_real_id, trial_probe_id],
        task_ids=[task_id],
        experiment_ids=[experiment_id],
        api_key_ids=[api_key_model.id] if api_key_model else None,
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_experiment_trials_returns_org_scoped_rows(client, seed_experiment_with_trials):
    exp_id, org_key = seed_experiment_with_trials
    resp = await client.get(
        f"/experiments/{exp_id}/trials",
        headers={"Authorization": f"Bearer {org_key}"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert {r["is_probe"] for r in rows} == {True, False}
    assert all(r["experiment_id"] == exp_id for r in rows)


@pytest.mark.asyncio
async def test_experiment_trials_requires_auth(client):
    resp = await client.get("/experiments/exp_x/trials")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_experiment_trials_does_not_leak_across_orgs(
    client, seed_experiment_with_trials
):
    """Org B's key must not see org A's experiment trials."""
    exp_id, _org_a_key = seed_experiment_with_trials

    suffix = uuid.uuid4().hex[:8]
    org_b_id = f"org_etb_{suffix}"
    api_key_model = None
    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_b_id, name=f"Other Org {suffix}", slug=f"other-org-{suffix}"
            )
        )
        api_key_model, org_b_key = create_api_key(
            org_id=org_b_id, name=f"other-key-{suffix}", scope=APIKeyScope.READ
        )
        session.add(api_key_model)

    try:
        resp = await client.get(
            f"/experiments/{exp_id}/trials",
            headers={"Authorization": f"Bearer {org_b_key}"},
        )
        assert resp.status_code == 200
        # org B is authenticated but owns none of org A's experiment trials.
        assert resp.json() == []
    finally:
        await _cleanup(
            api_key_ids=[api_key_model.id] if api_key_model else None,
            org_ids=[org_b_id],
        )
