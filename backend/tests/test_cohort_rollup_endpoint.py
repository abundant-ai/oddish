"""HTTP-level tests for GET /experiments/{id}/cohort-rollup.

READ-scoped, generation-free. `build_cohort_rollup`'s own logic (coverage
counting, per-model deltas, missing-comparison reporting) is covered by
`test_cohort_rollup_build.py`; this file only exercises the route: auth
gating and that the real service is wired up end to end. Mirrors
`test_experiment_trials_api.py`'s client/API-key fixtures and
`test_cohort_rollup_build.py`'s dataclass fixture style, against the real
local Postgres. Run with the backend env sourced:

    set -a && source .env && set +a && uv run pytest tests/test_cohort_rollup_endpoint.py -v
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, OrganizationModel
from oddish.core.api_keys import create_api_key
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    experiment_trials,
    get_session,
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

    async with get_session() as session:
        if trial_ids:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.id.in_(trial_ids))
            )
        if task_ids:
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
# Fixtures: app / client
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


# ---------------------------------------------------------------------------
# Fixture: an org with a real READ-scoped API key
#
# seeded_collection and empty_experiment both hang off this org so that
# auth_headers (minted here) actually authorizes the requests the tests make
# against their data.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def org_with_read_key():
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_cre_{suffix}"

    api_key_model = None
    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=f"CRE Org {suffix}", slug=f"cre-org-{suffix}"
            )
        )
        api_key_model, raw_key = create_api_key(
            org_id=org_id, name=f"cre-key-{suffix}", scope=APIKeyScope.READ
        )
        session.add(api_key_model)

    yield {"org_id": org_id, "raw_key": raw_key}

    await _cleanup(
        api_key_ids=[api_key_model.id] if api_key_model else None,
        org_ids=[org_id],
    )


@pytest.fixture
def auth_headers(org_with_read_key):
    return {"Authorization": f"Bearer {org_with_read_key['raw_key']}"}


# ---------------------------------------------------------------------------
# Fixture: a scope that can't fail a READ check honestly
#
# APIKeyScope's hierarchy (auth/types.py) is READ=1 < TASKS=2 < FULL=3 --
# READ is the floor, so no real API key of any granted scope can fail
# `auth.require_scope(APIKeyScope.READ)`. To still guard that the call is
# actually wired into this route (delete it and this test starts failing by
# going from 403 to 200), this overrides `require_auth` with an AuthContext
# carrying a scope value outside that hierarchy entirely -- a shape the real
# enum can never produce -- mirroring the dependency-override technique
# `test_collections_route.py` already uses for its own scope-gating tests.
# ---------------------------------------------------------------------------


class _NoScope:
    """Duck-types enough of `APIKeyScope` (a `.value` for the 403 detail
    message) to stand in for a scope outside the READ/TASKS/FULL hierarchy."""

    value = "none"


@pytest.fixture
def api_key_without_read(app):
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org-scope-test",
        user_id="u-scope-test",
        scope=_NoScope(),
    )
    yield "unused"
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Fixture: seeded_collection -- one collection, one compared-eligible version
# ---------------------------------------------------------------------------


@dataclass
class SeededCollection:
    experiment_id: str
    org_id: str


@pytest_asyncio.fixture
async def seeded_collection(org_with_read_key):
    """A collection experiment gathering one trial homed elsewhere.

    Exercises the same collection membership path
    (`oddish.core.experiment_membership.trial_in_experiment`) Task 2 built,
    at the HTTP layer this time. No stored comparison is seeded -- the
    version is uncompared, which is enough to make
    `coverage.task_versions_total` >= 1 without needing an `analyzer_blocks`
    row.
    """
    org_id = org_with_read_key["org_id"]
    suffix = uuid.uuid4().hex[:8]
    home_id = f"exp_sc_home_{suffix}"
    collection_id = f"exp_sc_coll_{suffix}"
    task_id = f"task_sc_{suffix}"
    version_id = f"{task_id}-v1"
    trial_id = f"trial_sc_{suffix}"

    async with get_session() as session:
        session.add(ExperimentModel(id=home_id, name=f"sc-home-{suffix}", org_id=org_id))
        session.add(
            ExperimentModel(
                id=collection_id,
                name=f"sc-collection-{suffix}",
                org_id=org_id,
                is_collection=True,
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                name=f"sc-task-{suffix}",
                user="test",
                task_path="/tmp/fake",
                org_id=org_id,
            )
        )
        await session.flush()
        session.add(
            TaskVersionModel(id=version_id, task_id=task_id, version=1, task_path="/tmp/fake")
        )
        await session.flush()
        session.add(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=home_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key=f"sc-queue-{suffix}",
                status=TrialStatus.SUCCESS,
            )
        )
        await session.flush()
        await session.execute(
            experiment_trials.insert().values(
                experiment_id=collection_id, trial_id=trial_id, deleted_at=None
            )
        )

    yield SeededCollection(experiment_id=collection_id, org_id=org_id)

    await _cleanup(
        trial_ids=[trial_id],
        task_ids=[task_id],
        experiment_ids=[home_id, collection_id],
    )


# ---------------------------------------------------------------------------
# Fixture: empty_experiment -- an experiment with no comparable versions
# ---------------------------------------------------------------------------


@dataclass
class EmptyExperiment:
    experiment_id: str
    org_id: str


@pytest_asyncio.fixture
async def empty_experiment(org_with_read_key):
    org_id = org_with_read_key["org_id"]
    experiment_id = f"exp_ee_{uuid.uuid4().hex[:8]}"

    async with get_session() as session:
        session.add(
            ExperimentModel(id=experiment_id, name=f"ee-exp-{experiment_id}", org_id=org_id)
        )

    yield EmptyExperiment(experiment_id=experiment_id, org_id=org_id)

    await _cleanup(experiment_ids=[experiment_id])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollup_requires_read_scope(client, api_key_without_read):
    res = await client.get(
        "/experiments/exp_1/cohort-rollup",
        headers={"Authorization": f"Bearer {api_key_without_read}"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_rollup_returns_coverage_and_categories(client, auth_headers, seeded_collection):
    res = await client.get(
        f"/experiments/{seeded_collection.experiment_id}/cohort-rollup",
        headers=auth_headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["coverage"]["task_versions_total"] >= 1
    assert {c["category"] for c in body["categories"]} == {
        "planning",
        "testing_verification",
        "debugging",
        "scope_adherence",
        "coherence",
        "environment_tooling",
    }


@pytest.mark.asyncio
async def test_empty_experiment_is_a_body_not_a_404(client, auth_headers, empty_experiment):
    """Zero comparable versions is a coverage statement, not an error."""
    res = await client.get(
        f"/experiments/{empty_experiment.experiment_id}/cohort-rollup",
        headers=auth_headers,
    )
    assert res.status_code == 200
    assert res.json()["coverage"]["task_versions_compared"] == 0
