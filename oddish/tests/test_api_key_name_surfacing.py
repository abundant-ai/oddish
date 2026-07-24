"""End-to-end: the submitting API key's NAME reaches every task/experiment view.

These exercise the real query paths rather than the response builders because
the risk this PR carries lives in the query options, not the builders. Reading
``TaskModel.api_key_id`` / ``ExperimentModel.api_key_id`` in a builder while the
column is missing from the compact ``load_only`` sets fires a lazy load outside
the request greenlet and 500s with ``MissingGreenlet`` -- and only the compact
paths (``compact_trials=True``, ``task-shells``, ``slim-tasks``) show it. A
builder unit test cannot catch that: in-memory models have every attribute set.

The public/share paths are covered too: they never pass an ``api_key_names``
map, so the names must stay ``None`` there no matter what the DB holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.tasks_query import (
    list_experiment_slim_tasks,
    list_experiment_task_shells_core,
    list_tasks_core,
)
from oddish.db.models import APIKeyModel
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    generate_id,
    task_experiments,
)


async def _seed(session, *, org_id="org1"):
    """Task + experiment both submitted by distinct, named API keys."""
    task_key = APIKeyModel(
        id=generate_id(),
        org_id=org_id,
        name="ci-bot",
        key_prefix="ok_ci123",
        key_hash=generate_id(),
    )
    exp_key = APIKeyModel(
        id=generate_id(),
        org_id=org_id,
        name="nightly-sweep",
        key_prefix="ok_ni456",
        key_hash=generate_id(),
    )
    session.add_all([task_key, exp_key])
    await session.flush()

    experiment = ExperimentModel(
        name="apikey-exp", org_id=org_id, api_key_id=exp_key.id
    )
    task = TaskModel(
        name="apikey-task",
        org_id=org_id,
        user="tester",
        task_path="s3://tasks/apikey-task",
        api_key_id=task_key.id,
    )
    session.add_all([experiment, task])
    await session.flush()

    version = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path=task.task_path
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id

    # Link through the association table, not ``task.experiments.append`` --
    # the relationship is ``lazy="select"``, so appending would lazy-load the
    # collection outside the greenlet.
    await session.execute(
        task_experiments.insert().values(task_id=task.id, experiment_id=experiment.id)
    )
    trial_id = generate_id()
    session.add(
        TrialModel(
            id=trial_id,
            name=trial_id,
            task_id=task.id,
            task_version_id=version.id,
            experiment_id=experiment.id,
            org_id=org_id,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5.5",
            model="gpt-5.5",
            status=TrialStatus.SUCCESS,
            reward=1,
        )
    )
    await session.flush()
    task_id, experiment_id = task.id, experiment.id
    # Drop the seeded instances from the identity map. Without this the query
    # under test just hands back these same in-memory objects with every
    # attribute already populated, so a column missing from a ``load_only`` set
    # would never lazy-load and the test would pass vacuously.
    session.expunge_all()
    return task_id, experiment_id


@pytest.mark.asyncio
async def test_compact_experiment_path_surfaces_both_key_names(session):
    """The compact path is the one guarded by ``TASK_STATUS_RESPONSE_COLUMNS``."""
    task_id, experiment_id = await _seed(session)

    responses = await list_tasks_core(
        session, experiment_id=experiment_id, org_id="org1", compact_trials=True
    )

    row = next(r for r in responses if r.id == task_id)
    assert row.api_key_name == "ci-bot"
    assert row.experiment_api_key_name == "nightly-sweep"


@pytest.mark.asyncio
async def test_full_path_surfaces_both_key_names(session):
    task_id, experiment_id = await _seed(session)

    responses = await list_tasks_core(
        session, experiment_id=experiment_id, org_id="org1"
    )

    row = next(r for r in responses if r.id == task_id)
    assert row.api_key_name == "ci-bot"
    assert row.experiment_api_key_name == "nightly-sweep"


@pytest.mark.asyncio
async def test_task_shells_path_surfaces_both_key_names(session):
    """First paint of the experiment page -- its own ``load_only`` set."""
    task_id, experiment_id = await _seed(session)

    responses = await list_experiment_task_shells_core(
        session, experiment_id=experiment_id, org_id="org1"
    )

    row = next(r for r in responses if r.id == task_id)
    assert row.api_key_name == "ci-bot"
    assert row.experiment_api_key_name == "nightly-sweep"


@pytest.mark.asyncio
async def test_slim_tasks_path_surfaces_both_key_names(session):
    """Phase-2 grid load -- must agree with the shell it replaces."""
    task_id, experiment_id = await _seed(session)

    responses = await list_experiment_slim_tasks(
        session, experiment_id=experiment_id, org_id="org1"
    )

    row = next(r for r in responses if r.id == task_id)
    assert row.api_key_name == "ci-bot"
    assert row.experiment_api_key_name == "nightly-sweep"


@pytest.mark.asyncio
async def test_public_task_status_never_resolves_key_names(session):
    """The share view must not leak an org's key names, even with keys set."""
    from oddish.core.sharing.helpers import get_task_status_counts

    task_id, experiment_id = await _seed(session)

    response = await get_task_status_counts(session, task_id, [])

    assert response.api_key_name is None
    assert response.experiment_api_key_name is None
