"""Tests for the experiment probes aggregation helper.

Exercises ``oddish.core.experiments.list_experiment_probes_core`` against the
real local Postgres (same pattern as ``test_probe_presets.py`` / ``test_local_runner.py``:
seed rows, yield, teardown via id-scoped deletes).

Run with your backend env sourced:

    set -a && source .env && set +a && uv run pytest tests/test_experiment_probes_api.py -v
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from oddish.core.experiments import list_experiment_probes_core
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
    task_experiments,
)
from models import OrganizationModel


# ---------------------------------------------------------------------------
# Shared teardown helpers
# ---------------------------------------------------------------------------


async def _cleanup(
    *,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    version_ids: list[str] | None = None,
    experiment_ids: list[str] | None = None,
    org_ids: list[str] | None = None,
) -> None:
    """Hard-delete seed rows in FK-safe order (trials → tasks → experiments → orgs)."""
    async with get_session() as session:
        if trial_ids:
            await session.execute(
                TrialModel.__table__.delete().where(
                    TrialModel.id.in_(trial_ids)
                )
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
        if version_ids:
            await session.execute(
                TaskVersionModel.__table__.delete().where(
                    TaskVersionModel.id.in_(version_ids)
                )
            )
        if experiment_ids:
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id.in_(experiment_ids)
                )
            )
        if org_ids:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id.in_(org_ids)
                )
            )


# ---------------------------------------------------------------------------
# Fixture: experiment with no probe trials
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def empty_experiment():
    """An experiment with one task but NO probe trials."""
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_ep_{suffix}"
    experiment_id = f"exp_ep_{suffix}"
    task_id = f"task_ep_{suffix}"
    trial_id = f"trial_ep_{suffix}"

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"Test Org {suffix}", slug=f"test-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"ep-test-{suffix}", org_id=org_id))
        session.add(
            TaskModel(
                id=task_id,
                name=f"ep-task-{suffix}",
                user="test",
                task_path="/tmp/fake",
                org_id=org_id,
            )
        )
        await session.flush()
        # Link task to experiment
        await session.execute(
            task_experiments.insert().values(
                task_id=task_id,
                experiment_id=experiment_id,
                deleted_at=None,
            )
        )
        # Non-probe trial (is_probe=False, default)
        session.add(
            TrialModel(
                id=trial_id,
                name=f"{task_id}-0",
                task_id=task_id,
                experiment_id=experiment_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key="test-ep",
                status=TrialStatus.QUEUED,
                origin=TrialOrigin.ODDISH,
                is_probe=False,
            )
        )

    yield {
        "experiment_id": experiment_id,
        "task_id": task_id,
        "trial_id": trial_id,
        "org_id": org_id,
    }

    await _cleanup(
        trial_ids=[trial_id],
        task_ids=[task_id],
        experiment_ids=[experiment_id],
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Fixture: experiment with one probed task (current version + probe trial)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def probed_experiment():
    """An experiment with one task that has a probe trial for its current version."""
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_ep_{suffix}"
    experiment_id = f"exp_ep_{suffix}"
    task_id = f"task_ep_{suffix}"
    version_id = f"{task_id}-v1"
    probe_trial_id = f"ptrial_ep_{suffix}"

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"Test Org {suffix}", slug=f"test-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"ep-probe-test-{suffix}", org_id=org_id))
        # Insert task without current_version_id first (to satisfy FK order)
        session.add(
            TaskModel(
                id=task_id,
                name=f"ep-probe-task-{suffix}",
                user="test",
                task_path="/tmp/fake",
                org_id=org_id,
                current_version_id=None,
            )
        )
        await session.flush()
        # Insert the version
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="/tmp/fake",
            )
        )
        await session.flush()
        # Update task to point at the version
        task = await session.get(TaskModel, task_id)
        task.current_version_id = version_id
        await session.flush()
        # Link task to experiment
        await session.execute(
            task_experiments.insert().values(
                task_id=task_id,
                experiment_id=experiment_id,
                deleted_at=None,
            )
        )
        # Probe trial (is_probe=True, linked to current version)
        session.add(
            TrialModel(
                id=probe_trial_id,
                name=f"{task_id}-probe",
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=experiment_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key="test-ep-probe",
                status=TrialStatus.QUEUED,
                origin=TrialOrigin.ODDISH,
                is_probe=True,
            )
        )

    yield {
        "experiment_id": experiment_id,
        "task_id": task_id,
        "version_id": version_id,
        "probe_trial_id": probe_trial_id,
        "org_id": org_id,
    }

    await _cleanup(
        trial_ids=[probe_trial_id],
        task_ids=[task_id],
        version_ids=[version_id],
        experiment_ids=[experiment_id],
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Fixture: experiment with one task that has TWO probe trials
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dual_probe_experiment():
    """An experiment with one task that has two probe trials for its current version.

    Used to verify that the helper returns exactly one row (the most recent probe).
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_ep_{suffix}"
    experiment_id = f"exp_ep_{suffix}"
    task_id = f"task_ep_{suffix}"
    version_id = f"{task_id}-v1"
    probe_trial_id_1 = f"ptrial1_ep_{suffix}"
    probe_trial_id_2 = f"ptrial2_ep_{suffix}"

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"Test Org {suffix}", slug=f"test-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"ep-dual-probe-{suffix}", org_id=org_id))
        session.add(
            TaskModel(
                id=task_id,
                name=f"ep-dual-task-{suffix}",
                user="test",
                task_path="/tmp/fake",
                org_id=org_id,
                current_version_id=None,
            )
        )
        await session.flush()
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=1,
                task_path="/tmp/fake",
            )
        )
        await session.flush()
        task = await session.get(TaskModel, task_id)
        task.current_version_id = version_id
        await session.flush()
        await session.execute(
            task_experiments.insert().values(
                task_id=task_id,
                experiment_id=experiment_id,
                deleted_at=None,
            )
        )
        # First (older) probe trial
        session.add(
            TrialModel(
                id=probe_trial_id_1,
                name=f"{task_id}-probe-1",
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=experiment_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key="test-ep-probe",
                status=TrialStatus.SUCCESS,
                origin=TrialOrigin.ODDISH,
                is_probe=True,
            )
        )
        await session.flush()
        # Second (newer) probe trial — different status so we can verify which was chosen
        session.add(
            TrialModel(
                id=probe_trial_id_2,
                name=f"{task_id}-probe-2",
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=experiment_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key="test-ep-probe",
                status=TrialStatus.QUEUED,
                origin=TrialOrigin.ODDISH,
                is_probe=True,
            )
        )

    yield {
        "experiment_id": experiment_id,
        "task_id": task_id,
        "version_id": version_id,
        "probe_trial_id_1": probe_trial_id_1,
        "probe_trial_id_2": probe_trial_id_2,
        "org_id": org_id,
    }

    await _cleanup(
        trial_ids=[probe_trial_id_1, probe_trial_id_2],
        task_ids=[task_id],
        version_ids=[version_id],
        experiment_ids=[experiment_id],
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_probe_trials_returns_empty_list(empty_experiment):
    """An experiment whose tasks have only non-probe trials returns []."""
    async with get_session() as session:
        rows = await list_experiment_probes_core(
            session,
            experiment_id=empty_experiment["experiment_id"],
            org_id=empty_experiment["org_id"],
        )
    assert rows == []


@pytest.mark.asyncio
async def test_probe_trial_appears_in_result(probed_experiment):
    """A task with a probe trial on its current version surfaces one row."""
    async with get_session() as session:
        rows = await list_experiment_probes_core(
            session,
            experiment_id=probed_experiment["experiment_id"],
            org_id=probed_experiment["org_id"],
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == probed_experiment["task_id"]
    assert row.probe_trial_id == probed_experiment["probe_trial_id"]
    assert row.model == "anthropic/claude-sonnet-4-6"
    assert row.status == TrialStatus.QUEUED.value
    assert row.version == 1


@pytest.mark.asyncio
async def test_org_scoping_excludes_other_org(probed_experiment):
    """A different org_id returns no rows (org isolation enforced)."""
    async with get_session() as session:
        rows = await list_experiment_probes_core(
            session,
            experiment_id=probed_experiment["experiment_id"],
            org_id="org_completely_different",
        )
    assert rows == []


@pytest.mark.asyncio
async def test_dual_probe_trials_deduped_to_one_row(dual_probe_experiment):
    """A task with two probe trials on the same version returns exactly one row
    (the most recent probe trial).
    """
    async with get_session() as session:
        rows = await list_experiment_probes_core(
            session,
            experiment_id=dual_probe_experiment["experiment_id"],
            org_id=dual_probe_experiment["org_id"],
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.task_id == dual_probe_experiment["task_id"]
    # The second probe trial was inserted after the first so it should be
    # selected as the most recent.  Its probe_trial_id is probe_trial_id_2.
    assert row.probe_trial_id == dual_probe_experiment["probe_trial_id_2"]
