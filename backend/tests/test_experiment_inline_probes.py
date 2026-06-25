"""list_tasks_core merges effective-version probe trials into task.trials.

Run with backend env sourced:
    set -a && source .env && set +a && uv run pytest tests/test_experiment_inline_probes.py -v
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from oddish.core.endpoints.tasks_query import list_tasks_core
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    get_session,
    task_experiments,
)
from models import OrganizationModel


async def _cleanup(*, trial_ids, task_ids, version_ids, experiment_ids, org_ids):
    async with get_session() as session:
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.id.in_(trial_ids))
        )
        await session.execute(
            task_experiments.delete().where(task_experiments.c.task_id.in_(task_ids))
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id.in_(task_ids))
        )
        await session.execute(
            TaskVersionModel.__table__.delete().where(
                TaskVersionModel.id.in_(version_ids)
            )
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id.in_(experiment_ids)
            )
        )
        await session.execute(
            OrganizationModel.__table__.delete().where(
                OrganizationModel.id.in_(org_ids)
            )
        )


@pytest_asyncio.fixture
async def experiment_with_probe():
    s = uuid.uuid4().hex[:8]
    org_id = f"org_ip_{s}"
    exp_id = f"exp_ip_{s}"
    task_id = f"task_ip_{s}"
    v0_id = f"{task_id}-v0"
    v_id = f"{task_id}-v1"
    real_id = f"trial_real_{s}"
    probe_id = f"trial_probe_{s}"
    off_probe_id = f"trial_offprobe_{s}"

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=f"Org {s}", slug=f"org-{s}"))
        session.add(ExperimentModel(id=exp_id, name=f"ip-{s}", org_id=org_id))
        # Insert task without current_version_id first (FK order: task before version)
        session.add(
            TaskModel(
                id=task_id,
                name=f"ip-task-{s}",
                user="test",
                task_path="/tmp/fake",
                org_id=org_id,
                current_version_id=None,
            )
        )
        await session.flush()
        session.add(TaskVersionModel(id=v0_id, task_id=task_id, version=0, task_path="/tmp/fake"))
        session.add(TaskVersionModel(id=v_id, task_id=task_id, version=1, task_path="/tmp/fake"))
        await session.flush()
        task_obj = await session.get(TaskModel, task_id)
        task_obj.current_version_id = v_id
        await session.flush()
        await session.execute(
            task_experiments.insert().values(
                task_id=task_id, experiment_id=exp_id, deleted_at=None
            )
        )
        session.add(
            TrialModel(
                id=real_id, name=f"{task_id}-0", task_id=task_id,
                experiment_id=exp_id, org_id=org_id, agent="claude-code",
                provider="anthropic", queue_key="test-ip", model="claude-sonnet-4-6",
                task_version_id=v_id, is_probe=False, status=TrialStatus.SUCCESS,
                reward=1,
            )
        )
        session.add(
            TrialModel(
                id=probe_id, name=f"{task_id}-probe", task_id=task_id,
                experiment_id=exp_id, org_id=org_id, agent="claude-code",
                provider="anthropic", queue_key="test-ip", model="claude-sonnet-4-6",
                task_version_id=v_id, is_probe=True, status=TrialStatus.SUCCESS,
                reward=1,
            )
        )
        # Off-version probe must NOT appear (different task_version_id).
        session.add(
            TrialModel(
                id=off_probe_id, name=f"{task_id}-probe-old", task_id=task_id,
                experiment_id=exp_id, org_id=org_id, agent="claude-code",
                provider="anthropic", queue_key="test-ip", model="claude-sonnet-4-6",
                task_version_id=v0_id, is_probe=True, status=TrialStatus.SUCCESS,
                reward=1,
            )
        )
        await session.commit()

    yield {"org_id": org_id, "exp_id": exp_id, "task_id": task_id,
           "real_id": real_id, "probe_id": probe_id}

    await _cleanup(
        trial_ids=[real_id, probe_id, off_probe_id], task_ids=[task_id],
        version_ids=[v0_id, v_id], experiment_ids=[exp_id], org_ids=[org_id],
    )


@pytest.mark.asyncio
async def test_probe_trial_merged_into_experiment_task_trials(experiment_with_probe):
    f = experiment_with_probe
    async with get_session() as session:
        responses = await list_tasks_core(
            session,
            experiment_id=f["exp_id"],
            org_id=f["org_id"],
            include_trials=True,
            compact_trials=True,
        )
    task = next(r for r in responses if r.id == f["task_id"])
    ids = {t.id for t in task.trials}
    assert f["real_id"] in ids
    assert f["probe_id"] in ids          # on-version probe is merged
    assert all(not t.id.startswith("trial_offprobe") for t in task.trials)
    probe = next(t for t in task.trials if t.id == f["probe_id"])
    assert probe.is_probe is True
    # Probe must not inflate aggregate counts — only the 1 real trial counts.
    assert task.total == 1
    assert task.completed == 1
    # The probe trial is still present in .trials (for inline rendering).
    assert any(t.id == f["probe_id"] for t in task.trials)
