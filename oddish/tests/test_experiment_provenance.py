"""Experiment provenance: ``experiment.owner`` / ``experiment.link`` are scoped
to the experiment and set once (the creating run), not inherited from the
shared, mutable task fields.

Each run stamps the experiment it targets with the submitter (owner) and PR
link, but only when still empty, so:
  * a later run by a different user does NOT change the original owner/link, and
  * running the SAME (shared) task under a different experiment stamps that
    other experiment without touching the first.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.schemas import TaskSweepSubmission, AgentModelPair  # noqa: E402
from oddish.core.endpoints import create_task_sweep_core  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TrialModel,
    get_session,
    task_experiments,
)

PR_A = "https://github.com/abundant-ai/experiments/pull/100"
PR_B = "https://github.com/abundant-ai/experiments/pull/200"
E1 = "exp-prov-one"
E2 = "exp-prov-two"


def _submission(task_id, experiment_id, link, *, user="alice", github_username=None):
    return TaskSweepSubmission(
        task_id=task_id,
        append_to_task=True,
        experiment_id=experiment_id,
        link=link,
        configs=[
            AgentModelPair(agent="claude-code", model="anthropic/claude-sonnet-4-6", n_trials=1)
        ],
        user=user,
        github_username=github_username,
    )


async def _experiment(name):
    async with get_session() as s:
        return (
            await s.execute(select(ExperimentModel).where(ExperimentModel.name == name))
        ).scalar_one_or_none()


@pytest_asyncio.fixture
async def seeded_task_id(tmp_path):
    task_id = "test_experiment_provenance_task"
    task_dir = tmp_path / "fake-task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("solve")
    (task_dir / "task.toml").write_text('version = "1.0"\n')
    async with get_session() as s:
        s.add(TaskModel(id=task_id, name="exp-prov-test", user="uploader", task_path=str(task_dir)))
    yield task_id
    async with get_session() as s:
        await s.execute(TrialModel.__table__.delete().where(TrialModel.task_id == task_id))
        await s.execute(task_experiments.delete().where(task_experiments.c.task_id == task_id))
        await s.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))
        await s.execute(ExperimentModel.__table__.delete().where(ExperimentModel.name.in_([E1, E2])))


@pytest.mark.asyncio
async def test_experiment_owner_and_link_are_set_once_and_scoped(monkeypatch, seeded_task_id):
    monkeypatch.setenv("ODDISH_LOCAL_MODE", "1")
    import oddish.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "settings", cfg_mod.Settings())
    monkeypatch.setattr("oddish.worker.local_runner.run_trial_locally", AsyncMock())

    # 1. Creating run: owner + link stamped from the submitter (not task.user).
    async with get_session() as s:
        await create_task_sweep_core(
            s,
            submission=_submission(seeded_task_id, E1, PR_A, user="octocat@x.com", github_username="octocat"),
            org_id=None,
        )
    e1 = await _experiment(E1)
    assert e1.owner == "octocat"  # github username, not the task uploader
    assert e1.link == PR_A

    # 2. Later run by a different user / PR must NOT change E1 (set-once).
    async with get_session() as s:
        await create_task_sweep_core(
            s,
            submission=_submission(seeded_task_id, E1, PR_B, user="someone@x.com", github_username="someone"),
            org_id=None,
        )
    e1 = await _experiment(E1)
    assert e1.owner == "octocat"
    assert e1.link == PR_A

    # 3. The SAME shared task under a different experiment stamps E2 only.
    async with get_session() as s:
        await create_task_sweep_core(
            s,
            submission=_submission(seeded_task_id, E2, PR_B, user="bob@x.com", github_username="bob"),
            org_id=None,
        )
    e2 = await _experiment(E2)
    assert e2.owner == "bob"
    assert e2.link == PR_B
    e1 = await _experiment(E1)
    assert e1.owner == "octocat" and e1.link == PR_A  # untouched


@pytest.mark.asyncio
async def test_owner_falls_back_to_user_without_github(monkeypatch, seeded_task_id):
    monkeypatch.setenv("ODDISH_LOCAL_MODE", "1")
    import oddish.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "settings", cfg_mod.Settings())
    monkeypatch.setattr("oddish.worker.local_runner.run_trial_locally", AsyncMock())

    async with get_session() as s:
        await create_task_sweep_core(
            s,
            submission=_submission(seeded_task_id, E1, None, user="plain-user", github_username=None),
            org_id=None,
        )
    e1 = await _experiment(E1)
    assert e1.owner == "plain-user"  # falls back to user when no github handle
    assert e1.link is None  # link-less run leaves it empty
